"""规则打分模块路由: 规则目录/打分参数覆盖/配比预设/批量规则打分。

从 app.py 抽出。画像编辑见 profile.py, AI 规则矫正见 config.py。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query

from ...store import index
from .. import runtime

router = APIRouter(prefix="/api", tags=["scoring"])


@router.get("/rules")
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
    from ...core.profile import load_profile
    from ...core.scoring import build_rules_catalog
    from ...core.scoring_config import load_overrides

    catalog = build_rules_catalog(profile=load_profile())
    catalog["overrides"] = load_overrides().to_dict()
    return catalog


@router.put("/scoring-config")
async def api_scoring_config_save(body: dict = Body(...)) -> dict:
    """保存打分参数覆盖。

    body 是 {field_name: value} 字典, 字段名对应 ScoringOverrides 的 dataclass 字段:
      - reject_confidence_floor: float (0-1, 淘汰置信度下限)
      - f01_max ~ w05_max: float (各评分项的满分)

    值为 null 时清除该项覆盖, 回退到代码默认值。
    保存后需重新规则打分才生效。
    """
    from ...core.scoring_config import ScoringOverrides, load_overrides, save_overrides

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
    runtime.log.info(f"打分覆盖已保存: {sum(1 for v in merged.values() if v is not None)} 项生效")
    return {"ok": True, "overrides": new_ov.to_dict()}


@router.get("/scoring-config/presets")
async def api_scoring_config_presets() -> dict:
    """返回评分项配比预设方案, 供规则概览页一键套用。

    每套预设覆盖全部评分项, 保证各维度满分之和 = 10。
    """
    from ...core.scoring_config import SCORING_ITEM_PRESETS

    return {"presets": SCORING_ITEM_PRESETS}


@router.post("/score-all")
async def api_score_all(force: bool = Query(False)) -> dict:
    """批量规则打分 (后台执行)。"""
    from ...core.score_runner import score_all

    task_key = "score-all"
    runtime._run_in_thread(score_all, task_key, force=force)
    return {"task": task_key, "status": "started"}
