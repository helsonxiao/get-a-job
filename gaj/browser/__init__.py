"""网页版大模型 CDP 驱动。

通过 Chrome DevTools Protocol 驱动已登录的网页版大模型,
用于 AI 打分和简历生成。

使用方式:
    from gaj.browser import get_driver
    driver = get_driver("deepseek")  # 需要 Chrome CDP 已运行
    result = driver.ask("你好")
"""

from __future__ import annotations

from ..config import SETTINGS
from ..logging_setup import get_logger
from .cdp import CDPSession, ensure_chrome_running, create_session
from .llm_driver import LLMDriver
from .llm_driver_deepseek import DeepSeekDriver
from .llm_driver_doubao import DoubaoDriver
from .llm_driver_kimi import KimiDriver
from .llm_driver_tongyi import TongyiDriver

log = get_logger("browser")

_DRIVERS: dict[str, type[LLMDriver]] = {
    "deepseek": DeepSeekDriver,
    "doubao": DoubaoDriver,
    "tongyi": TongyiDriver,
    "kimi": KimiDriver,
}


def available_providers() -> list[str]:
    """返回所有已注册的 provider 名称。"""
    return list(_DRIVERS.keys())


def get_driver(
    provider: str,
    session: CDPSession | None = None,
    sid: str | None = None,
) -> LLMDriver:
    """获取网页版大模型驱动。

    Args:
        provider: provider 名称 (deepseek/doubao/tongyi/kimi)
        session: 已有的 CDPSession (复用), None 则自动创建
        sid: 已有的标签页 sessionId (复用), None 则自动创建

    调用此函数前, Chrome 必须已以 CDP 调试模式运行, 且用户已登录目标大模型。
    """
    cls = _DRIVERS.get(provider)
    if not cls:
        raise ValueError(
            f"未知 provider: {provider}。可选: {', '.join(_DRIVERS.keys())}"
        )

    own_session = False
    if session is None:
        session = create_session()
        own_session = True

    if sid is None:
        # 创建专用标签页 (后台打开, 不抢焦点)
        tid, sid = session.create_target(cls.chat_url, background=True)
        log.info(f"[{provider}] 创建标签页 (后台): {tid}")
        # 等待页面加载
        import time
        time.sleep(3)

    driver = cls(session, sid)

    # 如果是自己创建的 session, 关闭时需要清理
    if own_session:
        driver._own_session = session  # type: ignore

    return driver


def close_driver(driver: LLMDriver) -> None:
    """关闭驱动 (关闭标签页 + 断开 session)。"""
    session = getattr(driver, "_own_session", None)
    if session:
        try:
            session.close()
        except Exception:
            pass
