"""系统层路由: 仪表盘统计/筛选项/后台任务/Provider/重建索引/SSE 日志流。

从 app.py 抽出。SSE 日志桥 (SINK → 订阅队列广播) 也在此模块,
app.py 的 startup/shutdown 调用 init_sse()/shutdown_sse() 完成绑定。
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from ...logging_setup import SINK
from ...store import index

router = APIRouter(prefix="/api", tags=["system"])


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


def init_sse() -> None:
    """Web 服务启动时调用: 绑定 event loop + 日志 SINK 桥接 (幂等, 防 reload 重复注册)。"""
    global _loop, _shutdown_event
    _loop = asyncio.get_event_loop()
    _shutdown_event = asyncio.Event()
    try:
        SINK.remove(_on_log)
    except (KeyError, ValueError):
        pass
    SINK.add(_on_log)


def shutdown_sse() -> None:
    """Web 服务关闭时调用: 通知所有 SSE 流退出, 解绑日志回调。"""
    if _shutdown_event is not None:
        _shutdown_event.set()
    try:
        SINK.remove(_on_log)
    except (KeyError, ValueError):
        pass


# ---------------------------------------------------------------- 路由


@router.get("/stats")
async def api_stats() -> dict:
    """仪表盘统计 (总数/打分进度/收藏/忽略 + 城市行业分布)。"""
    with index.session() as conn:
        f = index.facets(conn)
    return f


@router.get("/facets")
async def api_facets() -> dict:
    """筛选项与计数 (城市/行业下拉数据源)。"""
    with index.session() as conn:
        return index.facets(conn)


@router.get("/tasks")
async def api_tasks() -> dict:
    """查询后台任务状态。"""
    from .. import runtime

    with runtime._task_lock:
        return {"tasks": dict(runtime._running_tasks)}


@router.get("/providers")
async def api_providers() -> dict:
    """可用 AI provider 列表。"""
    try:
        from ...browser import available_providers

        return {"providers": available_providers()}
    except Exception:
        return {"providers": ["deepseek", "doubao", "tongyi", "kimi"]}


@router.post("/reindex")
async def api_reindex() -> dict:
    """重建索引 (后台执行)。"""
    from .. import runtime

    task_key = "reindex"
    runtime._run_in_thread(index.reindex, task_key)
    return {"task": task_key, "status": "started"}


@router.get("/logs/stream")
async def api_logs_stream():
    """SSE 实时日志流。"""
    assert _shutdown_event is not None

    async def event_stream():
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
