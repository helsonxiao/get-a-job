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
    updated_at   TEXT
);

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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_favorite ON jobs(favorite)")
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


def _job_row(job: Job, company: Company | None, summary: dict, rule: dict | None) -> dict:
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
        "manual_total": manual_total,
        "manual_note": manual.get("note", ""),
        "manual_at": manual.get("at", ""),
        "indexed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def upsert_job(conn: sqlite3.Connection, job: Job, company: Company | None = None) -> None:
    summary = repo.score_summary(job.job_id)
    rule = repo.load_rule_score(job.job_id)
    if company is None and job.company_id:
        company = repo.load_company(job.company_id)

    row = _job_row(job, company, summary, rule)
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
    for item in repo.list_ai_scores(job.job_id):
        conn.execute(
            "INSERT OR IGNORE INTO scores (job_id, kind, provider, model, total, status,"
            " recommendation, dims, created_at, file) VALUES (?,?,?,?,?,?,?,?,?,?)",
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
            ),
        )


def upsert_company(conn: sqlite3.Connection, company: Company, job_count: int = 0) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO companies (brand_id, name, short_name, industry, stage,"
        " scale_raw, scale_min, scale_max, nature, founded, capital, hours_per_day,"
        " job_count, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
            company.updated_at,
        ),
    )


def reindex(db_path: Path | None = None) -> dict:
    """从 data/ 下的文件全量重建索引。"""
    started = time.time()
    conn = connect(db_path)
    try:
        conn.execute("DELETE FROM jobs")
        conn.execute("DELETE FROM jobs_fts")
        conn.execute("DELETE FROM scores")
        conn.execute("DELETE FROM companies")

        companies = {c.brand_id: c for c in repo.iter_companies()}
        job_counts: dict[str, int] = {}

        n_jobs = 0
        for job in repo.iter_jobs():
            company = companies.get(job.company_id)
            upsert_job(conn, job, company)
            if job.company_id:
                job_counts[job.company_id] = job_counts.get(job.company_id, 0) + 1
            n_jobs += 1

        for brand_id, company in companies.items():
            upsert_company(conn, company, job_counts.get(brand_id, 0))

        conn.commit()
    finally:
        conn.close()

    elapsed = round(time.time() - started, 2)
    log.info(f"索引重建完成: {n_jobs} 个职位, {len(companies)} 家公司, 耗时 {elapsed}s")
    return {"jobs": n_jobs, "companies": len(companies), "seconds": elapsed}


def delete_job_from_index(conn: sqlite3.Connection, job_id: str) -> None:
    """从索引中删除职位 (含 FTS + scores)。文件系统由 repo.delete_job 负责。"""
    conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM jobs_fts WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM scores WHERE job_id = ?", (job_id,))
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
        "SELECT latest_ai_total, rule_total, manual_total FROM jobs WHERE job_id = ?", (job_id,)
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
        "SELECT latest_ai_total, rule_total, manual_total FROM jobs WHERE job_id = ?",
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
    sort: str = "best_total",
    desc: bool = True,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
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

    sort_col = sort if sort in _SORTABLE else "best_total"
    direction = "DESC" if desc else "ASC"
    sql = "SELECT * FROM jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    # 收藏永远排最前, 然后按 sort_col 排序, NULL 排最后
    sql += (
        f" ORDER BY favorite DESC, ({sort_col} IS NULL), {sort_col} {direction}"
        f" LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])

    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def count_jobs(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


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
