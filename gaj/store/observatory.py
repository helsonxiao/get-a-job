"""市场观察台查询: 从 index.py 抽出的独立领域模块。

所有函数接收已打开的 sqlite 连接, 不管理连接生命周期。
observatory_salary_pricing 等后续 G1/G2/S1 函数也落在此模块。
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from typing import Any  # noqa: F401  # 保持与 index.py 一致的类型导入风格

_VISIBLE = "(ignored = 0 OR ignored IS NULL)"

#: 聚合端把 NULL/空字段的展示值; 下钻端 (index._build_where) 会把"未知"翻译回 NULL/空匹配
_UNKNOWN = "未知"


def _norm(value: str | None) -> str:
    """NULL/空字段归一为展示值"未知", 与下钻筛选口径对齐。"""
    return value if value else _UNKNOWN


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    """线性插值分位, p in [0, 100]。"""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return round(sorted_vals[0], 2)
    k = (n - 1) * p / 100
    f = int(k)
    c = k - f
    if f + 1 < n:
        return round(sorted_vals[f] + (sorted_vals[f + 1] - sorted_vals[f]) * c, 2)
    return round(sorted_vals[f], 2)


def _median(vals: list[float]) -> float | None:
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return None
    mid = n // 2
    if n % 2 == 1:
        return round(s[mid], 2)
    return round((s[mid - 1] + s[mid]) / 2, 2)


def observatory_salary_pricing(conn: sqlite3.Connection) -> dict:
    """薪资定价曲线。

    返回市场对「每一年经验/每一级学历/每个行业/每个融资阶段」的定价(中位数+分位)。
    口径: 万元/年, 用 salary_mid; 过滤 ignored。
    空样本时 overall=None, 各分组返回 []。
    """
    visible = "(ignored = 0 OR ignored IS NULL)"
    rows = conn.execute(
        f"SELECT salary_mid, exp_min, edu_level, industry, stage"
        f" FROM jobs WHERE salary_mid IS NOT NULL AND salary_mid > 0 AND {visible}"
    ).fetchall()

    salaries = [r["salary_mid"] for r in rows]
    if not salaries:
        return {"overall": None, "by_exp": [], "by_edu": [], "by_industry": [], "by_stage": []}

    sorted_sal = sorted(salaries)
    overall = {
        "p10": _percentile(sorted_sal, 10),
        "p25": _percentile(sorted_sal, 25),
        "p50": _percentile(sorted_sal, 50),
        "p75": _percentile(sorted_sal, 75),
        "p90": _percentile(sorted_sal, 90),
        "mean": round(sum(salaries) / len(salaries), 2),
        "count": len(salaries),
    }

    # by_exp: 0-3 / 3-5 / 5-8 / 8+ / 不限
    exp_buckets = [
        ("0-3", "0-3年", lambda e: e is not None and e < 3),
        ("3-5", "3-5年", lambda e: e is not None and 3 <= e < 5),
        ("5-8", "5-8年", lambda e: e is not None and 5 <= e < 8),
        ("8+", "8年+", lambda e: e is not None and e >= 8),
        ("unlimited", "经验不限", lambda e: e is None),
    ]
    by_exp = []
    for bucket, label, pred in exp_buckets:
        vals = [r["salary_mid"] for r in rows if pred(r["exp_min"])]
        if vals:
            by_exp.append({"bucket": bucket, "label": label, "median": _median(vals), "count": len(vals)})

    # by_edu: 0=不限 1=高中及以下 2=大专 3=本科 4=硕士 5=博士
    edu_labels = {0: "不限", 1: "高中及以下", 2: "大专", 3: "本科", 4: "硕士", 5: "博士"}
    by_edu = []
    for lvl in sorted(edu_labels):
        vals = [r["salary_mid"] for r in rows if r["edu_level"] == lvl]
        if vals:
            by_edu.append({"level": lvl, "label": edu_labels[lvl], "median": _median(vals), "count": len(vals)})

    # by_industry: top 10 by count, 样本 >= 2
    industry_map: dict[str, list[float]] = {}
    for r in rows:
        ind = _norm(r["industry"])
        industry_map.setdefault(ind, []).append(r["salary_mid"])
    by_industry = sorted(
        [{"name": k, "median": _median(v), "count": len(v)} for k, v in industry_map.items() if len(v) >= 2],
        key=lambda x: x["count"], reverse=True,
    )[:10]

    # by_stage
    stage_map: dict[str, list[float]] = {}
    for r in rows:
        st = _norm(r["stage"])
        stage_map.setdefault(st, []).append(r["salary_mid"])
    by_stage = sorted(
        [{"name": k, "median": _median(v), "count": len(v)} for k, v in stage_map.items() if len(v) >= 2],
        key=lambda x: x["count"], reverse=True,
    )

    return {
        "overall": overall,
        "by_exp": by_exp,
        "by_edu": by_edu,
        "by_industry": by_industry,
        "by_stage": by_stage,
    }


def observatory_geo_heatmap(conn: sqlite3.Connection, cell_size: float = 0.01) -> dict:
    """区域机会热力图。按 lat/lng 网格分桶(cell_size 度≈1.1km)。

    每格: count、company_count(去重公司数)、avg_salary_mid、top_industry、top_district、top_company、
    district_total/district_company_total (所在区域全区岗位/公司数, 下钻抽屉的口径)。
    网格是空间近似, 点击下钻按 top_district 整区查询, 故 tooltip/前端应以 district_total 为准,
    保证图表数字与抽屉数字一致。
    过滤 lat IS NOT NULL 且 visible。返回网格中心点列表 + 经纬度边界, 供前端自适应缩放。
    空样本时 cells=[], bounds=None。
    """
    rows = conn.execute(
        f"SELECT lat, lng, salary_mid, industry, district, company_id, company_name FROM jobs"
        f" WHERE lat IS NOT NULL AND lng IS NOT NULL AND {_VISIBLE}"
    ).fetchall()
    if not rows:
        return {"cells": [], "bounds": None, "total": 0, "total_companies": 0, "top_districts": []}

    buckets: dict[tuple[int, int], dict] = defaultdict(
        lambda: {
            "count": 0,
            "salaries": [],
            "industries": Counter(),
            "districts": Counter(),
            "companies": Counter(),  # company_id -> company_name
        }
    )
    # 区域维度聚合 (与下钻口径一致): district -> {jobs, companies}
    district_totals: dict[str, dict] = defaultdict(lambda: {"jobs": 0, "companies": set()})
    all_companies: set = set()
    for r in rows:
        # 网格 key: floor(lat/cell), floor(lng/cell)
        key = (int(r["lat"] // cell_size), int(r["lng"] // cell_size))
        b = buckets[key]
        b["count"] += 1
        dist = r["district"] or ""
        dt = district_totals[dist]
        dt["jobs"] += 1
        if r["salary_mid"] is not None and r["salary_mid"] > 0:
            b["salaries"].append(r["salary_mid"])
        if r["industry"]:
            b["industries"][r["industry"]] += 1
        if r["district"]:
            b["districts"][r["district"]] += 1
        if r["company_id"]:
            b["companies"][r["company_id"]] += 1
            dt["companies"].add(r["company_id"])
            all_companies.add(r["company_id"])
            # 记公司名 (取最后一次非空)
            if r["company_name"]:
                b.setdefault("company_names", {})[r["company_id"]] = r["company_name"]

    cells = []
    min_lat = max_lat = min_lng = max_lng = None
    for (grid_lat, grid_lng), b in buckets.items():
        # 网格中心点
        clat = (grid_lat + 0.5) * cell_size
        clng = (grid_lng + 0.5) * cell_size
        if min_lat is None or clat < min_lat:
            min_lat = clat
        if max_lat is None or clat > max_lat:
            max_lat = clat
        if min_lng is None or clng < min_lng:
            min_lng = clng
        if max_lng is None or clng > max_lng:
            max_lng = clng
        sals = b["salaries"]
        avg_sal = round(sum(sals) / len(sals), 2) if sals else None
        top_ind = b["industries"].most_common(1)[0][0] if b["industries"] else None
        top_dist = b["districts"].most_common(1)[0][0] if b["districts"] else None
        comp_names = b.get("company_names", {})
        top_comp_id = b["companies"].most_common(1)[0][0] if b["companies"] else None
        top_comp = comp_names.get(top_comp_id) if top_comp_id else None
        # 该网格 top_district 的全区口径数字 (下钻抽屉显示的就是这两个数)
        dt = district_totals.get(top_dist or "", {"jobs": 0, "companies": set()})
        cells.append({
            "lat": round(clat, 4),
            "lng": round(clng, 4),
            "count": b["count"],
            "company_count": len(b["companies"]),
            "avg_salary": avg_sal,
            "top_industry": top_ind,
            "top_district": top_dist,
            "top_company": top_comp,
            "district_total": dt["jobs"],
            "district_company_total": len(dt["companies"]),
        })

    cells.sort(key=lambda x: x["count"], reverse=True)
    return {
        "cells": cells,
        "bounds": {
            "min_lat": round(min_lat, 4), "max_lat": round(max_lat, 4),
            "min_lng": round(min_lng, 4), "max_lng": round(max_lng, 4),
        },
        "total": len(rows),
        "total_companies": len(all_companies),
        "top_districts": observatory_district_top(conn, top_n=10),
    }


def observatory_district_top(conn: sqlite3.Connection, top_n: int = 10) -> list[dict]:
    """热点区域 Top N: 按 (district × 代表行业) 切片聚合。

    口径与前端下钻完全一致: 表格行的岗位数/公司数 = 点击后
    /api/jobs?district=X&industry=Y 与 /api/companies/by-district 的返回数。
    每个区域取岗位数最多的行业作为代表切片; district/industry 为空时显示"未知"
    (下钻端会把"未知"翻译回 NULL/空匹配)。
    """
    rows = conn.execute(
        f"SELECT district, industry, company_id, company_name, salary_mid FROM jobs"
        f" WHERE {_VISIBLE}"
    ).fetchall()
    # 先按 district 统计各行业岗位数, 取代表行业; 再按 (district, industry) 切片聚合
    dist_industry: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        dist_industry[_norm(r["district"])][_norm(r["industry"])] += 1
    rep_industry = {d: c.most_common(1)[0][0] for d, c in dist_industry.items()}

    slices: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "salaries": [], "companies": Counter(), "company_names": {}}
    )
    for r in rows:
        d = _norm(r["district"])
        if rep_industry.get(d) != _norm(r["industry"]):
            continue
        s = slices[d]
        s["count"] += 1
        if r["salary_mid"] and r["salary_mid"] > 0:
            s["salaries"].append(r["salary_mid"])
        if r["company_id"]:
            s["companies"][r["company_id"]] += 1
            if r["company_name"]:
                s["company_names"][r["company_id"]] = r["company_name"]

    items = []
    for d, s in slices.items():
        # 代表公司: 切片内岗位数最多的公司
        top_comp_id = s["companies"].most_common(1)[0][0] if s["companies"] else None
        top_comp = s["company_names"].get(top_comp_id) if top_comp_id else None
        items.append({
            "district": d,
            "industry": rep_industry.get(d, _UNKNOWN),
            "count": s["count"],
            "company_count": len(s["companies"]),
            "avg_salary": round(sum(s["salaries"]) / len(s["salaries"]), 2) if s["salaries"] else None,
            "top_company": top_comp,
        })
    items.sort(key=lambda x: x["count"], reverse=True)
    return items[:top_n]


def observatory_signal_radar(conn: sqlite3.Connection) -> dict:
    """加班/红旗信号雷达。

    返回:
      summary: overtime 各档计数 + outsourcing/travel 计数与占比;
      by_industry: 各行业红旗信号分布(加班重/外包/出差计数 + 红旗率), 样本>=3;
      red_flag_companies: 红旗公司榜(overtime=heavy 或 outsourcing=1 或出差频繁),
        按红旗岗位数排, 最多 20 家。
    口径: 过滤 visible; 空样本降级返回空列表。
    """
    rows = conn.execute(
        f"SELECT job_id, company_id, company_name, overtime, outsourcing, travel,"
        f" industry, salary_mid FROM jobs WHERE {_VISIBLE}"
    ).fetchall()
    if not rows:
        return {"summary": None, "by_industry": [], "red_flag_companies": []}

    total = len(rows)
    overtime_counter: Counter = Counter()
    outsourcing_count = 0
    travel_count = 0  # occasional + frequent + long_term
    for r in rows:
        ot = r["overtime"] or "unknown"
        overtime_counter[ot] += 1
        if r["outsourcing"]:
            outsourcing_count += 1
        tv = r["travel"]
        if tv and tv != "none":
            travel_count += 1

    summary = {
        "total": total,
        "overtime": {
            "heavy": overtime_counter.get("heavy", 0),
            "moderate": overtime_counter.get("moderate", 0),
            "light": overtime_counter.get("light", 0),
            "none": overtime_counter.get("none", 0),
            "unknown": overtime_counter.get("unknown", 0),
        },
        "outsourcing_count": outsourcing_count,
        "outsourcing_rate": round(outsourcing_count / total, 4) if total else 0,
        "travel_count": travel_count,
        "travel_rate": round(travel_count / total, 4) if total else 0,
    }

    # by_industry: 各行业红旗信号
    ind_map: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "heavy_ot": 0, "outsourcing": 0, "travel": 0}
    )
    for r in rows:
        ind = _norm(r["industry"])
        d = ind_map[ind]
        d["count"] += 1
        if r["overtime"] == "heavy":
            d["heavy_ot"] += 1
        if r["outsourcing"]:
            d["outsourcing"] += 1
        if r["travel"] and r["travel"] != "none":
            d["travel"] += 1
    by_industry = []
    for name, d in ind_map.items():
        if d["count"] < 3:
            continue
        red_flags = d["heavy_ot"] + d["outsourcing"] + d["travel"]
        by_industry.append({
            "name": name,
            "count": d["count"],
            "heavy_overtime": d["heavy_ot"],
            "outsourcing": d["outsourcing"],
            "travel": d["travel"],
            "red_flag_count": red_flags,
            "red_flag_rate": round(red_flags / d["count"], 4) if d["count"] else 0,
        })
    by_industry.sort(key=lambda x: x["red_flag_rate"], reverse=True)

    # red_flag_companies: 红旗公司榜
    comp_map: dict[str, dict] = defaultdict(
        lambda: {
            "name": "", "job_count": 0, "heavy_ot": 0, "outsourcing": 0,
            "travel": 0, "salaries": [],
        }
    )
    for r in rows:
        cid = r["company_id"]
        if not cid:
            continue
        is_red = (
            r["overtime"] == "heavy"
            or r["outsourcing"]
            or (r["travel"] in ("frequent", "long_term"))
        )
        if not is_red:
            continue
        d = comp_map[cid]
        d["name"] = r["company_name"] or d["name"]
        d["job_count"] += 1
        if r["overtime"] == "heavy":
            d["heavy_ot"] += 1
        if r["outsourcing"]:
            d["outsourcing"] += 1
        if r["travel"] in ("frequent", "long_term"):
            d["travel"] += 1
        if r["salary_mid"]:
            d["salaries"].append(r["salary_mid"])
    red_flag_companies = []
    for cid, d in comp_map.items():
        red_flag_companies.append({
            "brand_id": cid,
            "name": d["name"],
            "job_count": d["job_count"],
            "heavy_overtime": d["heavy_ot"],
            "outsourcing": d["outsourcing"],
            "travel": d["travel"],
            "flags": [k for k, v in (
                ("加班重", d["heavy_ot"] > 0),
                ("外包", d["outsourcing"] > 0),
                ("出差频繁", d["travel"] > 0),
            ) if v],
            "avg_salary": round(sum(d["salaries"]) / len(d["salaries"]), 2) if d["salaries"] else None,
        })
    red_flag_companies.sort(key=lambda x: x["job_count"], reverse=True)
    red_flag_companies = red_flag_companies[:20]

    return {
        "summary": summary,
        "by_industry": by_industry,
        "red_flag_companies": red_flag_companies,
    }


def observatory_skill_leaderboard(conn: sqlite3.Connection, top_n: int = 40) -> dict:
    """技能热度榜。

    展开 jobs.skills JSON → 每技能: demand_count、company_count、avg_salary、
    salary_premium%(相对市场均价)、top_industries。
    skills 用 Python 侧展开, 小写归一聚合, 显示取最高频原形。
    空样本时 items=[]。
    """
    rows = conn.execute(
        f"SELECT skills, company_id, salary_mid, industry FROM jobs"
        f" WHERE skills IS NOT NULL AND skills != '[]' AND {_VISIBLE}"
    ).fetchall()
    if not rows:
        return {"market_avg_salary": None, "items": []}

    # 市场均价(有薪资岗位)
    sal_rows = conn.execute(
        f"SELECT salary_mid FROM jobs WHERE salary_mid IS NOT NULL AND salary_mid > 0 AND {_VISIBLE}"
    ).fetchall()
    market_avg = round(sum(r["salary_mid"] for r in sal_rows) / len(sal_rows), 2) if sal_rows else None

    # skill_lower -> 累计数据; 原形 Counter 取最高频显示
    skill_data: dict[str, dict] = defaultdict(
        lambda: {
            "demand": 0, "companies": set(), "salaries": [],
            "industries": Counter(), "originals": Counter(),
        }
    )
    for r in rows:
        try:
            skills = json.loads(r["skills"]) or []
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(skills, list):
            continue
        cid = r["company_id"] or ""
        ind = _norm(r["industry"])
        for s in skills:
            if not isinstance(s, str):
                continue
            s = s.strip()
            if not s:
                continue
            key = s.lower()
            d = skill_data[key]
            d["demand"] += 1
            if cid:
                d["companies"].add(cid)
            if r["salary_mid"] and r["salary_mid"] > 0:
                d["salaries"].append(r["salary_mid"])
            d["industries"][ind] += 1
            d["originals"][s] += 1

    items = []
    for key, d in skill_data.items():
        avg_sal = round(sum(d["salaries"]) / len(d["salaries"]), 2) if d["salaries"] else None
        premium = None
        if avg_sal is not None and market_avg:
            premium = round((avg_sal - market_avg) / market_avg, 4)
        items.append({
            "skill": d["originals"].most_common(1)[0][0] if d["originals"] else key,
            "demand_count": d["demand"],
            "company_count": len(d["companies"]),
            "avg_salary": avg_sal,
            "salary_premium": premium,
            "top_industries": [
                {"name": n, "count": c} for n, c in d["industries"].most_common(3)
            ],
        })
    items.sort(key=lambda x: x["demand_count"], reverse=True)
    items = items[:top_n]
    return {"market_avg_salary": market_avg, "items": items}
