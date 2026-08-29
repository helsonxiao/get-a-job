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

SCHEMA_VERSION = "1.1"

#: 输出契约中禁止出现的字段名 (防止未来改动引入单条记录泄漏)
FORBIDDEN_KEYS = frozenset({
    "job_id", "url", "address", "gps", "lat", "lng", "brand_id",
    "boss", "company_name", "first_seen", "last_seen", "indexed_at",
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
    """同一次打包内的公司脱敏别名表: 同一公司在不同区块拿到同一别名。"""

    def __init__(self) -> None:
        self._map: dict[str, str] = {}

    def build(self, companies: list[dict]) -> None:
        """按 job_count 降序确定性分配别名 (公司A/B/C..., 超出字母池用数字)。"""
        ordered = sorted(
            companies,
            key=lambda c: (-(c.get("job_count") or 0), c.get("_key") or ""),
        )
        for i, c in enumerate(ordered):
            if c["_key"] not in self._map:
                if i < len(_ALIAS_POOL):
                    self._map[c["_key"]] = f"公司{_ALIAS_POOL[i]}"
                else:
                    self._map[c["_key"]] = f"公司{i + 1}"

    def alias(self, key: str | None) -> str:
        if key is None:
            return "公司—"
        return self._map.get(key, "公司—")


def _company_key(item: dict) -> str | None:
    return item.get("brand_id") or item.get("name") or None


def _deidentify_focus_companies(
    top_companies: list[dict], registry: _AliasRegistry
) -> list[dict]:
    """代表公司脱敏: 明文公司名 → 别名, 去除 brand_id 与个人打分字段。"""
    result = []
    for c in top_companies:
        result.append({
            "alias": registry.alias(_company_key(c)),
            "job_count": c.get("job_count"),
            "avg_salary": c.get("avg_salary"),
        })
    return result


def _deidentify_red_flag_companies(
    red_flag_companies: list[dict], registry: _AliasRegistry
) -> list[dict]:
    """红旗公司榜脱敏: 保留聚合信号计数与标签, 去除 brand_id/明文名。"""
    result = []
    for c in red_flag_companies:
        result.append({
            "alias": registry.alias(_company_key(c)),
            "job_count": c.get("job_count"),
            "heavy_overtime": c.get("heavy_overtime"),
            "outsourcing": c.get("outsourcing"),
            "travel": c.get("travel"),
            "flags": c.get("flags", []),
            "avg_salary": c.get("avg_salary"),
        })
    return result


# ---------------------------------------------------------------- 雇主画像


def _employer_block(conn: sqlite3.Connection) -> dict:
    """雇主侧聚合 (schema 1.1 新增): 月薪构成/谈薪带宽/工时/规模/性质/福利。

    全部为聚合统计, 不含单条记录; 数据来自 jobs × companies 左连接。
    """
    rows = conn.execute(
        f"SELECT j.company_id, j.salary_mid, j.salary_min, j.salary_max, j.salary_months,"
        f" j.welfare, c.scale_min, c.scale_max, c.nature, c.hours_per_day"
        f" FROM jobs j LEFT JOIN companies c ON c.brand_id = j.company_id"
        f" WHERE {observatory._VISIBLE}"
    ).fetchall()
    total = len(rows)

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
        "months_mix": months_mix,
        "salary_spread": salary_spread,
        "hours_dist": hours_dist,
        "scale_dist": scale_dist,
        "nature_dist": nature_dist,
        "top_welfare": top_welfare,
    }


# ---------------------------------------------------------------- 组装


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
    }

    focus_name = items[0]["name"] if items else None
    focus_detail = (
        observatory.observatory_industry_detail(conn, focus_name)
        if focus_name else None
    )

    # 脱敏: 先汇总所有公司出现点建别名表, 同一公司全程同一别名
    registry = _AliasRegistry()
    registry.build([
        {**c, "_key": _company_key(c)}
        for c in market["signal_radar"].get("red_flag_companies", [])
    ])
    if focus_detail is not None:
        registry.build([
            {**c, "_key": _company_key(c)}
            for c in focus_detail.get("top_companies", [])
        ])
    market["signal_radar"]["red_flag_companies"] = _deidentify_red_flag_companies(
        market["signal_radar"].get("red_flag_companies", []), registry
    )
    if focus_detail is not None:
        focus_detail["top_companies"] = _deidentify_focus_companies(
            focus_detail.get("top_companies", []), registry
        )

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
