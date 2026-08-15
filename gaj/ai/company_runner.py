"""公司级 AI 评价编排层 (图鉴词条)。

与岗位 AI 打分对称的设计:
  - 提示词构建 → 网页版大模型调用 → 响应解析 → 落盘
  - 落盘路径 data/companies/<brand_id>/scores/ai_<provider>_<时间戳>.json
  - append-only: 每次评价追加一个新文件, 多 provider / 多次评价可对照
  - 写入 context_fingerprint: 记录"这份评价是在什么画像预期下做出的"
  - 即使解析失败也保存原始回复, 方便事后排查

与岗位打分的差异 (用户已拍板):
  - 纯手动触发, 不做任何自动调度, 不进 backlog, 不与岗位打分抢预算
"""

from __future__ import annotations

import re
import time

from .. import config as cfg
from ..core.models import Company, Job
from ..core.profile import Profile, load_profile
from ..logging_setup import get_logger
from ..store import repo
from .parser import extract_json
from .prompts import build_company_analysis_prompt

log = get_logger("ai.company_runner")

_VALID_URGENCY = {"急招", "常规", "缓慢"}
_VALID_WORTH = {"强烈推荐", "值得考虑", "需谨慎", "不建议"}

#: 网页版 DeepSeek 开联网搜索时会在正文里塞 [citation:5] 之类的引用标记
_CITATION_RE = re.compile(r"\[\s*citation\s*:\s*\d+\s*\]", re.IGNORECASE)


def _strip_citations(text: str) -> str:
    """剔除网页版大模型的引用标记, 让词条文本干净可读。"""
    if not text:
        return ""
    return re.sub(r"\s{2,}", " ", _CITATION_RE.sub("", text)).strip()


# ---------------------------------------------------------------- 上下文加载


def _load_company_jobs(brand_id: str) -> list[Job]:
    """找出该公司名下的全部岗位 (遍历文件真相源)。"""
    jobs: list[Job] = []
    for job in repo.iter_jobs():
        if job.company_id == brand_id:
            jobs.append(job)
    return jobs


def _build_job_context(jobs: list[Job]) -> dict[str, dict]:
    """为提示词收集每个岗位的规则分 / AI 分 / JD 文本。"""
    ctx: dict[str, dict] = {}
    for j in jobs:
        ctx[j.job_id] = {
            "rule": repo.load_rule_score(j.job_id),
            "ai_scores": repo.list_ai_scores(j.job_id),
            "jd_text": repo.read_text(repo.job_dir(j.job_id) / cfg.JOB_JD_TEXT),
        }
    return ctx


def build_company_prompt(
    brand_id: str,
    *,
    profile: Profile | None = None,
) -> str:
    """构建公司级评价提示词 (不调用大模型), 供 dry-run / 调试。"""
    company = repo.load_company(brand_id)
    if not company:
        raise FileNotFoundError(f"公司不存在: {brand_id}")
    jobs = _load_company_jobs(brand_id)
    return build_company_analysis_prompt(
        company, jobs, profile or load_profile(), _build_job_context(jobs)
    )


# ---------------------------------------------------------------- 解析


def _clamp(value, lo: float, hi: float, default: float = 0.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return round(max(lo, min(hi, v)), 2)


def _str_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return []


def normalize_company_analysis(raw: dict, provider: str, brand_id: str) -> dict:
    """规范化公司级评价, 保证落盘字段齐全。"""
    urgency = str(raw.get("hiring_urgency", "")).strip()
    if urgency not in _VALID_URGENCY:
        urgency = "常规"
    worth = str(raw.get("worth_joining", "")).strip()
    if worth not in _VALID_WORTH:
        worth = "值得考虑"
    return {
        "brand_id": brand_id,
        "provider": provider,
        "kind": "company_analysis",
        "company_score_ai": _clamp(raw.get("company_score_ai"), 0, 10),
        "business_analysis": _strip_citations(str(raw.get("business_analysis", "") or "")),
        "tech_stack_profile": _strip_citations(str(raw.get("tech_stack_profile", "") or "")),
        "hiring_urgency": urgency,
        "hiring_urgency_reason": _strip_citations(str(raw.get("hiring_urgency_reason", "") or "")),
        "worth_joining": worth,
        "worth_joining_reason": _strip_citations(str(raw.get("worth_joining_reason", "") or "")),
        "highlights": [_strip_citations(s) for s in _str_list(raw.get("highlights"))],
        "risks": [_strip_citations(s) for s in _str_list(raw.get("risks"))],
        "interview_strategy": [_strip_citations(s) for s in _str_list(raw.get("interview_strategy"))],
        "business_keywords": _str_list(raw.get("business_keywords")),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def parse_company_response(raw_text: str, provider: str, brand_id: str = "") -> dict | None:
    """从大模型回复中提取并规范化公司级评价。失败返回 None。"""
    obj = extract_json(raw_text)
    if not obj:
        return None
    # 最低质量门槛: 词条正文都没有就当解析失败
    if not str(obj.get("business_analysis", "") or "").strip():
        return None
    return normalize_company_analysis(obj, provider=provider, brand_id=brand_id)


# ---------------------------------------------------------------- 执行


def analyze_company(
    brand_id: str,
    provider: str = "deepseek",
    *,
    driver=None,
    profile: Profile | None = None,
    save_raw: bool = True,
) -> dict:
    """用网页版大模型对一家公司做评价 (图鉴词条)。纯手动触发。

    Returns:
        dict: {
            "success": bool,
            "brand_id": str,
            "provider": str,
            "result": dict | None,
            "raw_response": str,
            "error": str | None,
            "elapsed": float,
        }
    """
    started = time.time()
    company = repo.load_company(brand_id)
    if not company:
        raise FileNotFoundError(f"公司不存在: {brand_id}")
    jobs = _load_company_jobs(brand_id)
    if not jobs:
        raise ValueError(f"公司 {company.name or brand_id} 名下没有岗位, 无法评价")

    prof = profile or load_profile()
    prompt = build_company_analysis_prompt(
        company, jobs, prof, _build_job_context(jobs)
    )
    log.info(
        f"[company:{brand_id}] {company.name} AI 评价开始 | provider={provider}"
        f" | 岗位 {len(jobs)} 个 | prompt={len(prompt)} 字"
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
        log.info(f"[company:{brand_id}] 大模型回复 {len(raw_response)} 字")

        result = parse_company_response(raw_response, provider=provider, brand_id=brand_id)
        if result:
            result["raw_response"] = raw_response
            # 记录评价时的上下文指纹, 供日后判断"这份评价是在什么预期下做的"
            from ..core.context import compute_context_fingerprint

            result["context_fingerprint"] = compute_context_fingerprint()
            path = repo.save_company_ai_score(brand_id, provider, result)
            log.info(
                f"[company:{brand_id}] AI 评价完成: {result['worth_joining']}"
                f" {result['company_score_ai']}/10 -> {path.name}"
            )
        else:
            error = "无法从回复中解析出公司级评价 JSON"
            log.warning(f"[company:{brand_id}] {error}, 原始回复前 300 字: {raw_response[:300]}")
            if save_raw:
                _save_raw_fallback(brand_id, provider, raw_response, prompt)

    except Exception as exc:
        error = str(exc)
        log.error(f"[company:{brand_id}] AI 评价失败: {error}")
        if raw_response and save_raw:
            _save_raw_fallback(brand_id, provider, raw_response, prompt)

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
        "brand_id": brand_id,
        "provider": provider,
        "result": result,
        "raw_response": raw_response,
        "error": error,
        "elapsed": elapsed,
    }


def _save_raw_fallback(brand_id: str, provider: str, raw: str, prompt: str) -> None:
    """解析失败时把原始回复存下来, 方便事后排查。"""
    import json

    d = repo.company_scores_dir(brand_id)
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    path = d / f"ai_{provider}_raw_{stamp}.json"
    payload = {
        "brand_id": brand_id,
        "provider": provider,
        "kind": "company_analysis",
        "status": "PARSE_FAILED",
        "raw_response": raw,
        "prompt": prompt[:5000],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"原始回复已保存: {path.name}")
