"""职位模块路由: 列表/详情/收藏/忽略/人工调分/AI 打分触发/简历生成/删除。

从 app.py 抽出, 只含职位级 (jobs) 查询与写入; 公司级见 companies.py,
规则配置见 scoring.py, 主简历 (非按职位生成) 见 resume.py。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query

from ... import config as cfg
from ...store import index, repo
from .. import runtime

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("")
async def api_jobs(
    search: str = Query("", description="搜索关键词"),
    city: str = Query("", description="城市, 逗号分隔"),
    status: str = Query("", description="规则状态, 逗号分隔"),
    scored: str = Query("all", description="all|none|rule_only|ai|no_ai"),
    provider: str = Query("", description="AI provider, 逗号分隔"),
    salary_min: Optional[float] = Query(None, description="薪资上限最小值 (万)"),
    online: bool = Query(False, description="只看在线"),
    outsourcing: Optional[bool] = Query(None, description="外包过滤"),
    favorite: str = Query("all", description="all|only|exclude"),
    ignored: str = Query("exclude", description="exclude|all|only, 默认排除已忽略"),
    sort: str = Query("best_total"),
    desc: bool = Query(True),
    limit: int = Query(50, le=500),
    offset: int = Query(0, ge=0),
    industry: str = Query("", description="行业精确筛选 (市场观察台下钻)"),
    district: str = Query("", description="区域精确筛选 (市场观察台下钻)"),
    overtime: str = Query("", description="加班档位筛选 heavy|moderate|light|none|unknown"),
    skill: str = Query("", description="技能筛选 (skills LIKE)"),
    edu_level: Optional[int] = Query(None, description="学历等级 0-5"),
    exp_min: Optional[float] = Query(None, description="经验下限精确匹配"),
    company_id: str = Query("", description="公司 brand_id 精确筛选"),
    has_salary: bool = Query(False, description="只含有薪资样本的岗位 (薪资定价 tab 下钻口径)"),
) -> dict:
    """职位列表 (支持搜索/筛选/排序/分页 + 观察台下钻筛选)。"""
    with index.session() as conn:
        items = index.query_jobs(
            conn,
            search=search,
            cities=city.split(",") if city else [],
            statuses=status.split(",") if status else [],
            scored=scored,
            providers=provider.split(",") if provider else [],
            salary_min=salary_min,
            online_only=online,
            outsourcing=outsourcing,
            favorite=favorite,
            ignored=ignored,
            sort=sort,
            desc=desc,
            limit=limit,
            offset=offset,
            industry=industry,
            district=district,
            overtime=overtime,
            skill=skill,
            edu_level=edu_level,
            exp_min=exp_min,
            company_id=company_id,
            has_salary=has_salary,
        )
        total = index.count_jobs(
            conn,
            search=search,
            cities=city.split(",") if city else [],
            statuses=status.split(",") if status else [],
            scored=scored,
            providers=provider.split(",") if provider else [],
            salary_min=salary_min,
            online_only=online,
            outsourcing=outsourcing,
            favorite=favorite,
            ignored=ignored,
            industry=industry,
            district=district,
            overtime=overtime,
            skill=skill,
            edu_level=edu_level,
            exp_min=exp_min,
            company_id=company_id,
            has_salary=has_salary,
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/{job_id}")
async def api_job_detail(job_id: str) -> dict:
    """职位详情 (公司 + 规则分 + 全部 AI 打分 + JD 全文)。"""
    job = repo.load_job(job_id)
    if not job:
        raise HTTPException(404, f"职位不存在: {job_id}")
    company = repo.load_company(job.company_id) if job.company_id else None
    rule_score = repo.load_rule_score(job_id)
    ai_scores = repo.list_ai_scores(job_id)
    jd_text = repo.read_text(repo.job_dir(job_id) / cfg.JOB_JD_TEXT)
    job_dict = job.to_dict()
    # 补充索引派生字段 (文件真相源里没有, 详情页徽章要用)
    with index.session() as conn:
        row = conn.execute(
            "SELECT ai_stale, ai_stale_reason, ai_count, ai_needed"
            " FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    if row:
        job_dict["ai_stale"] = bool(row["ai_stale"])
        job_dict["ai_stale_reason"] = row["ai_stale_reason"] or ""
        job_dict["ai_count"] = row["ai_count"] or 0
        job_dict["ai_needed"] = bool(row["ai_needed"])
    return {
        "job": job_dict,
        "company": company.to_dict() if company else None,
        "rule_score": rule_score,
        "ai_scores": ai_scores,
        "jd_text": jd_text,
        "score_summary": repo.score_summary(job_id),
    }


@router.post("/{job_id}/ai-score")
async def api_ai_score(
    job_id: str,
    provider: str = Query("deepseek"),
    deep: bool = Query(False),
) -> dict:
    """触发单个职位 AI 打分 (后台执行, 通过 SSE 看进度)。"""
    if not repo.job_exists(job_id):
        raise HTTPException(404, f"职位不存在: {job_id}")

    from ...ai.runner import score_with_ai

    task_key = f"ai-score-{job_id}-{provider}"
    with runtime._task_lock:
        if task_key in runtime._running_tasks and runtime._running_tasks[task_key]["status"] == "running":
            return {"task": task_key, "status": "already_running"}

    runtime._run_in_thread(score_with_ai, task_key, job_id=job_id, provider=provider, deep=deep)
    return {"task": task_key, "status": "started"}


@router.post("/{job_id}/ai-reparse")
async def api_ai_reparse(job_id: str, body: dict = Body(...)) -> dict:
    """人工编辑 AI 原始回复后重新解析并保存。

    body: {"raw_text": "...", "provider": "deepseek"}
    """
    if not repo.job_exists(job_id):
        raise HTTPException(404, f"职位不存在: {job_id}")
    raw_text = (body or {}).get("raw_text", "").strip()
    provider = (body or {}).get("provider", "unknown")
    if not raw_text:
        raise HTTPException(400, "raw_text 不能为空")

    from ...ai.parser import parse_ai_response

    result = parse_ai_response(raw_text, provider=provider, job_id=job_id)
    if not result:
        raise HTTPException(422, "解析失败: 无法从编辑后的文本中提取 JSON")
    result["provider"] = provider
    result["deep_analysis_report"] = result.get("deep_analysis_report", "")
    result["raw_response"] = raw_text
    repo.save_ai_score(job_id, provider, result)
    runtime.log.info(f"人工重新解析成功: {job_id} ({provider}) -> {result['status']} {result['total_score']}/10")
    # 刷新索引
    try:
        from ...ai.runner import _refresh_index
        _refresh_index(job_id)
    except Exception:
        pass
    return {"ok": True, "job_id": job_id, "result": result}


@router.post("/{job_id}/resume")
async def api_resume(
    job_id: str,
    provider: str = Query("deepseek"),
    style: str = Query("optimize"),
) -> dict:
    """触发按职位定制简历生成 (后台执行)。"""
    if not repo.job_exists(job_id):
        raise HTTPException(404, f"职位不存在: {job_id}")

    from ...resume.generator import generate_resume

    task_key = f"resume-{job_id}-{provider}"
    runtime._run_in_thread(
        generate_resume, task_key, job_id=job_id, provider=provider, style=style,
    )
    return {"task": task_key, "status": "started"}


@router.delete("/{job_id}")
async def api_delete_job(job_id: str) -> dict:
    """删除职位 (含打分文件 + 索引行)。同步执行。"""
    if not repo.job_exists(job_id):
        raise HTTPException(404, f"职位不存在: {job_id}")
    repo.delete_job(job_id)
    with index.session() as conn:
        index.delete_job_from_index(conn, job_id)
    runtime.log.info(f"职位已删除: {job_id}")
    return {"ok": True, "job_id": job_id}


@router.post("/batch-delete")
async def api_batch_delete_jobs(body: dict = Body(...)) -> dict:
    """批量删除职位。body: {"ids": ["id1","id2",...]}"""
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "ids 必须是非空数组")
    deleted: list[str] = []
    failed: list[dict] = []
    with index.session() as conn:
        for jid in ids:
            try:
                if repo.job_exists(jid):
                    repo.delete_job(jid)
                index.delete_job_from_index(conn, jid)
                deleted.append(jid)
            except Exception as exc:
                failed.append({"job_id": jid, "error": str(exc)})
    runtime.log.info(f"批量删除完成: {len(deleted)} 成功, {len(failed)} 失败")
    return {"ok": True, "deleted": deleted, "failed": failed,
            "deleted_count": len(deleted)}


@router.post("/{job_id}/favorite")
async def api_set_favorite(job_id: str, body: dict = Body(...)) -> dict:
    """切换收藏状态。body: {"favorite": true/false}"""
    if not repo.job_exists(job_id):
        raise HTTPException(404, f"职位不存在: {job_id}")
    fav = bool(body.get("favorite", False))
    final = repo.set_job_favorite(job_id, fav)
    # 同步索引: 重新 upsert 即可
    with index.session() as conn:
        job = repo.load_job(job_id)
        if job:
            index.upsert_job(conn, job)
    return {"ok": True, "job_id": job_id, "favorite": final}


@router.post("/{job_id}/ignore")
async def api_set_ignore(job_id: str, body: dict = Body(...)) -> dict:
    """切换忽略状态。body: {"ignored": true/false}

    忽略的职位在列表默认不展示, 但仍可通过筛选器查看。
    """
    if not repo.job_exists(job_id):
        raise HTTPException(404, f"职位不存在: {job_id}")
    ign = bool(body.get("ignored", False))
    final = repo.set_job_ignored(job_id, ign)
    with index.session() as conn:
        job = repo.load_job(job_id)
        if job:
            index.upsert_job(conn, job)
    return {"ok": True, "job_id": job_id, "ignored": final}


@router.post("/{job_id}/manual-override")
async def api_set_manual_override(job_id: str, body: dict = Body(...)) -> dict:
    """人工调分覆盖。body: {"total": 75.0, "note": "条件太苛刻, 实际可接受"}

    total 为 null 时清除人工调分, 回退到 AI/规则分。
    note 用于记录调整原因, 后续简历生成可引用。
    """
    if not repo.job_exists(job_id):
        raise HTTPException(404, f"职位不存在: {job_id}")
    total = body.get("total")
    note = body.get("note", "") or ""
    if total is not None:
        try:
            total = float(total)
        except (TypeError, ValueError):
            raise HTTPException(400, "total 必须是数字或 null")
        if total < 0 or total > 100:
            raise HTTPException(400, "total 应在 0-100 之间")
    override = repo.set_manual_override(job_id, total, note)
    # 同步索引
    with index.session() as conn:
        index.update_manual_override(conn, job_id, total, note)
    runtime.log.info(f"人工调分已更新: {job_id} -> {total} ({note[:50]})")
    return {"ok": True, "job_id": job_id, "manual_override": override}


@router.delete("/{job_id}/ai-scores/{file_name}")
async def api_delete_ai_score(job_id: str, file_name: str) -> dict:
    """删除指定的 AI 打分文件。file_name 形如 ai_deepseek_20260731T123456.json"""
    if not repo.job_exists(job_id):
        raise HTTPException(404, f"职位不存在: {job_id}")
    try:
        ok = repo.delete_ai_score(job_id, file_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not ok:
        raise HTTPException(404, f"AI 打分文件不存在: {file_name}")
    with index.session() as conn:
        index.delete_score_from_index(conn, job_id, file_name)
    return {"ok": True, "job_id": job_id, "file": file_name}
