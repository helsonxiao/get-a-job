"""简历针对性优化生成器。

基于用户的主简历和目标岗位 JD, 通过网页版大模型生成针对性优化的简历。
属于 AI 模块的薄封装 —— 复用 browser 驱动和 prompts, 但输出是 Markdown
而非 JSON, 落盘走 repo.save_tailored_resume。
"""

from __future__ import annotations

import time

from ..ai.prompts import build_resume_prompt
from ..core.models import Company, Job
from ..core.profile import Profile, load_profile
from ..logging_setup import get_logger
from ..store import repo

log = get_logger("resume")


def generate_resume(
    job_id: str,
    provider: str = "deepseek",
    *,
    style: str = "optimize",
    driver=None,
    profile: Profile | None = None,
) -> dict:
    """为目标岗位生成针对性优化的简历。

    Args:
        job_id: 目标职位 ID
        provider: 大模型 provider
        style: optimize=在原简历基础上优化; rewrite=完全重写
        driver: 已有驱动 (复用), None 则自动创建
        profile: 用户画像

    Returns:
        dict: {
            "success": bool,
            "job_id": str,
            "provider": str,
            "resume_path": str | None,
            "content": str,
            "error": str | None,
            "elapsed": float,
        }
    """
    started = time.time()
    job = repo.load_job(job_id)
    if not job:
        raise FileNotFoundError(f"职位不存在: {job_id}")

    company = repo.load_company(job.company_id) if job.company_id else None
    prof = profile or load_profile()
    master_resume = repo.load_master_resume()

    if not master_resume.strip():
        raise ValueError(
            f"主简历为空。请先创建: {repo.master_resume_path()}"
        )

    prompt = build_resume_prompt(job, company, master_resume, prof, style=style)
    log.info(
        f"[{job_id}] 简历生成开始 | provider={provider} | style={style}"
        f" | prompt={len(prompt)} 字"
    )

    own_driver = False
    content = ""
    error = None
    resume_path = None

    try:
        if driver is None:
            from ..browser import get_driver

            driver = get_driver(provider)
            own_driver = True

        content = driver.ask(prompt)
        log.info(f"[{job_id}] 简历生成完成, {len(content)} 字")

        # 大模型有时会在前面加一句"好的, 以下是..." —— 去掉
        content = _strip_preamble(content)

        meta = {
            "job_id": job_id,
            "provider": provider,
            "style": style,
            "job_title": job.title,
            "company_name": company.name if company else job.company_name,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        path = repo.save_tailored_resume(job_id, content, meta)
        resume_path = str(path)
        log.info(f"[{job_id}] 简历已保存: {path.name}")

    except Exception as exc:
        error = str(exc)
        log.error(f"[{job_id}] 简历生成失败: {error}")

    finally:
        if own_driver:
            try:
                from ..browser import close_driver

                close_driver(driver)
            except Exception:
                pass

    elapsed = round(time.time() - started, 1)
    return {
        "success": resume_path is not None,
        "job_id": job_id,
        "provider": provider,
        "resume_path": resume_path,
        "content": content,
        "error": error,
        "elapsed": elapsed,
    }


def _strip_preamble(text: str) -> str:
    """去掉大模型回复开头的寒暄, 找到简历正文起点。"""
    text = text.strip()
    # 如果第一行不是 Markdown 标题, 尝试找到第一个 # 开头的行
    if text.startswith("#"):
        return text
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith("#"):
            return "\n".join(lines[i:])
    return text
