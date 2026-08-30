"""报告数据包路由: 面向不开源报告生成器的单一只读端点。

GET /api/report/bundle → schema v1.0 聚合数据包 (口径元数据 + 质量基线 +
市场聚合 + 焦点行业), 字段契约见 store/reportbundle.py。
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from ...store import index, reportbundle

router = APIRouter(prefix="/api/report", tags=["report"])


@router.get("/bundle")
async def api_report_bundle(
    top_industries: int = Query(12, ge=1, le=40),
) -> dict:
    """报告数据包: 单次调用返回生成报告所需的全部聚合数据。"""
    with index.session() as conn:
        return reportbundle.build_report_bundle(conn, top_industries=top_industries)
