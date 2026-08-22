"""主简历模块路由: 主简历 (master.md) 读取与保存。

从 app.py 抽出。按职位定制生成简历的触发入口在 jobs.py (POST /api/jobs/{id}/resume)。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from ...store import repo
from .. import runtime

router = APIRouter(prefix="/api/resume", tags=["resume"])


@router.get("")
async def api_resume_get() -> dict:
    """返回主简历内容 (Markdown)。"""
    path = repo.master_resume_path()
    content = ""
    if path.exists():
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            runtime.log.warning(f"读取主简历失败: {exc}")
    return {
        "content": content,
        "path": str(path),
        "exists": bool(content.strip()),
        "size": len(content),
    }


@router.put("")
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
    runtime.log.info(f"主简历已保存: {path} ({len(content)} 字)")
    return {"ok": True, "path": str(path), "size": len(content)}
