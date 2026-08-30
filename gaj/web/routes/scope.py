"""来源口径管理 API —— 筛选链接列表 / 自定义命名。

数据源: source_links 表 (口径注册表) + jobs.source_link (岗位归属)。
口径隔离的查询与出报告见 gaj store/reportbundle.build_report_bundle(scope_link=)。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...store import index, reportbundle

router = APIRouter(prefix="/api/scope", tags=["scope"])


class RenameBody(BaseModel):
    link: str
    label: str


@router.get("/links")
async def list_scope_links() -> dict:
    """全部口径链接: 链接 / 自定义命名 / 岗位数; 附未分口径(历史数据)报数。"""
    with index.session() as conn:
        rows = conn.execute(
            "SELECT s.link, s.label, s.created_at, COUNT(j.job_id) AS jobs"
            " FROM source_links s LEFT JOIN jobs j ON j.source_link = s.link"
            " GROUP BY s.link ORDER BY jobs DESC"
        ).fetchall()
        unscoped = conn.execute(
            "SELECT COUNT(*) FROM jobs"
            " WHERE (ignored = 0 OR ignored IS NULL)"
            " AND (source_link IS NULL OR source_link = '')"
        ).fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE (ignored = 0 OR ignored IS NULL)"
        ).fetchone()[0]
    return {
        "links": [
            {"link": r["link"], "label": r["label"], "created_at": r["created_at"],
             "job_count": r["jobs"]}
            for r in rows
        ],
        "unscoped_job_count": unscoped,
        "total_job_count": total,
        "note": "未分口径 = 历史数据无 source_link, 生成报告时不参与任何单口径统计",
    }


@router.post("/rename")
async def rename_scope_link(body: RenameBody) -> dict:
    """给口径链接设置自定义命名 (报告标题将优先使用该命名)。"""
    if not body.link.strip():
        raise HTTPException(status_code=422, detail="link 不能为空")
    label = body.label.strip()
    if len(label) > 40:
        raise HTTPException(status_code=422, detail="命名过长 (≤40 字)")
    with index.session() as conn:
        cur = conn.execute(
            "UPDATE source_links SET label = ? WHERE link = ?", (label, body.link.strip())
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="口径链接不存在 (先生成一次该口径报告即可登记)")
        conn.commit()
    return {"ok": True, "link": body.link.strip(), "label": label}


@router.get("/preview/{link_path:path}")
async def scope_preview(link_path: str) -> dict:
    """口径预览: 给定链接, 返回该口径的岗位数与 bundle 摘要 (不落盘)。"""
    # link 经 query 参数传递更稳, 这里用 path 兼容; 前端拼接完整链接为查询参数
    raise HTTPException(status_code=400, detail="请使用 /api/scope/preview?link=...")


@router.get("/preview")
async def scope_preview_query(link: str) -> dict:
    """口径预览: 指定链接 → 该口径岗位数 + bundle 头部摘要 (标题命名预览用)。"""
    with index.session() as conn:
        bundle = reportbundle.build_report_bundle(conn, scope_link=link)
    meta = bundle["meta"]
    return {
        "link": link,
        "label": (meta.get("scope") or {}).get("scope_label", ""),
        "job_count": meta["job_count"],
        "company_count": meta["company_count"],
        "window": meta["window"],
        "unscoped_job_count": (meta.get("scope") or {}).get("unscoped_job_count"),
    }
