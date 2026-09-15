"""报告数据包路由: 面向不开源报告生成器的单一只读端点。

GET /api/report/bundle → schema v3.1 聚合数据包 (口径元数据 + 质量基线 +
市场聚合 + 焦点行业), 字段契约见 store/reportbundle.py。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ...store import index, reportbundle

router = APIRouter(prefix="/api/report", tags=["report"])


@router.get("/bundle")
async def api_report_bundle(
    top_industries: int = Query(12, ge=1, le=40),
    scope: str = Query("", description="来源筛选链接 (口径隔离)"),
    snapshot: str = Query("", description="历史快照引用: snapshot_id 或 period_month / period_quarter (scope 须同时给)"),
) -> dict:
    """报告数据包: 单次调用返回生成报告所需的全部聚合数据。

    scope: 来源筛选链接 —— 指定后全部聚合只统计该口径 (meta.scope 显式报数)。
    snapshot: 历史快照引用 —— 指定后岗位集换成该快照的 snapshot_members
    (图卡与快照口径一致), 且不再顺手固化新快照。
    """
    with index.session() as conn:
        try:
            return reportbundle.build_report_bundle(
                conn, top_industries=top_industries, scope_link=scope or None,
                snapshot_ref=snapshot or None,
            )
        except reportbundle.SnapshotRefError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
