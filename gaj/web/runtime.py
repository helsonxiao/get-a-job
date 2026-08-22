"""后台任务运行时基础设施: 供 app.py 和 routes/* 共享。

抽离原因: routes/* 模块的 ai-analyze 等路由需要 _run_in_thread/_task_lock/
_running_tasks, 若从 app.py 导入会循环引用 (app.py include_router routes)。
独立模块打破环, 职责清晰。
"""
from __future__ import annotations

import threading
from typing import Any

from ..logging_setup import get_logger

log = get_logger("web")

#: 全局后台任务状态: task_key -> {status, key, ...}
_running_tasks: dict[str, dict] = {}
_task_lock = threading.Lock()


def _run_in_thread(func, task_key: str, **kwargs) -> None:
    """在后台线程执行耗时任务, 状态记录到 _running_tasks。"""
    with _task_lock:
        _running_tasks[task_key] = {"status": "running", "key": task_key}

    def _wrapper():
        try:
            result = func(**kwargs)
            with _task_lock:
                _running_tasks[task_key] = {
                    "status": "done", "key": task_key, "result": _safe_serialize(result),
                }
        except Exception as exc:
            log.error(f"后台任务 {task_key} 失败: {exc}")
            with _task_lock:
                _running_tasks[task_key] = {
                    "status": "error", "key": task_key, "error": str(exc),
                }

    t = threading.Thread(target=_wrapper, daemon=True, name=f"gaj-{task_key}")
    t.start()


def _safe_serialize(obj: Any) -> Any:
    """把可能含 dataclass / Path 的对象转成 JSON 安全的结构。"""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return {k: _safe_serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe_serialize(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)):
        return obj
    return str(obj)
