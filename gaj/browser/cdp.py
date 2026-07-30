"""CDP 会话管理 —— 复用 boss_scraper.cdp_session 的成熟实现。

网页版大模型驱动需要一个已登录的 Chrome (CDP 调试模式)。
本模块负责:
  - 检测 Chrome 是否在 CDP 端口运行
  - 创建/复用 CDPSession
  - 管理一个专用标签页用于大模型对话
"""

from __future__ import annotations

from ..config import SETTINGS
from ..logging_setup import get_logger

log = get_logger("browser.cdp")

# 复用老爬虫的 CDP 实现 (已验证可用)
from boss_scraper.cdp_session import CDPSession  # noqa: E402,F401

_CDP_IMPORT_OK = True


def ensure_chrome_running(port: int | None = None) -> bool:
    """检查 Chrome 是否在 CDP 调试端口运行。未运行则提示用户。"""
    import requests

    port = port or SETTINGS.crawl.cdp_port
    try:
        resp = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=3)
        if resp.status_code == 200:
            ver = resp.json().get("Browser", "unknown")
            log.debug(f"Chrome CDP 就绪 (端口 {port}, {ver})")
            return True
    except Exception:
        pass
    log.error(
        f"Chrome CDP 未运行 (端口 {port})。请先启动:\n"
        f"  python3 -m gaj setup-chrome\n"
        f"然后在浏览器里登录你要用的大模型网页版 (DeepSeek/豆包/通义/Kimi)。"
    )
    return False


def create_session(port: int | None = None) -> CDPSession:
    """创建 CDP 会话。调用前应先 ensure_chrome_running。"""
    port = port or SETTINGS.crawl.cdp_port
    if not ensure_chrome_running(port):
        raise RuntimeError(
            f"Chrome CDP 未运行 (端口 {port})。请先执行: python3 -m gaj setup-chrome"
        )
    return CDPSession(cdp_port=port)


def create_chat_tab(
    session: CDPSession, url: str, background: bool = True
) -> tuple[str, str]:
    """创建一个专用标签页用于大模型对话, 返回 (targetId, sessionId)。

    默认 background=True 在后台打开, 不抢夺当前焦点。
    """
    return session.create_target(url, background=background)
