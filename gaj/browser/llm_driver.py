"""网页版大模型通用驱动。

通过 CDP 协议驱动已登录的网页版大模型 (DeepSeek/豆包/通义/Kimi):
  1. 导航到聊天页面, 检查登录态
  2. (可选) 新建对话, 避免上下文串味
  3. 往输入框注入 prompt
  4. 点击发送 (或按 Enter)
  5. 轮询 DOM 直到回复文本稳定 (连续 N 次无变化)
  6. 提取最终回复

选择器会随网站更新失效 —— 每个子类把选择器集中在类属性上,
失败时 log 警告, 不崩溃。这是网页版驱动的固有风险。
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from ..config import AIConfig, SETTINGS
from ..logging_setup import get_logger
from .cdp import CDPSession

log = get_logger("browser.llm")


class LLMDriver:
    """网页版大模型驱动基类。子类只需覆盖类属性 (选择器 + URL)。"""

    name: str = "base"
    chat_url: str = ""

    # 输入框: 优先 textarea, 也支持 contenteditable div
    input_selector: str = ""
    # 发送按钮 (为空则用 Enter 键)
    send_selector: str = ""
    # 最新一条助手回复的容器
    response_selector: str = ""
    # 新建对话按钮 (可选)
    clear_selector: str = ""
    # 新建对话按钮的文本 (用于按文本查找, 应对 hash 类名变化)
    clear_button_text: str = ""
    # 登录态检测 (默认用 input_selector)
    login_check_selector: str = ""

    def __init__(
        self,
        session: CDPSession,
        sid: str,
        config: AIConfig | None = None,
        tid: str | None = None,
    ):
        self.session = session
        self.sid = sid
        self.config = config or SETTINGS.ai
        self.tid = tid  # 本驱动标签页的 targetId (可为 None, 如复用外部标签页)
        self._ready = False
        # 焦点管理: 进入对话前持有焦点的标签页, 结束后恢复
        self.prev_focused_tid: str | None = None
        self._in_focus = False

    def new_conversation(self) -> None:
        """点击新建对话按钮。fresh_conversation=False 时跳过。

        查找策略:
          1. CSS 选择器 (clear_selector)
          2. 按文本内容查找 (clear_button_text) —— 应对 hash 类名变化
        """
        if not self.config.fresh_conversation:
            return

        # 策略 1: CSS 选择器
        if self.clear_selector:
            js = f"""
            (function() {{
                var btn = document.querySelector({self._js_str(self.clear_selector)});
                if (btn) {{ btn.click(); return true; }}
                return false;
            }})()
            """
            if self.session.eval_js(js, self.sid):
                log.debug(f"[{self.name}] 已新建对话 (CSS)")
                time.sleep(1.5)
                return

        # 策略 2: 按文本查找 —— DeepSeek 的"开启新对话"按钮用 hash 类名, 不稳定
        if self.clear_button_text:
            txt = self._js_str(self.clear_button_text)
            js = f"""
            (function() {{
                // 遍历所有可点击元素, 找包含目标文本的
                var target = {txt};
                var candidates = document.querySelectorAll(
                    'div[role="button"], button, a, div[tabindex]'
                );
                for (var i = 0; i < candidates.length; i++) {{
                    var el = candidates[i];
                    var text = (el.textContent || '').trim();
                    if (text === target || text.indexOf(target) >= 0) {{
                        el.click();
                        return true;
                    }}
                }}
                return false;
            }})()
            """
            if self.session.eval_js(js, self.sid):
                log.debug(f"[{self.name}] 已新建对话 (文本匹配)")
                time.sleep(1.5)
                return

        log.warning(f"[{self.name}] 新建对话按钮未找到")

    # ---------------------------------------------------------------- 登录态

    def ensure_logged_in(self) -> bool:
        """检查登录态。已登录则置 _ready=True 并返回 True。

        策略:
          1. 优先用 login_check_selector 探测登录态 (子类可覆盖)
          2. 否则用 input_selector 是否存在作为登录信号
          3. 若都不存在, 默认认为已登录 (信任用户在浏览器里已登录)
        """
        if self._ready:
            return True

        # 导航到聊天页 (子类 chat_url)
        if self.chat_url:
            self.session.navigate(self.chat_url, self.sid)
            time.sleep(2.0)

        # 探测选择器: 登录态检查 > 输入框
        probe = self.login_check_selector or self.input_selector
        if probe:
            js = f"""
            (function() {{
                var el = document.querySelector({self._js_str(probe)});
                return !!el;
            }})()
            """
            found = self.session.eval_js(js, self.sid)
            if found:
                self._ready = True
                log.info(f"[{self.name}] 登录态确认 OK")
                return True
            log.warning(f"[{self.name}] 登录态检查失败, 探测选择器: {probe}")
            return False

        # 没配置探测选择器, 默认信任
        self._ready = True
        return True

    # ---------------------------------------------------------------- 发送

    def send_prompt(self, prompt: str) -> None:
        """注入 prompt 到输入框并点击发送。

        发送后会验证输入框是否已清空 —— React 应用在成功提交后会清空
        textarea, 如果没清空说明发送失败, 会重试。
        """
        if not self._ready and not self.ensure_logged_in():
            raise RuntimeError(f"[{self.name}] 未登录, 无法发送 prompt")

        # 注入文本
        injected = self._inject_text(prompt)
        if not injected:
            raise RuntimeError(
                f"[{self.name}] 输入框注入失败 ({self.input_selector}), 选择器可能已失效"
            )

        # 发送前停顿 (模拟人类), 同时等 React 状态更新 (发送按钮 enable)
        pause = self._random(self.config.pre_send_pause_min, self.config.pre_send_pause_max)
        time.sleep(pause)

        # 尝试发送: 按钮点击 → Enter 键, 最多重试 3 轮
        for attempt in range(3):
            sent = self._click_send()
            if sent:
                log.info(f"[{self.name}] 发送按钮点击成功 (第 {attempt+1} 次)")
                break
            log.warning(f"[{self.name}] 发送按钮点击失败 (第 {attempt+1} 次), 尝试 Enter 键")
            self._press_enter()
            time.sleep(1.0)
            # 检查输入框是否已清空 (= 发送成功)
            if self._is_input_cleared():
                break
            # 重新注入文本 (Enter 可能把文本发出去了但按钮没响应, 也可能没发)
            # 如果没清空, 重新注入再试
            if attempt < 2:
                log.debug(f"[{self.name}] 输入框未清空, 重新注入文本重试")
                self._inject_text(prompt)
                time.sleep(0.5)

        # 最终验证: 等一下看输入框是否清空
        time.sleep(1.5)
        if not self._is_input_cleared():
            log.warning(
                f"[{self.name}] 发送后输入框未清空, prompt 可能未成功发送。"
                f"将继续等待回复 (如果 {self.config.generation_timeout}s 内无回复会超时)"
            )
        else:
            log.info(f"[{self.name}] prompt 已发送 ({len(prompt)} 字)")

    def _is_input_cleared(self) -> bool:
        """检查输入框是否已清空 (React 提交成功后会清空 textarea)。"""
        sel = self._js_str(self.input_selector)
        js = f"""
        (function() {{
            var ta = document.querySelector({sel});
            if (!ta) return true;  // 找不到输入框, 视为已清空
            if (ta.tagName === 'TEXTAREA' || ta.tagName === 'INPUT') {{
                return (ta.value || '').trim() === '';
            }}
            return (ta.innerText || '').trim() === '';
        }})()
        """
        try:
            return bool(self.session.eval_js(js, self.sid))
        except Exception:
            return False

    def _inject_text(self, text: str) -> bool:
        """往输入框注入文本。处理 textarea 和 contenteditable 两种情况。"""
        # 转义文本用于 JS 字符串
        escaped = self._js_str(text)
        js = f"""
        (function() {{
            var ta = document.querySelector({self._js_str(self.input_selector)});
            if (!ta) return false;

            // textarea / input: 用 native setter 触发 React onChange
            if (ta.tagName === 'TEXTAREA' || ta.tagName === 'INPUT') {{
                var setter = Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype, 'value'
                ) || Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                );
                if (setter && setter.set) {{
                    setter.set.call(ta, {escaped});
                }} else {{
                    ta.value = {escaped};
                }}
                ta.dispatchEvent(new Event('input', {{bubbles: true}}));
                ta.dispatchEvent(new Event('change', {{bubbles: true}}));
                return true;
            }}

            // contenteditable div
            ta.focus();
            ta.innerText = {escaped};
            ta.dispatchEvent(new InputEvent('input', {{bubbles: true, data: {escaped}}}));
            return true;
        }})()
        """
        return bool(self.session.eval_js(js, self.sid))

    def _click_send(self) -> bool:
        """点击发送按钮。先试配置的选择器, 再用启发式搜索找附近按钮。"""
        # 策略 1: 配置的选择器
        if self.send_selector:
            js = f"""
            (function() {{
                var btn = document.querySelector({self._js_str(self.send_selector)});
                if (!btn) return false;
                // disabled 检测: HTML disabled 属性 + CSS class (如 ds-button--disabled)
                var cls = (btn.className || '').toLowerCase();
                if (btn.disabled || btn.getAttribute('disabled') !== null) return false;
                if (cls.indexOf('disabled') >= 0) return false;
                btn.click();
                return true;
            }})()
            """
            if bool(self.session.eval_js(js, self.sid)):
                return True

        # 策略 2: 启发式 —— 在输入框附近找发送类按钮
        # 注意: 后台标签页 getBoundingClientRect 全返回 0, 坐标距离法失效,
        #       所以先试坐标法, 坐标法找不到时回退到 DOM 结构法 (textarea 同容器内的按钮)
        sel = self._js_str(self.input_selector)
        js = f"""
        (function() {{
            var ta = document.querySelector({sel});
            if (!ta) return false;
            var taRect = ta.getBoundingClientRect();
            var bgMode = (taRect.width === 0 && taRect.height === 0);  // 后台标签页

            // 查找输入框右侧/下方 200px 范围内的可点击元素
            var all = document.querySelectorAll(
                'button, div[role="button"], [class*="send"], [class*="submit"], ' +
                '[class*="button"], [aria-label*="发送"], [aria-label*="send"]'
            );
            var best = null;
            var bestDist = 999999;

            for (var i = 0; i < all.length; i++) {{
                var el = all[i];
                var elCls = (el.className || '').toLowerCase();
                if (el.disabled || el.getAttribute('disabled') !== null) continue;
                if (elCls.indexOf('disabled') >= 0) continue;  // CSS class disabled
                // 跳过明显不是发送按钮的 (如"登录"按钮)
                var txt = (el.textContent || '').trim();
                if (txt.length > 10) continue;

                if (bgMode) {{
                    // 后台标签页: 不用坐标, 按 DOM 距离 (DOM 层级距离)
                    // 只收 primary / circle / send 类按钮
                    if (elCls.indexOf('primary') < 0 && elCls.indexOf('circle') < 0 &&
                        elCls.indexOf('send') < 0) continue;
                    // 确保在输入框的附近 (向上找共同祖先, 最多 5 层)
                    var p = ta.parentElement;
                    var found = false;
                    for (var j = 0; j < 5; j++) {{
                        if (!p) break;
                        if (p.contains(el)) {{ found = true; break; }}
                        p = p.parentElement;
                    }}
                    if (found) {{ best = el; break; }}
                }} else {{
                    var rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    var dx = rect.x + rect.width/2 - (taRect.x + taRect.width);
                    var dy = rect.y + rect.height/2 - (taRect.y + taRect.height/2);
                    var dist = Math.sqrt(dx*dx + dy*dy);
                    if (dist < 150 && dist < bestDist) {{
                        bestDist = dist;
                        best = el;
                    }}
                }}
            }}

            if (best) {{
                best.click();
                return true;
            }}
            return false;
        }})()
        """
        return bool(self.session.eval_js(js, self.sid))

    def _press_enter(self) -> None:
        """通过 CDP Input.dispatchKeyEvent 发送可信 Enter 键。

        合成 KeyboardEvent (document.dispatchEvent) 缺少 isTrusted=true,
        React/Vue 的表单提交逻辑会忽略它。必须走 CDP 输入协议才能触发。
        """
        sel = self._js_str(self.input_selector)
        # 先 focus 输入框
        self.session.eval_js(
            f"(function(){{var ta=document.querySelector({sel});if(ta)ta.focus();}})()",
            self.sid,
        )
        # 用 CDP 派发可信的 keyDown + keyUp 事件
        for evt_type in ("keyDown", "keyUp"):
            self.session.send(
                "Input.dispatchKeyEvent",
                {
                    "type": evt_type,
                    "key": "Enter",
                    "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13,
                },
                sid=self.sid,
            )
        time.sleep(0.3)

    # ---------------------------------------------------------------- 标签页焦点管理

    def bring_to_front(self) -> bool:
        """把本驱动的标签页切到前台。

        后台标签页可能被浏览器节流 (渲染/定时器降速), 导致页面 JS 不执行、
        一直拿不到回复。切到前台可解除节流。
        """
        if not self.tid:
            return False
        try:
            self.session.activate_target(self.tid)
            return True
        except Exception as e:
            log.warning(f"[{self.name}] 激活标签页失败: {e}")
            return False

    def _enter_focus(self) -> None:
        """进入对话前的焦点处理 (tab_mode=foreground 时切前台)。

        先尽力记住当前持有焦点的标签页 (用户正在看的页面),
        再把聊天标签页激活到前台, 处理完成后由 _exit_focus 恢复。
        """
        if self.config.tab_mode != "foreground" or self._in_focus:
            return
        # 记住用户原来在看哪个标签页 (get_driver 创建标签页前可能已捕获)
        if self.prev_focused_tid is None:
            try:
                self.prev_focused_tid = self.session.find_focused_target(
                    exclude={self.tid} if self.tid else set()
                )
            except Exception as e:
                log.debug(f"[{self.name}] 记录原焦点标签页失败: {e}")
        if self.bring_to_front():
            self._in_focus = True
            log.info(f"[{self.name}] 标签页已切到前台")
            time.sleep(1.5)  # 给页面一点时间解除节流/恢复渲染

    def _exit_focus(self) -> None:
        """对话结束后恢复原来聚焦的标签页, 把焦点还给用户。"""
        if not self._in_focus:
            return
        self._in_focus = False
        tid = self.prev_focused_tid
        self.prev_focused_tid = None
        if not tid:
            return
        try:
            self.session.activate_target(tid)
            log.info(f"[{self.name}] 已切回原标签页: {tid}")
        except Exception as e:
            log.debug(f"[{self.name}] 切回原标签页失败 (可能已被关闭): {e}")

    def _rescue_no_response(self) -> None:
        """长时间无任何回复时的救援。

        实测后台标签页有概率一直拿不到响应, 手动切 tab 即恢复 ——
        这里自动完成同样的动作: 切到前台; 若发现输入框还没清空
        (说明 prompt 根本没发出去), 再补一次发送。
        """
        log.warning(
            f"[{self.name}] 等待 {self.config.no_response_watchdog:.0f}s 仍无回复, "
            f"启动救援: 切换标签页到前台"
        )
        self.bring_to_front()
        time.sleep(2.0)
        try:
            if not self._is_input_cleared():
                log.warning(f"[{self.name}] 输入框未清空, prompt 可能未发送, 重新尝试发送")
                if not self._click_send():
                    self._press_enter()
                time.sleep(1.5)
        except Exception as e:
            log.warning(f"[{self.name}] 救援重发失败: {e}")

    # ---------------------------------------------------------------- 轮询

    def wait_for_completion(self) -> str:
        """轮询回复区直到文本稳定。返回最终文本。"""
        if not self.response_selector:
            raise RuntimeError(f"[{self.name}] 未配置 response_selector")

        sel = self._js_str(self.response_selector)
        js = f"""
        (function() {{
            var nodes = document.querySelectorAll({sel});
            if (!nodes || nodes.length === 0) return '';
            // 取最后一条助手回复
            return nodes[nodes.length - 1].innerText || nodes[nodes.length - 1].textContent || '';
        }})()
        """

        last_hash = ""
        stable_count = 0
        start = time.time()
        last_text = ""
        no_response_logged = False
        progress_logged_at = 0.0
        rescued = False

        while True:
            elapsed = time.time() - start
            if elapsed > self.config.generation_timeout:
                log.warning(
                    f"[{self.name}] 生成超时 ({self.config.generation_timeout}s), "
                    f"返回当前文本 ({len(last_text)} 字)"
                )
                return last_text

            # 无响应看门狗: 一直拿不到回复时切前台救援 (后台节流/发送未生效)
            if (
                not last_text
                and not rescued
                and elapsed > self.config.no_response_watchdog
            ):
                rescued = True
                self._rescue_no_response()

            # 10 秒还没收到任何回复, 提示一下
            if not last_text and elapsed > 10 and not no_response_logged:
                log.warning(
                    f"[{self.name}] 已等待 {round(elapsed, 0)}s 仍未收到回复, "
                    f"继续轮询... (超时 {self.config.generation_timeout}s)"
                )
                no_response_logged = True

            # 收到回复后, 每 15 秒输出一次进度
            if last_text and elapsed - progress_logged_at > 15:
                log.info(
                    f"[{self.name}] 生成中... {len(last_text)} 字, "
                    f"已等待 {round(elapsed, 0)}s"
                )
                progress_logged_at = elapsed

            time.sleep(self.config.poll_interval)
            text = self.session.eval_js(js, self.sid, timeout=15) or ""
            text = text.strip()

            if text:
                last_text = text
                h = hashlib.md5(text.encode()).hexdigest()
                if h == last_hash:
                    stable_count += 1
                    if stable_count >= self.config.stable_rounds:
                        log.info(
                            f"[{self.name}] 生成完成 ({len(text)} 字, "
                            f"耗时 {round(elapsed, 1)}s)"
                        )
                        return text
                else:
                    stable_count = 0
                    last_hash = h

    # ---------------------------------------------------------------- 完整流程

    def ask(self, prompt: str) -> str:
        """完整流程: 登录检查 → (切前台) → 新建对话 → 发送 → 等待完成 → (切回)。

        tab_mode=foreground (默认) 时, 提问期间聊天标签页会被激活到前台,
        避免后台标签页被节流导致拿不到响应; 无论成功还是异常, 结束后都会
        尽力把焦点切回用户原来在看的标签页。
        """
        if not self._ready:
            if not self.ensure_logged_in():
                raise RuntimeError(
                    f"[{self.name}] 未登录。请在浏览器里登录 {self.chat_url} 后重试"
                )

        self._enter_focus()
        try:
            self.new_conversation()
            self.send_prompt(prompt)
            return self.wait_for_completion()
        finally:
            self._exit_focus()

    # ---------------------------------------------------------------- 工具

    @staticmethod
    def _js_str(s: str) -> str:
        """转义 Python 字符串为 JS 字符串字面量 (单引号包裹)。"""
        return "'" + s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"

    @staticmethod
    def _random(lo: float, hi: float) -> float:
        import random
        return random.uniform(lo, hi)
