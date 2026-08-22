"""公司图鉴路由: 从 app.py 抽出的 APIRouter。

只含公司级查询/写入/AI 评价路由。职位侧路由仍在 app.py。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query

from ...store import index, repo
from ...store import companies as cmp
from .. import runtime

router = APIRouter(prefix="/api/companies", tags=["companies"])


@router.get("/facets")
async def api_company_facets() -> dict:
    """图鉴顶栏概况 (已鉴定/已解锁/想去数) + 行业/阶段筛选选项。"""
    with index.session() as conn:
        return cmp.company_facets(conn)


@router.get("")
async def api_companies(
    q: str = Query("", description="公司名搜索"),
    industry: str = Query(""),
    stage: str = Query(""),
    city: str = Query(""),
    favorite: str = Query("all", description="all|only"),
    scored_only: bool = Query(False, description="只看有公司分的"),
    include_excluded: bool = Query(False, description="含匿名/串号公司"),
    sort: str = Query("score", description="score|jobs|salary|best|updated"),
    desc: bool = Query(True),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    """公司图鉴列表。三榜即时切换: sort=score(分数榜)/jobs(招聘力度榜)/salary(薪资榜)。"""
    with index.session() as conn:
        items = cmp.query_companies(
            conn, q=q, industry=industry, stage=stage, city=city,
            favorite=favorite, include_excluded=include_excluded,
            scored_only=scored_only, sort=sort, desc=desc,
            limit=limit, offset=offset,
        )
        total = cmp.count_companies(
            conn, q=q, industry=industry, stage=stage, city=city,
            favorite=favorite, include_excluded=include_excluded,
            scored_only=scored_only,
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/by-district")
async def api_companies_by_district(
    district: str = Query(..., description="区域名, 如 海淀区"),
    industry: str = Query("", description="可选: 叠加行业筛选"),
    limit: int = Query(100, le=500),
) -> dict:
    """按区域查公司 (市场观察台热力图"按公司"模式钻取)。"""
    if not district.strip():
        return {"items": [], "total": 0}
    with index.session() as conn:
        items = cmp.companies_by_district(
            conn, district=district.strip(), industry=industry, limit=limit
        )
    return {"items": items, "total": len(items), "district": district.strip()}


@router.get("/{brand_id}")
async def api_company_detail(brand_id: str) -> dict:
    """公司详情: 完整资料 + 聚合统计 + 名下岗位 (按分排序)。"""
    company = repo.load_company(brand_id)
    if not company:
        raise HTTPException(404, f"公司不存在: {brand_id}")
    with index.session() as conn:
        stats = conn.execute(
            "SELECT * FROM company_stats WHERE brand_id = ?", (brand_id,)
        ).fetchone()
        job_rows = conn.execute(
            "SELECT * FROM jobs WHERE company_id = ?"
            " ORDER BY favorite DESC, (best_total IS NULL), best_total DESC,"
            " last_seen DESC",
            (brand_id,),
        ).fetchall()
        dims_avg = cmp.company_dims_avg(conn, brand_id)
    jobs = [index._row_to_dict(r) for r in job_rows]
    return {
        "company": company.to_dict(),
        "stats": dict(stats) if stats else None,
        "dims_avg": dims_avg,
        "jobs": jobs,
        "job_count": len(jobs),
        "ai_scores": repo.list_company_ai_scores(brand_id),
    }


@router.post("/{brand_id}/favorite")
async def api_set_company_favorite(brand_id: str, body: dict = Body(...)) -> dict:
    """切换"想去"状态 (公司级收藏)。body: {"favorite": true/false}"""
    if repo.load_company(brand_id) is None:
        raise HTTPException(404, f"公司不存在: {brand_id}")
    fav = bool(body.get("favorite", False))
    final = repo.set_company_favorite(brand_id, fav)
    with index.session() as conn:
        index.set_company_favorite(conn, brand_id, fav)
    return {"ok": True, "brand_id": brand_id, "favorite": final}


@router.post("/{brand_id}/ai-analyze")
async def api_company_ai_analyze(
    brand_id: str,
    provider: str = Query("deepseek"),
) -> dict:
    """触发公司级 AI 评价 (纯手动, 后台执行, 通过 SSE 看进度)。

    与岗位打分对称: 全量落盘到 data/companies/<brand_id>/scores/,
    append-only 历史; 不做任何自动调度。
    """
    if repo.load_company(brand_id) is None:
        raise HTTPException(404, f"公司不存在: {brand_id}")

    from ...ai.company_runner import analyze_company

    task_key = f"company-analyze-{brand_id}-{provider}"
    with runtime._task_lock:
        if runtime._running_tasks.get(task_key, {}).get("status") == "running":
            return {"task": task_key, "status": "already_running"}

    runtime._run_in_thread(analyze_company, task_key, brand_id=brand_id, provider=provider)
    return {"task": task_key, "status": "started"}


@router.delete("/{brand_id}/ai-scores/{file_name}")
async def api_delete_company_ai_score(brand_id: str, file_name: str) -> dict:
    """删除指定的公司级 AI 评价文件。file_name 形如 ai_deepseek_20260814T123456.json"""
    if repo.load_company(brand_id) is None:
        raise HTTPException(404, f"公司不存在: {brand_id}")
    try:
        ok = repo.delete_company_ai_score(brand_id, file_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not ok:
        raise HTTPException(404, f"AI 评价文件不存在: {file_name}")
    return {"ok": True, "brand_id": brand_id, "file": file_name}


@router.post("/{brand_id}/ai-reparse")
async def api_company_ai_reparse(brand_id: str, body: dict = Body(...)) -> dict:
    """人工编辑公司级 AI 原始回复后重新解析并保存。

    body: {"raw_text": "...", "provider": "deepseek"}
    与岗位侧 /api/jobs/{id}/ai-reparse 对称: 解析成功则追加保存为新文件,
    并刷新 company_stats (因为 company_score_ai 可能进入图鉴分数)。
    解析失败返回 422, 不写入文件。
    """
    if repo.load_company(brand_id) is None:
        raise HTTPException(404, f"公司不存在: {brand_id}")
    raw_text = (body or {}).get("raw_text", "").strip()
    provider = (body or {}).get("provider", "unknown")
    if not raw_text:
        raise HTTPException(400, "raw_text 不能为空")

    from ...ai.company_runner import parse_company_response

    result = parse_company_response(raw_text, provider=provider, brand_id=brand_id)
    if not result:
        raise HTTPException(422, "解析失败: 无法从编辑后的文本中提取 JSON")
    result["raw_response"] = raw_text
    # 记录评价时的上下文指纹, 与自动评价流程保持一致
    try:
        from ...core.context import compute_context_fingerprint

        result["context_fingerprint"] = compute_context_fingerprint()
    except Exception:
        pass
    path = repo.save_company_ai_score(brand_id, provider, result)
    runtime.log.info(
        f"人工重新解析公司评价成功: {brand_id} ({provider}) ->"
        f" {result['worth_joining']} {result['company_score_ai']}/10 -> {path.name}"
    )
    # 刷新公司聚合统计: company_score_ai 可能影响图鉴分数 (头部加权无候选时回退)
    try:
        with index.session() as conn:
            index.refresh_company_stats(conn, [brand_id])
    except Exception as exc:
        runtime.log.warning(f"刷新公司统计失败: {exc}")
    return {"ok": True, "brand_id": brand_id, "result": result}
