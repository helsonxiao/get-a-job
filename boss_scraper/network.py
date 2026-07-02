"""
网络请求处理模块

核心功能: 在浏览器页面上下文中执行 XMLHttpRequest 调用，
模拟 BOSS直聘的分页 API 请求，实现自动翻页。

设计要点:
  - 使用 XMLHttpRequest 而非 fetch: BOSS直聘反爬 SDK 会 patch
    XMLHttpRequest.prototype 自动注入 zp_token / token 等反爬头
  - 同时从 bst cookie 手动提取 zp_token 作为 fallback
  - credentials 自动携带浏览器 cookies (withCredentials=true)
  - 随机 3-8 秒间隔避免触发反爬
  - 请求失败自动重试 (最多 3 次)
  - 通过 hasMore 字段判断是否继续翻页
"""

import time
import random
import json
from typing import Optional

from .logger import get_logger

log = get_logger("network")

# BOSS直聘职位列表 API 端点
JOBLIST_API_URL = "https://www.zhipin.com/wapi/zpgeek/search/joblist.json"

# 翻页延迟范围 (秒)
DELAY_MIN = 3
DELAY_MAX = 8

# 最大重试次数
MAX_RETRIES = 3

# 重试基础延迟 (秒)
RETRY_BASE_DELAY = 5


def build_fetch_js(page: int, params: dict) -> str:
    """构建在页面上下文中执行的 XMLHttpRequest JavaScript 代码

    使用 XMLHttpRequest 而非 fetch，因为 BOSS直聘的反爬 SDK 会 patch
    XMLHttpRequest.prototype 来自动注入 zp_token / token 等反爬头。
    通过 XHR 发请求，SDK 拦截器会自动添加这些头，无需手动管理。

    同时从 bst cookie 提取 zp_token 作为 fallback（两者值相同）。

    Args:
        page: 页码 (1-based)
        params: 搜索参数字典 (city, position, salary, stage, query 等)

    Returns:
        JavaScript 代码字符串 (返回 Promise, 配合 awaitPromise 使用)
    """
    # 构建参数对象
    js_params = {}
    js_params["page"] = str(page)
    js_params["pageSize"] = params.get("pageSize", "15")
    js_params["city"] = params.get("city", "")
    js_params["position"] = params.get("position", "")
    js_params["salary"] = params.get("salary", "")
    js_params["stage"] = params.get("stage", "")
    js_params["query"] = params.get("query", "")
    js_params["scene"] = params.get("scene", "1")

    # 空参数
    for key in [
        "expectInfo",
        "multiSubway",
        "multiBusinessDistrict",
        "jobType",
        "experience",
        "degree",
        "industry",
        "scale",
        "encryptExpectId",
    ]:
        js_params[key] = params.get(key, "")

    # 将参数字典转为 JSON, 供 JS 使用
    params_json = json.dumps(js_params, ensure_ascii=False)

    js = f"""
(new Promise(function(resolve, reject) {{
    var params = {params_json};
    var formData = new URLSearchParams();
    for (var key in params) {{
        formData.append(key, params[key]);
    }}

    var url = '{JOBLIST_API_URL}?_=' + Date.now();

    // 从 bst cookie 提取 zp_token (两者值相同)
    var zpToken = '';
    var cookieMatch = document.cookie.match(/bst=([^;]+)/);
    if (cookieMatch) {{
        try {{ zpToken = decodeURIComponent(cookieMatch[1]); }} catch(e) {{ zpToken = cookieMatch[1]; }}
    }}

    var xhr = new XMLHttpRequest();
    xhr.open('POST', url, true);
    xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
    xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
    xhr.setRequestHeader('Accept', 'application/json, text/plain, */*');
    // 手动设置 zp_token (与 bst cookie 同值); SDK 拦截器也可能再设一次
    if (zpToken) {{
        xhr.setRequestHeader('zp_token', zpToken);
    }}
    xhr.withCredentials = true;
    xhr.timeout = 30000;

    xhr.onreadystatechange = function() {{
        if (xhr.readyState === 4) {{
            if (xhr.status >= 200 && xhr.status < 300) {{
                try {{
                    var data = JSON.parse(xhr.responseText);
                    resolve(JSON.stringify(data));
                }} catch(e) {{
                    resolve(JSON.stringify({{
                        error: 'JSON parse error: ' + e.message,
                        raw: xhr.responseText.substring(0, 500)
                    }}));
                }}
            }} else {{
                resolve(JSON.stringify({{
                    error: 'HTTP ' + xhr.status,
                    status: xhr.status,
                    body: xhr.responseText.substring(0, 300)
                }}));
            }}
        }}
    }};

    xhr.onerror = function() {{
        resolve(JSON.stringify({{error: 'XHR network error'}}));
    }};

    xhr.ontimeout = function() {{
        resolve(JSON.stringify({{error: 'XHR timeout'}}));
    }};

    xhr.send(formData.toString());
}}))
"""
    return js


def fetch_job_list_page(ws, sid, page, params, timeout=30):
    """在页面上下文中执行 XHR 获取指定页的职位列表

    使用 CDP Runtime.evaluate + awaitPromise 在浏览器页面内执行
    XMLHttpRequest 调用，获取 BOSS直聘的职位列表 API 响应。
    使用 XHR 而非 fetch，以便页面反爬 SDK 自动注入 zp_token 等头。

    Args:
        ws: CDPSession 实例
        sid: Target sessionId
        page: 页码 (1-based)
        params: 搜索参数字典
        timeout: CDP 超时秒数

    Returns:
        API 响应字典 (包含 code, zpData 等), 失败返回 None
    """
    js = build_fetch_js(page, params)

    try:
        result = ws.eval_async_js(js, sid, timeout=timeout)
    except TimeoutError as e:
        log.error(f"页面 {page} fetch 超时: {e}")
        return None
    except Exception as e:
        log.error(f"页面 {page} fetch 异常: {e}")
        return None

    if result is None:
        log.error(f"页面 {page} fetch 返回 None")
        return None

    # 如果返回的是字符串, 解析 JSON
    if isinstance(result, str):
        try:
            data = json.loads(result)
        except (json.JSONDecodeError, ValueError) as e:
            log.error(f"页面 {page} 响应 JSON 解析失败: {e}")
            log.debug(f"原始响应: {result[:500]}")
            return None
    elif isinstance(result, dict):
        data = result
    else:
        log.error(f"页面 {page} 响应类型异常: {type(result)}")
        return None

    # 检查错误
    if "error" in data:
        log.error(f"页面 {page} fetch 错误: {data['error']}")
        return None

    # 检查 API 返回码
    code = data.get("code")
    if code is None:
        log.error(f"页面 {page} 响应缺少 code 字段")
        return None
    if code != 0:
        msg = data.get("message", "")
        log.error(f"页面 {page} API 返回错误: code={code}, message={msg}")
        return None

    return data


def fetch_job_list_with_retry(
    ws, sid, page, params, max_retries=MAX_RETRIES, timeout=30
):
    """带重试机制的职位列表获取

    Args:
        ws: CDPSession 实例
        sid: Target sessionId
        page: 页码
        params: 搜索参数
        max_retries: 最大重试次数
        timeout: CDP 超时秒数

    Returns:
        API 响应字典, 所有重试均失败返回 None
    """
    for attempt in range(1, max_retries + 1):
        log.info(
            f"获取第 {page} 页职位列表 (尝试 {attempt}/{max_retries})..."
        )

        data = fetch_job_list_page(ws, sid, page, params, timeout=timeout)

        if data is not None:
            zp_data = data.get("zpData", {})
            job_list = zp_data.get("jobList", [])
            has_more = zp_data.get("hasMore", False)
            res_count = zp_data.get("resCount", 0)
            log.info(
                f"第 {page} 页成功: {len(job_list)} 个职位, "
                f"hasMore={has_more}, resCount={res_count}"
            )
            return data

        if attempt < max_retries:
            delay = RETRY_BASE_DELAY * attempt + random.uniform(1, 3)
            log.warning(f"第 {page} 页获取失败, {delay:.1f}s 后重试...")
            time.sleep(delay)

    log.error(f"第 {page} 页所有 {max_retries} 次重试均失败")
    return None


def random_page_delay(min_sec=DELAY_MIN, max_sec=DELAY_MAX):
    """随机翻页延迟

    在两次分页请求之间插入随机延迟, 模拟人类操作节奏。

    Args:
        min_sec: 最小延迟秒数
        max_sec: 最大延迟秒数
    """
    delay = random.uniform(min_sec, max_sec)
    log.info(f"等待 {delay:.1f}s (翻页随机延迟)...")
    time.sleep(delay)
    return delay


def extract_job_items(api_response):
    """从 API 响应中提取职位列表项

    Args:
        api_response: API 响应字典 (包含 zpData.jobList)

    Returns:
        (job_list, has_more, res_count) 元组
    """
    zp_data = api_response.get("zpData", {})
    job_list = zp_data.get("jobList", [])
    has_more = zp_data.get("hasMore", False)
    res_count = zp_data.get("resCount", 0)
    return job_list, has_more, res_count


def build_job_detail_url(encrypt_job_id):
    """根据 encryptJobId 构建职位详情页 URL

    Args:
        encrypt_job_id: 加密的职位 ID

    Returns:
        职位详情页 URL
    """
    return f"https://www.zhipin.com/job_detail/{encrypt_job_id}.html"


def build_company_url(encrypt_brand_id):
    """根据 encryptBrandId 构建公司介绍页 URL

    Args:
        encrypt_brand_id: 加密的品牌 ID

    Returns:
        公司介绍页 URL, 如果 brand_id 为空则返回空字符串
    """
    if not encrypt_brand_id:
        return ""
    return f"https://www.zhipin.com/gongsi/{encrypt_brand_id}.html"
