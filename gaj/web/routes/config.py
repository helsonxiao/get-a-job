"""规则配置矫正路由: AI 根据画像预期 + 市场数据给出配置调整建议。

流程: 纯手动触发 → 后台线程跑网页版大模型 → 结果落盘 (append-only) →
前端可视化 diff → 人工勾选复核 → 复用既有 /api/profile 与
/api/scoring-config 保存 → 回写 applied 标记。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query

from .. import runtime

router = APIRouter(prefix="/api/config", tags=["config"])


@router.post("/calibrate")
async def api_config_calibrate(
    provider: str = Query("deepseek"),
) -> dict:
    """触发一次 AI 规则矫正 (后台执行, 进度见 SSE 日志)。"""
    from ...ai.calibrator import run_calibration

    task_key = f"config-calibrate-{provider}"
    with runtime._task_lock:
        if runtime._running_tasks.get(task_key, {}).get("status") == "running":
            return {"task": task_key, "status": "already_running"}

    runtime._run_in_thread(run_calibration, task_key, provider=provider)
    return {"task": task_key, "status": "started"}


@router.get("/calibrate/latest")
async def api_config_calibrate_latest() -> dict:
    """最新一次矫正结果 (含解析失败时的原始回复, 供人工排查)。"""
    from ...ai.calibrator import latest_calibration

    return {"result": latest_calibration()}


@router.get("/calibrate/dry-run")
async def api_config_calibrate_dry_run() -> dict:
    """返回将发给大模型的提示词 (调试用, 不调用大模型)。"""
    from ...ai.calibrator import build_calibration_prompt

    return {"prompt": build_calibration_prompt()}


@router.post("/calibrate/applied")
async def api_config_calibrate_applied(body: dict = Body(...)) -> dict:
    """人工复核应用后回写标记。body: {"file": "calibrate_deepseek_xxx.json", "fields": ["f01_max"]}"""
    from ...ai.calibrator import mark_applied

    file_name = str((body or {}).get("file", "")).strip()
    fields = [str(f) for f in (body or {}).get("fields", []) if f]
    if not file_name or not fields:
        raise HTTPException(400, "file 和 fields 不能为空")
    updated = mark_applied(file_name, fields)
    if updated is None:
        raise HTTPException(404, f"矫正记录不存在: {file_name}")
    return {"ok": True, "result": updated}
