"""Kimi (Moonshot) 网页版驱动。

URL: https://kimi.moonshot.cn/chat/
选择器基于 2026 年中的页面结构, 可能随网站更新失效。
"""

from __future__ import annotations

from .llm_driver import LLMDriver


class KimiDriver(LLMDriver):
    name = "kimi"
    chat_url = "https://kimi.moonshot.cn/chat/"
    input_selector = "textarea.chat-input, textarea[placeholder*='输入'], div[contenteditable='true']"
    send_selector = "button.send-button, div[role='button'].send-btn, .submit-btn"
    response_selector = ".markdown, .kimi-message, [data-role='assistant']"
    clear_selector = "a[href='/chat'], .new-chat-btn, [aria-label='新建对话']"
    clear_button_text = "新建对话"
