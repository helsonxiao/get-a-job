"""文件仓储 —— 真相源。

目录约定:
    data/jobs/<job_id>/
        job.json              归一化职位 (真相源)
        raw_list_item.json    列表 API 原始项
        raw_jd_dom.json       详情页原始提取
        jd.md                 清洗后的 JD 正文, 方便直接读
        scores/rule.json      规则打分
        scores/ai_<provider>_<时间戳>.json   每次 AI 打分独立成文件
    data/companies/<brand_id>/
        company.json
        intro.md
        raw_company_dom.json

设计取舍:
  - 所有写入走"临时文件 + 原子替换", 中途 Ctrl+C 不会留下半个 JSON。
  - AI 打分不覆盖, 每次新增一个文件 —— 你要能看到"上周元宝给 7.2, 今天
    DeepSeek 给 6.4"这种差异, 而不是只剩最后一次结果。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Iterator

from .. import config as cfg
from ..core.models import Company, Job
from ..logging_setup import get_logger

log = get_logger("repo")

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.\-~]")


def safe_dirname(raw_id: str) -> str:
    """把 ID 转成安全的目录名。BOSS 的 brandId 尾部常带 ~~, 保留即可。"""
    name = _UNSAFE_RE.sub("_", (raw_id or "").strip())
    return name[:120] or "unknown"


# ---------------------------------------------------------------- 原子读写


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(f"读取 {path} 失败: {exc}")
        return default


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text or "", encoding="utf-8")
    tmp.replace(path)


def read_text(path: Path, default: str = "") -> str:
    if not path.exists():
        return default
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return default


# ---------------------------------------------------------------- 职位


def job_dir(job_id: str) -> Path:
    return cfg.JOBS_DIR / safe_dirname(job_id)


def job_exists(job_id: str) -> bool:
    return (job_dir(job_id) / cfg.JOB_FILE).exists()


def save_job(
    job: Job,
    *,
    raw_list_item: dict | None = None,
    raw_jd_dom: dict | None = None,
) -> Path:
    d = job_dir(job.job_id)
    d.mkdir(parents=True, exist_ok=True)
    write_json(d / cfg.JOB_FILE, job.to_dict())
    if raw_list_item is not None:
        write_json(d / cfg.JOB_RAW_LIST_FILE, raw_list_item)
    if raw_jd_dom is not None:
        write_json(d / cfg.JOB_RAW_JD_FILE, raw_jd_dom)

    jd = job.jd or {}
    parts = [f"# {job.title}", "", f"> {job.company_name} · {job.city} · {job.salary.get('raw','')}", ""]
    for label, key in (
        ("岗位职责", "responsibility"),
        ("任职要求", "requirement"),
        ("加分项", "bonus"),
        ("公司提供", "benefit"),
    ):
        body = (jd.get(key) or "").strip()
        if body:
            parts += [f"## {label}", "", body, ""]
    if not jd.get("responsibility") and jd.get("full"):
        parts += ["## 原文", "", jd["full"], ""]
    write_text(d / cfg.JOB_JD_TEXT, "\n".join(parts))
    return d


def load_job(job_id: str) -> Job | None:
    data = read_json(job_dir(job_id) / cfg.JOB_FILE)
    return Job.from_dict(data) if data else None


def iter_jobs() -> Iterator[Job]:
    if not cfg.JOBS_DIR.exists():
        return
    for d in sorted(cfg.JOBS_DIR.iterdir()):
        if not d.is_dir():
            continue
        data = read_json(d / cfg.JOB_FILE)
        if data:
            yield Job.from_dict(data)


def all_job_ids() -> set[str]:
    """已采集职位 ID 集合, 用于翻页时快速去重。"""
    ids: set[str] = set()
    if not cfg.JOBS_DIR.exists():
        return ids
    for d in cfg.JOBS_DIR.iterdir():
        if d.is_dir() and (d / cfg.JOB_FILE).exists():
            data = read_json(d / cfg.JOB_FILE, {})
            jid = data.get("job_id") if isinstance(data, dict) else None
            ids.add(jid or d.name)
    return ids


def touch_job_seen(job_id: str, *, online: bool = True) -> None:
    """职位已存在时只更新 last_seen / online, 不重新抓详情页。"""
    path = job_dir(job_id) / cfg.JOB_FILE
    data = read_json(path)
    if not data:
        return
    data["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    data["online"] = online
    write_json(path, data)


def set_job_favorite(job_id: str, favorite: bool) -> bool:
    """标记/取消收藏。返回最终状态。"""
    path = job_dir(job_id) / cfg.JOB_FILE
    data = read_json(path)
    if not data:
        return False
    data["favorite"] = bool(favorite)
    data["favorited_at"] = time.strftime("%Y-%m-%dT%H:%M:%S") if favorite else ""
    write_json(path, data)
    return data["favorite"]


def set_job_ignored(job_id: str, ignored: bool) -> bool:
    """标记/取消忽略。忽略后在列表默认不展示。返回最终状态。"""
    path = job_dir(job_id) / cfg.JOB_FILE
    data = read_json(path)
    if not data:
        return False
    data["ignored"] = bool(ignored)
    write_json(path, data)
    return data["ignored"]


def set_manual_override(
    job_id: str, total: float | None, note: str
) -> dict | None:
    """设置/清除人工调分覆盖。

    total=None 表示清除人工调分。
    返回写入的 manual_override dict (清除时返回 {})。
    """
    path = job_dir(job_id) / cfg.JOB_FILE
    data = read_json(path)
    if not data:
        return None
    if total is None:
        data["manual_override"] = {}
    else:
        data["manual_override"] = {
            "total": float(total),
            "note": note or "",
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    write_json(path, data)
    return data["manual_override"]


def delete_job(job_id: str) -> bool:
    """删除整个职位目录 (含打分)。返回是否删除成功。"""
    d = job_dir(job_id)
    if not d.exists():
        return False
    shutil.rmtree(d)
    log.info(f"已删除职位目录: {d}")
    return True


def delete_ai_score(job_id: str, file_name: str) -> bool:
    """删除指定 AI 打分文件。file_name 必须形如 ai_*.json, 防路径穿越。"""
    if not file_name.startswith("ai_") or not file_name.endswith(".json"):
        raise ValueError(f"非法的 AI 打分文件名: {file_name}")
    if "/" in file_name or "\\" in file_name or ".." in file_name:
        raise ValueError(f"非法的 AI 打分文件名: {file_name}")
    path = scores_dir(job_id) / file_name
    if not path.exists():
        return False
    path.unlink()
    log.info(f"已删除 AI 打分: {path}")
    return True


# ---------------------------------------------------------------- 公司


def company_dir(brand_id: str) -> Path:
    return cfg.COMPANIES_DIR / safe_dirname(brand_id)


def save_company(company: Company, *, raw_dom: dict | None = None) -> Path:
    d = company_dir(company.brand_id)
    d.mkdir(parents=True, exist_ok=True)
    write_json(d / cfg.COMPANY_FILE, company.to_dict())
    if raw_dom is not None:
        write_json(d / cfg.COMPANY_RAW_FILE, raw_dom)
    if company.intro:
        write_text(d / cfg.COMPANY_INTRO_FILE, company.intro)
    return d


def load_company(brand_id: str) -> Company | None:
    data = read_json(company_dir(brand_id) / cfg.COMPANY_FILE)
    return Company.from_dict(data) if data else None


def iter_companies() -> Iterator[Company]:
    if not cfg.COMPANIES_DIR.exists():
        return
    for d in sorted(cfg.COMPANIES_DIR.iterdir()):
        if not d.is_dir():
            continue
        data = read_json(d / cfg.COMPANY_FILE)
        if data:
            yield Company.from_dict(data)


def company_is_fresh(brand_id: str, max_age_days: int) -> bool:
    """公司资料是否还在缓存有效期内 —— 避免为每个职位重复抓同一家公司。"""
    company = load_company(brand_id)
    if not company or not company.intro:
        return False
    try:
        updated = time.mktime(time.strptime(company.updated_at, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return False
    return (time.time() - updated) < max_age_days * 86400


# ---------------------------------------------------------------- 打分


def scores_dir(job_id: str) -> Path:
    return job_dir(job_id) / cfg.JOB_SCORES_DIR


def save_rule_score(job_id: str, payload: dict) -> Path:
    path = scores_dir(job_id) / cfg.RULE_SCORE_FILE
    write_json(path, payload)
    return path


def load_rule_score(job_id: str) -> dict | None:
    return read_json(scores_dir(job_id) / cfg.RULE_SCORE_FILE)


def save_ai_score(job_id: str, provider: str, payload: dict) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    path = scores_dir(job_id) / f"ai_{safe_dirname(provider)}_{stamp}.json"
    write_json(path, payload)
    return path


def list_ai_scores(job_id: str) -> list[dict]:
    """按时间倒序返回该职位的全部 AI 打分记录。"""
    d = scores_dir(job_id)
    if not d.exists():
        return []
    out: list[dict] = []
    for p in sorted(d.glob("ai_*.json"), reverse=True):
        data = read_json(p)
        if isinstance(data, dict):
            data.setdefault("_file", p.name)
            out.append(data)
    return out


def latest_ai_score(job_id: str, provider: str | None = None) -> dict | None:
    for item in list_ai_scores(job_id):
        if provider is None or item.get("provider") == provider:
            return item
    return None


def score_summary(job_id: str) -> dict:
    """给列表页用的打分摘要: 有没有打过分、谁打的、什么时候。"""
    rule = load_rule_score(job_id)
    ai_list = list_ai_scores(job_id)
    providers: list[dict] = []
    seen: set[str] = set()
    for item in ai_list:
        prov = item.get("provider", "unknown")
        if prov in seen:
            continue
        seen.add(prov)
        providers.append(
            {
                "provider": prov,
                "model": item.get("model", ""),
                "total": item.get("total_score"),
                "recommendation": item.get("recommendation", ""),
                "at": item.get("created_at", ""),
            }
        )
    return {
        "has_rule": rule is not None,
        "rule_total": (rule or {}).get("total_score"),
        "rule_status": (rule or {}).get("status"),
        "ai_count": len(ai_list),
        "ai_providers": providers,
    }


# ---------------------------------------------------------------- 采集会话


def save_session(session: dict) -> Path:
    sid = session.get("session_id") or time.strftime("%Y%m%dT%H%M%S")
    path = cfg.SESSIONS_DIR / f"{safe_dirname(sid)}.json"
    write_json(path, session)
    return path


def list_sessions(limit: int = 50) -> list[dict]:
    if not cfg.SESSIONS_DIR.exists():
        return []
    out = []
    for p in sorted(cfg.SESSIONS_DIR.glob("*.json"), reverse=True)[:limit]:
        data = read_json(p)
        if data:
            out.append(data)
    return out


# ---------------------------------------------------------------- 简历


def master_resume_path() -> Path:
    return cfg.RESUMES_DIR / cfg.MASTER_RESUME


def load_master_resume() -> str:
    return read_text(master_resume_path())


def save_master_resume(text: str) -> Path:
    p = master_resume_path()
    write_text(p, text)
    return p


def save_tailored_resume(job_id: str, content: str, meta: dict) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    base = cfg.TAILORED_DIR / f"{safe_dirname(job_id)}_{stamp}"
    write_text(base.with_suffix(".md"), content)
    write_json(base.with_suffix(".json"), meta)
    return base.with_suffix(".md")


def list_tailored_resumes(job_id: str | None = None) -> list[dict]:
    if not cfg.TAILORED_DIR.exists():
        return []
    out = []
    pattern = f"{safe_dirname(job_id)}_*.md" if job_id else "*.md"
    for p in sorted(cfg.TAILORED_DIR.glob(pattern), reverse=True):
        meta = read_json(p.with_suffix(".json"), {}) or {}
        out.append(
            {
                "path": str(p),
                "name": p.name,
                "job_id": meta.get("job_id", p.stem.rsplit("_", 1)[0]),
                "created_at": meta.get("created_at", ""),
                "provider": meta.get("provider", ""),
            }
        )
    return out


# ---------------------------------------------------------------- 维护


def backup_dir(src: Path, label: str) -> Path | None:
    """归档一个目录, 用于迁移前留底。"""
    if not src.exists():
        return None
    dest = src.parent / f"{src.name}.backup-{label}"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    log.info(f"已备份 {src} -> {dest}")
    return dest
