"""采集状态持久化。

记录每次采集的搜索签名、覆盖率、时间和 URL, 作用有两个:

  1. 下次对同一搜索条件采集时, 如果上次几乎全是重复职位 (覆盖率 >= 0.9),
     自动放慢基础请求节奏 —— 职位都抓取过的情况下, 过于频繁调用列表 API
     会被 BOSS 拦截, 宁可慢一点。
  2. 记住最近一次使用的列表页 URL, `gaj agent daily` 无需每次重新指定。

文件: data/crawl_state.json —— 纯派生数据, 删掉不影响任何已有采集结果。
"""

from __future__ import annotations

import hashlib
import json
import time

from .. import config as cfg
from ..logging_setup import get_logger
from ..store import repo

log = get_logger("scraper.state")

STATE_PATH = cfg.DATA_ROOT / "crawl_state.json"


def search_signature(params_or_url: dict | str) -> str:
    """从搜索参数 (或列表页 URL) 计算稳定的搜索签名。

    签名只取影响搜索结果的参数 (city/position/query/salary/stage),
    同一搜索条件无论翻页到第几页, 签名都相同。
    """
    if isinstance(params_or_url, str):
        try:
            from boss_scraper.har_parser import extract_search_params_from_url

            params = extract_search_params_from_url(params_or_url)
        except Exception:
            params = {}
    else:
        params = params_or_url or {}
    key = {k: str(params.get(k, "")) for k in ("city", "position", "query", "salary", "stage")}
    raw = json.dumps(key, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def load_state() -> dict:
    return repo.read_json(
        STATE_PATH, default={"runs": {}, "last_url": "", "last_signature": ""}
    )


def save_state(state: dict) -> None:
    repo.write_json(STATE_PATH, state)


def record_crawl(url: str, stats: dict) -> dict:
    """采集结束后记录结果 (覆盖率、URL、时间)。返回写入的条目。"""
    state = load_state()
    sig = search_signature(url)
    entry = {
        "url": url,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "coverage": stats.get("coverage"),
        "jobs_scraped": stats.get("jobs_scraped", 0),
        "jobs_skipped_dup": stats.get("jobs_skipped_dup", 0),
        "jobs_found": stats.get("jobs_found", 0),
        "pages": stats.get("pages", 0),
        "early_stop_reason": stats.get("early_stop_reason"),
    }
    state.setdefault("runs", {})[sig] = entry
    state["last_url"] = url
    state["last_signature"] = sig
    state["last_crawl_at"] = entry["at"]
    save_state(state)
    log.info(f"采集状态已记录: sig={sig}, coverage={entry['coverage']}")
    return entry


def slowdown_factor(url: str, *, threshold: float = 0.9) -> float:
    """根据上次采集覆盖率, 返回本次基础翻页延迟的放大倍数。

    上次覆盖率 >= threshold (几乎全是已抓取的重复职位) 时, 返回
    CrawlConfig.coverage_slowdown_factor (默认 2 倍); 否则返回 1.0。
    """
    sig = search_signature(url)
    run = (load_state().get("runs") or {}).get(sig)
    if not run:
        return 1.0
    cov = run.get("coverage")
    if cov is not None and cov >= threshold:
        factor = cfg.SETTINGS.crawl.coverage_slowdown_factor
        log.info(
            f"该搜索条件上次采集覆盖率 {cov:.0%} (职位基本都抓过了), "
            f"本次基础翻页延迟 x{factor}"
        )
        return factor
    return 1.0


def last_crawl_url() -> str:
    """最近一次采集使用的列表页 URL (没有则返回空串)。"""
    return load_state().get("last_url", "") or ""
