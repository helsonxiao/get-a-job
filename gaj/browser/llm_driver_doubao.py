"""豆包 (Doubao) 网页版驱动。

URL: https://www.doubao.com/chat/
选择器基于 2026 年中的页面结构, 可能随网站更新失效。
"""

from __future__ import annotations

from .llm_driver import LLMDriver


class DoubaoDriver(LLMDriver):
    name = "doubao"
    chat_url = "https://www.doubao.com/chat/"
    input_selector = "textarea[data-testid='chat_input'], textarea[placeholder*='输入'], div[contenteditable='true']"
    send_selector = "button[data-testid='send_button'], div[role='button'].send-btn"
    response_selector = "[data-testid='message_text_content'], .message-content, [data-role='assistant']"
    clear_selector = "a[href='/chat'], .new-conversation, [aria-label='新对话']"
    clear_button_text = "发起新对话"
