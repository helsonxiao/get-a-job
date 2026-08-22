"""FastAPI Web 应用 —— 组装层。

本模块只负责: app 创建/路由挂载/生命周期/静态资源/启动入口。
具体路由按功能域拆在 routes/ 下 (见 routes/__init__.py 模块地图):
  jobs → /api/jobs/**        companies → /api/companies/**
  observatory → /api/observatory/**   scoring → /api/rules|scoring-config|score-all
  config → /api/config/**    profile → /api/profile/**
  resume → /api/resume       system → /api/stats|facets|tasks|providers|reindex|logs/stream

前端为 Alpine.js 组件岛架构 (ADR-001):
  static/core/   共享层 (store/icons)
  static/views/  视图岛 (jobs/guide/config/resume/observatory)
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .. import config as cfg
from ..logging_setup import setup
from . import routes
from .runtime import log

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="坑位图鉴", docs_url="/api/docs")

for _router in routes.ALL_ROUTERS:
    app.include_router(_router)


@app.on_event("startup")
async def _startup() -> None:
    setup()
    cfg.ensure_dirs()
    routes.system.init_sse()
    log.info("Web 服务启动")


@app.on_event("shutdown")
async def _shutdown() -> None:
    routes.system.shutdown_sse()
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
