"""
Chrome 环境管理模块

功能:
  - 检测 CDP 调试端口是否就绪
  - 检查 BOSS直聘登录状态
  - 启动 Chrome CDP 调试模式
  - 运行环境诊断
"""

import os
import time
import platform
import subprocess

from .logger import get_logger
from .cdp_session import (
    CDPSession,
    require_runtime_dependencies,
    get_requests,
    get_default_chrome_path,
    DEFAULT_CDP_PORT,
    DEFAULT_CHROME_PATH,
    DEFAULT_CDP_DATA_DIR,
)

log = get_logger("chrome")


def is_cdp_ready(cdp_port=DEFAULT_CDP_PORT):
    """检查 Chrome CDP 调试端口是否就绪

    Args:
        cdp_port: CDP 端口号

    Returns:
        True 如果 CDP 就绪, False 否则
    """
    if not require_runtime_dependencies():
        return False
    requests = get_requests()
    try:
        resp = requests.get(f"http://127.0.0.1:{cdp_port}/json/version", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def check_login_state(cdp_port=DEFAULT_CDP_PORT):
    """检查 BOSS直聘登录状态

    通过检测 __zp_stoken__ cookie 是否存在来判断登录状态。

    Args:
        cdp_port: CDP 端口号

    Returns:
        True 如果已登录, False 否则
    """
    if not is_cdp_ready(cdp_port):
        return False
    try:
        ws = CDPSession(cdp_port)
        tid, sid = ws.create_target("about:blank")

        ws.send("Page.navigate", {"url": "https://www.zhipin.com/"}, sid)
        time.sleep(3)

        val = ws.eval_js("document.cookie.includes('__zp_stoken__')", sid)
        is_logged_in = val == True

        ws.close_target(tid)
        ws.close()
        return is_logged_in
    except Exception as e:
        log.error(f"登录状态检查失败: {e}")
        return False


def run_setup_chrome(cdp_port=DEFAULT_CDP_PORT):
    """启动 Chrome CDP 调试模式

    使用独立的用户数据目录, 避免与日常浏览冲突。

    Args:
        cdp_port: CDP 端口号
    """
    os.makedirs(DEFAULT_CDP_DATA_DIR, exist_ok=True)
    print(f"准备启动 Chrome CDP 模式...")
    print(f"  CDP端口: {cdp_port}")
    print(f"  Profile目录: {DEFAULT_CDP_DATA_DIR}")

    cmd = [
        DEFAULT_CHROME_PATH,
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={DEFAULT_CDP_DATA_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-allow-origins=*",
    ]

    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if platform.system() == "Windows":
        kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        ) | getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen(cmd, **kwargs)

    print(f"\n等待 Chrome 启动...")
    for i in range(30):
        time.sleep(1)
        if is_cdp_ready(cdp_port):
            print(f"✅ Chrome CDP 已就绪 (端口 {cdp_port})")
            print(f"\n请在弹出的 Chrome 浏览器中登录 BOSS直聘 (zhipin.com)")
            return True
    print(f"❌ Chrome 启动超时")
    return False


def run_check(cdp_port=DEFAULT_CDP_PORT):
    """运行完整环境诊断

    检查项: 依赖 → Chrome 路径 → CDP 连接 → 登录状态

    Args:
        cdp_port: CDP 端口号
    """
    print("=== 环境检查 ===")
    print()

    print("1. 检查依赖...")
    if require_runtime_dependencies():
        print("   ✅ 依赖已安装 (requests, websocket-client)")
    else:
        print("   ❌ 依赖缺失")
        return False

    print()
    print("2. 检查 Chrome 路径...")
    if os.path.exists(DEFAULT_CHROME_PATH):
        print(f"   ✅ Chrome 路径: {DEFAULT_CHROME_PATH}")
    else:
        print(f"   ⚠️ Chrome 未找到: {DEFAULT_CHROME_PATH}")

    print()
    print("3. 检查 CDP 连接...")
    if is_cdp_ready(cdp_port):
        print(f"   ✅ CDP 已就绪 (端口 {cdp_port})")
        print()
        print("4. 检查登录状态...")
        if check_login_state(cdp_port):
            print("   ✅ 已登录 BOSS直聘")
            return True
        else:
            print("   ❌ 未登录 BOSS直聘")
            return False
    else:
        print(f"   ❌ CDP 未就绪 (端口 {cdp_port})")
        print(f"   请运行 --setup-chrome 启动 Chrome CDP")
        return False
