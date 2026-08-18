"""通义千问 (Tongyi) 网页版驱动。

URL: https://tongyi.aliyun.com/qianwen/
选择器基于 2026 年中的页面结构, 可能随网站更新失效。
"""

from __future__ import annotations

import time

from .llm_driver import LLMDriver, log


class TongyiDriver(LLMDriver):
    name = "tongyi"
    chat_url = "https://tongyi.aliyun.com/qianwen/"
    input_selector = "textarea#baiduOrXunFeiInput, textarea[placeholder], div[contenteditable='true']"
    send_selector = "button.send-btn, div[role='button'].btn-send, .submit-btn"
    response_selector = ".message-content, .tongyi-message, [data-role='assistant']"
    clear_selector = "a[href='/qianwen/'], .new-chat, [aria-label='新建对话']"
    clear_button_text = "新建对话"

    def _inject_text(self, text: str) -> bool:
        """通义千问输入框注入 —— 优先用 CDP Input.insertText 派发可信输入。

        千问前端框架不响应 JS 合成的 input/change 事件, 导致原生 setter
        注入后输入框文本可见, 但框架内部状态未更新, 发送按钮保持灰色
        (disabled)。改用 CDP Input.insertText 注入文本 (isTrusted=true),
        框架的事件监听器无法区分它和真实键盘输入, 才能正确激活发送按钮。
        """
        sel = self._js_str(self.input_selector)

        # 1. 聚焦输入框并清空已有内容 (避免历史残留干扰)
        focused = self.session.eval_js(f"""
        (function() {{
            var ta = document.querySelector({sel});
            if (!ta) return false;
            ta.focus();
            if (ta.tagName === 'TEXTAREA' || ta.tagName === 'INPUT') {{
                var setter = Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype, 'value'
                ) || Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                );
                if (setter && setter.set) setter.set.call(ta, '');
                else ta.value = '';
                ta.dispatchEvent(new Event('input', {{bubbles: true}}));
            }} else {{
                ta.innerText = '';
            }}
            return true;
        }})()
        """, self.sid)
        if not focused:
            log.warning(f"[tongyi] 输入框未找到 ({self.input_selector})")
            return False

        # 2. 用 CDP Input.insertText 注入文本 —— 浏览器层面的可信输入事件,
        #    任何框架的事件监听器都会正确识别 (无法和真实键盘输入区分)
        try:
            self.session.send(
                "Input.insertText",
                {"text": text},
                sid=self.sid,
            )
            # 给框架一点时间处理 input 事件并更新发送按钮状态
            time.sleep(0.5)
            log.debug(f"[tongyi] CDP Input.insertText 注入成功 ({len(text)} 字)")
            return True
        except Exception as e:
            log.warning(f"[tongyi] CDP Input.insertText 失败: {e}, 回退到 JS 注入")
            return super()._inject_text(text)
