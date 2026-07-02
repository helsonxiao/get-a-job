"""
CDP 协议会话管理模块

封装 Chrome DevTools Protocol 的 WebSocket 通信，提供:
  - CDP 命令收发 (send)
  - JavaScript 执行 (eval_js)
  - 异步 JavaScript 执行 (eval_async_js) — 用于在页面上下文中执行 fetch
  - 人类行为模拟 (_human_simulate)
  - Target (标签页) 管理
"""

import json
import time
import random
import platform
import subprocess
import os

from .logger import get_logger

log = get_logger("cdp")

# 运行时依赖: 延迟导入
_requests = None
_websocket = None


def require_runtime_dependencies():
    """检查并加载 requests 和 websocket-client"""
    global _requests, _websocket
    missing = []
    if _requests is None:
        try:
            import requests as _r
            _requests = _r
        except ImportError:
            missing.append("requests")
    if _websocket is None:
        try:
            import websocket as _w
            _websocket = _w
        except ImportError:
            missing.append("websocket-client")
    if missing:
        log.error(f"缺少运行时依赖: {' '.join(missing)}")
        log.error(f"  pip install {' '.join(missing)}")
        return False
    return True


def get_requests():
    """获取 requests 模块 (需先调用 require_runtime_dependencies)"""
    return _requests


def get_websocket():
    """获取 websocket 模块"""
    return _websocket


def get_default_chrome_path():
    """跨平台检测 Chrome 可执行文件路径"""
    system = platform.system()
    if system == "Darwin":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if system == "Windows":
        import ntpath
        local = os.environ.get("LOCALAPPDATA")
        if local:
            p = ntpath.join(local, "Google", "Chrome", "Application", "chrome.exe")
            if os.path.exists(p):
                return p
        return "chrome.exe"
    for c in ["/usr/bin/google-chrome", "/usr/bin/chromium-browser", "/usr/bin/chromium"]:
        if os.path.exists(c):
            return c
    return "google-chrome"


DEFAULT_CDP_PORT = 9222
DEFAULT_CHROME_PATH = get_default_chrome_path()
DEFAULT_CDP_DATA_DIR = os.path.expanduser("~/.boss-zhipin-scraper/cdp-profile")


class CDPSession:
    """CDP WebSocket 会话

    通过 Chrome 的远程调试端口建立 WebSocket 连接，
    发送 CDP 命令并等待匹配的响应。
    """

    def __init__(self, cdp_port=DEFAULT_CDP_PORT):
        if not require_runtime_dependencies():
            raise RuntimeError("缺少 CDP 运行依赖 (requests, websocket-client)")
        self.cdp_port = cdp_port
        log.debug(f"正在连接 CDP (端口 {cdp_port})...")
        resp = _requests.get(f"http://127.0.0.1:{cdp_port}/json/version", timeout=10)
        ws_url = resp.json()["webSocketDebuggerUrl"]
        self.ws = _websocket.create_connection(ws_url, timeout=60)
        self.mid = 0
        log.debug("CDP WebSocket 已连接")

    def send(self, method, params=None, sid=None, timeout=30):
        """发送 CDP 命令并等待匹配的响应

        Args:
            method: CDP 方法名, 如 "Page.navigate"
            params: 参数字典
            sid: Target sessionId (用于子标签页)
            timeout: 超时秒数

        Returns:
            CDP 响应字典
        """
        self.mid += 1
        msg = {"id": self.mid, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        self.ws.send(json.dumps(msg, ensure_ascii=False))

        start_time = time.time()
        max_retries = 1000
        for attempt in range(max_retries):
            elapsed = time.time() - start_time
            if elapsed > timeout:
                raise TimeoutError(
                    f"CDP send({method}) 超时 ({timeout}s), "
                    f"已跳过 {attempt} 条不匹配消息"
                )
            try:
                raw = self.ws.recv()
            except _websocket.WebSocketTimeoutException:
                raise TimeoutError(f"CDP WebSocket recv 超时, method={method}")
            try:
                r = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            if r.get("id") == self.mid:
                if "error" in r:
                    err_msg = r["error"].get("message", str(r["error"]))
                    log.error(f"CDP 错误 ({method}): {err_msg}")
                    raise RuntimeError(f"CDP 错误 ({method}): {err_msg}")
                return r
            # 跳过不匹配的消息 (事件等)
            event_name = r.get("method", "unknown")
            log.debug(f"跳过不匹配消息 (id={r.get('id')}, event={event_name})")
        raise TimeoutError(
            f"CDP send({method}) 在 {max_retries} 条消息内未找到匹配响应"
        )

    def eval_js(self, js, sid, timeout=30):
        """在页面上下文中执行同步 JavaScript

        Args:
            js: JavaScript 代码字符串 (应返回值)
            sid: Target sessionId
            timeout: 超时秒数

        Returns:
            JS 执行结果 (Python 类型), 失败返回 None
        """
        r = self.send(
            "Runtime.evaluate",
            {"expression": js, "returnByValue": True},
            sid,
            timeout=timeout,
        )
        return self._extract_eval_result(r)

    def eval_async_js(self, js, sid, timeout=60):
        """在页面上下文中执行异步 JavaScript (Promise)

        用于在页面内执行 fetch() 等异步操作。

        Args:
            js: JavaScript 代码字符串 (应返回 Promise)
            sid: Target sessionId
            timeout: 超时秒数

        Returns:
            JS 执行结果 (Python 类型), 失败返回 None
        """
        r = self.send(
            "Runtime.evaluate",
            {
                "expression": js,
                "returnByValue": True,
                "awaitPromise": True,
            },
            sid,
            timeout=timeout,
        )
        return self._extract_eval_result(r)

    @staticmethod
    def _extract_eval_result(r):
        """从 CDP Runtime.evaluate 响应中提取结果"""
        result_obj = r.get("result", {}).get("result", {})
        if result_obj.get("subtype") == "error":
            desc = result_obj.get("description", "unknown JS error")
            log.error(f"JS eval error: {desc}")
            return None
        exception = r.get("result", {}).get("exceptionDetails")
        if exception:
            desc = exception.get("exception", {}).get(
                "description", exception.get("text", "unknown")
            )
            log.error(f"JS exception: {desc}")
            return None
        return result_obj.get("value", None)

    def navigate(self, url, sid, wait_sec=None):
        """导航到指定 URL 并等待加载

        Args:
            url: 目标 URL
            sid: Target sessionId
            wait_sec: 等待秒数, None 则随机 3-6 秒
        """
        if wait_sec is None:
            wait_sec = random.uniform(3, 6)
        # 净化 URL: 移除可能的反斜杠污染
        clean = url.replace("%5C", "").replace("%5c", "").replace("\\", "")
        if "/?" in clean:
            clean = clean.replace("/?", "?")
        if clean != url:
            log.debug(f"URL 净化: {url} → {clean}")
        log.debug(f"导航到: {clean}")
        self.send("Page.navigate", {"url": clean}, sid)
        time.sleep(wait_sec)

    def close(self):
        """关闭 WebSocket 连接"""
        try:
            self.ws.close()
            log.debug("CDP WebSocket 已关闭")
        except Exception:
            pass

    # ----------------------------------------------------------------
    # Target (标签页) 管理
    # ----------------------------------------------------------------
    def create_target(self, url="about:blank"):
        """创建新标签页, 返回 (targetId, sessionId)"""
        # 调试: 记录传给 Chrome 的 URL
        if url != "about:blank":
            log.debug(f"create_target URL: {url}")
            if "%5C" in url or "%5c" in url or "\\" in url:
                log.warning(f"create_target URL 包含反斜杠! URL={url}")
        r = self.send("Target.createTarget", {"url": url})
        tid = r["result"]["targetId"]
        r = self.send("Target.attachToTarget", {"targetId": tid, "flatten": True})
        sid = r["result"]["sessionId"]
        log.debug(f"已创建标签页: targetId={tid}, sessionId={sid}")
        return tid, sid

    def close_target(self, tid):
        """关闭标签页"""
        try:
            self.send("Target.closeTarget", {"targetId": tid})
            log.debug(f"已关闭标签页: {tid}")
        except Exception:
            pass


def human_simulate(ws, sid):
    """模拟人类阅读行为: 随机滚动 + 偶尔鼠标移动

    Args:
        ws: CDPSession 实例
        sid: Target sessionId
    """
    scroll_count = random.randint(3, 7)
    for i in range(scroll_count):
        if random.random() < 0.12:
            delta = -random.randint(80, 200)
        else:
            delta = random.randint(200, 600)
        ws.eval_js(f"window.scrollBy(0,{delta})", sid)
        if random.random() < 0.35:
            time.sleep(random.uniform(2.0, 5.0))
        else:
            time.sleep(random.uniform(0.8, 1.8))

    if random.random() < 0.5:
        ws.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseMoved",
                "x": random.randint(200, 800),
                "y": random.randint(200, 600),
            },
            sid,
        )
        time.sleep(random.uniform(0.5, 1.5))
