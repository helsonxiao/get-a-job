"""同口径采集快照: 采集纪元(epoch)隔离 + 快照捕获 / 列表 / Diff / 回溯。

核心语义 (见 specs/obs-snapshot-diff/spec.md):
  - 每个口径(source_link)维护一个「活跃纪元」(collection_epochs 表 status='active')。
    所有采集 (daily/full 不加区分) 把岗位打上当前活跃纪元。
  - 唯一触发快照的入口是「带 scope 的 report 导出」: 冻结活跃纪元内岗位为一份
    不可变快照 (聚合指标 metrics + 岗位成员)，成功后闭合旧纪元、开启新纪元。
    后续采集进入新纪元 → 下一份快照不会带入上一纪元的滞留数据。
  - 历史纪元 `jobs` 行不删除; `jobs.collection_epoch` 持久化于 job.json,
    reindex 后不丢失; 可按 source_link + collection_epoch 回溯完整岗位行。

说明: 本模块不 import gaj.store.index 的模块级符号 (连接/影子用函数内延迟 import),
避免与 index/reportbundle 形成循环依赖。连接可以直接用 index.session / index.connect。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime
from typing import Any

from . import observatory

#: 快照成员行保留的关键字段 (写入 snapshot_members)
_MEMBER_COLS = (
    "job_id", "title", "company_name", "city", "district", "industry",
    "salary_mid", "exp_min", "edu_level", "overtime", "outsourcing",
    "best_total", "online",
)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def period_labels(captured_at: str) -> tuple[str, str]:
    """由采集/导出时点派生 (period_month, period_quarter)。

    例子: "2026-09-04T19:00:00" → ("2026-09", "2026-Q3")
    解析失败时回退到当前月份, 避免异常中断整个流程。
    """
    try:
        dt = datetime.fromisoformat((captured_at or "").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        dt = datetime.now()
    pm = f"{dt.year:04d}-{dt.month:02d}"
    q = (dt.month - 1) // 3 + 1
    return pm, f"{dt.year:04d}-Q{q}"


def _epoch_id() -> str:
    return uuid.uuid4().hex[:16]


# ---------------------------------------------------------------- 纪元管理


def ensure_active_epoch(conn: sqlite3.Connection, source_link: str) -> str:
    """返回该口径当前活跃纪元; 没有则自动开启一个 (每口径至多一个 active)。"""
    source_link = (source_link or "").strip()
    row = conn.execute(
        "SELECT epoch_id FROM collection_epochs"
        " WHERE source_link = ? AND status = 'active' ORDER BY opened_at DESC LIMIT 1",
        (source_link,),
    ).fetchone()
    if row and row["epoch_id"]:
        return row["epoch_id"]
    eid = _epoch_id()
    conn.execute(
        "INSERT INTO collection_epochs (epoch_id, source_link, opened_at, status, closed_at)"
        " VALUES (?,?,?, 'active', '')",
        (eid, source_link, _now_iso()),
    )
    return eid


def active_epoch(conn: sqlite3.Connection, source_link: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM collection_epochs"
        " WHERE source_link = ? AND status = 'active' ORDER BY opened_at DESC LIMIT 1",
        (source_link,),
    ).fetchone()
    return dict(row) if row else None


def active_epoch_id(source_link: str) -> str:
    """独立连接获取 (必要时开启) 某口径当前活跃纪元 id。

    用于采集侧 (adapter) 在没有现成连接时给岗位打纪元标记。
    """
    from . import index as _index

    with _index.session() as conn:
        return ensure_active_epoch(conn, source_link)


def list_epochs(conn: sqlite3.Connection, source_link: str) -> list[dict]:
    rows = conn.execute(
        "SELECT epoch_id, source_link, opened_at, status, closed_at"
        " FROM collection_epochs WHERE source_link = ? ORDER BY opened_at DESC",
        (source_link,),
    ).fetchall()
    return [dict(r) for r in rows]


def close_and_open_new_epoch(conn: sqlite3.Connection, source_link: str) -> dict:
    """原子地: 闭合当前活跃纪元并开启新纪元。返回 {closed: eid|None, opened: eid}。"""
    source_link = (source_link or "").strip()
    closed = None
    row = conn.execute(
        "SELECT epoch_id FROM collection_epochs"
        " WHERE source_link = ? AND status = 'active' ORDER BY opened_at DESC LIMIT 1",
        (source_link,),
    ).fetchone()
    if row:
        closed = row["epoch_id"]
        conn.execute(
            "UPDATE collection_epochs SET status = 'closed', closed_at = ?"
            " WHERE epoch_id = ?",
            (_now_iso(), closed),
        )
    opened = _epoch_id()
    conn.execute(
        "INSERT INTO collection_epochs (epoch_id, source_link, opened_at, status, closed_at)"
        " VALUES (?,?,?, 'active', '')",
        (opened, source_link, _now_iso()),
    )
    return {"closed": closed, "opened": opened}


# ---------------------------------------------------------------- 快照捕获


def capture_snapshot(
    conn: sqlite3.Connection,
    source_link: str,
    include_ignored: bool = True,
) -> dict:
    """固化该口径「活跃纪元」为一份快照，并闭合旧纪元、开启新纪元。

    - 用 temp.jobs 影子限定 source_link + 活跃纪元 (含历史无纪元 ''/NULL 的首纪元);
    - 复用 observatory_* 聚合各视图 → observatory_snapshots.metrics;
    - 落快照成员行 → snapshot_members (不可变);
    - 成功后推进纪元: 后续采集进入新纪元 → 收敛隔离。
    返回快照摘要 dict。
    """
    source_link = (source_link or "").strip()
    epoch_id = ensure_active_epoch(conn, source_link)
    now = _now_iso()
    period_month, period_quarter = period_labels(now)

    conn.execute("DROP TABLE IF EXISTS temp.jobs")
    try:
        # ---- 口径影子 + 纪元过滤 (覆盖 reportbundle 遗留的仅口径影子) ----
        conn.execute(
            "CREATE TEMP TABLE jobs AS SELECT * FROM main.jobs"
            " WHERE source_link = ?"
            "   AND (collection_epoch = ? OR collection_epoch IS NULL OR collection_epoch = '')",
            (source_link, epoch_id),
        )
        if include_ignored:
            # 与报告导出一致: 含忽略模式下影子内全部视为未忽略, 放行 _VISIBLE
            conn.execute("UPDATE temp.jobs SET ignored = 0")

        # ---- 各观察台视图聚合 ----
        metrics: dict[str, Any] = {
            "salary_pricing": observatory.observatory_salary_pricing(conn),
            "signal_radar": observatory.observatory_signal_radar(conn),
            "skill_leaderboard": observatory.observatory_skill_leaderboard(conn),
            "company_boards": observatory.observatory_company_boards(conn),
            "industry_list": observatory.observatory_industry_list(conn),
            "geo_heatmap": observatory.observatory_geo_heatmap(conn),
        }

        visible_rows = conn.execute(
            "SELECT job_id, company_id FROM temp.jobs"
            " WHERE (ignored = 0 OR ignored IS NULL)"
        ).fetchall()
        job_count = len(visible_rows)
        company_count = len({r["company_id"] for r in visible_rows if r["company_id"]})

        snapshot_id = uuid.uuid4().hex[:16]
        conn.execute(
            "INSERT INTO observatory_snapshots (snapshot_id, source_link, epoch_id,"
            " captured_at, period_month, period_quarter, job_count, company_count,"
            " metrics, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                snapshot_id, source_link, epoch_id, now, period_month, period_quarter,
                job_count, company_count, json.dumps(metrics, ensure_ascii=False), now,
            ),
        )

        # ---- 成员行 (仅可见岗位) ----
        member_rows = conn.execute(
            f"SELECT {', '.join(_MEMBER_COLS)} FROM temp.jobs"
            " WHERE (ignored = 0 OR ignored IS NULL)"
        ).fetchall()
        conn.executemany(
            f"INSERT OR REPLACE INTO snapshot_members (snapshot_id, {', '.join(_MEMBER_COLS)})"
            f" VALUES ({', '.join(['?'] * (1 + len(_MEMBER_COLS)))})",
            [tuple([snapshot_id] + [r[c] for c in _MEMBER_COLS]) for r in member_rows],
        )
    finally:
        conn.execute("DROP TABLE IF EXISTS temp.jobs")

    # ---- 推进纪元: 后续采集进入新纪元 (先落快照, 成功后切换) ----
    close_and_open_new_epoch(conn, source_link)

    return {
        "snapshot_id": snapshot_id,
        "source_link": source_link,
        "epoch_id": epoch_id,
        "captured_at": now,
        "period_month": period_month,
        "period_quarter": period_quarter,
        "job_count": job_count,
        "company_count": company_count,
    }


# ---------------------------------------------------------------- 查询

_MEMBER_KEYS = ("job_id", "title", "company_name", "city")


def list_snapshots(conn: sqlite3.Connection, source_link: str) -> list[dict]:
    rows = conn.execute(
        "SELECT snapshot_id, source_link, epoch_id, captured_at, period_month,"
        " period_quarter, job_count, company_count, created_at"
        " FROM observatory_snapshots WHERE source_link = ?"
        " ORDER BY captured_at DESC, created_at DESC",
        (source_link,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_snapshot(
    conn: sqlite3.Connection, source_link: str, ref: str
) -> dict | None:
    """按 snapshot_id 或 period_month / period_quarter 解析一份快照。"""
    ref = (ref or "").strip()
    row = conn.execute(
        "SELECT * FROM observatory_snapshots WHERE source_link = ?"
        " AND (snapshot_id = ? OR period_month = ? OR period_quarter = ?)"
        " ORDER BY captured_at DESC LIMIT 1",
        (source_link, ref, ref, ref),
    ).fetchone()
    if not row:
        return None
    out = dict(row)
    try:
        out["metrics"] = json.loads(out["metrics"] or "{}")
    except (json.JSONDecodeError, TypeError):
        out["metrics"] = {}
    return out


def load_epoch_jobs(
    conn: sqlite3.Connection, source_link: str, epoch_id: str
) -> list[dict]:
    """按 source_link + collection_epoch 回溯该纪元完整岗位行。"""
    rows = conn.execute(
        "SELECT * FROM jobs WHERE source_link = ? AND collection_epoch = ?",
        (source_link, epoch_id),
    ).fetchall()
    return [dict(r) for r in rows]


def snapshot_members(
    conn: sqlite3.Connection, snapshot_id: str, *, limit: int | None = None
) -> list[dict]:
    sql = ("SELECT " + ", ".join(_MEMBER_KEYS)
           + " FROM snapshot_members WHERE snapshot_id = ?")
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, (snapshot_id,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- Diff


def _list_delta(new_items: list[dict], old_items: list[dict], key: str):
    """通用列表 diff: 按 key 比较, 返回该 key 的新增/消失清单。"""
    new_keys = {it.get(key) for it in new_items if it.get(key) is not None}
    old_keys = {it.get(key) for it in old_items if it.get(key) is not None}
    added = sorted(k for k in new_keys if k not in old_keys)
    removed = sorted(k for k in old_keys if k not in new_keys)
    return {"added": added, "removed": removed, "added_count": len(added), "removed_count": len(removed)}


def diff_snapshots(
    conn: sqlite3.Connection, source_link: str, from_ref: str, to_ref: str
) -> dict:
    """同一口径两个快照的差异对比 (岗位成员进出 + 各观察台视图 delta)。"""
    snap_a = get_snapshot(conn, source_link, from_ref)
    snap_b = get_snapshot(conn, source_link, to_ref)
    if not snap_a or not snap_b:
        missing = [r for r, s in ((from_ref, snap_a), (to_ref, snap_b)) if not s]
        return {"error": f"未找到快照引用: {missing}"}

    members_a = snapshot_members(conn, snap_a["snapshot_id"])
    members_b = snapshot_members(conn, snap_b["snapshot_id"])
    ids_a = {m["job_id"] for m in members_a}
    ids_b = {m["job_id"] for m in members_b}
    by_id_a = {m["job_id"]: m for m in members_a}
    by_id_b = {m["job_id"]: m for m in members_b}

    added_ids = sorted(ids_b - ids_a)
    removed_ids = sorted(ids_a - ids_b)

    ma: dict = snap_a.get("metrics") or {}
    mb: dict = snap_b.get("metrics") or {}

    deltas: dict[str, Any] = {}

    # 薪资分位 (overall)
    sa = (ma.get("salary_pricing") or {}).get("overall") or {}
    sb = (mb.get("salary_pricing") or {}).get("overall") or {}
    sal_delta = {}
    for k in ("p10", "p25", "p50", "p75", "p90", "mean", "count"):
        old = sa.get(k)
        new = sb.get(k)
        if old is None and new is None:
            continue
        row = {"old": old, "new": new}
        if isinstance(old, (int, float)) and isinstance(new, (int, float)):
            row["delta"] = round(new - old, 2)
        sal_delta[k] = row
    deltas["salary_overall"] = sal_delta

    # 技能榜
    deltas["skills"] = _list_delta(
        (mb.get("skill_leaderboard") or {}).get("items") or [],
        (ma.get("skill_leaderboard") or {}).get("items") or [],
        "skill",
    )
    # 公司双榜
    deltas["company_hiring"] = _list_delta(
        (mb.get("company_boards") or {}).get("hiring") or [],
        (ma.get("company_boards") or {}).get("hiring") or [],
        "company",
    )
    deltas["company_salary"] = _list_delta(
        (mb.get("company_boards") or {}).get("salary") or [],
        (ma.get("company_boards") or {}).get("salary") or [],
        "company",
    )
    # 行业分布
    deltas["industries"] = _list_delta(
        (mb.get("industry_list") or {}).get("items") or [],
        (ma.get("industry_list") or {}).get("items") or [],
        "name",
    )
    # 雷达红旗概要
    ra = (ma.get("signal_radar") or {}).get("summary") or {}
    rb = (mb.get("signal_radar") or {}).get("summary") or {}
    radar_delta = {}
    for k in ("total", "outsourcing_count", "travel_count"):
        old, new = ra.get(k), rb.get(k)
        if old is None and new is None:
            continue
        row = {"old": old, "new": new}
        if isinstance(old, (int, float)) and isinstance(new, (int, float)):
            row["delta"] = new - old
        radar_delta[k] = row
    deltas["signal_radar"] = radar_delta

    return {
        "source_link": source_link,
        "from": {"ref": from_ref, "snapshot_id": snap_a["snapshot_id"],
                 "period_month": snap_a["period_month"], "period_quarter": snap_a["period_quarter"],
                 "job_count": snap_a["job_count"]},
        "to": {"ref": to_ref, "snapshot_id": snap_b["snapshot_id"],
               "period_month": snap_b["period_month"], "period_quarter": snap_b["period_quarter"],
               "job_count": snap_b["job_count"]},
        "jobs_added": [by_id_b[j] for j in added_ids],
        "jobs_added_count": len(added_ids),
        "jobs_removed": [by_id_a[j] for j in removed_ids],
        "jobs_removed_count": len(removed_ids),
        "metric_deltas": deltas,
    }