"""通义千问 (Tongyi) 网页版驱动。

URL: https://tongyi.aliyun.com/qianwen/
选择器基于 2026 年中的页面结构, 可能随网站更新失效。
"""

from __future__ import annotations

from .llm_driver import LLMDriver


class TongyiDriver(LLMDriver):
    name = "tongyi"
    chat_url = "https://tongyi.aliyun.com/qianwen/"
    input_selector = "textarea#baiduOrXunFeiInput, textarea[placeholder], div[contenteditable='true']"
    send_selector = "button.send-btn, div[role='button'].btn-send, .submit-btn"
    response_selector = ".message-content, .tongyi-message, [data-role='assistant']"
    clear_selector = "a[href='/qianwen/'], .new-chat, [aria-label='新建对话']"
    clear_button_text = "新建对话"
