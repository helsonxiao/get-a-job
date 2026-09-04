"""采集纪元快照测试: 隔离 / 快照捕获 / 列表 / diff / 回溯 / report 导出钩子。

用 in-memory SQLite 建真实 schema, 直插最小列集, 不触碰真实 data/。
验证核心场景: 导出后旧纪元遗留行不再进新快照 (11月快照不带9月数据)。
"""

from __future__ import annotations

import pytest

from gaj.store import index, observatory_snapshot as obsnap, reportbundle

SCOPE = "https://www.zhipin.com/web/geek/job?query=AI&city=101190100"


def _make_db():
    return index.connect(":memory:")


def _insert_job(conn, job_id, *, salary_mid=20, industry="计算机软件", epoch="", online=1):
    conn.execute(
        """INSERT INTO jobs (job_id, title, company_id, company_name, city, district,
           salary_mid, exp_min, edu_level, industry, overtime, outsourcing, travel,
           skills, first_seen, last_seen, ignored, source_link, collection_epoch, online)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
        (job_id, f"岗位{job_id}", f"c{job_id}", f"公司{job_id}", "无锡", "新区",
         salary_mid, 3, 3, industry, "moderate", 0, "none", '["C++"]',
         "2026-09-01T10:00:00", "2026-09-04T10:00:00", SCOPE, epoch, online),
    )


@pytest.fixture()
def conn():
    c = _make_db()
    # ---- 首纪元数据的 3 个岗位 (均打上当前活跃纪元) ----
    epoch1 = obsnap.ensure_active_epoch(c, SCOPE)
    for j in ("a", "b", "c"):
        _insert_job(c, j, salary_mid=20, epoch=epoch1)
    c.commit()
    yield c
    c.close()


def test_capture_collects_active_epoch_members(conn):
    shot = obsnap.capture_snapshot(conn, SCOPE)
    conn.commit()
    # 快照记录 3 个岗位
    assert shot["job_count"] == 3
    # 聚合指标已写入 (薪资定价 overall 存在即说明聚合跑了)
    snap = obsnap.get_snapshot(conn, SCOPE, shot["snapshot_id"])
    assert snap["period_month"]  # YYYY-MM
    assert snap["period_quarter"]  # YYYY-Qn
    assert snap["metrics"]["salary_pricing"]["overall"]["count"] == 3
    # 成员表 3 行
    members = obsnap.snapshot_members(conn, shot["snapshot_id"])
    assert {m["job_id"] for m in members} == {"a", "b", "c"}


def test_epoch_isolation_after_export(conn):
    # 第一份快照
    shot1 = obsnap.capture_snapshot(conn, SCOPE)
    conn.commit()
    # 导出后进入新纪元
    new_epoch = obsnap.active_epoch(conn, SCOPE)["epoch_id"]

    # 模拟下一期采集: 岗位 a/b 还在 (重新打新纪元), 岗位 c 消失 (仍留在旧纪元 → 应被隔离),
    # 岗位 d 是新岗位 (打新纪元)
    conn.execute("UPDATE jobs SET collection_epoch = ? WHERE job_id IN ('a','b')", (new_epoch,))
    # c 不更新 epoch → 停留在旧纪元
    _insert_job(conn, "d", salary_mid=30, epoch=new_epoch)
    conn.commit()

    # 第二份快照: 只统计新纪元内岗位 (a/b/d), 不带动 c
    shot2 = obsnap.capture_snapshot(conn, SCOPE)
    conn.commit()
    assert shot2["job_count"] == 3  # a/b/d
    members2 = {m["job_id"] for m in obsnap.snapshot_members(conn, shot2["snapshot_id"])}
    assert members2 == {"a", "b", "d"}

    # 快照可列出, 有 2 份
    snaps = obsnap.list_snapshots(conn, SCOPE)
    assert len(snaps) == 2

    # Diff: 从第一份到第二份 → 新增 d, 消失 c (用显式 snapshot_id, 避免同秒排序不稳定)
    diff = obsnap.diff_snapshots(conn, SCOPE, shot1["snapshot_id"], shot2["snapshot_id"])
    assert diff["jobs_added_count"] == 1
    assert {x["job_id"] for x in diff["jobs_added"]} == {"d"}
    assert {x["job_id"] for x in diff["jobs_removed"]} == {"c"}


def test_report_export_hooks_snapshot_and_advances_epoch(conn):
    e_before = obsnap.active_epoch(conn, SCOPE)["epoch_id"]
    n_before = len(obsnap.list_snapshots(conn, SCOPE))
    # 带 scope 的 report 导出 → 固化快照 + 推进纪元
    reportbundle.build_report_bundle(conn, scope_link=SCOPE, include_ignored=True)
    conn.commit()
    assert len(obsnap.list_snapshots(conn, SCOPE)) == n_before + 1
    e_after = obsnap.active_epoch(conn, SCOPE)["epoch_id"]
    assert e_after and e_after != e_before


def test_report_export_without_scope_does_not_snapshot(conn):
    n_before = len(obsnap.list_snapshots(conn, SCOPE))
    reportbundle.build_report_bundle(conn, include_ignored=True)  # 无 scope
    conn.commit()
    assert len(obsnap.list_snapshots(conn, SCOPE)) == n_before


def test_historical_epoch_rows_retrievable(conn):
    shot = obsnap.capture_snapshot(conn, SCOPE)
    conn.commit()
    # 旧纪元完整岗位行可按 source_link + epoch 回溯
    rows = obsnap.load_epoch_jobs(conn, SCOPE, shot["epoch_id"])
    assert {r["job_id"] for r in rows} == {"a", "b", "c"}