"""OpenAI 兼容 API 驱动 (通用版)。

通过 HTTP 调用任意 OpenAI 兼容的 chat/completions 端点 (DeepSeek / 通义 /
豆包 / Kimi / vLLM / Ollama 等), 不依赖 Chrome CDP 与网页登录态。

环境变量配置 (无则走对应字段默认值):
    GAJ_API_BASE_URL   API 根地址, 默认 https://api.deepseek.com/v1
    GAJ_API_KEY         API Key (必填)
    GAJ_API_MODEL       模型名, 默认 deepseek-chat
    GAJ_API_TIMEOUT     单次请求超时秒数, 默认 300
    GAJ_API_MAX_TOKENS  输出上限, 默认 4096

使用方式:
    from gaj.browser import get_driver
    driver = get_driver("api")
    result = driver.ask("你好")
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from ..logging_setup import get_logger

log = get_logger("browser.api")

# 环境变量 -> (字段名, 默认值)
_ENV_MAP = {
    "GAJ_API_BASE_URL": ("base_url", "https://api.deepseek.com/v1"),
    "GAJ_API_KEY": ("api_key", ""),
    "GAJ_API_MODEL": ("model", "deepseek-chat"),
    "GAJ_API_TIMEOUT": ("timeout", 300),
    "GAJ_API_MAX_TOKENS": ("max_tokens", 4096),
}


class APIDriver:
    """OpenAI 兼容 API 驱动。

    与网页版 LLMDriver 的差异: 无 CDP 会话, 无登录态, ask() 直接走 HTTP。
    __init__ 不接收 CDPSession — 保持与 browser.get_driver() 的注册接口兼容:
    get_driver 对 "api" provider 走独立分支, 不创建 Chrome 会话。
    """

    name = "api"

    def __init__(self, *args, **kwargs):
        self.base_url = os.environ.get("GAJ_API_BASE_URL", "https://api.deepseek.com/v1")
        self.api_key = os.environ.get("GAJ_API_KEY", "")
        self.model = os.environ.get("GAJ_API_MODEL", "deepseek-chat")
        try:
            self.timeout = float(os.environ.get("GAJ_API_TIMEOUT", "300"))
        except ValueError:
            self.timeout = 300.0
        try:
            self.max_tokens = int(os.environ.get("GAJ_API_MAX_TOKENS", "4096"))
        except ValueError:
            self.max_tokens = 4096

        if not self.api_key:
            raise ValueError(
                "API 驱动缺少 Key: 请设置环境变量 GAJ_API_KEY "
                "(OpenAI 兼容端点, 如 DeepSeek: https://api.deepseek.com)"
            )

    # ------------------------------------------------------------- 公共接口

    def ask(self, prompt: str) -> str:
        """向 OpenAI 兼容端点发送一条 prompt, 返回模型回复文本。"""
        url = self.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        started = time.time()
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(
                f"[api] HTTP {e.code} 来自 {self.base_url}: {detail}"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"[api] 网络错误: {e.reason}") from e

        try:
            data = json.loads(raw)
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise RuntimeError(f"[api] 无法解析响应: {raw[:300]}") from e

        if not text or not text.strip():
            # 推理模型可能把内容放进 reasoning_content, 逐级回退
            try:
                text = data["choices"][0]["message"].get("reasoning_content", "")
            except Exception:
                pass

        log.debug(f"[api] 回复 {len(text)} 字符, 耗时 {time.time() - started:.1f}s")
        return text