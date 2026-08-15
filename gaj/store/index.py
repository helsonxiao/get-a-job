"""SQLite 派生索引。

这个库里没有任何独占数据 —— 删掉 index.db 再跑一次 reindex() 就能从
data/ 下的 JSON 文件完整重建。它存在的唯一目的是让 Web 界面能快速做
筛选、排序、全文搜索和跨表统计。
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .. import config as cfg
from ..core.models import Company, Job
from ..logging_setup import get_logger
from . import repo

log = get_logger("index")

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    title           TEXT,
    url             TEXT,
    company_id      TEXT,
    company_name    TEXT,
    city            TEXT,
    district        TEXT,
    address         TEXT,
    salary_raw      TEXT,
    salary_min      REAL,
    salary_max      REAL,
    salary_mid      REAL,
    salary_months   INTEGER,
    exp_min         REAL,
    exp_max         REAL,
    edu_level       INTEGER,
    edu_raw         TEXT,
    skills          TEXT,
    welfare         TEXT,
    overtime        TEXT,
    overtime_conf   REAL,
    work_mode       TEXT,
    outsourcing     INTEGER,
    travel          TEXT,
    team_size       INTEGER,
    online          INTEGER,
    first_seen      TEXT,
    last_seen       TEXT,
    quality_score   REAL,
    polluted        INTEGER,
    jd_length       INTEGER,
    industry        TEXT,
    stage           TEXT,
    scale_min       INTEGER,
    scale_max       INTEGER,
    nature          TEXT,
    rule_status     TEXT,
    rule_reject     TEXT,
    rule_total      REAL,
    rule_finance    REAL,
    rule_growth     REAL,
    rule_resource   REAL,
    rule_wlb        REAL,
    ai_needed       INTEGER,
    ai_count        INTEGER,
    ai_providers    TEXT,
    latest_ai_provider TEXT,
    latest_ai_total    REAL,
    latest_ai_at       TEXT,
    recommendation     TEXT,
    best_total      REAL,
    favorite        INTEGER DEFAULT 0,
    favorited_at    TEXT,
    ignored         INTEGER DEFAULT 0,
    ai_stale        INTEGER DEFAULT 0,
    ai_stale_reason TEXT,
    indexed_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_city    ON jobs(city);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_id);
CREATE INDEX IF NOT EXISTS idx_jobs_best    ON jobs(best_total);
CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(rule_status);

CREATE TABLE IF NOT EXISTS companies (
    brand_id     TEXT PRIMARY KEY,
    name         TEXT,
    short_name   TEXT,
    industry     TEXT,
    stage        TEXT,
    scale_raw    TEXT,
    scale_min    INTEGER,
    scale_max    INTEGER,
    nature       TEXT,
    founded      TEXT,
    capital      REAL,
    hours_per_day REAL,
    job_count    INTEGER,
    favorite     INTEGER DEFAULT 0,
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS company_stats (
    brand_id        TEXT PRIMARY KEY,
    job_count       INTEGER DEFAULT 0,
    online_count    INTEGER DEFAULT 0,
    scored_count    INTEGER DEFAULT 0,
    ai_scored_count INTEGER DEFAULT 0,
    best_score      REAL,
    avg_score       REAL,
    company_score   REAL,
    rank_tier       TEXT,
    salary_mid_avg  REAL,
    cities          TEXT,
    top_job_id      TEXT,
    top_job_title   TEXT,
    top_job_score   REAL,
    latest_seen     TEXT,
    has_intro       INTEGER DEFAULT 0,
    has_scope       INTEGER DEFAULT 0,
    excluded        INTEGER DEFAULT 0,
    computed_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_company_stats_score ON company_stats(company_score);

CREATE TABLE IF NOT EXISTS scores (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT,
    kind        TEXT,
    provider    TEXT,
    model       TEXT,
    total       REAL,
    status      TEXT,
    recommendation TEXT,
    dims        TEXT,
    created_at  TEXT,
    file        TEXT,
    context_fp  TEXT,
    UNIQUE(job_id, kind, file)
);

CREATE INDEX IF NOT EXISTS idx_scores_job ON scores(job_id);

CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
    job_id UNINDEXED, title, company_name, skills, jd, tokenize='trigram'
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or cfg.INDEX_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """轻量 schema 迁移: 给老库补新列和新索引。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "favorite" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN favorite INTEGER DEFAULT 0")
    if "favorited_at" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN favorited_at TEXT")
    if "manual_total" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN manual_total REAL")
    if "manual_note" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN manual_note TEXT")
    if "manual_at" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN manual_at TEXT")
    if "ignored" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN ignored INTEGER DEFAULT 0")
    if "ai_stale" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN ai_stale INTEGER DEFAULT 0")
    if "ai_stale_reason" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN ai_stale_reason TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_favorite ON jobs(favorite)")

    score_cols = {r[1] for r in conn.execute("PRAGMA table_info(scores)").fetchall()}
    if "context_fp" not in score_cols:
        conn.execute("ALTER TABLE scores ADD COLUMN context_fp TEXT")

    company_cols = {r[1] for r in conn.execute("PRAGMA table_info(companies)").fetchall()}
    if "favorite" not in company_cols:
        conn.execute("ALTER TABLE companies ADD COLUMN favorite INTEGER DEFAULT 0")

    conn.commit()


@contextmanager
def session(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """便捷连接: ``with index.session() as conn: ...``

    正常退出时提交, 异常时回滚, 无论如何都关闭连接。
    """
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _job_row(
    job: Job,
    company: Company | None,
    summary: dict,
    rule: dict | None,
    stale: tuple[int, str] = (0, ""),
) -> dict:
    sig = job.signals or {}
    rule = rule or {}
    dims = rule.get("dimension_scores", {}) or {}

    ai_providers = summary.get("ai_providers", [])
    latest = ai_providers[0] if ai_providers else {}
    ai_total = latest.get("total")
    rule_total = rule.get("total_score")
    # 优先级: 人工调分 > AI > 规则
    manual = job.manual_override or {}
    manual_total = manual.get("total")
    if manual_total is not None:
        best = manual_total
    elif ai_total is not None:
        best = ai_total
    else:
        best = rule_total

    return {
        "job_id": job.job_id,
        "title": job.title,
        "url": job.url,
        "company_id": job.company_id,
        "company_name": job.company_name or (company.name if company else ""),
        "city": job.city,
        "district": job.district,
        "address": job.address,
        "salary_raw": job.salary.get("raw", ""),
        "salary_min": job.salary.get("min_10k"),
        "salary_max": job.salary.get("max_10k"),
        "salary_mid": job.salary.get("mid_10k"),
        "salary_months": job.salary.get("months"),
        "exp_min": job.experience.get("min_years"),
        "exp_max": job.experience.get("max_years"),
        "edu_level": job.education.get("level"),
        "edu_raw": job.education.get("raw", ""),
        "skills": _j(job.skills),
        "welfare": _j(job.welfare),
        "overtime": (sig.get("overtime") or {}).get("value"),
        "overtime_conf": (sig.get("overtime") or {}).get("confidence"),
        "work_mode": (sig.get("work_mode") or {}).get("value"),
        "outsourcing": 1 if (sig.get("outsourcing") or {}).get("value") else 0,
        "travel": (sig.get("travel") or {}).get("value"),
        "team_size": (sig.get("team_size") or {}).get("value"),
        "online": 1 if job.online else 0,
        "first_seen": job.first_seen,
        "last_seen": job.last_seen,
        "quality_score": (job.quality or {}).get("score"),
        "polluted": 1 if (job.quality or {}).get("polluted") else 0,
        "jd_length": (job.quality or {}).get("jd_length"),
        "industry": company.industry if company else "",
        "stage": company.stage if company else "",
        "scale_min": company.scale_min if company else None,
        "scale_max": company.scale_max if company else None,
        "nature": company.nature if company else "",
        "rule_status": rule.get("status"),
        "rule_reject": rule.get("reject_reason"),
        "rule_total": rule_total,
        "rule_finance": dims.get("finance"),
        "rule_growth": dims.get("growth"),
        "rule_resource": dims.get("resource"),
        "rule_wlb": dims.get("wlb"),
        "ai_needed": 1 if rule.get("ai_intervention_needed") else 0,
        "ai_count": summary.get("ai_count", 0),
        "ai_providers": _j([p.get("provider") for p in ai_providers]),
        "latest_ai_provider": latest.get("provider"),
        "latest_ai_total": ai_total,
        "latest_ai_at": latest.get("at"),
        "recommendation": latest.get("recommendation"),
        "best_total": best,
        "favorite": 1 if job.favorite else 0,
        "favorited_at": job.favorited_at or "",
        "ignored": 1 if job.ignored else 0,
        "ai_stale": stale[0],
        "ai_stale_reason": stale[1],
        "manual_total": manual_total,
        "manual_note": manual.get("note", ""),
        "manual_at": manual.get("at", ""),
        "indexed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def upsert_job(
    conn: sqlite3.Connection,
    job: Job,
    company: Company | None = None,
    *,
    current_fp: str | None = None,
    refresh_company: bool = True,
) -> None:
    summary = repo.score_summary(job.job_id)
    rule = repo.load_rule_score(job.job_id)
    if company is None and job.company_id:
        company = repo.load_company(job.company_id)

    # 重打分过时标记: 基于最新一条 AI 打分 (文件是真相源)
    ai_items = repo.list_ai_scores(job.job_id)
    stale = (0, "")
    if ai_items:
        from ..core.context import stale_info

        stale = stale_info(ai_items[0], current_fp=current_fp)

    row = _job_row(job, company, summary, rule, stale=stale)
    cols = ", ".join(row.keys())
    placeholders = ", ".join(f":{k}" for k in row)
    conn.execute(f"INSERT OR REPLACE INTO jobs ({cols}) VALUES ({placeholders})", row)

    conn.execute("DELETE FROM jobs_fts WHERE job_id = ?", (job.job_id,))
    conn.execute(
        "INSERT INTO jobs_fts (job_id, title, company_name, skills, jd) VALUES (?,?,?,?,?)",
        (
            job.job_id,
            job.title,
            row["company_name"],
            " ".join(job.skills),
            job.jd.get("full", "")[:20000],
        ),
    )

    conn.execute("DELETE FROM scores WHERE job_id = ?", (job.job_id,))
    if rule:
        conn.execute(
            "INSERT OR IGNORE INTO scores (job_id, kind, provider, model, total, status,"
            " recommendation, dims, created_at, file) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                job.job_id,
                "rule",
                "rule-engine",
                f"v{rule.get('rules_version', '3')}",
                rule.get("total_score"),
                rule.get("status"),
                None,
                _j(rule.get("dimension_scores", {})),
                rule.get("created_at", ""),
                cfg.RULE_SCORE_FILE,
            ),
        )
    for item in ai_items:
        conn.execute(
            "INSERT OR IGNORE INTO scores (job_id, kind, provider, model, total, status,"
            " recommendation, dims, created_at, file, context_fp)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                job.job_id,
                "ai",
                item.get("provider"),
                item.get("model", ""),
                item.get("total_score"),
                item.get("status"),
                item.get("recommendation"),
                _j(item.get("dimension_scores", {})),
                item.get("created_at", ""),
                item.get("_file", ""),
                item.get("context_fingerprint"),
            ),
        )

    # 单岗刷新路径: 同步重算所属公司的聚合统计 (全量 reindex 走批量重建)
    if refresh_company and job.company_id:
        refresh_company_stats(conn, [job.company_id])


def upsert_company(conn: sqlite3.Connection, company: Company, job_count: int = 0) -> None:
    # INSERT OR REPLACE 会整行覆盖, 先保住用户手动设置的公司收藏
    existing = conn.execute(
        "SELECT favorite FROM companies WHERE brand_id = ?", (company.brand_id,)
    ).fetchone()
    favorite = existing["favorite"] if existing else (1 if getattr(company, "favorite", False) else 0)
    conn.execute(
        "INSERT OR REPLACE INTO companies (brand_id, name, short_name, industry, stage,"
        " scale_raw, scale_min, scale_max, nature, founded, capital, hours_per_day,"
        " job_count, favorite, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            company.brand_id,
            company.name,
            company.short_name,
            company.industry,
            company.stage,
            company.scale_raw,
            company.scale_min,
            company.scale_max,
            company.nature,
            company.founded,
            company.registered_capital_10k,
            company.hours_per_day,
            job_count,
            favorite,
            company.updated_at,
        ),
    )


def set_company_favorite(conn: sqlite3.Connection, brand_id: str, favorite: bool) -> None:
    """公司级收藏 (图鉴"想去清单")。"""
    conn.execute(
        "UPDATE companies SET favorite = ? WHERE brand_id = ?",
        (1 if favorite else 0, brand_id),
    )
    conn.commit()


# ---------------------------------------------------------------- 公司聚合


def _company_rank_tier(score: float | None) -> str:
    """company_score → S/A/B/C 等级徽章 (纯视觉层, 阈值见 GuideConfig)。"""
    if score is None:
        return ""
    thresholds = cfg.SETTINGS.guide.rank_tiers
    for label, th in zip(("S", "A", "B"), thresholds):
        if score >= th:
            return label
    return "C"


def refresh_company_stats(
    conn: sqlite3.Connection, brand_ids: Iterable[str] | None = None
) -> int:
    """重算 company_stats 派生表 (纯派生, 随时可整表重建)。

    brand_ids=None 时全量重建。公式结构见 design/图鉴进化方案.md B3 第一期:
      company_score = 头部加权均值 (前 3 个岗位 best_total, 0.5/0.3/0.2)
                    + 活跃度修正 (在线占比 + last_seen 新鲜度, ±0.5 内)
                    + 信息完整度加成 (简介/经营范围, 上限 +0.3)
    匿名雇主与 brand_id 串号的公司照样统计, 但标记 excluded=1 不进图鉴。
    """
    from ..core.context import parse_iso

    g = cfg.SETTINGS.guide
    now_ts = time.time()
    computed_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    if brand_ids is None:
        ids = [r["brand_id"] for r in conn.execute("SELECT brand_id FROM companies").fetchall()]
        conn.execute("DELETE FROM company_stats")
    else:
        ids = list(dict.fromkeys(brand_ids))
        if not ids:
            return 0
        conn.execute(
            f"DELETE FROM company_stats WHERE brand_id IN ({','.join('?' * len(ids))})",
            ids,
        )

    n = 0
    for brand_id in ids:
        company = repo.load_company(brand_id)
        rows = conn.execute(
            "SELECT job_id, title, best_total, manual_total, rule_status, salary_mid,"
            " city, online, last_seen, ai_count"
            " FROM jobs WHERE company_id = ? AND (ignored = 0 OR ignored IS NULL)",
            (brand_id,),
        ).fetchall()

        job_count = len(rows)
        online_count = sum(1 for r in rows if r["online"])
        scored = [r for r in rows if r["best_total"] is not None]
        scored_count = len(scored)
        ai_scored_count = sum(1 for r in rows if (r["ai_count"] or 0) > 0)
        best_score = max((r["best_total"] for r in scored), default=None)
        avg_score = (
            round(sum(r["best_total"] for r in scored) / scored_count, 2)
            if scored_count else None
        )
        salary_mids = [r["salary_mid"] for r in rows if r["salary_mid"] is not None]
        salary_mid_avg = (
            round(sum(salary_mids) / len(salary_mids), 2) if salary_mids else None
        )
        cities = sorted({r["city"] for r in rows if r["city"]})

        # 头部加权候选: 剔除规则淘汰岗, 但人工调分或 AI 打分过的保留
        # —— 二者都代表用户主动介入过该岗位, 用户意志优先于规则自动淘汰
        head_pool = [
            r for r in scored
            if r["rule_status"] != "REJECTED"
            or r["manual_total"] is not None
            or (r["ai_count"] or 0) > 0
        ]
        head_pool.sort(key=lambda r: r["best_total"], reverse=True)
        head = head_pool[: len(g.head_weights)]

        company_score: float | None = None
        if head:
            weights = g.head_weights[: len(head)]
            base = sum(r["best_total"] * w for r, w in zip(head, weights)) / sum(weights)

            # 活跃度修正
            online_ratio = online_count / job_count if job_count else 0.0
            seen_ts_list = [t for t in (parse_iso(r["last_seen"]) for r in rows) if t]
            seen_ts = max(seen_ts_list) if seen_ts_list else None
            age_days = (now_ts - seen_ts) / 86400 if seen_ts else float("inf")
            if age_days <= g.fresh_window_high_days:
                fresh = g.fresh_bonus_high
            elif age_days <= g.fresh_window_low_days:
                fresh = g.fresh_bonus_low
            else:
                fresh = -g.fresh_penalty
            activity = (online_ratio - 0.5) * g.online_ratio_factor + fresh
            activity = max(-g.activity_cap, min(g.activity_cap, activity))

            # 信息完整度加成
            intro = company.intro if company else ""
            scope = company.business_scope if company else ""
            bonus = min(
                g.info_bonus_each * (bool(intro) + bool(scope)), g.info_bonus_cap
            )

            company_score = round(max(0.0, min(10.0, base + activity + bonus)), 2)

        # 头部加权无候选 (如全部规则淘汰且无 AI/人工分), 回退到公司级 AI 评价分
        if company_score is None:
            ai_company = repo.latest_company_ai_score(brand_id)
            if ai_company:
                try:
                    company_score = round(
                        max(0.0, min(10.0, float(ai_company["company_score_ai"]))), 2
                    )
                except (TypeError, ValueError):
                    pass

        # 最佳岗位 (展示用): 全部非忽略岗位里 best_total 最高的
        top_row = max(
            (r for r in rows if r["best_total"] is not None),
            key=lambda r: r["best_total"],
            default=None,
        )
        seen_values = [r["last_seen"] for r in rows if r["last_seen"]]

        excluded = 1 if (
            brand_id.startswith("anon-")
            or (company and (company.anonymous or company.data_conflict))
        ) else 0

        conn.execute(
            "INSERT OR REPLACE INTO company_stats ("
            " brand_id, job_count, online_count, scored_count, ai_scored_count,"
            " best_score, avg_score, company_score, rank_tier, salary_mid_avg,"
            " cities, top_job_id, top_job_title, top_job_score, latest_seen,"
            " has_intro, has_scope, excluded, computed_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                brand_id,
                job_count,
                online_count,
                scored_count,
                ai_scored_count,
                best_score,
                avg_score,
                company_score,
                _company_rank_tier(company_score),
                salary_mid_avg,
                _j(cities),
                top_row["job_id"] if top_row else None,
                top_row["title"] if top_row else None,
                top_row["best_total"] if top_row else None,
                max(seen_values) if seen_values else None,
                1 if (company and company.intro) else 0,
                1 if (company and company.business_scope) else 0,
                excluded,
                computed_at,
            ),
        )
        n += 1

    conn.commit()
    return n


def reindex(db_path: Path | None = None) -> dict:
    """从 data/ 下的文件全量重建索引。"""
    started = time.time()
    conn = connect(db_path)
    try:
        conn.execute("DELETE FROM jobs")
        conn.execute("DELETE FROM jobs_fts")
        conn.execute("DELETE FROM scores")
        conn.execute("DELETE FROM companies")
        conn.execute("DELETE FROM company_stats")

        companies = {c.brand_id: c for c in repo.iter_companies()}
        job_counts: dict[str, int] = {}

        # 上下文指纹只算一次, 供全部岗位的 stale 判定复用
        from ..core.context import compute_context_fingerprint

        current_fp = compute_context_fingerprint()

        n_jobs = 0
        for job in repo.iter_jobs():
            company = companies.get(job.company_id)
            # refresh_company=False: 公司聚合统一在下面批量重建
            upsert_job(conn, job, company, current_fp=current_fp, refresh_company=False)
            if job.company_id:
                job_counts[job.company_id] = job_counts.get(job.company_id, 0) + 1
            n_jobs += 1

        for brand_id, company in companies.items():
            upsert_company(conn, company, job_counts.get(brand_id, 0))

        refresh_company_stats(conn)

        conn.commit()
    finally:
        conn.close()

    elapsed = round(time.time() - started, 2)
    log.info(f"索引重建完成: {n_jobs} 个职位, {len(companies)} 家公司, 耗时 {elapsed}s")
    return {"jobs": n_jobs, "companies": len(companies), "seconds": elapsed}


def delete_job_from_index(conn: sqlite3.Connection, job_id: str) -> None:
    """从索引中删除职位 (含 FTS + scores)。文件系统由 repo.delete_job 负责。"""
    row = conn.execute("SELECT company_id FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    company_id = row["company_id"] if row else None
    conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM jobs_fts WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM scores WHERE job_id = ?", (job_id,))
    if company_id:
        refresh_company_stats(conn, [company_id])
    conn.commit()


def delete_score_from_index(conn: sqlite3.Connection, job_id: str, file_name: str) -> None:
    """从索引中删除单条 AI 打分, 并刷新 jobs 表的 AI 汇总字段。"""
    conn.execute(
        "DELETE FROM scores WHERE job_id = ? AND file = ? AND kind = 'ai'",
        (job_id, file_name),
    )
    # 重新汇总该职位的 AI 打分状态
    rows = conn.execute(
        "SELECT provider, total, created_at FROM scores"
        " WHERE job_id = ? AND kind = 'ai' ORDER BY created_at DESC",
        (job_id,),
    ).fetchall()
    if rows:
        first = rows[0]
        providers = [r["provider"] for r in rows]
        conn.execute(
            "UPDATE jobs SET ai_count = ?, ai_providers = ?,"
            " latest_ai_provider = ?, latest_ai_total = ?, latest_ai_at = ?"
            " WHERE job_id = ?",
            (len(rows), _j(providers), first["provider"], first["total"],
             first["created_at"], job_id),
        )
    else:
        conn.execute(
            "UPDATE jobs SET ai_count = 0, ai_providers = '[]',"
            " latest_ai_provider = NULL, latest_ai_total = NULL, latest_ai_at = NULL"
            " WHERE job_id = ?",
            (job_id,),
        )
    # best_total 依赖 ai_total, 需重算
    row = conn.execute(
        "SELECT company_id, latest_ai_total, rule_total, manual_total FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    if row:
        manual_total = row["manual_total"]
        ai_total = row["latest_ai_total"]
        rule_total = row["rule_total"]
        # 优先级: manual > ai > rule
        if manual_total is not None:
            best = manual_total
        elif ai_total is not None:
            best = ai_total
        else:
            best = rule_total
        conn.execute("UPDATE jobs SET best_total = ? WHERE job_id = ?", (best, job_id))
    # 同步刷新 stale 标记 (文件是真相源)
    from ..core.context import stale_info

    stale_flag, stale_reason = stale_info(repo.latest_ai_score(job_id))
    conn.execute(
        "UPDATE jobs SET ai_stale = ?, ai_stale_reason = ? WHERE job_id = ?",
        (stale_flag, stale_reason, job_id),
    )
    # best_total 变了, 公司聚合分同步刷新
    if row and row["company_id"]:
        refresh_company_stats(conn, [row["company_id"]])
    conn.commit()


def update_manual_override(
    conn: sqlite3.Connection,
    job_id: str,
    total: float | None,
    note: str,
) -> None:
    """更新人工调分覆盖。

    total=None 表示清除人工调分, 回退到 AI/规则分。
    会同步重算 best_total。
    """
    import time as _time

    if total is None:
        conn.execute(
            "UPDATE jobs SET manual_total = NULL, manual_note = NULL, manual_at = NULL"
            " WHERE job_id = ?",
            (job_id,),
        )
    else:
        conn.execute(
            "UPDATE jobs SET manual_total = ?, manual_note = ?, manual_at = ?"
            " WHERE job_id = ?",
            (float(total), note, _time.strftime("%Y-%m-%dT%H:%M:%S"), job_id),
        )
    # 重算 best_total
    row = conn.execute(
        "SELECT company_id, latest_ai_total, rule_total, manual_total FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    if row:
        manual_total = row["manual_total"]
        ai_total = row["latest_ai_total"]
        rule_total = row["rule_total"]
        if manual_total is not None:
            best = manual_total
        elif ai_total is not None:
            best = ai_total
        else:
            best = rule_total
        conn.execute("UPDATE jobs SET best_total = ? WHERE job_id = ?", (best, job_id))
    # 人工调分影响公司聚合分
    if row and row["company_id"]:
        refresh_company_stats(conn, [row["company_id"]])
    conn.commit()


# ---------------------------------------------------------------- stale 刷新


def refresh_ai_stale(
    conn: sqlite3.Connection,
    current_fp: str | None = None,
    ttl_days: float | None = None,
) -> None:
    """批量刷新 jobs.ai_stale 标记 (纯 SQL, 不读文件)。

    用于画像/规则配置保存后、backlog 构建前这类"预期变了"的时刻。
    判定与 core.context.stale_info 保持一致:
      - 最新 AI 分的上下文指纹 != 当前指纹 → context_changed (优先)
      - 最新 AI 分超过 TTL → expired
      - 旧数据 (无指纹) 只受 TTL 约束
    """
    from ..core.context import DEFAULT_STALE_TTL_DAYS, compute_context_fingerprint

    fp = current_fp or compute_context_fingerprint()
    ttl = cfg.SETTINGS.ai.stale_ttl_days if ttl_days is None else ttl_days
    ttl = DEFAULT_STALE_TTL_DAYS if ttl is None else ttl
    cutoff = time.strftime(
        "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - ttl * 86400)
    )

    # 每个岗位的最新一条 AI 打分 (按 created_at, 同时间按文件名倒序兜底)
    latest_cte = (
        "WITH latest AS ("
        "  SELECT job_id, context_fp, created_at,"
        "         ROW_NUMBER() OVER ("
        "           PARTITION BY job_id ORDER BY created_at DESC, file DESC"
        "         ) AS rn"
        "  FROM scores WHERE kind = 'ai'"
        ") "
    )

    conn.execute("UPDATE jobs SET ai_stale = 0, ai_stale_reason = ''")
    conn.execute(
        latest_cte
        + "UPDATE jobs SET ai_stale = 1, ai_stale_reason = 'expired'"
        " WHERE job_id IN ("
        "   SELECT job_id FROM latest WHERE rn = 1"
        "     AND created_at != '' AND created_at < ?"
        " )",
        (cutoff,),
    )
    conn.execute(
        latest_cte
        + "UPDATE jobs SET ai_stale = 1, ai_stale_reason = 'context_changed'"
        " WHERE job_id IN ("
        "   SELECT job_id FROM latest WHERE rn = 1"
        "     AND context_fp IS NOT NULL AND context_fp != '' AND context_fp != ?"
        " )",
        (fp,),
    )
    conn.commit()


# ---------------------------------------------------------------- 查询

_SORTABLE = {
    "best_total", "rule_total", "latest_ai_total", "salary_max", "salary_min",
    "salary_mid", "last_seen", "first_seen", "quality_score", "jd_length", "title",
    "favorited_at", "exp_max",
}


def _search_clause(search: str) -> tuple[str, list[Any]]:
    """构造搜索条件。

    坑: FTS5 的 trigram 分词器要求查询串**至少 3 个字符**, 少于 3 个直接返回
    空结果。而中文里 "全栈" "算法" "外包" 这种两字词恰恰是最常搜的。
    所以短查询走 LIKE 兜底 (数据量在万级以内, 全表扫可以接受),
    长查询才走 FTS。
    """
    text = search.strip()
    if not text:
        return "1=1", []

    if len(text) >= 3:
        # 双引号包成短语, 避免 " - * : 等 FTS 语法字符引发 OperationalError
        escaped = text.replace('"', '""')
        return (
            "job_id IN (SELECT job_id FROM jobs_fts WHERE jobs_fts MATCH ?)",
            [f'"{escaped}"'],
        )

    like = f"%{text}%"
    return (
        "job_id IN (SELECT job_id FROM jobs_fts WHERE title LIKE ?"
        " OR company_name LIKE ? OR skills LIKE ? OR jd LIKE ?)",
        [like, like, like, like],
    )


def _build_where(
    *,
    search: str = "",
    cities: Iterable[str] = (),
    statuses: Iterable[str] = (),
    scored: str = "all",
    providers: Iterable[str] = (),
    salary_min: float | None = None,
    online_only: bool = False,
    outsourcing: bool | None = None,
    favorite: str = "all",
    ignored: str = "exclude",
    new_since: str = "",
) -> tuple[str, list[Any]]:
    """构造列表筛选的 WHERE 子句与参数, 供 query / count 共用。"""
    where: list[str] = []
    params: list[Any] = []
    if search:
        clause, search_params = _search_clause(search)
        where.append(clause)
        params.extend(search_params)
    cities = [c for c in cities if c]
    if cities:
        where.append(f"city IN ({','.join('?' * len(cities))})")
        params.extend(cities)
    statuses = [s for s in statuses if s]
    if statuses:
        where.append(f"rule_status IN ({','.join('?' * len(statuses))})")
        params.extend(statuses)
    if scored == "none":
        where.append("rule_total IS NULL AND ai_count = 0")
    elif scored == "rule_only":
        where.append("rule_total IS NOT NULL AND ai_count = 0")
    elif scored == "ai":
        where.append("ai_count > 0")
    elif scored == "no_ai":
        where.append("ai_count = 0")
    providers = [p for p in providers if p]
    if providers:
        clause = " OR ".join(["ai_providers LIKE ?"] * len(providers))
        where.append(f"({clause})")
        params.extend([f'%"{p}"%' for p in providers])
    if salary_min is not None:
        where.append("salary_max >= ?")
        params.append(salary_min)
    if online_only:
        where.append("online = 1")
    if outsourcing is not None:
        where.append("outsourcing = ?")
        params.append(1 if outsourcing else 0)
    if favorite == "only":
        where.append("favorite = 1")
    elif favorite == "exclude":
        where.append("favorite = 0")
    if ignored == "exclude":
        where.append("(ignored = 0 OR ignored IS NULL)")
    elif ignored == "only":
        where.append("ignored = 1")
    if new_since:
        # first_seen 历史上有 "YYYY-MM-DD HH:MM:SS" 和 ISO "T" 两种格式,
        # 归一化成 T 再比较, 避免字符串比较出错
        where.append("REPLACE(first_seen, ' ', 'T') >= ?")
        params.append(new_since)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return clause, params


def query_jobs(
    conn: sqlite3.Connection,
    *,
    search: str = "",
    cities: Iterable[str] = (),
    statuses: Iterable[str] = (),
    scored: str = "all",          # all | none | rule_only | ai
    providers: Iterable[str] = (),
    salary_min: float | None = None,
    online_only: bool = False,
    outsourcing: bool | None = None,
    favorite: str = "all",        # all | only | exclude
    ignored: str = "exclude",     # exclude | all | only
    new_since: str = "",          # first_seen 不早于该时间 (ISO 字符串)
    sort: str = "best_total",
    desc: bool = True,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    where_clause, params = _build_where(
        search=search, cities=cities, statuses=statuses, scored=scored,
        providers=providers, salary_min=salary_min, online_only=online_only,
        outsourcing=outsourcing, favorite=favorite, ignored=ignored,
        new_since=new_since,
    )
    sort_col = sort if sort in _SORTABLE else "best_total"
    direction = "DESC" if desc else "ASC"
    sql = (
        "SELECT * FROM jobs" + where_clause +
        # 收藏永远排最前, 然后按 sort_col 排序, NULL 排最后
        f" ORDER BY favorite DESC, ({sort_col} IS NULL), {sort_col} {direction}"
        f" LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])

    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def count_jobs(
    conn: sqlite3.Connection,
    *,
    search: str = "",
    cities: Iterable[str] = (),
    statuses: Iterable[str] = (),
    scored: str = "all",
    providers: Iterable[str] = (),
    salary_min: float | None = None,
    online_only: bool = False,
    outsourcing: bool | None = None,
    favorite: str = "all",
    ignored: str = "exclude",
    new_since: str = "",
) -> int:
    """带筛选条件的职位计数, 参数与 query_jobs 一致。"""
    where_clause, params = _build_where(
        search=search, cities=cities, statuses=statuses, scored=scored,
        providers=providers, salary_min=salary_min, online_only=online_only,
        outsourcing=outsourcing, favorite=favorite, ignored=ignored,
        new_since=new_since,
    )
    sql = "SELECT COUNT(*) FROM jobs" + where_clause
    return conn.execute(sql, params).fetchone()[0]


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("skills", "welfare", "ai_providers"):
        if isinstance(d.get(key), str):
            try:
                d[key] = json.loads(d[key])
            except (json.JSONDecodeError, TypeError):
                d[key] = []
    return d


def facets(conn: sqlite3.Connection) -> dict:
    """给筛选面板用的可选项与计数。"""

    def group(col: str) -> list[dict]:
        rows = conn.execute(
            f"SELECT {col} AS k, COUNT(*) AS n FROM jobs"
            f" WHERE {col} IS NOT NULL AND {col} != '' AND (ignored = 0 OR ignored IS NULL)"
            f" GROUP BY {col} ORDER BY n DESC"
        ).fetchall()
        return [{"value": r["k"], "count": r["n"]} for r in rows]

    # 统计默认排除被忽略的职位, 与列表展示一致
    visible = "(ignored = 0 OR ignored IS NULL)"
    total = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {visible}").fetchone()[0]
    scored_rule = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE rule_total IS NOT NULL AND {visible}"
    ).fetchone()[0]
    scored_ai = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE ai_count > 0 AND {visible}"
    ).fetchone()[0]
    favorite_count = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE favorite = 1 AND {visible}"
    ).fetchone()[0]
    ignored_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE ignored = 1"
    ).fetchone()[0]
    return {
        "total": total,
        "scored_rule": scored_rule,
        "scored_ai": scored_ai,
        "unscored": total - scored_rule,
        "favorite": favorite_count,
        "ignored": ignored_count,
        "cities": group("city"),
        "industries": group("industry"),
        "stages": group("stage"),
        "statuses": group("rule_status"),
        "overtime": group("overtime"),
        "providers": [
            {"value": r["provider"], "count": r["n"]}
            for r in conn.execute(
                "SELECT provider, COUNT(DISTINCT job_id) AS n FROM scores"
                " WHERE kind='ai' GROUP BY provider ORDER BY n DESC"
            ).fetchall()
        ],
    }


# ---------------------------------------------------------------- 公司图鉴查询


_COMPANY_SELECT = (
    "SELECT c.brand_id, c.name, c.short_name, c.industry, c.stage, c.scale_raw,"
    " c.scale_min, c.scale_max, c.nature, c.founded, c.capital, c.hours_per_day,"
    " c.favorite, c.updated_at,"
    " s.job_count, s.online_count, s.scored_count, s.ai_scored_count,"
    " s.best_score, s.avg_score, s.company_score, s.rank_tier, s.salary_mid_avg,"
    " s.cities, s.top_job_id, s.top_job_title, s.top_job_score, s.latest_seen,"
    " s.has_intro, s.has_scope, s.excluded"
    " FROM companies c LEFT JOIN company_stats s ON s.brand_id = c.brand_id"
)

_COMPANY_SORTS = {
    "score": "s.company_score",
    "jobs": "s.job_count",
    "salary": "s.salary_mid_avg",
    "best": "s.best_score",
    "updated": "c.updated_at",
}


def _company_where(
    *,
    q: str = "",
    industry: str = "",
    stage: str = "",
    city: str = "",
    favorite: str = "all",
    include_excluded: bool = False,
    scored_only: bool = False,
) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if not include_excluded:
        where.append("(s.excluded = 0 OR s.excluded IS NULL)")
    if q.strip():
        where.append("(c.name LIKE ? OR c.short_name LIKE ?)")
        like = f"%{q.strip()}%"
        params.extend([like, like])
    if industry:
        where.append("c.industry = ?")
        params.append(industry)
    if stage:
        where.append("c.stage = ?")
        params.append(stage)
    if city:
        # cities 是 JSON 数组文本 (ensure_ascii=False), LIKE 匹配足够
        where.append("s.cities LIKE ?")
        params.append(f"%{city}%")
    if favorite == "only":
        where.append("c.favorite = 1")
    if scored_only:
        where.append("s.company_score IS NOT NULL")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return clause, params


def query_companies(
    conn: sqlite3.Connection,
    *,
    q: str = "",
    industry: str = "",
    stage: str = "",
    city: str = "",
    favorite: str = "all",
    include_excluded: bool = False,
    scored_only: bool = False,
    sort: str = "score",
    desc: bool = True,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    where_clause, params = _company_where(
        q=q, industry=industry, stage=stage, city=city, favorite=favorite,
        include_excluded=include_excluded, scored_only=scored_only,
    )
    sort_col = _COMPANY_SORTS.get(sort, "s.company_score")
    direction = "DESC" if desc else "ASC"
    sql = (
        _COMPANY_SELECT + where_clause +
        # 收藏置顶, NULL 排最后, 同分按名字稳定排序
        f" ORDER BY c.favorite DESC, ({sort_col} IS NULL), {sort_col} {direction},"
        " c.name ASC LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("cities"), str):
            try:
                d["cities"] = json.loads(d["cities"])
            except (json.JSONDecodeError, TypeError):
                d["cities"] = []
        out.append(d)
    return out


def count_companies(
    conn: sqlite3.Connection,
    *,
    q: str = "",
    industry: str = "",
    stage: str = "",
    city: str = "",
    favorite: str = "all",
    include_excluded: bool = False,
    scored_only: bool = False,
) -> int:
    where_clause, params = _company_where(
        q=q, industry=industry, stage=stage, city=city, favorite=favorite,
        include_excluded=include_excluded, scored_only=scored_only,
    )
    sql = "SELECT COUNT(*) FROM companies c LEFT JOIN company_stats s ON s.brand_id = c.brand_id" + where_clause
    return conn.execute(sql, params).fetchone()[0]


def company_facets(conn: sqlite3.Connection) -> dict:
    """图鉴顶栏概况 + 筛选面板选项。"""
    row = conn.execute(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN company_score IS NOT NULL THEN 1 ELSE 0 END) AS scored,"
        " SUM(CASE WHEN ai_scored_count > 0 THEN 1 ELSE 0 END) AS unlocked"
        " FROM company_stats WHERE excluded = 0"
    ).fetchone()
    fav = conn.execute(
        "SELECT COUNT(*) FROM companies c"
        " LEFT JOIN company_stats s ON s.brand_id = c.brand_id"
        " WHERE c.favorite = 1 AND (s.excluded = 0 OR s.excluded IS NULL)"
    ).fetchone()[0]

    def group(col: str) -> list[dict]:
        rows = conn.execute(
            f"SELECT {col} AS k, COUNT(*) AS n FROM companies c"
            " LEFT JOIN company_stats s ON s.brand_id = c.brand_id"
            f" WHERE {col} IS NOT NULL AND {col} != ''"
            " AND (s.excluded = 0 OR s.excluded IS NULL)"
            f" GROUP BY {col} ORDER BY n DESC"
        ).fetchall()
        return [{"value": r["k"], "count": r["n"]} for r in rows]

    return {
        "total": row["total"] or 0,
        "scored": row["scored"] or 0,
        "unlocked": row["unlocked"] or 0,
        "favorite": fav,
        "industries": group("c.industry"),
        "stages": group("c.stage"),
    }


def company_dims_avg(conn: sqlite3.Connection, brand_id: str) -> dict | None:
    """公司四维分均值 (对比雷达图用)。

    每个岗位优先取最新一条 AI 打分的维度分, 没有则退回规则维度分;
    再对所有岗位求均值。全部岗位都没有维度分时返回 None。
    """
    rows = conn.execute(
        "SELECT job_id FROM jobs WHERE company_id = ? AND (ignored = 0 OR ignored IS NULL)",
        (brand_id,),
    ).fetchall()
    dims_list: list[dict] = []
    for r in rows:
        jid = r["job_id"]
        s = conn.execute(
            "SELECT dims FROM scores WHERE job_id = ? AND kind = 'ai'"
            " AND dims IS NOT NULL AND dims != '' AND dims != '{}'"
            " ORDER BY created_at DESC, file DESC LIMIT 1",
            (jid,),
        ).fetchone()
        if not s:
            s = conn.execute(
                "SELECT dims FROM scores WHERE job_id = ? AND kind = 'rule'"
                " AND dims IS NOT NULL AND dims != '' AND dims != '{}'",
                (jid,),
            ).fetchone()
        if not s:
            continue
        try:
            d = json.loads(s["dims"])
        except (json.JSONDecodeError, TypeError):
            continue
        if any(isinstance(d.get(k), (int, float)) for k in ("finance", "growth", "resource", "wlb")):
            dims_list.append(d)
    if not dims_list:
        return None
    out: dict = {}
    for k in ("finance", "growth", "resource", "wlb"):
        vals = [d[k] for d in dims_list if isinstance(d.get(k), (int, float))]
        out[k] = round(sum(vals) / len(vals), 2) if vals else None
    return out
