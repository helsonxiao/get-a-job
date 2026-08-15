"""爬虫适配层 —— 把 boss_scraper 接入 gaj 数据管线。

boss_scraper 是经过验证的老爬虫, 直接复用它的 CDP 采集能力。
但它的存储格式 (jobs/<自增ID>/) 和 gaj (data/jobs/<encryptJobId>/) 不同,
所以适配层的工作是:

  1. 调用 boss_scraper.JobCrawler 采集 (写入临时目录)
  2. 调用 gaj.store.migrate 把老格式迁移到 gaj 数据目录
  3. (可选) 跑规则打分
  4. 重建索引

这三步是串行的, 任何一步失败都不影响已完成的步骤。
"""

from __future__ import annotations

import time
from typing import Any

from .. import config as cfg
from ..logging_setup import get_logger
from ..store import index, repo

log = get_logger("scraper")


def crawl(
    list_url: str,
    *,
    max_pages: int | None = None,
    fetch_company: bool = True,
    delay_min: float = 3,
    delay_max: float = 8,
    cdp_port: int | None = None,
    auto_migrate: bool = True,
    auto_score: bool = True,
    auto_reindex: bool = True,
    skip_recent_hours: float | None = None,
) -> dict:
    """从 BOSS直聘列表页 URL 启动采集。

    完整流程: 爬取 → 迁移 → 打分 → 索引。

    Args:
        list_url: BOSS直聘筛选过的职位列表页 URL
        max_pages: 最大翻页数, None 不限
        fetch_company: 是否抓取公司详情页
        delay_min/max: 翻页延迟 (秒)
        cdp_port: CDP 端口, None 用配置默认值
        auto_migrate: 采集后自动迁移到 gaj 数据格式
        auto_score: 迁移后自动跑规则打分
        auto_reindex: 最后重建索引
        skip_recent_hours: 最近 N 小时内已采集的职位跳过, 避免重复抓取。
                           None 用配置 SETTINGS.scraper.skip_recent_hours (默认 60 天)。
                           设为 0 表示不跳过 (全量重采)。

    Returns:
        dict: {crawl_stats, migrated, scored, reindexed}
    """
    started = time.time()
    result: dict[str, Any] = {}
    incremental_count = 0

    # ---- 构建最近已采集职位 ID 集合, 翻页时跳过 ----
    if skip_recent_hours is None:
        from ..config import SETTINGS
        skip_recent_hours = SETTINGS.crawl.skip_recent_hours
    existing_ids: set[str] = set()
    if skip_recent_hours > 0:
        try:
            from datetime import datetime, timedelta

            cutoff = datetime.now() - timedelta(hours=skip_recent_hours)
            for job in repo.iter_jobs():
                ts = job.crawled_at or job.first_seen or ""
                try:
                    t = datetime.fromisoformat(ts.replace("Z", ""))
                except (ValueError, AttributeError):
                    continue
                if t >= cutoff:
                    existing_ids.add(job.job_id)
            if existing_ids:
                log.info(
                    f"最近 {skip_recent_hours}h 内已采集 {len(existing_ids)} 个职位, 将跳过"
                )
        except Exception as e:
            log.warning(f"构建已采集 ID 集合失败 (不影响采集): {e}")

    # ---- 增量入库回调: 爬虫每抓完一个职位立刻迁移+打分+入索引 ----
    def _upsert_index_safe(jid: str) -> None:
        try:
            job = repo.load_job(jid)
            if job:
                with index.session() as conn:
                    index.upsert_job(conn, job)
        except Exception as e:
            log.error(f"增量入索引失败 (job_id={jid}): {e}")

    def _on_job_saved(job_dir: str, job_id: str) -> None:
        """爬虫回调: job_dir 是老格式目录路径, job_id 是爬虫的自增数字 ID。

        gaj 系统用的是 BOSS 的 encryptJobId (从 meta.json.source_url 提取),
        所以不能直接拿 job_id 用, 得从迁移后的 data/jobs/<encryptJobId>/
        读出真正的 job_id。
        """
        nonlocal incremental_count
        try:
            from pathlib import Path

            from ..store.migrate import migrate_one

            mrep = migrate_one(Path(job_dir))
            if mrep.migrated == 0:
                log.warning(f"增量迁移未成功 (老目录={job_dir}): {mrep.skipped}")
                return

            # 从 meta.json 提取真正的 encryptJobId (与 migrate_one 用同一个正则)
            import json

            from ..store.migrate import _JOB_ID_RE

            meta_path = Path(job_dir) / "meta.json"
            real_jid = ""
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                url = meta.get("source_url", "")
                m = _JOB_ID_RE.search(url)
                if m:
                    real_jid = m.group(1)
            if not real_jid:
                log.warning(f"无法从 {meta_path} 提取 encryptJobId, 跳过打分/索引")
                return

            log.info(f"✓ 增量入库: {real_jid}")
            incremental_count += 1
        except Exception as e:
            log.error(f"增量迁移异常 (job_dir={job_dir}): {e}")
            return

        if auto_score:
            try:
                from ..core.score_runner import score_all

                score_all(only=real_jid, force=True)
            except Exception as e:
                log.error(f"增量打分失败 (job_id={real_jid}): {e}")
                _upsert_index_safe(real_jid)
        else:
            _upsert_index_safe(real_jid)

    # ---- 1. 爬取 ----
    try:
        from boss_scraper.cdp_session import DEFAULT_CDP_PORT
        from boss_scraper.crawler import JobCrawler

        from . import crawl_state

        port = cdp_port or cfg.SETTINGS.crawl.cdp_port or DEFAULT_CDP_PORT
        # 用临时目录, 不污染 gaj 的 data/jobs
        tmp_dir = str(cfg.RAW_DIR / f"crawl-{time.strftime('%Y%m%dT%H%M%S')}")

        # 覆盖率自适应: 上次采集几乎全是重复职位时, 本次放慢基础节奏,
        # 避免职位都抓过后高频调用列表 API 被 BOSS 反爬拦截
        factor = crawl_state.slowdown_factor(list_url)
        eff_min, eff_max = delay_min * factor, delay_max * factor
        if factor > 1.0:
            log.info(f"翻页延迟调整为 {eff_min:.0f}-{eff_max:.0f}s (覆盖率降速)")

        # 续翻: 上次因连续重复页停止时的页码, 本次前几页全重复时跳到那里再试
        resume_page = crawl_state.get_last_dup_page(list_url)

        crawler = JobCrawler(
            cdp_port=port,
            jobs_dir=tmp_dir,
            max_pages=max_pages,
            delay_min=eff_min,
            delay_max=eff_max,
            fetch_company=fetch_company,
            on_job_saved=_on_job_saved,
            existing_ids=existing_ids,
            dup_slowdown=cfg.SETTINGS.crawl.dup_slowdown,
            dup_stop_pages=cfg.SETTINGS.crawl.dup_stop_pages,
            slowdown_cap=cfg.SETTINGS.crawl.slowdown_cap,
            max_jobs_per_session=cfg.SETTINGS.crawl.max_jobs_per_session,
            resume_page=resume_page,
        )
        crawler.crawl_from_url(list_url)
        result["crawl_stats"] = crawler.stats.to_dict()
        result["crawl_stats_text"] = str(crawler.stats)
        result["crawl_dir"] = tmp_dir
        result["incremental"] = incremental_count
        log.info(f"采集完成: {crawler.stats} (增量入库 {incremental_count} 个)")

        # 记录覆盖率与 URL, 供下次降速决策 / agent daily 复用
        try:
            result["crawl_state"] = crawl_state.record_crawl(
                list_url, crawler.stats.to_dict()
            )
            # 续翻页码持久化: 仅当 crawler 显式修改过 last_dup_page 时才写
            # - covered 停止: 写入页码, 下次续翻
            # - hasMore=False 翻到底: 写入 0, 清除之前的锚点
            # 避免 session_limit / max_pages 等场景下初始值 0 误清有效锚点
            stats_dict = crawler.stats.to_dict()
            if stats_dict.get("last_dup_page_dirty"):
                crawl_state.save_last_dup_page(
                    list_url, stats_dict["last_dup_page"]
                )
        except Exception as exc:
            log.warning(f"记录采集状态失败 (不影响结果): {exc}")
    except Exception as exc:
        log.error(f"采集失败: {exc}")
        result["error"] = f"采集失败: {exc}"
        result["elapsed"] = round(time.time() - started, 1)
        return result

    # ---- 2. 迁移 (migrate 内部会调 reindex) ----
    migrated_reindexed = False
    if auto_migrate:
        try:
            from dataclasses import asdict
            from pathlib import Path

            from ..store.migrate import migrate as do_migrate

            src_path = Path(result.get("crawl_dir") or "jobs")
            report = do_migrate(src=src_path, rebuild_index=auto_reindex)
            result["migrated"] = asdict(report) if hasattr(report, "__dataclass_fields__") else str(report)
            migrated_reindexed = auto_reindex
            log.info(f"迁移完成: {report}")
        except Exception as exc:
            log.error(f"迁移失败: {exc}")
            result["migrate_error"] = str(exc)

    # ---- 3. 规则打分 ----
    if auto_score:
        try:
            from ..core.score_runner import score_all

            out = score_all(force=False)
            stats = out.get("stats", {})
            result["scored"] = stats
            log.info(f"规则打分完成: {stats}")
        except Exception as exc:
            log.error(f"打分失败: {exc}")
            result["score_error"] = str(exc)

    # ---- 4. 重建索引 (migrate 没做过才做) ----
    if auto_reindex and not migrated_reindexed:
        try:
            idx = index.reindex()
            result["reindexed"] = idx
            log.info(f"索引重建: {idx}")
        except Exception as exc:
            log.error(f"索引重建失败: {exc}")
            result["reindex_error"] = str(exc)

    result["elapsed"] = round(time.time() - started, 1)
    log.info(f"采集全流程完成, 耗时 {result['elapsed']}s")
    return result


def check_chrome(port: int | None = None) -> bool:
    """检查 Chrome CDP 是否就绪。"""
    from ..browser.cdp import ensure_chrome_running

    return ensure_chrome_running(port)
