"""DeepSeek 网页版驱动。

URL: https://chat.deepseek.com/
选择器基于 2026 年中的页面结构, 可能随网站更新失效。
失效时请用浏览器 DevTools 检查实际 DOM 并更新类属性。
"""

from __future__ import annotations

from .llm_driver import LLMDriver


class DeepSeekDriver(LLMDriver):
    name = "deepseek"
    chat_url = "https://chat.deepseek.com/"
    # DeepSeek 的输入框 (2026-07 实测: textarea[name="search"], 无 id)
    input_selector = "textarea[name='search'], textarea#chat-input, textarea[placeholder], div[contenteditable='true']"
    # 发送按钮 (2026-07 实测: ds-button--primary--filled--circle, 无 aria-label)
    # 注意 disabled 态用 CSS class ds-button--disabled 表示, 不是 HTML disabled 属性
    send_selector = "div[role='button'].ds-button--primary.ds-button--filled.ds-button--circle, div[role='button'].ds-icon-btn, button[aria-label='发送'], div.send-btn"
    # 助手回复 —— ds-markdown + ds-assistant-message-main-content 是稳定类名
    response_selector = (
        ".ds-markdown.ds-assistant-message-main-content, "
        ".ds-message--bot .ds-markdown, .markdown-body"
    )
    # 新建对话 —— hash 类名 _5a8ac7a 不稳定, 优先用文本匹配 (见 new_conversation)
    clear_selector = "a[href='/'], .new-chat-btn, [aria-label='新建对话']"
    # DeepSeek 特有: 按文本找"开启新对话"按钮
    clear_button_text = "开启新对话"
