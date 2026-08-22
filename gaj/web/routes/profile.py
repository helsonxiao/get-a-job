"""画像模块路由: 画像读取/保存/权重预设。

从 app.py 抽出。画像文件 data/profile.md 是打分与 AI 分析的真相源。
"""
from __future__ import annotations

from fastapi import APIRouter, Body

router = APIRouter(prefix="/api/profile", tags=["profile"])


@router.get("")
async def api_profile_get() -> dict:
    """返回当前画像 (解析后的字段) + 序列化模板元数据 (供前端构建表单)。"""
    from ...core.profile import _SERIALIZE_GROUPS, load_profile

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


@router.put("")
async def api_profile_save(body: dict = Body(...)) -> dict:
    """保存画像。前端提交字段字典, 后端序列化回 profile.md。

    保存后建议前端触发重新打分 (规则参数变了, 旧分数失效)。
    """
    from ...core.profile import load_profile, save_profile

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


@router.get("/weight-presets")
async def api_profile_weight_presets() -> dict:
    """返回权重预设方案列表, 供画像编辑页一键切换。

    返回形如 {"平衡型(默认)": {"growth": 30, "finance": 30, "wlb": 30, "resource": 10}, ...}
    """
    from ...core.profile import WEIGHT_PRESETS

    return {"presets": WEIGHT_PRESETS}
