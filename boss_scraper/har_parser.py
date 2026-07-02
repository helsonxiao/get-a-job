"""
HAR 文件解析与验证模块

功能:
  - 解析 HAR 文件中的分页请求结构
  - Base64 解码响应内容并验证 JSON 格式
  - 提取 API 请求参数模板 (URL, method, headers, body)
  - 验证分页数据格式和内容 (jobList, hasMore, resCount 等)
"""

import json
import base64
from typing import Optional
from urllib.parse import unquote, parse_qs

from .logger import get_logger

log = get_logger("har")

# BOSS直聘职位列表 API 的 URL 关键词
JOBLIST_API_KEYWORD = "joblist.json"


class HARParseError(Exception):
    """HAR 文件解析异常"""
    pass


class HARAnalysisResult:
    """HAR 分析结果"""

    def __init__(self):
        self.api_url: str = ""
        self.method: str = ""
        self.request_headers: dict = {}
        self.request_cookies: dict = {}
        self.request_params: dict = {}
        self.request_body_raw: str = ""
        self.response_status: int = 0
        self.response_headers: dict = {}
        self.response_set_cookies: list = []
        self.response_body_decoded: dict = {}
        self.response_raw_size: int = 0

    def __str__(self):
        return (
            f"HARAnalysisResult(\n"
            f"  api_url={self.api_url},\n"
            f"  method={self.method},\n"
            f"  params={ {k: v[:50] if isinstance(v, str) and len(v) > 50 else v for k, v in self.request_params.items()} },\n"
            f"  response_status={self.response_status},\n"
            f"  response_raw_size={self.response_raw_size},\n"
            f"  job_count={len(self.response_body_decoded.get('zpData', {}).get('jobList', []))},\n"
            f")"
        )


def parse_har_file(har_path: str) -> HARAnalysisResult:
    """解析 HAR 文件, 提取职位列表 API 的请求和响应信息

    Args:
        har_path: HAR 文件路径

    Returns:
        HARAnalysisResult 对象

    Raises:
        HARParseError: 如果 HAR 文件格式错误或找不到职位列表 API 请求
        FileNotFoundError: 如果文件不存在
    """
    log.info(f"正在解析 HAR 文件: {har_path}")

    with open(har_path, "r", encoding="utf-8") as f:
        har_data = json.load(f)

    entries = har_data.get("log", {}).get("entries", [])
    if not entries:
        raise HARParseError("HAR 文件中没有找到网络请求条目")

    log.debug(f"HAR 文件包含 {len(entries)} 个请求条目")

    # 查找职位列表 API 请求
    target_entry = None
    for entry in entries:
        url = entry.get("request", {}).get("url", "")
        if JOBLIST_API_KEYWORD in url:
            target_entry = entry
            break

    if target_entry is None:
        raise HARParseError(
            f"未找到包含 '{JOBLIST_API_KEYWORD}' 的请求条目"
        )

    result = _extract_entry_info(target_entry)
    log.info(
        f"HAR 解析完成: {result.method} {result.api_url}, "
        f"响应状态={result.response_status}, "
        f"职位数量={len(result.response_body_decoded.get('zpData', {}).get('jobList', []))}"
    )
    return result


def _extract_entry_info(entry: dict) -> HARAnalysisResult:
    """从 HAR entry 中提取请求和响应信息"""
    result = HARAnalysisResult()

    req = entry.get("request", {})
    resp = entry.get("response", {})

    # --- 请求信息 ---
    result.api_url = req.get("url", "")
    result.method = req.get("method", "")

    # 请求头
    for h in req.get("headers", []):
        result.request_headers[h["name"]] = h["value"]

    # 请求 cookies
    for c in req.get("cookies", []):
        result.request_cookies[c["name"]] = c["value"]

    # 请求参数 (POST body)
    post_data = req.get("postData", {})
    result.request_body_raw = post_data.get("text", "")

    # 解析 POST 参数
    params = {}
    for p in post_data.get("params", []):
        name = p.get("name", "")
        value = p.get("value", "")
        # URL 解码
        params[name] = unquote(value) if value else ""
    result.request_params = params

    # --- 响应信息 ---
    result.response_status = resp.get("status", 0)

    # 响应头
    for h in resp.get("headers", []):
        result.response_headers[h["name"]] = h["value"]

    # 响应 set-cookie
    for c in resp.get("cookies", []):
        result.response_set_cookies.append(
            {"name": c.get("name", ""), "value": c.get("value", "")}
        )

    # 响应内容 (Base64 编码)
    content = resp.get("content", {})
    result.response_raw_size = content.get("size", 0)
    encoding = content.get("encoding", "")
    text = content.get("text", "")

    if encoding == "base64" and text:
        result.response_body_decoded = _decode_and_verify_response(text)
    elif text:
        # 非 Base64 编码, 直接解析 JSON
        try:
            result.response_body_decoded = json.loads(text)
        except (json.JSONDecodeError, ValueError) as e:
            raise HARParseError(f"响应内容 JSON 解析失败: {e}")
    else:
        log.warning("响应内容为空")
        result.response_body_decoded = {}

    return result


def _decode_and_verify_response(b64_text: str) -> dict:
    """Base64 解码响应内容并验证 JSON 格式

    Args:
        b64_text: Base64 编码的响应内容

    Returns:
        解码后的 JSON 字典

    Raises:
        HARParseError: 如果 Base64 解码或 JSON 解析失败
    """
    try:
        decoded_bytes = base64.b64decode(b64_text)
    except Exception as e:
        raise HARParseError(f"Base64 解码失败: {e}")

    decoded_text = decoded_bytes.decode("utf-8")

    try:
        data = json.loads(decoded_text)
    except (json.JSONDecodeError, ValueError) as e:
        raise HARParseError(f"解码后内容 JSON 解析失败: {e}")

    _verify_response_structure(data)
    return data


def _verify_response_structure(data: dict):
    """验证响应 JSON 结构是否符合预期

    检查项:
      - code 字段存在 (0 = 成功)
      - zpData 字段存在
      - zpData.jobList 数组存在
      - zpData.hasMore 布尔值存在
      - zpData.resCount 整数存在

    Args:
        data: 解码后的响应 JSON

    Raises:
        HARParseError: 如果结构不符合预期
    """
    if "code" not in data:
        raise HARParseError("响应缺少 'code' 字段")

    if data["code"] != 0:
        log.warning(f"响应 code={data['code']}, message={data.get('message', '')}")

    zpData = data.get("zpData")
    if not isinstance(zpData, dict):
        raise HARParseError("响应缺少 'zpData' 字段或类型错误")

    if "jobList" not in zpData:
        raise HARParseError("zpData 缺少 'jobList' 字段")

    if "hasMore" not in zpData:
        log.warning("zpData 缺少 'hasMore' 字段 (无法判断是否还有更多数据)")

    if "resCount" not in zpData:
        log.warning("zpData 缺少 'resCount' 字段 (无法获取总结果数)")

    job_list = zpData.get("jobList", [])
    log.info(
        f"响应验证通过: {len(job_list)} 个职位, "
        f"hasMore={zpData.get('hasMore')}, "
        f"resCount={zpData.get('resCount')}, "
        f"totalCount={zpData.get('totalCount')}"
    )


def extract_search_params_from_url(url: str) -> dict:
    """从 BOSS直聘岗位列表页面 URL 中提取搜索参数

    将页面 URL 的查询参数转换为 API 请求所需的参数字典。

    Args:
        url: 页面 URL, 如 https://www.zhipin.com/web/geek/jobs?city=101190200&...

    Returns:
        参数字典, 包含 city, position, salary, stage, query 等
    """
    from urllib.parse import urlparse, parse_qs

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    params = {}
    # 从页面 URL 提取的参数
    for key in ["city", "position", "salary", "stage", "query"]:
        if key in qs:
            params[key] = qs[key][0]

    # 其他可能存在的参数
    for key in ["experience", "degree", "industry", "scale", "jobType"]:
        if key in qs:
            params[key] = qs[key][0]
        else:
            params[key] = ""

    # 固定参数
    params["scene"] = "1"
    params["pageSize"] = "15"

    # 空参数 (API 要求)
    for key in [
        "expectInfo",
        "multiSubway",
        "multiBusinessDistrict",
        "encryptExpectId",
    ]:
        if key not in params:
            params[key] = ""

    return params


def print_har_summary(result: HARAnalysisResult):
    """打印 HAR 分析结果的摘要信息"""
    print("\n" + "=" * 70)
    print("HAR 文件分析结果摘要")
    print("=" * 70)
    print(f"API URL:    {result.api_url}")
    print(f"Method:     {result.method}")
    print(f"Status:     {result.response_status}")
    print(f"Raw Size:   {result.response_raw_size} bytes")
    print()

    print("请求参数:")
    for k, v in sorted(result.request_params.items()):
        v_display = v[:80] + "..." if len(v) > 80 else v
        print(f"  {k:30s} = {v_display}")
    print()

    zp_data = result.response_body_decoded.get("zpData", {})
    job_list = zp_data.get("jobList", [])
    print(f"响应数据:")
    print(f"  code:       {result.response_body_decoded.get('code')}")
    print(f"  message:    {result.response_body_decoded.get('message')}")
    print(f"  resCount:   {zp_data.get('resCount')}")
    print(f"  totalCount: {zp_data.get('totalCount')}")
    print(f"  hasMore:    {zp_data.get('hasMore')}")
    print(f"  jobList:    {len(job_list)} 个职位")
    print()

    if job_list:
        print("第一个职位示例:")
        first = job_list[0]
        for k in [
            "encryptJobId",
            "jobName",
            "salaryDesc",
            "brandName",
            "brandStageName",
            "brandScaleName",
            "jobExperience",
            "jobDegree",
            "skills",
            "cityName",
            "areaDistrict",
        ]:
            v = first.get(k, "")
            v_display = str(v)[:80] + "..." if len(str(v)) > 80 else str(v)
            print(f"  {k:25s} = {v_display}")
    print("=" * 70 + "\n")
