"""crawl 进度文件与互斥锁 (gaj/scraper/progress.py) 的单元测试。"""

from __future__ import annotations

import json
import os

import pytest

from gaj.scraper import progress


@pytest.fixture()
def paths(tmp_path, monkeypatch):
    """把进度文件/锁路径指向临时目录, 隔离真实 data/。"""
    prog = tmp_path / "crawl_progress.json"
    lock = tmp_path / "crawl.lock"
    monkeypatch.setattr(progress, "PROGRESS_PATH", prog)
    monkeypatch.setattr(progress, "LOCK_PATH", lock)
    return {"progress": prog, "lock": lock}


# ---------------------------------------------------------------- 进度文件


def test_write_read_roundtrip_and_merge(paths):
    progress.write_progress({"phase": "crawl", "url": "https://x", "pid": 1})
    progress.write_progress({"phase": "migrate", "current_page": 3})
    data = progress.read_progress()
    assert data["phase"] == "migrate"
    assert data["url"] == "https://x"  # 未覆盖的字段保留 (合并语义)
    assert data["pid"] == 1
    assert data["current_page"] == 3
    assert "updated_at" in data


def test_read_progress_missing_returns_empty(paths):
    assert progress.read_progress() == {}


# ---------------------------------------------------------------- pid


def test_pid_alive_current_process_true():
    assert progress.pid_alive(os.getpid()) is True


def test_pid_alive_impossible_pid_false():
    assert progress.pid_alive(4194304) is False  # macOS pid 上限远小于该值
    assert progress.pid_alive(0) is False


# ---------------------------------------------------------------- 互斥锁


def test_acquire_then_busy_then_release(paths):
    assert progress.acquire_lock("https://a") is None  # 成功
    busy = progress.acquire_lock("https://b")
    assert busy is not None and busy["alive"] is True
    assert busy["pid"] == os.getpid()
    progress.release_lock()
    assert progress.acquire_lock("https://b") is None  # 释放后可再获取


def test_stale_lock_auto_takeover(paths):
    # 模拟崩溃残留: 锁文件存在但 pid 已死
    paths["lock"].write_text(
        json.dumps({"pid": 4194304, "started_at": "2026-01-01T00:00:00",
                    "url": "https://old"}),
        encoding="utf-8",
    )
    assert progress.acquire_lock("https://new") is None  # 自动接管
    lock = progress.read_lock()
    assert lock["pid"] == os.getpid()
    assert lock["url"] == "https://new"


def test_release_skips_foreign_lock(paths):
    # 锁不是当前进程的 (他人接管后的新锁), release 不应误删
    progress.acquire_lock("https://a")
    paths["lock"].write_text(
        json.dumps({"pid": 4194304, "started_at": "x", "url": "https://a"}),
        encoding="utf-8",
    )
    progress.release_lock()
    assert paths["lock"].exists()


# ---------------------------------------------------------------- snapshot


def test_snapshot_running_and_done(paths):
    assert progress.acquire_lock("https://a") is None
    progress.write_progress({"phase": "crawl", "pid": os.getpid(),
                             "started_at": "2026-01-01T00:00:00",
                             "current_page": 2, "stats": {"pages": 2}})
    snap = progress.snapshot()
    assert snap["running"] is True
    assert snap["phase"] == "crawl"
    assert snap["current_page"] == 2

    progress.write_progress({"phase": "done"})
    snap = progress.snapshot()
    assert snap["done"] is True
    assert snap["running"] is False


def test_snapshot_dead_pid_not_running(paths):
    paths["lock"].write_text(
        json.dumps({"pid": 4194304, "started_at": "x", "url": "u"}),
        encoding="utf-8",
    )
    snap = progress.snapshot()
    assert snap["running"] is False
    assert snap["pid_alive"] is False


# ---------------------------------------------------------------- 历史归档


def test_archive_run_caps_and_survives_overwrite(paths):
    for i in range(7):
        progress.archive_run({"url": f"https://x/{i}", "phase": "done",
                              "result_summary": {"n": i}})
    progress.write_progress({"phase": "crawl", "url": "https://new-run"})  # 新一轮覆盖顶层
    snap = progress.snapshot()
    runs = snap["last_runs"]
    assert len(runs) == 5  # 历史环封顶
    assert runs[0]["url"] == "https://x/6"  # 最新在前
    assert runs[-1]["url"] == "https://x/2"
    assert snap["phase"] == "crawl"  # 新一轮顶层状态不受影响


# ---------------------------------------------------------------- --wait


def test_crawl_status_wait_returns_immediately_when_done(paths, capsys):
    progress.write_progress({"phase": "done", "result_summary": {"elapsed": 1}})
    from gaj.agent import cli as agent_cli

    rc = agent_cli.main(["crawl-status", "--wait", "30"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["done"] is True
    assert payload["data"]["wait"]["timed_out"] is False
    assert payload["data"]["wait"]["requested"] == 30


def test_crawl_status_wait_timeout_when_not_done(paths, capsys):
    progress.write_progress({"phase": "crawl", "pid": os.getpid(),
                             "started_at": "2026-01-01T00:00:00"})
    assert progress.acquire_lock("https://a") is None
    from gaj.agent import cli as agent_cli

    rc = agent_cli.main(["crawl-status", "--wait", "1"])  # 1s 超时
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    d = payload["data"]
    assert d["done"] is False
    assert d["wait"]["timed_out"] is True
    assert d["running"] is True
    progress.release_lock()


# ---------------------------------------------------------------- CLI 信封


def test_crawl_status_envelope(paths, capsys):
    progress.write_progress({"phase": "crawl", "pid": os.getpid(),
                             "started_at": "2026-01-01T00:00:00"})
    from gaj.agent import cli as agent_cli

    rc = agent_cli.main(["crawl-status"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["command"] == "crawl-status"
    for key in ("running", "phase", "pid", "done"):
        assert key in payload["data"]
