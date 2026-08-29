"""报告数据打包层 —— 面向报告生成器的单一数据契约 (schema v1.0)。

设计要点:
- 一次调用返回报告所需的全部聚合数据, 生成器无需自行拼装多个观察台接口。
- 输出只含聚合统计与口径元数据, 不含任何可还原单条岗位记录的字段
  (job_id / url / address / gps / 明文公司名 / brand_id), 由单元测试强制约束。
- 聚合逻辑复用 store.observatory, 本模块只负责: 口径元数据 + 质量基线 +
  代表公司脱敏 + 组装。
- schema 版本规则: 删除字段或变更语义 → 升 major; 只增字段 → 升 minor。
  生成器按 schema_version 决定能否消费。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .. import config as cfg
from . import observatory

SCHEMA_VERSION = "2.0"

#: 输出契约中禁止出现的字段名 (防止未来改动引入单条记录泄漏)
FORBIDDEN_KEYS = frozenset({
    "job_id", "url", "address", "gps", "lat", "lng", "brand_id",
    "boss", "first_seen", "last_seen", "indexed_at",
})

_ALIAS_POOL = "ABCDEFGH"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _visible_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT company_id, city, district, industry, salary_mid, first_seen,"
        f" last_seen FROM jobs WHERE {observatory._VISIBLE}"
    ).fetchall()


# ---------------------------------------------------------------- 口径元数据


def _load_crawl_sessions(limit: int = 12) -> list[dict]:
    """读取 crawl_state.json 中最近的采集会话, 作为报告的筛选口径来源。"""
    state_file: Path = cfg.DATA_ROOT / "crawl_state.json"
    try:
        runs = json.loads(state_file.read_text(encoding="utf-8")).get("runs", {})
    except Exception:
        return []
    items = []
    for run in runs.values():
        items.append({
            "at": run.get("at"),
            "list_url": run.get("url", ""),
            "coverage": run.get("coverage"),
            "jobs_scraped": run.get("jobs_scraped"),
            "pages": run.get("pages"),
        })
    items.sort(key=lambda x: x.get("at") or "", reverse=True)
    return items[:limit]


def _crawl_meta(conn: sqlite3.Connection) -> dict:
    rows = _visible_rows(conn)
    job_count = len(rows)
    company_count = len({r["company_id"] for r in rows if r["company_id"]})

    first_seen = sorted(r["first_seen"] or "" for r in rows if r["first_seen"])
    last_seen = sorted(r["last_seen"] or "" for r in rows if r["last_seen"])
    window = {
        "first_seen_min": first_seen[0] if first_seen else None,
        "first_seen_max": first_seen[-1] if first_seen else None,
        "last_seen_min": last_seen[0] if last_seen else None,
        "last_seen_max": last_seen[-1] if last_seen else None,
    }

    city_counter: Counter = Counter(
        observatory._norm(r["city"]) for r in rows
    )
    cities = [
        {"city": name, "job_count": cnt}
        for name, cnt in city_counter.most_common(10)
    ]
    city_labeled = sum(cnt for name, cnt in city_counter.items() if name != observatory._UNKNOWN)

    industry_labeled = sum(1 for r in rows if (r["industry"] or "").strip())
    salary_labeled = sum(1 for r in rows if r["salary_mid"])

    meta = {
        "job_count": job_count,
        "company_count": company_count,
        "window": window,
        "cities": cities,
        "coverage": {
            "city_labeled_ratio": round(city_labeled / job_count, 4) if job_count else None,
            "industry_labeled_ratio": round(industry_labeled / job_count, 4) if job_count else None,
            "salary_labeled_ratio": round(salary_labeled / job_count, 4) if job_count else None,
        },
        "crawl_sessions": _load_crawl_sessions(),
    }
    return meta


def _fingerprint(meta: dict) -> str:
    """数据版本指纹: 同一批数据两次打包指纹一致 (不含生成时间)。"""
    w = meta["window"]
    raw = "|".join([
        str(meta["job_count"]),
        str(meta["company_count"]),
        str(w["first_seen_min"]),
        str(w["last_seen_max"]),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 质量基线


def _quality(conn: sqlite3.Connection, market: dict, focus_detail: dict | None) -> dict:
    """质量基线: 覆盖率 + 样本不足降级清单, 生成器据此渲染降级标注或拒绝输出。"""
    rows = _visible_rows(conn)
    job_count = len(rows)

    pricing = market["salary_pricing"]
    industry_list = market["industry_list"]

    degradations: list[dict] = []
    if pricing.get("overall") is None:
        degradations.append({
            "scope": "全市场薪资分位",
            "reason": f"薪资样本不足 (阈值: ≥{observatory._MIN_SALARY})",
            "sample_size": 0,
        })
    for item in industry_list.get("items", []):
        if item.get("salary_median") is None:
            degradations.append({
                "scope": f"行业「{item['name']}」薪资中位",
                "reason": f"薪资样本不足 (阈值: ≥{observatory._MIN_SALARY})",
                "sample_size": item.get("salary_count", 0),
            })
    if focus_detail is not None and focus_detail.get("signals") is None:
        degradations.append({
            "scope": f"行业「{focus_detail.get('name')}」红旗信号",
            "reason": f"样本不足 (阈值: ≥{observatory._MIN_SIGNAL})",
            "sample_size": focus_detail.get("job_count", 0),
        })

    return {
        "thresholds": {
            "min_salary_samples": observatory._MIN_SALARY,
            "min_signal_samples": observatory._MIN_SIGNAL,
        },
        "salary_coverage": (
            round(sum(1 for r in rows if r["salary_mid"]) / job_count, 4)
            if job_count else None
        ),
        "degradations": degradations,
    }


# ---------------------------------------------------------------- 脱敏


class _AliasRegistry:
    """同一次打包内的公司脱敏别名表: 同一公司在所有区块拿到同一别名。

    用法: 先用 build() 收集所有公司出现点 (可多次调用, 重复 key 取最大 job_count),
    再 finalize() 按全局 job_count 降序统一发号 (公司A/B/C..., 超出字母池用数字)。
    """

    def __init__(self) -> None:
        self._cand: dict[str, int] = {}
        self._map: dict[str, str] = {}

    def build(self, companies: list[dict]) -> None:
        for c in companies:
            k = c.get("_key")
            if not k:
                continue
            if k not in self._cand or (c.get("job_count") or 0) > self._cand[k]:
                self._cand[k] = c.get("job_count") or 0

    def finalize(self) -> None:
        ordered = sorted(self._cand, key=lambda k: (-self._cand[k], k))
        for i, k in enumerate(ordered):
            self._map[k] = f"公司{_ALIAS_POOL[i]}" if i < len(_ALIAS_POOL) else f"公司{i + 1}"

    def alias(self, key: str | None) -> str:
        if key is None:
            return "公司—"
        return self._map.get(key, "公司—")


def _company_key(item: dict) -> str | None:
    return item.get("brand_id") or item.get("name") or None


def _focus_company_entries(top_companies: list[dict]) -> list[dict]:
    """焦点行业代表公司: 真实公司名 + 在招数 + 均薪, 去除 brand_id/个人打分。"""
    return [
        {"company": c.get("name") or "未知公司",
         "job_count": c.get("job_count"),
         "avg_salary": c.get("avg_salary")}
        for c in top_companies
    ]


def _red_flag_entries(red_flag_companies: list[dict]) -> list[dict]:
    """红旗公司榜: 真实公司名 + 聚合信号计数与标签, 去除 brand_id。"""
    return [
        {"company": c.get("name") or "未知公司",
         "job_count": c.get("job_count"),
         "heavy_overtime": c.get("heavy_overtime"),
         "outsourcing": c.get("outsourcing"),
         "travel": c.get("travel"),
         "flags": c.get("flags", []),
         "avg_salary": c.get("avg_salary")}
        for c in red_flag_companies
    ]


# ---------------------------------------------------------------- 雇主画像


def _employer_block(conn: sqlite3.Connection) -> dict:
    """雇主侧聚合 (schema 1.1 新增): 月薪构成/谈薪带宽/工时/规模/性质/福利。

    全部为聚合统计, 不含单条记录; 数据来自 jobs × companies 左连接。
    """
    rows = conn.execute(
        f"SELECT j.company_id, j.salary_mid, j.salary_min, j.salary_max, j.salary_months,"
        f" j.salary_negotiable, j.work_mode, j.team_size, j.tech_depth, j.online,"
        f" j.welfare, c.scale_min, c.scale_max, c.nature, c.hours_per_day"
        f" FROM jobs j LEFT JOIN companies c ON c.brand_id = j.company_id"
        f" WHERE {observatory._VISIBLE}"
    ).fetchall()
    total = len(rows)

    # 面议占比 / 在招活跃度
    negotiable_ratio = (
        round(sum(1 for r in rows if r["salary_negotiable"]) / total, 4) if total else None
    )
    online_ratio = (
        round(sum(1 for r in rows if r["online"]) / total, 4) if total else None
    )

    # 工作模式分布 (onsite/hybrid/remote)
    _WM_LABEL = {"onsite": "现场办公", "hybrid": "混合办公", "remote": "远程"}
    wm_counter: Counter = Counter(
        r["work_mode"] or "unknown" for r in rows
    )
    work_mode_dist = [
        {"mode": m, "label": _WM_LABEL.get(m, "未标注"), "count": c,
         "ratio": round(c / total, 4) if total else None}
        for m, c in wm_counter.most_common()
    ]

    # 团队规模信号 (稀疏: 仅 JD 明确提及的岗位)
    team_known = [r["team_size"] for r in rows if r["team_size"] is not None]
    team_size_dist = {
        "known_count": len(team_known),
        "known_ratio": round(len(team_known) / total, 4) if total else None,
        "buckets": [
            {"bucket": b, "count": c}
            for b, c in sorted(_bucket_counts(
                team_known,
                [("<10人", lambda v: v < 10), ("10-50人", lambda v: 10 <= v < 50),
                 ("50-100人", lambda v: 50 <= v < 100), ("100人+", lambda v: v >= 100)],
            ).items(), key=lambda x: x[1], reverse=True)
        ],
    }

    # 技术深度信号 (0=JD 未体现, 数值越高要求越深)
    tech_vals = [r["tech_depth"] for r in rows if r["tech_depth"] is not None]
    tech_counter: Counter = Counter(tech_vals)
    tech_depth_dist = {
        "known_ratio": round(len(tech_vals) / total, 4) if total else None,
        "buckets": [
            {"bucket": b, "count": tech_counter.get(k, 0)}
            for k, b in ((0, "未体现"), (1, "L1 基础"), (2, "L2"), (3, "L3 熟练"),
                         (4, "L4"), (5, "L5 专家"), (6, "L6"))
        ],
    }

    # 薪资月数构成 (12/13/14/15 薪…): 影响真实年包, 谈薪必看
    months_counter: Counter = Counter(
        r["salary_months"] or 12 for r in rows if r["salary_mid"]
    )
    salaried = sum(months_counter.values()) or 1
    months_mix = [
        {"months": m, "count": c, "ratio": round(c / salaried, 4)}
        for m, c in sorted(months_counter.items())
    ]

    # JD 标注带宽: (max-min) 的中位绝对值与相对中值的比率, 反映标注口径下的可谈空间
    spreads_abs, spreads_ratio = [], []
    for r in rows:
        if r["salary_min"] and r["salary_max"] and r["salary_mid"]:
            spreads_abs.append(r["salary_max"] - r["salary_min"])
            spreads_ratio.append((r["salary_max"] - r["salary_min"]) / r["salary_mid"])
    salary_spread = {
        "median_abs_wan": observatory._median(spreads_abs),
        "median_ratio": observatory._median(spreads_ratio),
        "count": len(spreads_abs),
    }

    # 公示工时分布 (公司页公示的每日工时)
    def _hours_bucket(h):
        if h is None:
            return "未知"
        if h <= 8:
            return "≤8h"
        if h <= 9:
            return "8-9h"
        if h <= 10:
            return "9-10h"
        return "10h+"

    hours_counter: Counter = Counter(
        _hours_bucket(r["hours_per_day"]) for r in rows
    )
    order = ["≤8h", "8-9h", "9-10h", "10h+", "未知"]
    hours_dist = [
        {"bucket": b, "count": hours_counter.get(b, 0),
         "ratio": round(hours_counter.get(b, 0) / total, 4) if total else None}
        for b in order
    ]

    # 公司规模段 × 岗位数/公司数/薪资中位
    def _scale_bucket(r):
        s = r["scale_max"] or r["scale_min"]
        if s is None:
            return "未知规模"
        if s < 50:
            return "50人以下"
        if s < 150:
            return "50-150人"
        if s < 500:
            return "150-500人"
        if s < 1000:
            return "500-1000人"
        return "1000人以上"

    scale_map: dict = defaultdict(lambda: {"jobs": 0, "companies": set(), "salaries": []})
    for r in rows:
        b = _scale_bucket(r)
        scale_map[b]["jobs"] += 1
        if r["company_id"]:
            scale_map[b]["companies"].add(r["company_id"])
        if r["salary_mid"]:
            scale_map[b]["salaries"].append(r["salary_mid"])
    scale_order = ["50人以下", "50-150人", "150-500人", "500-1000人", "1000人以上", "未知规模"]
    scale_dist = [
        {"bucket": b,
         "job_count": scale_map[b]["jobs"],
         "company_count": len(scale_map[b]["companies"]),
         "salary_median": (
             observatory._median(scale_map[b]["salaries"])
             if len(scale_map[b]["salaries"]) >= observatory._MIN_SALARY else None
         )}
        for b in scale_order if b in scale_map
    ]

    # 公司性质分布
    nature_counter: Counter = Counter(
        observatory._norm(r["nature"]) for r in rows
    )
    nature_dist = [
        {"nature": n, "count": c, "ratio": round(c / total, 4) if total else None}
        for n, c in nature_counter.most_common(6)
    ]

    # 福利 Top10 (JD 福利标签词频)
    welfare_counter: Counter = Counter()
    for r in rows:
        try:
            for w in json.loads(r["welfare"] or "[]"):
                if w:
                    welfare_counter[str(w)] += 1
        except Exception:
            pass
    top_welfare = [
        {"welfare": w, "count": c, "ratio": round(c / total, 4) if total else None}
        for w, c in welfare_counter.most_common(10)
    ]

    return {
        "sample_count": total,
        "negotiable_ratio": negotiable_ratio,
        "online_ratio": online_ratio,
        "work_mode_dist": work_mode_dist,
        "team_size_dist": team_size_dist,
        "tech_depth_dist": tech_depth_dist,
        "months_mix": months_mix,
        "salary_spread": salary_spread,
        "hours_dist": hours_dist,
        "scale_dist": scale_dist,
        "nature_dist": nature_dist,
        "top_welfare": top_welfare,
    }


def _bucket_counts(values: list, rules: list[tuple[str, object]]) -> dict:
    out: dict = {}
    for v in values:
        for label, pred in rules:
            if pred(v):
                out[label] = out.get(label, 0) + 1
                break
    return out


def _geo_block(conn: sqlite3.Connection) -> dict:
    """区域热力 (脱敏): 观察台 geo 网格去掉 top_company 明文字段。"""
    geo = observatory.observatory_geo_heatmap(conn, cell_size=0.05)
    cells = [
        {"grid_lat": c.get("lat"), "grid_lng": c.get("lng"),
         "count": c.get("count"), "company_count": c.get("company_count"),
         "avg_salary": c.get("avg_salary"), "top_district": c.get("top_district"),
         "top_industry": c.get("top_industry")}
        for c in geo.get("cells", [])
    ]
    return {
        "cell_size": geo.get("cell_size", 0.05),
        "cells": cells,
        "gps_total": geo.get("total"),
    }


def _quadrant_block(conn: sqlite3.Connection, market_median) -> dict:
    """公司象限分布 (脱敏聚合): 在招活跃度 × 公司均薪, 只出象限统计不出公司。"""
    rows = conn.execute(
        f"SELECT company_id, salary_mid FROM jobs WHERE {observatory._VISIBLE}"
    ).fetchall()
    comp: dict = defaultdict(lambda: {"jobs": 0, "salaries": []})
    for r in rows:
        if not r["company_id"]:
            continue
        d = comp[r["company_id"]]
        d["jobs"] += 1
        if r["salary_mid"]:
            d["salaries"].append(r["salary_mid"])
    if not comp:
        return {"company_count": 0, "quadrants": []}

    comp_stats = [
        {"job_count": d["jobs"],
         "avg_salary": observatory._median(d["salaries"]) if d["salaries"] else None}
        for d in comp.values()
    ]
    with_salary = [c for c in comp_stats if c["avg_salary"] is not None]
    salary_line = observatory._median([c["avg_salary"] for c in with_salary]) if with_salary else None
    count_line = observatory._median([c["job_count"] for c in comp_stats])

    def _quad(c):
        hi_count = c["job_count"] >= (count_line or 0)
        hi_salary = c["avg_salary"] is not None and salary_line is not None and c["avg_salary"] >= salary_line
        if hi_count and hi_salary:
            return "q1"
        if hi_count:
            return "q2"
        if hi_salary:
            return "q3"
        return "q4"

    _LABEL = {"q1": "活跃且高薪", "q2": "活跃但平价", "q3": "少而精", "q4": "低活跃平价"}
    quads: dict = defaultdict(lambda: {"companies": 0, "jobs": 0, "salaries": []})
    for c in comp_stats:
        q = quads[_quad(c)]
        q["companies"] += 1
        q["jobs"] += c["job_count"]
        if c["avg_salary"] is not None:
            q["salaries"].append(c["avg_salary"])
    quadrant_list = [
        {"key": k, "label": _LABEL[k],
         "company_count": quads[k]["companies"],
         "job_count": quads[k]["jobs"],
         "avg_salary": observatory._median(quads[k]["salaries"]) if quads[k]["salaries"] else None}
        for k in ("q1", "q2", "q3", "q4")
    ]
    return {
        "company_count": len(comp_stats),
        "salary_line": salary_line,
        "count_line": count_line,
        "market_median": market_median,
        "quadrants": quadrant_list,
    }


# ---------------------------------------------------------------- 组装


def _company_board_rows(conn: sqlite3.Connection, top_n: int = 10):
    """公司双榜原始行 (未脱敏, 仅供注册表与内部脱敏函数消费): 招聘力度榜 + 薪资榜。"""
    rows = conn.execute(
        f"SELECT j.company_id, j.company_name, j.salary_mid, c.industry"
        f" FROM jobs j LEFT JOIN companies c ON c.brand_id = j.company_id"
        f" WHERE {observatory._VISIBLE}"
    ).fetchall()
    comp: dict = defaultdict(lambda: {"jobs": 0, "salaries": [], "industry": "", "name": ""})
    for r in rows:
        if not r["company_id"]:
            continue
        d = comp[r["company_id"]]
        d["jobs"] += 1
        d["industry"] = r["industry"] or d["industry"]
        d["name"] = r["company_name"] or d["name"]
        if r["salary_mid"]:
            d["salaries"].append(r["salary_mid"])
    out = [
        {"_key": cid, "company_name": d["name"], "industry": d["industry"],
         "job_count": d["jobs"], "salary_samples": len(d["salaries"]),
         "avg_salary": observatory._median(d["salaries"]) if d["salaries"] else None}
        for cid, d in comp.items()
    ]
    hiring = sorted(out, key=lambda c: (-c["job_count"], c["_key"]))[:top_n]
    salary_board = sorted(
        (c for c in out if c["avg_salary"] is not None and c["salary_samples"] >= 2),
        key=lambda c: (-c["avg_salary"], c["_key"]),
    )[:top_n]
    return hiring, salary_board


def _board_entries(entries, registry, *, anonymize: bool) -> list:
    """双榜条目: 完整版用真实公司名 (company), lite 版用脱敏别名 (alias)。

    两者均不含 brand_id / 岗位链接 / 联系方式等单条记录字段。
    """
    out = []
    for c in entries:
        row = {"industry": c.get("industry") or observatory._UNKNOWN,
               "job_count": c["job_count"], "avg_salary": c["avg_salary"]}
        if anonymize:
            row["alias"] = registry.alias(c["_key"])
        else:
            row["company"] = c.get("company_name") or "未知公司"
        out.append(row)
    return out


def build_report_bundle(conn: sqlite3.Connection, top_industries: int = 8) -> dict:
    """打包报告数据契约。

    top_industries: 行业对比表取前 N 个 (按岗位数降序, 与观察台口径一致)。
    """
    with_pricing = observatory.observatory_salary_pricing(conn)
    industry_list_full = observatory.observatory_industry_list(conn)

    # 行业对比表截取 top N, 但保留汇总字段
    items = industry_list_full.get("items", [])
    industry_list = {
        "items": items[:top_industries],
        "total_industries": len(items),
        "market_median": industry_list_full.get("market_median"),
        "unknown_job_count": industry_list_full.get("unknown_job_count"),
    }

    market = {
        "salary_pricing": with_pricing,
        "industry_list": industry_list,
        "signal_radar": observatory.observatory_signal_radar(conn),
        "skill_leaderboard": observatory.observatory_skill_leaderboard(conn, top_n=15),
        "employer_profile": _employer_block(conn),
        "geo": _geo_block(conn),
        "quadrant_dist": _quadrant_block(conn, industry_list.get("market_median")),
    }

    hiring_rows, salary_rows = _company_board_rows(conn)

    focus_name = items[0]["name"] if items else None
    focus_detail = (
        observatory.observatory_industry_detail(conn, focus_name)
        if focus_name else None
    )

    # 脱敏: 先汇总所有公司出现点建别名表, 同一公司全程同一别名
    registry = _AliasRegistry()
    registry.build([dict(c) for c in hiring_rows])
    registry.build([dict(c) for c in salary_rows])
    registry.finalize()
    market["signal_radar"]["red_flag_companies"] = _red_flag_entries(
        market["signal_radar"].get("red_flag_companies", [])
    )
    if focus_detail is not None:
        focus_detail["top_companies"] = _focus_company_entries(
            focus_detail.get("top_companies", [])
        )
    market["company_boards"] = {
        "hiring": _board_entries(hiring_rows, registry, anonymize=False),
        "salary": _board_entries(salary_rows, registry, anonymize=False),
        "hiring_lite": _board_entries(hiring_rows, registry, anonymize=True),
        "salary_lite": _board_entries(salary_rows, registry, anonymize=True),
    }

    meta = _crawl_meta(conn)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "data_fingerprint": _fingerprint(meta),
        "meta": meta,
        "quality": _quality(conn, market, focus_detail),
        "market": market,
        "focus": {
            "industry": focus_name,
            "detail": focus_detail,
        },
    }
