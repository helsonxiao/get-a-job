"""采集进度心跳与互斥锁。

解决两个 agent 场景的问题:

  1. 长采集 (几分钟~30 分钟) 期间没有任何进度落盘, agent 无法判断
     是否卡死。本模块在采集过程中把 phase / 当前页 / 实时统计原子地
     写进 ``data/crawl_progress.json``, 供 ``gaj agent crawl-status``
     低成本轮询 (无需 tail 日志)。

  2. 采集共享一个 CDP Chrome, 并发多开会触发反爬 (AGENT.md 红线)。
     ``data/crawl.lock`` 用 O_CREAT|O_EXCL 原子占位做互斥; 进程崩溃
     残留的 stale 锁通过 pid 存活检测自动接管。

两个文件都是纯派生数据, 删除无害。
"""

from __future__ import annotations

import json
import os
import time

from .. import config as cfg
from ..logging_setup import get_logger
from ..store import repo

log = get_logger("scraper.progress")

PROGRESS_PATH = cfg.DATA_ROOT / "crawl_progress.json"
LOCK_PATH = cfg.DATA_ROOT / "crawl.lock"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------- 进度文件


def write_progress(patch: dict, *, path=None) -> None:
    """合并写入进度文件 (原子写, 读侧不会看到半截 JSON)。"""
    p = path or PROGRESS_PATH
    data = repo.read_json(p, default={}) or {}
    data.update(patch)
    data["updated_at"] = _now()
    try:
        repo.write_json(p, data)
    except Exception as exc:  # 进度落盘失败绝不影响采集
        log.debug(f"写进度文件失败 (不影响采集): {exc}")


def read_progress(*, path=None) -> dict:
    return repo.read_json(path or PROGRESS_PATH, default={}) or {}


def clear_progress(*, path=None) -> None:
    p = path or PROGRESS_PATH
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


def archive_run(entry: dict, *, path=None, keep: int = 5) -> None:
    """把一次结束的采集 (done/error) 归档进进度文件的 last_runs 历史环。

    多口径串行采集时, 下一次采集启动会覆盖 phase/result_summary 等顶层
    字段, 但 last_runs 随合并写入一直保留 (最多 keep 条), 供事后核对
    每个口径的最终结果, 不用去翻日志。
    """
    runs = read_progress(path=path).get("last_runs") or []
    runs = ([entry] + runs)[:keep]
    write_progress({"last_runs": runs}, path=path)


# ---------------------------------------------------------------- pid 与锁


def pid_alive(pid: int) -> bool:
    """进程是否存活 (os.kill(pid, 0) 只做权限探测不发信号)。"""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但无权发信号, 视为活
    except OSError:
        return False


def read_lock(*, path=None) -> dict:
    return repo.read_json(path or LOCK_PATH, default={}) or {}


def acquire_lock(url: str, *, path=None) -> dict | None:
    """尝试获取采集互斥锁。

    Returns:
        None: 获取成功 (锁文件已创建, pid=当前进程)。
        dict: 获取失败, 返回当前持锁信息 (含 alive 字段)。
    """
    p = path or LOCK_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            lock = read_lock(path=p)
            if pid_alive(lock.get("pid", 0)):
                lock["alive"] = True
                return lock
            # stale 锁: 持有进程已死, 接管
            log.warning(f"发现 stale 采集锁 (pid={lock.get('pid')} 已不存在), 自动接管")
            try:
                p.unlink()
            except OSError:
                return {**lock, "alive": False}
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {"pid": os.getpid(), "started_at": _now(), "url": url},
                    ensure_ascii=False,
                )
            )
        return None


def release_lock(*, path=None) -> None:
    """释放锁 (仅当锁内 pid 是自己, 防止误删他人/接管后的新锁)。"""
    p = path or LOCK_PATH
    lock = read_lock(path=p)
    if lock and lock.get("pid") != os.getpid():
        log.debug("锁已被其他进程持有或接管, 跳过释放")
        return
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


def snapshot(*, progress_path=None, lock_path=None) -> dict:
    """合并进度 + 锁 + pid 存活性, 供 crawl-status / status 输出。"""
    prog = read_progress(path=progress_path)
    lock = read_lock(path=lock_path)
    pid = lock.get("pid") or prog.get("pid") or 0
    alive = pid_alive(pid)
    phase = prog.get("phase", "")
    running = bool(lock and alive and phase not in ("done", "error"))
    elapsed: float | None = None
    started = prog.get("started_at")
    if running and started:
        try:
            from datetime import datetime

            t0 = datetime.fromisoformat(started)
            elapsed = round((datetime.now() - t0).total_seconds(), 1)
        except ValueError:
            pass
    return {
        "running": running,
        "phase": phase,
        "pid": pid,
        "pid_alive": alive,
        "lock": lock or None,
        "current_page": prog.get("current_page"),
        "current": prog.get("current"),
        "stats": prog.get("stats"),
        "elapsed_seconds": elapsed,
        "started_at": started,
        "updated_at": prog.get("updated_at"),
        "done": phase in ("done", "error"),
        "error": prog.get("error"),
        "result_summary": prog.get("result_summary"),
        "last_runs": prog.get("last_runs") or [],
        "progress_file": str(progress_path or PROGRESS_PATH),
    }
