"""AI 打分编排层。

把提示词构建 → 网页版大模型调用 → 响应解析 → 落盘 串起来。

核心函数:
  - score_with_ai():  单个职位 AI 打分
  - batch_score():    批量打分 (默认只打规则引擎标记需要 AI 介入的)
  - build_prompt():   只构建提示词不调用 (dry-run / 调试)

设计要点:
  - 即使解析失败, 也把原始回复存下来, 方便事后排查
  - 批量打分支持多 provider 轮询, 避免单个模型限频
  - 每次打分后更新 SQLite 索引, Web 端立即可见
"""

from __future__ import annotations

import time
from typing import Any, Iterator

from .. import config as cfg
from ..core.models import Company, Job
from ..core.profile import Profile, load_profile
from ..core.scoring import score_job as rule_score_job
from ..logging_setup import get_logger
from ..store import index, repo
from .parser import parse_ai_response
from .prompts import build_scoring_prompt, preview_prompt

log = get_logger("ai.runner")


# ---------------------------------------------------------------- 上下文加载


def _load_context(job_id: str, profile: Profile | None = None) -> tuple[Job, Company | None, Profile]:
    job = repo.load_job(job_id)
    if not job:
        raise FileNotFoundError(f"职位不存在: {job_id}")
    company = repo.load_company(job.company_id) if job.company_id else None
    profile = profile or load_profile()
    return job, company, profile


def _get_rule_result(job_id: str) -> dict | None:
    return repo.load_rule_score(job_id)


# ---------------------------------------------------------------- 构建提示词


def build_prompt(
    job_id: str,
    *,
    deep: bool = False,
    profile: Profile | None = None,
    resume: str | None = None,
) -> str:
    """构建 AI 打分提示词 (不调用大模型)。

    用于 dry-run 检查提示词质量, 或给用户看看会问什么。
    """
    job, company, prof = _load_context(job_id, profile)
    rule = _get_rule_result(job_id)
    triggers = (rule or {}).get("triggered_ai_rules") or []
    if resume is None:
        resume = repo.load_master_resume()
    return build_scoring_prompt(
        job, company, prof, rule, triggers, deep=deep, resume=resume
    )


# ---------------------------------------------------------------- 单个打分


def score_with_ai(
    job_id: str,
    provider: str = "deepseek",
    *,
    deep: bool = False,
    driver=None,
    profile: Profile | None = None,
    save_raw: bool = True,
    resume: str | None = None,
) -> dict:
    """用网页版大模型给单个职位打分。

    Args:
        job_id: 职位 ID
        provider: 大模型 provider (deepseek/doubao/tongyi/kimi)
        deep: 是否生成深度分析报告
        driver: 已有的 LLMDriver 实例 (复用), None 则自动创建
        profile: 用户画像, None 则自动加载
        save_raw: 是否保存原始回复 (即使解析失败也存)
        resume: 主简历全文; None 则自动从 data/resumes/master.md 加载

    Returns:
        dict: {
            "success": bool,
            "job_id": str,
            "provider": str,
            "result": dict | None,   # 规范化的打分结果
            "raw_response": str,     # 原始回复
            "error": str | None,
            "elapsed": float,
        }
    """
    started = time.time()
    job, company, prof = _load_context(job_id, profile)
    rule = _get_rule_result(job_id)
    triggers = (rule or {}).get("triggered_ai_rules") or []

    # 如果规则引擎没打过, 先跑一次规则打分
    if not rule:
        log.info(f"[{job_id}] 规则打分缺失, 先执行规则打分")
        result = rule_score_job(job, company, prof, user_requested_ai=deep)
        rule = result.to_dict()
        repo.save_rule_score(job_id, rule)

    if resume is None:
        resume = repo.load_master_resume()
    if resume:
        log.info(f"[{job_id}] 已融入主简历 ({len(resume)} 字) 到 AI 提示词")

    prompt = build_scoring_prompt(
        job, company, prof, rule, triggers, deep=deep, resume=resume
    )
    log.info(
        f"[{job_id}] AI 打分开始 | provider={provider} | prompt={len(prompt)} 字"
        f" | triggers={[t.get('code') for t in triggers]}"
    )

    own_driver = False
    raw_response = ""
    error = None
    result = None

    try:
        if driver is None:
            from ..browser import get_driver

            driver = get_driver(provider)
            own_driver = True

        raw_response = driver.ask(prompt)
        log.info(f"[{job_id}] 大模型回复 {len(raw_response)} 字")

        result = parse_ai_response(raw_response, provider=provider, job_id=job_id)
        if result:
            result["provider"] = provider
            result["deep_analysis_report"] = result.get("deep_analysis_report", "")
            result["raw_response"] = raw_response
            # 记录打分时的上下文指纹, 供日后判断"这个分是在什么预期下打的"
            from ..core.context import compute_context_fingerprint

            result["context_fingerprint"] = compute_context_fingerprint()
            repo.save_ai_score(job_id, provider, result)
            log.info(
                f"[{job_id}] AI 打分完成: {result['status']} "
                f"{result['total_score']}/10 ({result['recommendation']})"
            )
            # 更新索引
            _refresh_index(job_id)
        else:
            error = "无法从回复中解析出 JSON"
            log.warning(f"[{job_id}] {error}, 原始回复前 300 字: {raw_response[:300]}")
            if save_raw:
                _save_raw_fallback(job_id, provider, raw_response, prompt)

    except Exception as exc:
        error = str(exc)
        log.error(f"[{job_id}] AI 打分失败: {error}")
        if raw_response and save_raw:
            _save_raw_fallback(job_id, provider, raw_response, prompt)

    finally:
        if own_driver:
            try:
                from ..browser import close_driver

                close_driver(driver)
            except Exception:
                pass

    elapsed = round(time.time() - started, 1)
    return {
        "success": result is not None,
        "job_id": job_id,
        "provider": provider,
        "result": result,
        "raw_response": raw_response,
        "error": error,
        "elapsed": elapsed,
    }


# ---------------------------------------------------------------- 批量打分


def _iter_ai_candidates(
    *,
    only_triggered: bool = True,
    skip_providers: set[str] | None = None,
    limit: int | None = None,
) -> Iterator[tuple[Job, dict]]:
    """找出需要 AI 打分的职位。

    Args:
        only_triggered: True=只看规则引擎标记 ai_intervention_needed 的;
                       False=所有职位
        skip_providers: 已被这些 provider 打过分的跳过
        limit: 最多返回多少条
    """
    skip_providers = skip_providers or set()
    count = 0
    for job in repo.iter_jobs():
        rule = _get_rule_result(job.job_id)
        if not rule:
            continue
        if only_triggered and not rule.get("ai_intervention_needed"):
            continue
        # 已被指定 provider 打过的跳过
        if skip_providers:
            existing = {p.get("provider") for p in repo.list_ai_scores(job.job_id)}
            if skip_providers & existing:
                continue
        yield job, rule
        count += 1
        if limit and count >= limit:
            break


def batch_score(
    provider: str = "deepseek",
    *,
    only_triggered: bool = True,
    limit: int | None = None,
    deep: bool = False,
    profile: Profile | None = None,
    candidates: list[dict] | None = None,
) -> dict:
    """批量 AI 打分。

    默认只处理规则引擎标记需要 AI 介入的职位。
    复用同一个 driver 实例, 避免反复创建/关闭标签页。

    Args:
        candidates: backlog 挑选出的候选 (见 gaj/ai/backlog.py)。
            提供时忽略 only_triggered/limit, 按候选列表打分。

    Returns:
        dict: {total, success, failed, skipped, details: [...]}
    """
    prof = profile or load_profile()
    stats = {"total": 0, "success": 0, "failed": 0, "skipped": 0}
    details: list[dict] = []

    meta_by_id: dict[str, dict] = {}
    if candidates is not None:
        pairs: list[tuple[Job, dict]] = []
        for c in candidates:
            job = repo.load_job(c["job_id"])
            if not job:
                log.warning(f"[{c.get('job_id')}] backlog 候选不存在, 跳过")
                continue
            meta_by_id[job.job_id] = c
            pairs.append((job, _get_rule_result(job.job_id) or {}))
    else:
        pairs = list(_iter_ai_candidates(only_triggered=only_triggered, limit=limit))
    if not pairs:
        log.info("没有需要 AI 打分的职位")
        return {**stats, "details": details}

    log.info(f"批量 AI 打分: {len(pairs)} 个职位, provider={provider}")

    # 复用 driver
    driver = None
    try:
        from ..browser import get_driver

        driver = get_driver(provider)
    except Exception as exc:
        log.error(f"无法创建浏览器驱动: {exc}")
        for job, _ in pairs:
            stats["total"] += 1
            stats["failed"] += 1
            item = {"job_id": job.job_id, "success": False, "error": str(exc)}
            meta = meta_by_id.get(job.job_id)
            if meta:
                item["pool"] = meta.get("pool")
                item["reason"] = meta.get("reason")
            details.append(item)
        return {**stats, "details": details}

    try:
        for job, rule in pairs:
            stats["total"] += 1
            log.info(
                f"[{stats['total']}/{len(pairs)}] {job.title} @ {job.company_name}"
            )
            try:
                out = score_with_ai(
                    job.job_id, provider, deep=deep, driver=driver, profile=prof,
                )
                meta = meta_by_id.get(job.job_id)
                if meta:
                    out["pool"] = meta.get("pool")
                    out["reason"] = meta.get("reason")
                if out["success"]:
                    stats["success"] += 1
                else:
                    stats["failed"] += 1
                details.append(out)
            except Exception as exc:
                stats["failed"] += 1
                details.append({
                    "job_id": job.job_id, "success": False, "error": str(exc),
                })
                log.error(f"[{job.job_id}] 异常: {exc}")

            # 提问间隔
            delay = cfg.SETTINGS.ai.between_calls_min + (
                cfg.SETTINGS.ai.between_calls_max - cfg.SETTINGS.ai.between_calls_min
            ) * 0.5
            log.debug(f"提问间隔 {delay:.0f}s")
            time.sleep(delay)
    finally:
        try:
            from ..browser import close_driver

            close_driver(driver)
        except Exception:
            pass

    log.info(
        f"批量打分完成: 成功 {stats['success']} / 失败 {stats['failed']}"
        f" / 共 {stats['total']}"
    )
    return {**stats, "details": details}


# ---------------------------------------------------------------- 工具


def _refresh_index(job_id: str) -> None:
    """打分后更新单个职位在索引中的状态。"""
    try:
        job = repo.load_job(job_id)
        if not job:
            return
        company = repo.load_company(job.company_id) if job.company_id else None
        with index.session() as conn:
            index.upsert_job(conn, job, company)
    except Exception as exc:
        log.warning(f"更新索引失败: {exc}")


def _save_raw_fallback(job_id: str, provider: str, raw: str, prompt: str) -> None:
    """解析失败时把原始回复存下来, 方事后排查。"""
    import json
    from pathlib import Path

    d = repo.scores_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    path = d / f"ai_{provider}_raw_{stamp}.json"
    payload = {
        "job_id": job_id,
        "provider": provider,
        "status": "PARSE_FAILED",
        "raw_response": raw,
        "prompt": prompt[:5000],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"原始回复已保存: {path.name}")
