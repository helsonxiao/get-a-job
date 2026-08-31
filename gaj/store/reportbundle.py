"""报告数据打包层 —— 面向报告生成器的单一数据契约 (schema v2.2)。

设计要点:
- 一次调用返回报告所需的全部聚合数据, 生成器无需自行拼装多个观察台接口。
- 输出只含聚合统计与口径元数据, 不含任何可还原单条岗位记录的字段
  (job_id / url / address / gps / brand_id), 由单元测试强制约束。
- 聚合逻辑复用 store.observatory, 本模块只负责: 口径元数据 + 质量基线 +
  代表公司脱敏 + 组装; 报告专有口径 (中位数统一) 在本层重算。
- schema 版本规则: 删除字段或变更语义 → 升 major; 只增字段 → 升 minor。
  生成器按 schema_version 决定能否消费。

v2.1 (2026-08-30, 读者价值审计):
- market.functions: 岗位职能分桶聚合 (标题关键词规则, 规则随包公示)。
- market.career_entry: 应届生入门口径 (JD 未标注经验门槛的岗位聚合)。
- market.local_pricing: 主导城市本地口径 vs 全样本对照。
- company_boards.hiring/salary 条目新增 city (公司已标注城市众数, 可为 null);
  lite 版不带 city (防反推)。
- skill_leaderboard 改为报告口径: 技能标签归一化 + 薪资中位数,
  溢价基准 = 全市场中位 (与行业溢价口径一致)。
- red_flag_companies / focus.top_companies / focus.top_districts 的 avg_salary
  键名不变, 语义统一为中位数 (报告全文「薪资中位」标签对应同一口径)。

v2.2 (2026-08-30, 问题导向重构):
- 榜单候选池扩容: 行业 12 / 公司双榜 30 / 技能榜 30 (build_report_bundle 可传参);
  生成器按样本量自适应取 Top N, 契约本身只承诺「足够大的候选池」。

v2.3a (2026-08-31, 报告默认含已忽略岗位):
- build_report_bundle 新增 include_ignored 参数 (默认 True):
  市场报告体现全市场, 个人「忽略」只影响个人工作台不影响统计;
  关闭时与旧口径一致 (仅可见岗位)。meta.scope.ignored_included 显式报数。

v3.0 (2026-08-31, 报告↔页面同源重构):
- 技能榜唯一实现移至 observatory.observatory_skill_leaderboard (归一化 +
  中位数 + 市场中位溢价); 公司双榜唯一实现移至
  observatory.observatory_company_boards (中位数 + 岗位标注城市)。
- **company_boards 删除 hiring_lite/salary_lite 字段** —— 脱敏代号化移交
  reporter 渲染层独立完成, gaj 只出真名聚合。破坏性变更, 升 major。
- web 观察台技能榜「均薪」列同步改「薪资中位」。

v2.3 (2026-08-30, 来源口径隔离):
- build_report_bundle 新增 scope_link 参数: 指定后全部聚合只统计
  jobs.source_link = scope_link 的岗位 (temp 表影子实现, 聚合代码零改动)。
- meta 新增 scope 块: {scope_link, scope_label, job_count, total_job_count,
  unscoped_job_count} —— 未分口径 (无 source_link) 的历史数据在此显式报数,
  指定口径后绝不混入其它口径。
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

SCHEMA_VERSION = "3.0"

#: 榜单池默认容量 (v2.2): bundle 输出足够大的候选池, 由生成器按样本量自适应切片。
#: 只增不改, 旧生成器兼容 (多出来的行会被旧生成器全量渲染或自行截断)。
DEFAULT_BOARD_SIZE = 30
DEFAULT_SKILL_SIZE = 30
DEFAULT_INDUSTRY_SIZE = 12

#: 输出契约中禁止出现的字段名 (防止未来改动引入单条记录泄漏)
FORBIDDEN_KEYS = frozenset({
    "job_id", "url", "address", "gps", "lat", "lng", "brand_id",
    "boss", "first_seen", "last_seen", "indexed_at",
})

_ALIAS_POOL = "ABCDEFGH"


#: 信号判定规则说明 (与 core/signals.py 的规则一一对应, 随报告输出保证可复现)
SIGNAL_DEFINITIONS = {
    "heavy_overtime": "规则判定：JD 明确提及 996/大小周/单休等作息 → 判为长工时；或公司公示工时 ≥10 小时/天；或供餐/宿舍/班车/房补等 3 项以上长工时伴随福利叠加",
    "moderate_overtime": "规则判定：JD 有 1-2 项长工时伴随福利，或未证实双休",
    "light_overtime": "规则判定：JD 明确双休/弹性作息，或公示工时 ≤8.5 小时/天",
    "outsourcing": "规则判定：JD 含外包/驻场等表述，或公司名/所属行业含「外包、人力、劳务、派遣」字样",
    "travel": "规则判定：JD 明确「长期出差/常驻项目地」计为频繁档，仅提及「出差」计为偶尔档，未提及计为无",
    "tech_depth": "规则判定：统计 JD 命中技术深度关键词（架构设计、性能优化、技术选型、高并发、分布式、源码等 14 个）的个数，0 = JD 未体现",
}

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
    """数据版本指纹: 同一批数据两次打包指纹一致 (不含生成时间)。

    指定口径时掺入口径链接哈希 —— 不同口径的指纹必然不同。
    """
    w = meta["window"]
    raw = "|".join([
        str(meta["job_count"]),
        str(meta["company_count"]),
        str(w["first_seen_min"]),
        str(w["last_seen_max"]),
    ])
    scope = (meta.get("scope") or {}).get("scope_link") or ""
    if scope:
        raw += "|scope:" + scope
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


def _company_key(item: dict) -> str | None:
    return item.get("brand_id") or item.get("name") or None


def _red_flag_entries(red_flag_companies: list[dict], hours_map: dict,
                      company_median_map: dict | None = None) -> list[dict]:
    """信号公司榜: 真实公司名 + 公示工时(客观列) + 聚合信号计数, 去除 brand_id。

    avg_salary 语义为报告口径中位数: 传入 company_median_map 时按公司覆写。
    """
    out = []
    for c in red_flag_companies:
        med = None
        if company_median_map is not None:
            key = c.get("brand_id")
            med = company_median_map.get(key) if key else None
        elif c.get("avg_salary") is not None:
            med = c.get("avg_salary")
        out.append({"company": c.get("name") or "未知公司",
                    "job_count": c.get("job_count"),
                    "hours_per_day": hours_map.get(_company_key(c)),
                    "heavy_overtime": c.get("heavy_overtime"),
                    "outsourcing": c.get("outsourcing"),
                    "travel": c.get("travel"),
                    "flags": c.get("flags", []),
                    "avg_salary": med})
    return out


def _focus_company_entries(top_companies: list[dict], comp_ind_median: dict,
                           industry: str) -> list[dict]:
    """焦点行业代表公司: 真实公司名 + 在招数 + 行业内薪资中位。

    avg_salary 键名沿用契约, 语义 = 该公司在本行业内的薪资中位数
    (comp_ind_median 查不到时保留观察台原值, 保证旧行为可用)。
    """
    return [
        {"company": c.get("name") or "未知公司",
         "job_count": c.get("job_count"),
         "avg_salary": comp_ind_median.get(
             (industry, c.get("brand_id")), c.get("avg_salary")
         ) if c.get("brand_id") else c.get("avg_salary")}
        for c in top_companies
    ]


def _focus_district_entries(top_districts: list[dict], dist_ind_median: dict,
                            industry: str) -> list[dict]:
    """焦点行业区域分布: 区县岗位/公司数 + 行业×区县薪资中位。"""
    return [
        {"district": d.get("district"),
         "job_count": d.get("job_count"),
         "company_count": d.get("company_count"),
         "avg_salary": dist_ind_median.get(
             (industry, d.get("district")), d.get("avg_salary")
         )}
        for d in top_districts
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
    hours_counter: Counter = Counter(
        observatory.hours_bucket(r["hours_per_day"]) for r in rows
    )
    hours_dist = [
        {"bucket": b, "count": hours_counter.get(b, 0),
         "ratio": round(hours_counter.get(b, 0) / total, 4) if total else None}
        for b in observatory.HOURS_ORDER
    ]

    # 公司规模段 × 岗位数/公司数/薪资中位
    scale_map: dict = defaultdict(lambda: {"jobs": 0, "companies": set(), "salaries": []})
    for r in rows:
        b = observatory.scale_bucket(r["scale_max"], r["scale_min"])
        scale_map[b]["jobs"] += 1
        if r["company_id"]:
            scale_map[b]["companies"].add(r["company_id"])
        if r["salary_mid"]:
            scale_map[b]["salaries"].append(r["salary_mid"])
    scale_dist = [
        {"bucket": b,
         "job_count": scale_map[b]["jobs"],
         "company_count": len(scale_map[b]["companies"]),
         "salary_median": (
             observatory._median(scale_map[b]["salaries"])
             if len(scale_map[b]["salaries"]) >= observatory._MIN_SALARY else None
         )}
        for b in observatory.SCALE_ORDER if b in scale_map
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


# ---------------------------------------------------------------- 报告口径重算 (v2.1)


def _salary_median_maps(conn: sqlite3.Connection):
    """报告口径统一: 公司/区县级薪资一律取中位数。

    观察台聚合的 avg_salary 为均值 (供 gaj web UI 使用, 语义不变);
    报告以中位数为统一口径, 此处按下列三种口径重算:
    - by_company: brand_id -> 公司全部在招岗位薪资中位 (信号公司榜口径);
    - by_comp_ind: (industry, brand_id) -> 行业内公司薪资中位 (焦点代表公司口径);
    - by_dist_ind: (industry, district) -> 行业×区县薪资中位 (焦点区域分布口径)。
    """
    comp: dict = defaultdict(list)
    comp_ind: dict = defaultdict(list)
    dist_ind: dict = defaultdict(list)
    for r in conn.execute(
        f"SELECT company_id, industry, district, salary_mid FROM jobs WHERE {observatory._VISIBLE}"
    ).fetchall():
        if not r["salary_mid"]:
            continue
        if r["company_id"]:
            comp[r["company_id"]].append(r["salary_mid"])
            if (r["industry"] or "").strip():
                comp_ind[(r["industry"].strip(), r["company_id"])].append(r["salary_mid"])
        dist = (r["district"] or "").strip()
        ind = (r["industry"] or "").strip()
        if dist and ind:
            dist_ind[(ind, dist)].append(r["salary_mid"])
    return (
        {k: observatory._median(v) for k, v in comp.items()},
        {k: observatory._median(v) for k, v in comp_ind.items()},
        {k: observatory._median(v) for k, v in dist_ind.items()},
    )


# ---------------------------------------------------------------- 职能分桶 (v2.1)

#: 岗位职能分桶规则 (按序匹配, 先专后泛; 命中即归桶, 不再下探)。
#: 规则随包输出 (FUNCTION_RULES 供报告直接引用), 保证可复现。
FUNCTION_RULES = [
    ("算法/AI", ["算法", "ai", "机器学习", "深度学习", "pytorch", "机器人", "视觉", "nlp",
                "大模型", "图像", "多模态", "agi", "llm"]),
    ("前端/客户端", ["前端", "javascript", "vue", "react", "小程序", "h5", "android", "ios",
                   "客户端", "flutter", "wpf"]),
    ("测试", ["测试", "qa", "ate "]),
    ("数据", ["数据分析", "数据开发", "大数据", "etl", "数据仓库", "数据库", "数据平台",
              "数据治理", "bi "]),
    ("运维/IT 支持", ["运维", "网络工程师", "技术支持", "桌面运维", "虚拟化", "系统管理", "it ",
                    "it支持", "信息系统"]),
    ("产品/项目/实施", ["产品经理", "项目经理", "方案", "实施", "交付", "售前", "需求分析",
                      "项目工程师", "fae", "现场应用"]),
    ("嵌入式/硬件/机械", ["嵌入式", "单片机", "mcu", "硬件", "fpga", "电路", "上位机", "电气",
                        "固件", "bms", "机械", "结构", "暖通", "变频器", "硬件驱动", "液压",
                        "仿真", "cae", "cad", "流体", "电机", "电池", "储能系统", "自动化设备"]),
    ("后端/软件开发", ["后端", "服务端", "java", "golang", "php", ".net", "c#", "软件开发",
                      "软件工程师", "开发工程师", "程序", "全栈", "python", "c++", "c/c++",
                      "软件", "开发", "工程师"]),
]


def _classify_function(title: str | None) -> str:
    t = (title or "").lower()
    for name, kws in FUNCTION_RULES:
        for kw in kws:
            if kw in t:
                return name
    return "其他/未分类"


def _functions_block(conn: sqlite3.Connection) -> dict:
    """职能分桶聚合 (v2.1): 读者心智单位是职能而非行业。

    每桶: 岗位数 / 薪资 P25/P50/P75 (样本 ≥2) / 薪资样本数 /
    未标注经验门槛占比 / 技能 Top3 (归一化)。
    """
    rows = conn.execute(
        f"SELECT title, salary_mid, exp_min, company_id, skills FROM jobs WHERE {observatory._VISIBLE}"
    ).fetchall()
    buckets: dict = defaultdict(
        lambda: {"jobs": 0, "salaries": [], "exp_known": 0, "exp_zero": 0,
                 "companies": set(), "skill_counter": Counter()}
    )
    for r in rows:
        f = _classify_function(r["title"])
        d = buckets[f]
        d["jobs"] += 1
        if r["company_id"]:
            d["companies"].add(r["company_id"])
        if r["salary_mid"]:
            d["salaries"].append(r["salary_mid"])
        if r["exp_min"] is not None:
            d["exp_known"] += 1
            if r["exp_min"] == 0:
                d["exp_zero"] += 1
        for s in observatory.iter_normalized_skills(r["skills"]):
            d["skill_counter"][s] += 1

    items = []
    for name, d in buckets.items():
        sal = sorted(d["salaries"])
        items.append({
            "name": name,
            "job_count": d["jobs"],
            "company_count": len(d["companies"]),
            "salary_p25": observatory._percentile(sal, 25) if len(sal) >= observatory._MIN_SALARY else None,
            "salary_p50": observatory._median(sal) if len(sal) >= observatory._MIN_SALARY else None,
            "salary_p75": observatory._percentile(sal, 75) if len(sal) >= observatory._MIN_SALARY else None,
            "salary_count": len(sal),
            "exp_unlabeled_ratio": (
                round(d["exp_zero"] / d["exp_known"], 4) if d["exp_known"] else None
            ),
            "top_skills": [
                {"name": k, "count": v} for k, v in d["skill_counter"].most_common(3)
            ],
        })
    items.sort(key=lambda x: x["job_count"], reverse=True)
    return {
        "method": "按岗位标题关键词规则分桶, 规则按序匹配 (先专后泛), 命中即归桶; 规则清单见 bundle FUNCTION_RULES",
        "items": items,
    }


# ---------------------------------------------------------------- 应届生入门口径 (v2.1)

#: 标题高级/管理岗标记: 用于从「未标注经验门槛」中剔除可投性低的高级岗
_SENIOR_MARKERS = ("高级", "资深", "专家", "主管", "经理", "总监", "负责人", "架构师", "主任")


def _career_entry_block(conn: sqlite3.Connection) -> dict:
    """应届生入门口径 (v2.1): JD 未标注最低经验要求 (exp_min=0) 的岗位聚合。

    诚实口径: 「未标注经验门槛」≠「应届生岗位」——含真正无经验要求的岗位,
    也含仅未写明的高级岗; 另给出剔除标题含高级/管理标记后的可投子集。
    """
    rows = conn.execute(
        f"SELECT title, company_id, company_name, city, district, industry, salary_mid,"
        f" edu_level, exp_min FROM jobs WHERE exp_min = 0 AND {observatory._VISIBLE}"
    ).fetchall()
    total = len(rows)
    sal = sorted(r["salary_mid"] for r in rows if r["salary_mid"])
    non_senior = [r for r in rows
                  if not any(m in (r["title"] or "") for m in _SENIOR_MARKERS)]
    sal_ns = sorted(r["salary_mid"] for r in non_senior if r["salary_mid"])

    def _strip(stats):
        if not stats:
            return None
        return {
            "p25": observatory._percentile(stats, 25),
            "p50": observatory._median(stats),
            "p75": observatory._percentile(stats, 75),
            "count": len(stats),
        }

    edu_labels = {0: "不限", 1: "高中及以下", 2: "大专", 3: "本科", 4: "硕士", 5: "博士"}
    edu_counter: Counter = Counter(edu_labels.get(r["edu_level"], "未知") for r in rows)
    city_counter: Counter = Counter(observatory._norm(r["city"]) for r in rows)
    ind_counter: Counter = Counter(observatory._norm(r["industry"]) for r in rows)
    comp: dict = defaultdict(lambda: {"name": "", "jobs": 0, "salaries": []})
    for r in rows:
        if not r["company_id"]:
            continue
        d = comp[r["company_id"]]
        d["name"] = r["company_name"] or d["name"]
        d["jobs"] += 1
        if r["salary_mid"]:
            d["salaries"].append(r["salary_mid"])
    top_companies = [
        {"company": d["name"] or "未知公司", "job_count": d["jobs"],
         "salary_median": observatory._median(d["salaries"]) if d["salaries"] else None}
        for d in comp.values()
    ]
    top_companies.sort(key=lambda x: x["job_count"], reverse=True)
    return {
        "method": (
            "口径: JD 未标注最低经验要求的在招岗位, 含真正无经验要求的岗位与仅未写明的岗位;"
            "已按标题剔除高级/管理岗作为可投子集。未标注门槛 ≠ 应届生岗位, 请结合职能与职级词判断。"
        ),
        "unlabeled_exp_count": total,
        "salary": _strip(sal),
        "non_senior": {
            "count": len(non_senior),
            "salary": _strip(sal_ns),
        },
        "by_edu": [
            {"label": k, "count": v} for k, v in edu_counter.most_common()
        ],
        "by_city": [
            {"city": k, "count": v} for k, v in city_counter.most_common()
        ],
        "by_industry": [
            {"name": k, "count": v} for k, v in ind_counter.most_common(5)
        ],
        "top_companies": top_companies[:8],
    }


# ---------------------------------------------------------------- 本地口径对照 (v2.1)


def _local_pricing_block(conn: sqlite3.Connection, all_median) -> dict:
    """主导城市本地口径 vs 全样本 (v2.1): 标题承诺城市时读者最关心的口径。

    主导城市 = 已标注城市中岗位数最多者; 城市未标注岗位无法从现有字段
    (无 GPS/区县) 回填, 如实计入全样本并单独报数。
    """
    rows = conn.execute(
        f"SELECT city, industry, salary_mid FROM jobs WHERE {observatory._VISIBLE}"
    ).fetchall()
    city_counter: Counter = Counter(
        observatory._norm(r["city"]) for r in rows
        if observatory._norm(r["city"]) != observatory._UNKNOWN
    )
    if not city_counter:
        return None
    focus_city, _n = city_counter.most_common(1)[0]
    local_sal = sorted(
        r["salary_mid"] for r in rows
        if observatory._norm(r["city"]) == focus_city and r["salary_mid"]
    )
    ind_counter: dict = defaultdict(lambda: {"jobs": 0, "salaries": []})
    for r in rows:
        if observatory._norm(r["city"]) != focus_city:
            continue
        ind = observatory._norm(r["industry"])
        if ind == observatory._UNKNOWN:
            continue
        d = ind_counter[ind]
        d["jobs"] += 1
        if r["salary_mid"]:
            d["salaries"].append(r["salary_mid"])
    top_industries = [
        {"name": k, "job_count": v["jobs"],
         "salary_median": (
             observatory._median(v["salaries"])
             if len(v["salaries"]) >= observatory._MIN_SALARY else None)}
        for k, v in ind_counter.items()
    ]
    top_industries.sort(key=lambda x: x["job_count"], reverse=True)
    unlabeled = sum(
        1 for r in rows if observatory._norm(r["city"]) == observatory._UNKNOWN
    )
    return {
        "city": focus_city,
        "city_job_count": city_counter[focus_city],
        "salary": {
            "p25": observatory._percentile(local_sal, 25),
            "p50": observatory._median(local_sal),
            "p75": observatory._percentile(local_sal, 75),
            "count": len(local_sal),
        },
        "all_sample": {
            "job_count": len(rows),
            "salary_median": all_median,
        },
        "top_industries": top_industries[:3],
        "unlabeled_city_jobs": unlabeled,
        "unlabeled_city_ratio": round(unlabeled / len(rows), 4) if rows else None,
    }


# ---------------------------------------------------------------- 组装


def _scope_meta(conn: sqlite3.Connection, scope_link: str) -> dict:
    """口径元数据: 链接/自定义命名/各口径岗位数 (含未分口径历史数据显式报数)。

    注意: 指定口径期间 temp.jobs 影子了 jobs 表, 统计全库必须用 main.jobs 全限定名。
    """
    scoped_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE " + observatory._VISIBLE
    ).fetchone()[0]
    ignored_included = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE ignored = 1"
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM main.jobs").fetchone()[0]
    unscoped = conn.execute(
        "SELECT COUNT(*) FROM main.jobs"
        " WHERE source_link IS NULL OR source_link = ''"
    ).fetchone()[0]
    label_row = conn.execute(
        "SELECT label FROM main.source_links WHERE link = ?", (scope_link,)
    ).fetchone()
    label = (label_row[0] if label_row else "") or ""
    return {
        "scope_link": scope_link,
        "scope_label": label,
        "job_count": scoped_count,
        "total_job_count": total,
        "unscoped_job_count": unscoped,
        "ignored_included": ignored_included,
        "include_ignored": True,
    }


def build_report_bundle(conn: sqlite3.Connection, top_industries: int = DEFAULT_INDUSTRY_SIZE,
                        board_size: int = DEFAULT_BOARD_SIZE,
                        skill_size: int = DEFAULT_SKILL_SIZE,
                        scope_link: str | None = None,
                        include_ignored: bool = True) -> dict:
    """打包报告数据契约。

    top_industries: 行业对比表候选池容量 (按岗位数降序, 与观察台口径一致)。
    board_size: 公司双榜候选池容量; skill_size: 技能榜候选池容量。
    v2.2 起三者仅是「候选池」, 生成器按样本量自适应取 Top N 渲染。
    scope_link: 来源筛选链接 (口径隔离)。指定后全部聚合只统计该链接的岗位:
      实现 = temp 表影子 (temp.jobs 覆盖同名表, 聚合代码零改动),
      finally 中拆除影子; 口径注册进 source_links 表并写入 meta.scope。
    """
    scoped = bool(scope_link)
    # v2.3a: 统一走影子 —— 影子表把 ignored 列清零, 使聚合的 _VISIBLE
    # (ignored=0) 在「含已忽略」模式下放行全部行; 口径过滤同层完成。
    # 统计要在影子创建前完成 (影子内 ignored 恒 0, 统计不出真实忽略数)。
    conn.execute("DROP TABLE IF EXISTS temp.jobs")
    where, params = [], []
    if scope_link:
        where.append("source_link = ?")
        params.append(scope_link)
        conn.execute(
            "INSERT OR IGNORE INTO main.source_links (link, label, created_at)"
            " VALUES (?, '', ?)",
            (scope_link, datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")),
        )
    if not include_ignored:
        where.append("(ignored = 0 OR ignored IS NULL)")
    where_sql = (" AND " + " AND ".join(where)) if where else ""
    if include_ignored:
        q = "SELECT COUNT(*) FROM main.jobs WHERE ignored = 1"
        q_params = []
        if scope_link:
            q += " AND source_link = ?"
            q_params = [scope_link]
        ignored_in_scope = conn.execute(q, q_params).fetchone()[0]
    else:
        ignored_in_scope = 0
    conn.execute(
        "CREATE TEMP TABLE jobs AS SELECT * FROM main.jobs WHERE 1=1" + where_sql,
        params,
    )
    # 影子内全部视为「未忽略」: 让聚合的 _VISIBLE (ignored=0) 在含忽略模式下放行
    conn.execute("UPDATE temp.jobs SET ignored = 0")
    try:
        return _build_scoped(conn, top_industries, board_size, skill_size,
                             scope_link if scoped else None,
                             include_ignored=include_ignored,
                             ignored_in_scope=ignored_in_scope)
    finally:
        conn.execute("DROP TABLE IF EXISTS temp.jobs")


def _build_scoped(conn: sqlite3.Connection, top_industries: int,
                  board_size: int, skill_size: int,
                  scope_link: str | None, include_ignored: bool = True,
                  ignored_in_scope: int = 0) -> dict:
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
        "signal_radar": {**observatory.observatory_signal_radar(conn),
                         "definitions": SIGNAL_DEFINITIONS},
        "skill_leaderboard": observatory.observatory_skill_leaderboard(conn, top_n=skill_size),
        "employer_profile": _employer_block(conn),
        "geo": _geo_block(conn),
        "functions": _functions_block(conn),
        "career_entry": _career_entry_block(conn),
    }

    company_median_map, comp_ind_median, dist_ind_median = _salary_median_maps(conn)

    boards_full = observatory.observatory_company_boards(conn, top_n=board_size)

    focus_name = items[0]["name"] if items else None
    focus_detail = (
        observatory.observatory_industry_detail(conn, focus_name)
        if focus_name else None
    )

    hours_map = {
        row[0]: row[1]
        for row in conn.execute("SELECT brand_id, hours_per_day FROM companies")
    }
    # 报告口径统一: 信号公司/焦点代表公司/区域分布 avg_salary 覆写为中位数
    market["signal_radar"]["red_flag_companies"] = _red_flag_entries(
        market["signal_radar"].get("red_flag_companies", []),
        hours_map,
        company_median_map,
    )
    if focus_detail is not None:
        focus_detail["top_companies"] = _focus_company_entries(
            focus_detail.get("top_companies", []), comp_ind_median, focus_name
        )
        focus_detail["top_districts"] = _focus_district_entries(
            focus_detail.get("top_districts", []), dist_ind_median, focus_name
        )
    market["local_pricing"] = _local_pricing_block(
        conn, industry_list_full.get("market_median")
    )
    market["company_boards"] = {
        "hiring": boards_full["hiring"],
        "salary": boards_full["salary"],
    }

    meta = _crawl_meta(conn)
    if scope_link:
        meta["scope"] = _scope_meta(conn, scope_link)
        meta["scope"]["ignored_included"] = ignored_in_scope
    else:
        meta["scope"] = {
            "scope_link": None, "scope_label": "",
            "job_count": meta["job_count"],
            "total_job_count": conn.execute(
                "SELECT COUNT(*) FROM main.jobs"
            ).fetchone()[0],
            "unscoped_job_count": 0,
            "ignored_included": ignored_in_scope,
            "include_ignored": include_ignored,
        }

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
