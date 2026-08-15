"""gaj agent —— 面向 AI 智能体的操作接口。

设计原则:
  1. stdout 只输出一个 JSON 信封 (envelope), 日志全部走 stderr 和 logs/,
     智能体可以直接 json.loads(stdout)。
  2. 错误用稳定错误码表达, 智能体据此决定重试、降级或通知用户。
  3. 退出码: 0=成功, 1=执行失败, 2=参数错误。

信封格式:
    {"ok": true,  "command": "status", "version": "...", "data": {...}}
    {"ok": false, "command": "crawl",  "version": "...",
     "error": {"code": "chrome_not_ready", "message": "..."}, "data": {...}}

命令一览:
    status                    系统与数据概况 (健康检查)
    jobs                      查询职位列表 (筛选/排序/分页)
    job <ID>                  单个职位全量详情 (JD + 打分)
    analyze --job ID|--auto   AI 分析打分
    analyze --company BRAND_ID  公司级 AI 评价 (图鉴词条, 手动)
    crawl [--url URL]         增量采集 (自动降速/提前结束)
    daily [--url URL]         每日编排: 采集 → 挑候选 → AI 分析 → 摘要
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta

from ..logging_setup import get_logger, setup

log = get_logger("agent")

VERSION = "0.1.0"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


# ---------------------------------------------------------------- 信封输出


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str))


def _ok(command: str, data: dict) -> int:
    _emit({"ok": True, "command": command, "version": VERSION, "data": data})
    return EXIT_OK


def _err(
    command: str,
    code: str,
    message: str,
    data: dict | None = None,
    exit_code: int = EXIT_FAIL,
) -> int:
    payload: dict = {
        "ok": False,
        "command": command,
        "version": VERSION,
        "error": {"code": code, "message": message},
    }
    if data:
        payload["data"] = data
    _emit(payload)
    return exit_code


# ---------------------------------------------------------------- 环境检查


def _chrome_ready() -> bool:
    """静默检查 Chrome CDP 端口 (不打日志)。"""
    from .. import config as cfg

    try:
        import requests

        r = requests.get(
            f"http://127.0.0.1:{cfg.SETTINGS.crawl.cdp_port}/json/version",
            timeout=3,
        )
        return r.status_code == 200
    except Exception:
        return False


def _boss_logged_in() -> bool:
    """静默检查 BOSS直聘登录态 (不打日志)。"""
    from .. import config as cfg

    try:
        from boss_scraper.chrome_manager import check_login_state

        return bool(check_login_state(cfg.SETTINGS.crawl.cdp_port))
    except Exception:
        return False


def _ai_error_code(exc: Exception) -> str:
    """把驱动层异常映射成稳定错误码。"""
    msg = str(exc)
    if "CDP 未运行" in msg:
        return "chrome_not_ready"
    if "未登录" in msg:
        return "not_logged_in"
    if "不存在" in msg:
        return "job_not_found"
    if "超时" in msg or "timeout" in msg.lower():
        return "timeout"
    return "ai_failed"


# ---------------------------------------------------------------- 数据压缩


_JOB_FIELDS = (
    "job_id", "title", "company_name", "company_id", "city", "district", "salary_raw",
    "salary_mid", "salary_max", "exp_min", "exp_max", "edu_raw", "skills",
    "rule_status", "rule_total", "latest_ai_provider", "latest_ai_total",
    "best_total", "ai_count", "ai_needed", "favorite", "online",
    "first_seen", "last_seen", "url",
)


def _compact_job(row: dict) -> dict:
    out = {k: row.get(k) for k in _JOB_FIELDS}
    out["score"] = row.get("best_total")  # 便于智能体直接取最优分
    return out


def _compact_ai_score(item: dict) -> dict:
    """AI 打分记录的紧凑视图 (去掉冗长的 raw_response)。"""
    return {
        "provider": item.get("provider"),
        "model": item.get("model"),
        "status": item.get("status"),
        "total_score": item.get("total_score"),
        "recommendation": item.get("recommendation"),
        "dimension_scores": item.get("dimension_scores"),
        "deep_analysis_report": item.get("deep_analysis_report") or None,
        "created_at": item.get("created_at"),
        "file": item.get("_file"),
    }


def _compact_score_out(d: dict) -> dict:
    """score_with_ai 输出的紧凑视图。"""
    r = d.get("result") or {}
    out = {
        "job_id": d.get("job_id"),
        "success": bool(d.get("success")),
        "provider": d.get("provider"),
        "status": r.get("status"),
        "total_score": r.get("total_score"),
        "recommendation": r.get("recommendation"),
        "dimension_scores": r.get("dimension_scores"),
        "deep_analysis_report": r.get("deep_analysis_report") or None,
        "error": d.get("error"),
        "elapsed": d.get("elapsed"),
    }
    if d.get("pool"):
        out["pool"] = d.get("pool")
    if d.get("reason"):
        out["reason"] = d.get("reason")
    return out


def _compact_company_out(d: dict) -> dict:
    """analyze_company 输出的紧凑视图 (不带 raw_response)。"""
    r = d.get("result") or {}
    return {
        "brand_id": d.get("brand_id"),
        "success": bool(d.get("success")),
        "provider": d.get("provider"),
        "company_score_ai": r.get("company_score_ai"),
        "worth_joining": r.get("worth_joining"),
        "worth_joining_reason": r.get("worth_joining_reason"),
        "hiring_urgency": r.get("hiring_urgency"),
        "business_analysis": r.get("business_analysis") or None,
        "tech_stack_profile": r.get("tech_stack_profile") or None,
        "highlights": r.get("highlights"),
        "risks": r.get("risks"),
        "interview_strategy": r.get("interview_strategy"),
        "business_keywords": r.get("business_keywords"),
        "error": d.get("error"),
        "elapsed": d.get("elapsed"),
    }


# ---------------------------------------------------------------- status


def cmd_status(args) -> int:
    from .. import config as cfg
    from ..browser import available_providers
    from ..scraper import crawl_state
    from ..store import index

    chrome = _chrome_ready()
    data: dict = {
        "time": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "chrome_cdp_ready": chrome,
        "cdp_port": cfg.SETTINGS.crawl.cdp_port,
        "boss_logged_in": _boss_logged_in() if chrome else False,
        "providers": available_providers(),
        "ai_config": {
            "default_provider": cfg.SETTINGS.ai.provider,
            "tab_mode": cfg.SETTINGS.ai.tab_mode,
            "generation_timeout": cfg.SETTINGS.ai.generation_timeout,
        },
        "crawl_config": {
            "dup_stop_pages": cfg.SETTINGS.crawl.dup_stop_pages,
            "dup_slowdown": cfg.SETTINGS.crawl.dup_slowdown,
            "max_jobs_per_session": cfg.SETTINGS.crawl.max_jobs_per_session,
            "page_delay": [
                cfg.SETTINGS.crawl.page_delay_min,
                cfg.SETTINGS.crawl.page_delay_max,
            ],
        },
        "data_root": str(cfg.DATA_ROOT),
    }

    try:
        with index.session() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total,"
                " COALESCE(SUM(rule_total IS NOT NULL), 0) AS rule_scored,"
                " COALESCE(SUM(ai_count > 0), 0) AS ai_scored,"
                " COALESCE(SUM(ai_needed = 1 AND ai_count = 0"
                "   AND (ignored = 0 OR ignored IS NULL)), 0) AS ai_pending,"
                " COALESCE(SUM(favorite = 1), 0) AS favorites,"
                " COALESCE(SUM(ignored = 1), 0) AS ignored"
                " FROM jobs"
            ).fetchone()
            data["jobs"] = {
                "total": row["total"],
                "rule_scored": row["rule_scored"],
                "ai_scored": row["ai_scored"],
                "ai_pending": row["ai_pending"],
                "favorites": row["favorites"],
                "ignored": row["ignored"],
            }
    except Exception as e:
        data["jobs"] = {"error": str(e)}

    # 打分 backlog: 未打分可补 / 已过时需重打
    try:
        from ..ai.backlog import backlog_stats

        data["backlog"] = backlog_stats()
    except Exception as e:
        data["backlog"] = {"error": str(e)}

    state = crawl_state.load_state()
    sig = state.get("last_signature") or ""
    last_run = (state.get("runs") or {}).get(sig) or {}
    data["last_crawl"] = {
        "url": state.get("last_url", ""),
        "at": state.get("last_crawl_at", ""),
        "coverage": last_run.get("coverage"),
        "early_stop_reason": last_run.get("early_stop_reason"),
    }
    return _ok("status", data)


# ---------------------------------------------------------------- jobs


def cmd_jobs(args) -> int:
    from ..store import index

    cities = [c.strip() for c in args.city.split(",") if c.strip()] if args.city else []
    statuses = [s.strip().upper() for s in args.status.split(",") if s.strip()] if args.status else []
    new_since = ""
    if args.new_within_hours:
        cutoff = datetime.now() - timedelta(hours=args.new_within_hours)
        new_since = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    kwargs = dict(
        search=args.search or "",
        cities=cities,
        statuses=statuses,
        scored=args.scored,
        salary_min=args.min_salary,
        online_only=args.online,
        favorite="only" if args.favorite else "all",
        ignored="all" if args.include_ignored else "exclude",
        new_since=new_since,
    )
    try:
        with index.session() as conn:
            rows = index.query_jobs(
                conn, **kwargs, sort=args.sort, desc=not args.asc,
                limit=args.limit, offset=args.offset,
            )
            total = index.count_jobs(conn, **kwargs)
    except Exception as e:
        return _err("jobs", "index_error", f"查询索引失败: {e}")

    return _ok(
        "jobs",
        {
            "total": total,
            "limit": args.limit,
            "offset": args.offset,
            "jobs": [_compact_job(r) for r in rows],
        },
    )


# ---------------------------------------------------------------- job


def cmd_job(args) -> int:
    from .. import config as cfg
    from ..store import repo

    job = repo.load_job(args.id)
    if not job:
        return _err("job", "job_not_found", f"职位不存在: {args.id}")

    jd_text = repo.read_text(repo.job_dir(args.id) / cfg.JOB_JD_TEXT, "")
    rule = repo.load_rule_score(args.id)
    ai_scores = repo.list_ai_scores(args.id)
    company = repo.load_company(job.company_id) if job.company_id else None

    return _ok(
        "job",
        {
            "job": job.to_dict(),
            "jd_markdown": jd_text,
            "rule_score": rule,
            "ai_scores": [_compact_ai_score(s) for s in ai_scores],
            "company": company.to_dict() if company else None,
        },
    )


# ---------------------------------------------------------------- analyze


def cmd_analyze(args) -> int:
    if not args.job and not args.auto and not getattr(args, "company", None):
        return _err(
            "analyze", "usage",
            "请指定 --job ID / --auto / --company BRAND_ID",
            exit_code=EXIT_USAGE,
        )

    if not _chrome_ready():
        return _err(
            "analyze", "chrome_not_ready",
            "Chrome CDP 未运行。请先执行: python3 -m gaj setup-chrome",
        )

    if getattr(args, "company", None):
        # 公司级 AI 评价 (图鉴词条): 手动触发, 有保鲜期缓存
        from ..ai.company_runner import analyze_company

        try:
            out = analyze_company(
                args.company, args.provider,
                force=getattr(args, "force", False),
                max_age_days=args.max_age_days,
            )
        except FileNotFoundError as exc:
            return _err("analyze", "company_not_found", str(exc))
        except ValueError as exc:
            return _err("analyze", "usage", str(exc), exit_code=EXIT_USAGE)
        except Exception as exc:
            return _err("analyze", _ai_error_code(exc), str(exc))
        return _ok(
            "analyze",
            {
                "mode": "company",
                "total": 1,
                "success": 1 if out["success"] else 0,
                "failed": 0 if out["success"] else 1,
                "cached": out.get("cached", False),
                "cache_reason": out.get("cache_reason", ""),
                "results": [_compact_company_out(out)],
            },
        )

    if args.job:
        from ..ai.runner import score_with_ai

        try:
            out = score_with_ai(args.job, args.provider, deep=args.deep)
        except Exception as exc:
            return _err("analyze", _ai_error_code(exc), str(exc))
        if not out["success"]:
            # 打分失败也返回结构化结果, 由智能体决定是否重试
            return _ok(
                "analyze",
                {
                    "total": 1, "success": 0, "failed": 1,
                    "results": [_compact_score_out(out)],
                },
            )
        return _ok(
            "analyze",
            {
                "total": 1, "success": 1, "failed": 0,
                "results": [_compact_score_out(out)],
            },
        )

    # --auto: backlog 队列驱动 (补历史欠分 + 重打过分)
    from ..ai.backlog import pick_backlog

    pool = getattr(args, "pool", None) or "all"
    try:
        candidates = pick_backlog(
            pool,
            limit=args.limit,
            min_rule_score=args.min_rule_score,
            include_rejected=args.include_rejected,
            cooldown_hours=args.cooldown_hours,
            max_age_days=args.max_age_days,
        )
    except Exception as exc:
        return _err("analyze", "internal", f"挑选打分候选失败: {exc}")

    if args.dry_run:
        return _ok(
            "analyze",
            {
                "dry_run": True,
                "pool": pool,
                "count": len(candidates),
                "candidates": candidates,
                "note": "dry-run: 仅返回候选名单, 未调用大模型",
            },
        )

    if not candidates:
        return _ok(
            "analyze",
            {
                "total": 0, "success": 0, "failed": 0, "skipped": 0,
                "results": [],
                "note": f"pool={pool} 没有可打分的候选 (可能都已打分且未过时)",
            },
        )

    from ..ai.runner import batch_score

    try:
        out = batch_score(args.provider, deep=args.deep, candidates=candidates)
    except Exception as exc:
        return _err("analyze", _ai_error_code(exc), str(exc))

    return _ok(
        "analyze",
        {
            "total": out["total"],
            "success": out["success"],
            "failed": out["failed"],
            "skipped": out.get("skipped", 0),
            "results": [_compact_score_out(d) for d in out.get("details", [])],
        },
    )


# ---------------------------------------------------------------- crawl


def cmd_crawl(args) -> int:
    from ..scraper import crawl as do_crawl
    from ..scraper import crawl_state

    url = args.url or crawl_state.last_crawl_url()
    if not url:
        return _err(
            "crawl", "no_crawl_url",
            "没有可用的列表页 URL。请用 --url 传入 BOSS直聘筛选后的列表页 URL "
            "(首次之后会记住, 之后可省略)。",
            exit_code=EXIT_USAGE,
        )

    if not _chrome_ready():
        return _err(
            "crawl", "chrome_not_ready",
            "Chrome CDP 未运行。请先执行: python3 -m gaj setup-chrome",
        )
    if not _boss_logged_in():
        return _err(
            "crawl", "not_logged_in",
            "未登录 zhipin.com。请在 CDP Chrome 窗口里登录 BOSS直聘后重试。",
        )

    out = do_crawl(
        url,
        max_pages=args.max_pages,
        fetch_company=not args.no_company,
        auto_score=not args.no_score,
    )
    if "error" in out:
        return _err("crawl", "crawl_failed", out["error"], data=out)
    return _ok("crawl", out)


# ---------------------------------------------------------------- daily


def _pick_candidates(new_ids: list[str], limit: int) -> list[str]:
    """挑选 AI 分析候选: 优先本次新抓且被规则标记需 AI 介入的,
    其次新抓的高规则分职位, 仍不足则补历史待办 (ai_needed 且未 AI 打分)。"""
    from ..store import index, repo

    if limit <= 0:
        return []

    rows: list[tuple[str, dict]] = []
    for jid in new_ids:
        try:
            if repo.latest_ai_score(jid):
                continue  # 已有 AI 打分
            rule = repo.load_rule_score(jid)
            if not rule:
                continue
            rows.append((jid, rule))
        except Exception:
            continue
    # 需 AI 介入的排前面, 其次按规则分从高到低
    rows.sort(
        key=lambda x: (
            0 if x[1].get("ai_intervention_needed") else 1,
            -(x[1].get("total_score") or 0),
        )
    )

    picked: list[str] = []
    seen: set[str] = set()
    for jid, _ in rows:
        if len(picked) >= limit:
            break
        picked.append(jid)
        seen.add(jid)

    if len(picked) < limit:
        # 新岗不足时补历史欠分: 走完整 backfill 队列
        # (规则标记需 AI 介入的排前, 其次按规则分降序清欠)
        try:
            from ..ai.backlog import pick_backlog

            back = pick_backlog(
                "backfill", limit=limit - len(picked), prefer_triggered=True
            )
            for c in back:
                jid = c.get("job_id")
                if jid and jid not in seen:
                    picked.append(jid)
                    seen.add(jid)
        except Exception as e:
            log.warning(f"补充历史 AI 待办候选失败: {e}")

    return picked[:limit]


def _build_digest(
    url: str,
    crawl_data: dict | None,
    analyzed: list[dict],
    new_ids: list[str],
) -> str:
    """生成人类/智能体可读的每日摘要 (markdown)。"""
    from ..store import repo

    today = time.strftime("%Y-%m-%d")
    lines = [f"## 坑位图鉴每日职位摘要 ({today})", ""]

    if crawl_data:
        cs = crawl_data.get("crawl_stats") or {}
        cov = cs.get("coverage")
        cov_txt = f"{cov:.0%}" if isinstance(cov, (int, float)) else "N/A"
        early = cs.get("early_stop_reason")
        early_txt = "（职位已覆盖, 提前结束）" if early == "covered" else ""
        lines.append(
            f"- 采集: 翻 {cs.get('pages', 0)} 页, 发现 {cs.get('jobs_found', 0)} 个职位, "
            f"新抓 {cs.get('jobs_scraped', 0)} 个, 重复跳过 {cs.get('jobs_skipped_dup', 0)} 个"
            f" (覆盖率 {cov_txt}){early_txt}, 耗时 {cs.get('elapsed_seconds', 0)}s"
        )
    else:
        lines.append("- 采集: 本次未执行")
    lines.append("")

    # 新职位里的规则分 Top5
    top_new: list[tuple[str, dict]] = []
    for jid in new_ids:
        rule = repo.load_rule_score(jid)
        job = repo.load_job(jid)
        if job and rule:
            top_new.append((jid, {"job": job, "rule": rule}))
    top_new.sort(key=lambda x: -(x[1]["rule"].get("total_score") or 0))
    if top_new:
        lines.append(f"### 新职位 Top {min(5, len(top_new))} (按规则分)")
        for jid, item in top_new[:5]:
            job, rule = item["job"], item["rule"]
            lines.append(
                f"- **{job.title}** @ {job.company_name} · {job.city} · "
                f"{job.salary.get('raw', '薪资未知')} · 规则分 "
                f"{rule.get('total_score')} ({rule.get('status')}) · `{jid}`"
            )
        lines.append("")

    if analyzed:
        lines.append("### AI 分析结果")
        for a in analyzed:
            if a.get("success"):
                lines.append(
                    f"- `{a.get('job_id')}` → {a.get('total_score')}/10 "
                    f"[{a.get('status')}] {a.get('recommendation', '')}"
                )
            else:
                lines.append(f"- `{a.get('job_id')}` → 分析失败: {a.get('error')}")
        lines.append("")

    lines.append(f"> 搜索 URL: {url}")
    return "\n".join(lines)


def cmd_daily(args) -> int:
    """每日编排: 增量采集 → 挑候选 → AI 分析 → 生成摘要。"""
    from .. import config as cfg
    from ..scraper import crawl as do_crawl
    from ..scraper import crawl_state
    from ..store import repo

    url = args.url or crawl_state.last_crawl_url()
    if not url:
        return _err(
            "daily", "no_crawl_url",
            "没有可用的列表页 URL。请用 --url 传入, 或先执行一次 "
            "`gaj agent crawl --url <BOSS列表页URL>`。",
            exit_code=EXIT_USAGE,
        )

    if not _chrome_ready():
        return _err(
            "daily", "chrome_not_ready",
            "Chrome CDP 未运行。请先执行: python3 -m gaj setup-chrome",
        )

    warnings: list[str] = []
    crawl_data: dict | None = None
    new_ids: list[str] = []

    # ---- 1) 增量采集 ----
    if args.no_crawl:
        warnings.append("已按 --no-crawl 跳过采集")
    elif not _boss_logged_in():
        warnings.append("未登录 zhipin.com, 本次跳过采集 (只分析存量职位)")
    else:
        before = repo.all_job_ids()
        out = do_crawl(url, max_pages=args.max_pages, fetch_company=True, auto_score=True)
        new_ids = sorted(repo.all_job_ids() - before)
        if "error" in out:
            warnings.append(f"采集失败: {out['error']}")
        else:
            crawl_data = out
            crawl_data["new_job_ids"] = new_ids

    # ---- 2) 挑选 AI 分析候选 ----
    candidates = _pick_candidates(new_ids, args.analyze_limit)

    # ---- 3) AI 分析 (复用同一个驱动/标签页) ----
    analyzed: list[dict] = []
    if candidates:
        try:
            from ..ai.runner import score_with_ai
            from ..browser import close_driver, get_driver

            driver = get_driver(args.provider)
            try:
                for i, jid in enumerate(candidates):
                    out = score_with_ai(
                        jid, args.provider, deep=args.deep, driver=driver
                    )
                    analyzed.append(_compact_score_out(out))
                    if i < len(candidates) - 1:
                        delay = (
                            cfg.SETTINGS.ai.between_calls_min
                            + cfg.SETTINGS.ai.between_calls_max
                        ) / 2
                        time.sleep(delay)
            finally:
                close_driver(driver)
        except Exception as exc:
            warnings.append(f"AI 分析中断: {exc}")

    # ---- 4) 摘要 ----
    digest = _build_digest(url, crawl_data, analyzed, new_ids)
    data = {
        "date": time.strftime("%Y-%m-%d"),
        "url": url,
        "crawl": crawl_data,
        "new_job_count": len(new_ids),
        "analyzed": analyzed,
        "analyzed_success": sum(1 for a in analyzed if a.get("success")),
        "warnings": warnings,
        "digest_markdown": digest,
    }
    return _ok("daily", data)


# ---------------------------------------------------------------- 入口


_HANDLERS = {
    "status": cmd_status,
    "jobs": cmd_jobs,
    "job": cmd_job,
    "analyze": cmd_analyze,
    "crawl": cmd_crawl,
    "daily": cmd_daily,
}


def main(argv: list[str] | None = None) -> int:
    setup()
    ap = argparse.ArgumentParser(
        prog="gaj agent",
        description="坑位图鉴（GAJ）面向 AI 智能体的操作接口 (stdout 只输出 JSON)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
命令说明:
  status   健康检查与数据概况。返回 chrome_cdp_ready/boss_logged_in/providers、
           职位统计(total/rule_scored/ai_scored/ai_pending/favorites)、
           backlog(unscored/stale_total/stale_context_changed/stale_expired)、
           last_crawl(URL/时间/覆盖率/early_stop_reason)。
           任何自动化流程的第一步。

  jobs     查询职位列表。返回 total + jobs[]，每项含 job_id/title/company_name/
           salary_raw/rule_status/rule_total/latest_ai_total/best_total/score/url。
           支持 --search/--city/--status/--scored/--min-salary/--online/--favorite/
           --include-ignored/--new-within-hours/--sort/--asc/--limit/--offset。

  job      单个职位全量详情。返回 job(归一化字段)/jd_markdown(清洗 JD 全文)/
           rule_score(规则打分及触发的规则)/ai_scores(历史 AI 打分，倒序)/
           company(公司资料)。分析/改写简历时用 jd_markdown。

  analyze  AI 分析打分。三种互斥模式:
           --job <ID>     单职位深度分析，返回 total_score(0-10)/status/
                          recommendation/dimension_scores/deep_analysis_report。
           --auto         走 backlog 打分队列(自带去重与冷却)，不重复打已打且
                          未过时的分。--pool: backfill=补历史未打分, rescore=重打
                          过时分, all=两者合并(默认)。--dry-run 只看候选名单。
           --company <ID> 公司级 AI 尽调(图鉴词条)，返回 business_analysis/
                          tech_stack_profile/hiring_urgency/worth_joining/
                          highlights/risks/interview_strategy/company_score_ai。
                          默认 180 天保鲜期, 未过期且上下文未变则跳过大模型调用
                          (cached=true)。--force 强制重评。
           --provider: deepseek(默认,成熟)/doubao/tongyi/kimi(实验性)。
           --deep: 生成深度分析报告。

  crawl    增量采集 BOSS直聘职位。已抓过的自动跳过。连续整页全重复时翻页间隔
           逐次拉长(上限 60s)，连续 3 页全重复即提前结束(early_stop=covered)。
           新职位达 60 个也会停止(early_stop=session_limit)。
           续翻: covered时若上次有 last_dup_page 记录, 跳到那里再试几页,
           有新职位则继续, 全重复才真停(下次从更后页续翻)。
           返回 crawl_stats(含 last_dup_page/resume_used) + migrated + scored。
           首次需 --url 提供 BOSS 筛选页 URL，之后记住可省略。

  daily    每日编排: 采集→AI分析→摘要，一条命令完成日常流程。
           始终产出 digest_markdown，局部失败不阻断(warnings 里可见)。
           新岗位不足时预算自动流向 backlog 补历史欠分。
           返回 crawl(含 new_job_ids)/analyzed[]/analyzed_success/
           warnings/digest_markdown。

错误码: usage/chrome_not_ready/not_logged_in/no_crawl_url/job_not_found/
       crawl_failed/ai_failed/timeout/index_error/internal
退出码: 0=成功 1=失败 2=参数错误
超时预算: status/jobs/job 30s, crawl 最坏 30min, daily 建议 45min
""",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="系统与数据概况 (健康检查)")

    p = sub.add_parser("jobs", help="查询职位列表")
    p.add_argument("--search", default="", help="全文搜索 (职位/公司/技能/JD)")
    p.add_argument("--city", default="", help="城市, 逗号分隔多个")
    p.add_argument("--status", default="", help="规则状态: PASS,REVIEW,REJECTED")
    p.add_argument("--scored", default="all", choices=["all", "none", "rule_only", "ai", "no_ai"],
                   help="打分状态: all/none/rule_only/ai/no_ai")
    p.add_argument("--min-salary", type=float, default=None, help="salary_max >= 该值 (K)")
    p.add_argument("--online", action="store_true", help="只看在线职位")
    p.add_argument("--favorite", action="store_true", help="只看收藏")
    p.add_argument("--include-ignored", action="store_true", help="包含已忽略职位")
    p.add_argument("--new-within-hours", type=float, default=None, help="只看最近 N 小时新发现")
    p.add_argument("--sort", default="best_total", help="排序字段 (best_total/rule_total/last_seen/...)")
    p.add_argument("--asc", action="store_true", help="升序")
    p.add_argument("--limit", type=int, default=20, help="返回条数上限 (默认 20)")
    p.add_argument("--offset", type=int, default=0, help="分页偏移")

    p = sub.add_parser("job", help="单个职位详情 (jd_markdown + 规则/AI 打分 + 公司资料)")
    p.add_argument("id", help="职位 ID (encryptJobId)")

    p = sub.add_parser("analyze", help="AI 分析打分 (--job/--auto/--company 三选一)")
    p.add_argument("--job", metavar="ID", help="单个职位深度分析")
    p.add_argument("--company", metavar="BRAND_ID", help="公司级 AI 尽调 (图鉴词条, 需该公司有岗位)")
    p.add_argument(
        "--auto", action="store_true",
        help="走 backlog 打分队列 (自带去重与冷却，不重复打已打且未过时的分)",
    )
    p.add_argument(
        "--pool", default="", choices=["", "backfill", "rescore", "all"],
        help="--auto 候选池: backfill=补历史未打分, rescore=重打已过时的分, all=合并 (默认 all)",
    )
    p.add_argument("--provider", default="deepseek", help="deepseek(成熟)/doubao/tongyi/kimi(实验性)")
    p.add_argument("--deep", action="store_true", help="生成深度分析报告")
    p.add_argument("--limit", type=int, default=5, help="--auto 时最多分析几个 (默认 5)")
    p.add_argument("--min-rule-score", type=float, default=None, help="规则分下限, 低于不打")
    p.add_argument("--cooldown-hours", type=float, default=None, help="重打冷却 (小时, 默认 72)")
    p.add_argument("--max-age-days", type=float, default=None, help="分数保鲜期 (天, 岗位默认 90, 公司默认 180)")
    p.add_argument("--include-rejected", action="store_true", help="包含 REJECTED 岗位")
    p.add_argument("--dry-run", action="store_true", help="只返回候选名单, 不调用大模型")
    p.add_argument("--force", action="store_true", help="强制重新评价, 跳过保鲜期缓存 (--company 适用)")

    p = sub.add_parser("crawl", help="增量采集 (自动降速/覆盖提前结束)")
    p.add_argument("--url", default="", help="BOSS 列表页 URL (缺省用上次记住的)")
    p.add_argument("--max-pages", type=int, default=10, help="最大翻页数 (默认 10)")
    p.add_argument("--no-company", action="store_true", help="不抓公司详情页")
    p.add_argument("--no-score", action="store_true", help="不自动规则打分")

    p = sub.add_parser("daily", help="每日编排: 采集→AI分析→摘要 (定时任务首选)")
    p.add_argument("--url", default="", help="BOSS 列表页 URL (缺省用上次记住的)")
    p.add_argument("--max-pages", type=int, default=10, help="最大翻页数 (默认 10)")
    p.add_argument("--no-crawl", action="store_true", help="跳过采集, 只分析存量")
    p.add_argument("--analyze-limit", type=int, default=3, help="AI 分析的职位数 (默认 3)")
    p.add_argument("--provider", default="deepseek", help="deepseek/doubao/tongyi/kimi")
    p.add_argument("--deep", action="store_true", help="生成深度分析报告")

    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else EXIT_USAGE
        if code != 0:
            _emit(
                {
                    "ok": False,
                    "command": None,
                    "version": VERSION,
                    "error": {
                        "code": "usage",
                        "message": "参数错误, 用法详见 stderr 的帮助输出或 AGENT.md",
                    },
                }
            )
        return code

    try:
        return _HANDLERS[args.command](args)
    except Exception as exc:
        import traceback

        log.error(f"agent 命令 {args.command} 异常: {exc}\n{traceback.format_exc()}")
        return _err(args.command, "internal", f"内部错误: {exc}")


if __name__ == "__main__":
    sys.exit(main())
