"""公司图鉴查询: 从 index.py 抽出的独立领域模块。

包含公司列表/详情/ facets /维度均值等只读查询。
公司级写入 (favorite/excluded 等) 仍在 index.py 的 upsert/set 函数。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any


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


def companies_by_district(
    conn: sqlite3.Connection,
    *,
    district: str,
    industry: str = "",
    limit: int = 100,
) -> list[dict]:
    """按区域查公司 (热力图"按公司"模式下的钻取)。

    通过 jobs.district 关联公司, 返回该区域有岗位的公司列表,
    附岗位数/均薪/最佳分聚合。industry 可选叠加行业筛选。
    district/industry 传"未知"时匹配 NULL/空 (与观察台聚合口径对齐)。
    """
    where = ["j.company_id IS NOT NULL AND j.company_id != ''",
             "(j.ignored = 0 OR j.ignored IS NULL)"]
    params: list[Any] = []
    if district == "未知":
        where.append("(j.district IS NULL OR j.district = '')")
    else:
        where.append("j.district = ?")
        params.append(district)
    if industry:
        if industry == "未知":
            where.append("(j.industry IS NULL OR j.industry = '')")
        else:
            where.append("j.industry = ?")
            params.append(industry)
    sql = (
        "SELECT c.brand_id, c.name, c.industry, c.stage, c.scale_raw, c.favorite,"
        " COUNT(j.job_id) AS job_count,"
        " AVG(j.salary_mid) AS salary_mid_avg,"
        " MAX(j.best_total) AS best_score"
        " FROM jobs j JOIN companies c ON c.brand_id = j.company_id"
        " WHERE " + " AND ".join(where) +
        " GROUP BY c.brand_id"
        " ORDER BY best_score DESC, job_count DESC"
        " LIMIT ?"
    )
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("salary_mid_avg") is not None:
            d["salary_mid_avg"] = round(d["salary_mid_avg"], 2)
        if d.get("best_score") is not None:
            d["best_score"] = round(d["best_score"], 2)
        out.append(d)
    return out


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
