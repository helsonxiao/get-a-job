"""FastAPI Web 应用 —— Web 图鉴 + AI 打分控制台。

路由总览:
  GET  /                    首页 (单页应用)
  GET  /api/stats           仪表盘统计
  GET  /api/facets          筛选项与计数
  GET  /api/jobs            职位列表 (支持搜索/筛选/排序/分页)
  GET  /api/jobs/{id}       职位详情 (含公司 + 全部打分)
  POST /api/jobs/{id}/ai-score    触发 AI 打分
  POST /api/jobs/{id}/resume      触发简历生成
  POST /api/score-all       批量规则打分
  POST /api/reindex         重建索引
  GET  /api/companies       公司图鉴列表 (sort=score|jobs|salary 三榜)
  GET  /api/companies/facets      图鉴概况与筛选选项
  GET  /api/companies/{id}  公司详情 (资料 + 统计 + 名下岗位)
  POST /api/companies/{id}/favorite  公司级"想去"收藏
  POST /api/companies/{id}/ai-analyze  公司级 AI 评价 (纯手动触发)
  DELETE /api/companies/{id}/ai-scores/{file}  删除一条公司级 AI 评价
  POST /api/companies/{id}/ai-reparse  人工编辑公司级 AI 原始回复后重新解析
  GET  /api/logs/stream     SSE 实时日志流
  GET  /api/providers       可用 AI provider 列表
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import config as cfg
from ..logging_setup import SINK, get_logger, setup
from ..store import index, repo

from .runtime import log, _run_in_thread, _task_lock, _running_tasks, _safe_serialize

STATIC_DIR = Path(__file__).parent / "static"

# ---------------------------------------------------------------- SSE 日志桥

#: 每个 SSE 连接一个独立队列, 日志广播到所有连接
_subscribers: set[asyncio.Queue] = set()
_loop: asyncio.AbstractEventLoop | None = None
#: 关闭信号: set 后 SSE 流立即退出, 不再阻塞 uvicorn reload
_shutdown_event: asyncio.Event | None = None


def _on_log(payload: dict) -> None:
    """日志 SINK 回调: 把日志广播到所有 SSE 订阅者 (线程安全)。"""
    if _loop is None or not _subscribers:
        return
    try:
        _loop.call_soon_threadsafe(_broadcast, payload)
    except Exception:
        pass


def _broadcast(payload: dict) -> None:
    """在 event loop 中把日志推到所有订阅者队列。"""
    for q in _subscribers:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass  # 队列满了就丢, 不阻塞其他订阅者


def _subscribe() -> asyncio.Queue:
    """注册一个新的 SSE 订阅者, 返回其专属队列。"""
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    _subscribers.add(q)
    return q


def _unsubscribe(q: asyncio.Queue) -> None:
    """取消订阅。"""
    _subscribers.discard(q)


def _attach_log_bridge() -> None:
    """把日志 SINK 桥接到 SSE 广播 (幂等, 防止 reload 重复注册)。"""
    try:
        SINK.remove(_on_log)
    except (KeyError, ValueError):
        pass
    SINK.add(_on_log)


# ---------------------------------------------------------------- FastAPI

app = FastAPI(title="坑位图鉴", docs_url="/api/docs")

from .routes.observatory import router as _obs_router
from .routes.companies import router as _companies_router
from .routes.config import router as _config_router
app.include_router(_obs_router)
app.include_router(_companies_router)
app.include_router(_config_router)


@app.on_event("startup")
async def _startup() -> None:
    global _loop, _shutdown_event
    setup()
    cfg.ensure_dirs()
    _loop = asyncio.get_event_loop()
    _shutdown_event = asyncio.Event()
    _attach_log_bridge()
    log.info("Web 服务启动")


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _shutdown_event is not None:
        _shutdown_event.set()
    SINK.remove(_on_log)
    log.info("Web 服务关闭")


# ---------------------------------------------------------------- 页面


@app.get("/")
async def index_page() -> FileResponse:
    # no-cache 防止浏览器缓存旧版 HTML (热重载场景)
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------- API


@app.get("/api/stats")
async def api_stats() -> dict:
    with index.session() as conn:
        f = index.facets(conn)
    return f


@app.get("/api/facets")
async def api_facets() -> dict:
    with index.session() as conn:
        return index.facets(conn)


@app.get("/api/jobs")
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


@app.get("/api/jobs/{job_id}")
async def api_job_detail(job_id: str) -> dict:
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


@app.post("/api/jobs/{job_id}/ai-score")
async def api_ai_score(
    job_id: str,
    provider: str = Query("deepseek"),
    deep: bool = Query(False),
) -> dict:
    """触发单个职位 AI 打分 (后台执行, 通过 SSE 看进度)。"""
    if not repo.job_exists(job_id):
        raise HTTPException(404, f"职位不存在: {job_id}")

    from ..ai.runner import score_with_ai

    task_key = f"ai-score-{job_id}-{provider}"
    with _task_lock:
        if task_key in _running_tasks and _running_tasks[task_key]["status"] == "running":
            return {"task": task_key, "status": "already_running"}

    _run_in_thread(score_with_ai, task_key, job_id=job_id, provider=provider, deep=deep)
    return {"task": task_key, "status": "started"}


@app.post("/api/jobs/{job_id}/ai-reparse")
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

    from ..ai.parser import parse_ai_response

    result = parse_ai_response(raw_text, provider=provider, job_id=job_id)
    if not result:
        raise HTTPException(422, "解析失败: 无法从编辑后的文本中提取 JSON")
    result["provider"] = provider
    result["deep_analysis_report"] = result.get("deep_analysis_report", "")
    result["raw_response"] = raw_text
    repo.save_ai_score(job_id, provider, result)
    log.info(f"人工重新解析成功: {job_id} ({provider}) -> {result['status']} {result['total_score']}/10")
    # 刷新索引
    try:
        from ..ai.runner import _refresh_index
        _refresh_index(job_id)
    except Exception:
        pass
    return {"ok": True, "job_id": job_id, "result": result}


@app.post("/api/jobs/{job_id}/resume")
async def api_resume(
    job_id: str,
    provider: str = Query("deepseek"),
    style: str = Query("optimize"),
) -> dict:
    """触发简历生成 (后台执行)。"""
    if not repo.job_exists(job_id):
        raise HTTPException(404, f"职位不存在: {job_id}")

    from ..resume.generator import generate_resume

    task_key = f"resume-{job_id}-{provider}"
    _run_in_thread(
        generate_resume, task_key, job_id=job_id, provider=provider, style=style,
    )
    return {"task": task_key, "status": "started"}


@app.delete("/api/jobs/{job_id}")
async def api_delete_job(job_id: str) -> dict:
    """删除职位 (含打分文件 + 索引行)。同步执行。"""
    if not repo.job_exists(job_id):
        raise HTTPException(404, f"职位不存在: {job_id}")
    repo.delete_job(job_id)
    with index.session() as conn:
        index.delete_job_from_index(conn, job_id)
    log.info(f"职位已删除: {job_id}")
    return {"ok": True, "job_id": job_id}


@app.post("/api/jobs/batch-delete")
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
    log.info(f"批量删除完成: {len(deleted)} 成功, {len(failed)} 失败")
    return {"ok": True, "deleted": deleted, "failed": failed,
            "deleted_count": len(deleted)}


@app.post("/api/jobs/{job_id}/favorite")
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


@app.post("/api/jobs/{job_id}/ignore")
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


@app.post("/api/jobs/{job_id}/manual-override")
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
    log.info(f"人工调分已更新: {job_id} -> {total} ({note[:50]})")
    return {"ok": True, "job_id": job_id, "manual_override": override}


@app.delete("/api/jobs/{job_id}/ai-scores/{file_name}")
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


@app.post("/api/score-all")
async def api_score_all(force: bool = Query(False)) -> dict:
    """批量规则打分 (后台执行)。"""
    from ..core.score_runner import score_all

    task_key = "score-all"
    _run_in_thread(score_all, task_key, force=force)
    return {"task": task_key, "status": "started"}


@app.post("/api/reindex")
async def api_reindex() -> dict:
    """重建索引 (后台执行)。"""
    task_key = "reindex"
    _run_in_thread(index.reindex, task_key)
    return {"task": task_key, "status": "started"}


@app.get("/api/tasks")
async def api_tasks() -> dict:
    """查询后台任务状态。"""
    with _task_lock:
        return {"tasks": dict(_running_tasks)}


@app.get("/api/providers")
async def api_providers() -> dict:
    try:
        from ..browser import available_providers

        return {"providers": available_providers()}
    except Exception:
        return {"providers": ["deepseek", "doubao", "tongyi", "kimi"]}



# ---------------------------------------------------------------- 规则与画像配置


@app.get("/api/rules")
async def api_rules() -> dict:
    """返回规则目录 + 当前覆盖项, 供前端展示和编辑。

    每个评分项包含:
      - max: 当前生效满分 (用户覆盖值或代码默认值)
      - default_max: 代码默认满分
      - detail: 判定逻辑的人类可读描述

    每条硬规则额外包含:
      - threshold: {value, source, source_label, editable_in} 当前阈值和来源

    overrides: 当前保存的覆盖项字典 (None 表示用默认值)
    """
    from ..core.profile import load_profile
    from ..core.scoring import build_rules_catalog
    from ..core.scoring_config import load_overrides

    catalog = build_rules_catalog(profile=load_profile())
    catalog["overrides"] = load_overrides().to_dict()
    return catalog


@app.put("/api/scoring-config")
async def api_scoring_config_save(body: dict = Body(...)) -> dict:
    """保存打分参数覆盖。

    body 是 {field_name: value} 字典, 字段名对应 ScoringOverrides 的 dataclass 字段:
      - reject_confidence_floor: float (0-1, 淘汰置信度下限)
      - f01_max ~ w05_max: float (各评分项的满分)

    值为 null 时清除该项覆盖, 回退到代码默认值。
    保存后需重新规则打分才生效。
    """
    from ..core.scoring_config import ScoringOverrides, load_overrides, save_overrides

    old = load_overrides()
    # 合并: 未提交的字段保留原值
    known = {f for f in ScoringOverrides.__dataclass_fields__}
    merged = old.to_dict()
    for k, v in body.items():
        if k not in known:
            continue
        if v is None:
            merged[k] = None
        else:
            try:
                merged[k] = float(v)
            except (TypeError, ValueError):
                raise HTTPException(400, f"{k} 必须是数字或 null")
    # 校验 reject_confidence_floor 范围
    if merged.get("reject_confidence_floor") is not None:
        v = merged["reject_confidence_floor"]
        if v < 0 or v > 1:
            raise HTTPException(400, "reject_confidence_floor 应在 0-1 之间")
    # 校验 max_points 范围
    for k, v in merged.items():
        if k.endswith("_max") and v is not None and (v < 0 or v > 100):
            raise HTTPException(400, f"{k} 应在 0-100 之间")

    new_ov = ScoringOverrides(**merged)
    save_overrides(new_ov)
    log.info(f"打分覆盖已保存: {sum(1 for v in merged.values() if v is not None)} 项生效")
    return {"ok": True, "overrides": new_ov.to_dict()}


@app.get("/api/scoring-config/presets")
async def api_scoring_config_presets() -> dict:
    """返回评分项配比预设方案, 供规则概览页一键套用。

    每套预设覆盖全部评分项, 保证各维度满分之和 = 10。
    """
    from ..core.scoring_config import SCORING_ITEM_PRESETS

    return {"presets": SCORING_ITEM_PRESETS}


@app.get("/api/profile")
async def api_profile_get() -> dict:
    """返回当前画像 (解析后的字段) + 序列化模板元数据 (供前端构建表单)。"""
    from ..core.profile import _SERIALIZE_GROUPS, load_profile

    profile = load_profile()
    fields = profile.to_dict()
    # 去掉 raw 字段 (解析过程的原始键值对, 前端不需要)
    fields.pop("raw", None)
    return {
        "fields": fields,
        "groups": [
            {
                "title": gtitle,
                "fields": [
                    {"name": fname, "label": flabel, "type": ftype}
                    for fname, flabel, ftype in flist
                ],
            }
            for gtitle, flist in _SERIALIZE_GROUPS
        ],
        "weights": profile.normalized_weights,
    }


@app.put("/api/profile")
async def api_profile_save(body: dict = Body(...)) -> dict:
    """保存画像。前端提交字段字典, 后端序列化回 profile.md。

    保存后建议前端触发重新打分 (规则参数变了, 旧分数失效)。
    """
    from ..core.profile import load_profile, save_profile

    # 合并: 未提交的字段保留原值, 避免前端漏传导致丢数据
    old = load_profile().to_dict()
    old.pop("raw", None)
    merged = {**old, **body}
    save_profile(merged)

    # 验证: 重新解析确认写回成功
    new = load_profile()
    return {
        "ok": True,
        "fields": {k: v for k, v in new.to_dict().items() if k != "raw"},
        "weights": new.normalized_weights,
    }


@app.get("/api/profile/weight-presets")
async def api_profile_weight_presets() -> dict:
    """返回权重预设方案列表, 供画像编辑页一键切换。

    返回形如 {"平衡型(默认)": {"growth": 30, "finance": 30, "wlb": 30, "resource": 10}, ...}
    """
    from ..core.profile import WEIGHT_PRESETS

    return {"presets": WEIGHT_PRESETS}


# ---------------------------------------------------------------- 主简历


@app.get("/api/resume")
async def api_resume_get() -> dict:
    """返回主简历内容 (Markdown)。"""
    path = repo.master_resume_path()
    content = ""
    if path.exists():
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning(f"读取主简历失败: {exc}")
    return {
        "content": content,
        "path": str(path),
        "exists": bool(content.strip()),
        "size": len(content),
    }


@app.put("/api/resume")
async def api_resume_save(body: dict = Body(...)) -> dict:
    """保存主简历 (Markdown)。

    body: {"content": "...markdown..."}
    支持 .md 文件上传后前端读成文本传过来, 后端只认字符串。
    """
    content = body.get("content")
    if content is None or not isinstance(content, str):
        raise HTTPException(400, "content 必须是非空字符串")
    # 简单校验: 不接受超长内容 (防止误传二进制)
    if len(content) > 200_000:
        raise HTTPException(400, "简历内容过长 (>200KB), 请确认是 Markdown 文本")
    path = repo.save_master_resume(content)
    log.info(f"主简历已保存: {path} ({len(content)} 字)")
    return {"ok": True, "path": str(path), "size": len(content)}


@app.get("/api/logs/stream")
async def api_logs_stream():
    """SSE 实时日志流。"""

    async def event_stream():
        assert _shutdown_event is not None
        my_queue = _subscribe()
        # 先发一个连接确认
        yield f"data: {json.dumps({'type': 'connected'})}\n\n"
        try:
            while not _shutdown_event.is_set():
                try:
                    get_task = asyncio.ensure_future(my_queue.get())
                    shutdown_task = asyncio.ensure_future(_shutdown_event.wait())
                    done, pending = await asyncio.wait(
                        {get_task, shutdown_task},
                        timeout=30,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    # 取消未完成的任务, 避免泄漏
                    for t in pending:
                        t.cancel()
                    if get_task in done:
                        payload = get_task.result()
                        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    else:
                        if _shutdown_event.is_set():
                            break
                        # 心跳
                        yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
                except asyncio.CancelledError:
                    break
        finally:
            _unsubscribe(my_queue)
        # 发送关闭信号, 前端可据此重连
        yield f"data: {json.dumps({'type': 'shutdown'})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------- 入口


def run(host: str = "127.0.0.1", port: int = 8765, reload: bool = True) -> None:
    """启动 Web 服务。

    reload=True 时 (默认), Python 文件变更自动重启服务。
    前端 index.html 通过 FileResponse 实时读取, 改完刷新浏览器即可, 无需重启。
    """
    import uvicorn

    setup()
    log.info(f"启动 Web 服务: http://{host}:{port} (reload={reload})")
    if reload:
        # reload 模式下必须传 import string, 不能传 app 对象
        # timeout_graceful_shutdown=3: SSE 连接最长等 3s 后强制关闭, 避免 reload 卡死
        uvicorn.run(
            "gaj.web.app:app", host=host, port=port,
            log_level="info", reload=True, timeout_graceful_shutdown=3,
        )
    else:
        uvicorn.run(app, host=host, port=port, log_level="info")
