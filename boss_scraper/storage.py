"""
数据存储模块

功能:
  - 自动递增的 Job ID 管理
  - 按 jobs/<id:03d>/ 目录结构持久化保存
  - 文件写入 (JSON / 纯文本)
  - 去重检查 (基于 encryptJobId)
  - 原始 API 响应保存 (用于调试和回溯)
"""

import os
import re
import json
import time

from .logger import get_logger

log = get_logger("storage")


def get_next_job_id(jobs_dir):
    """获取下一个可用的 Job ID (自增)

    扫描 jobs_dir 下的子目录, 找到最大数字 ID 并 +1。

    Args:
        jobs_dir: jobs 根目录路径

    Returns:
        下一个可用的数字 ID
    """
    if not os.path.exists(jobs_dir):
        os.makedirs(jobs_dir, exist_ok=True)
        return 1
    existing = []
    for name in os.listdir(jobs_dir):
        if os.path.isdir(os.path.join(jobs_dir, name)):
            num_match = re.search(r"(\d+)", name)
            if num_match:
                existing.append(int(num_match.group(1)))
    return max(existing) + 1 if existing else 1


def _write_json(path, data):
    """写入 JSON 文件"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _write_text(path, text):
    """写入纯文本文件"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def save_to_jobs_dir(result, jobs_root="jobs"):
    """将抓取结果保存到 jobs/<id:03d>/ 目录

    保存文件:
      1. structured.json     - 最终结构化数据
      2. jd_dom_data.json    - JD 页原始 DOM 提取数据
      3. company_dom_data.json - 公司页原始 DOM 提取数据 (如有)
      4. jd_full.txt         - JD 全文
      5. company_intro.txt   - 公司介绍全文 (如有)
      6. meta.json           - 元数据 (ID, URL, 时间戳等)

    Args:
        result: 包含 jd_data, company_data, structured 的字典
        jobs_root: jobs 根目录

    Returns:
        (job_dir, job_id) 元组
    """
    jobs_root = os.path.abspath(jobs_root)
    next_id = get_next_job_id(jobs_root)
    job_dir = os.path.join(jobs_root, f"{next_id:03d}")
    os.makedirs(job_dir, exist_ok=True)

    structured = result.get("structured", {})
    jd_data = result.get("jd_data", {})
    company_data = result.get("company_data")

    # 1) 结构化数据
    _write_json(os.path.join(job_dir, "structured.json"), structured)

    # 2) JD 页原始 DOM 提取数据
    _write_json(os.path.join(job_dir, "jd_dom_data.json"), jd_data)

    # 3) 公司页原始 DOM 提取数据
    if company_data:
        _write_json(os.path.join(job_dir, "company_dom_data.json"), company_data)

    # 4) JD 全文
    jd_full = jd_data.get("jd_full", "") or ""
    if jd_full:
        _write_text(os.path.join(job_dir, "jd_full.txt"), jd_full)

    # 5) 公司介绍全文
    company_intro = company_data.get("company_intro", "") if company_data else ""
    if company_intro:
        _write_text(os.path.join(job_dir, "company_intro.txt"), company_intro)

    # 6) 元数据
    meta = {
        "job_id": next_id,
        "job_dir": job_dir,
        "source_url": structured.get("_source_url", ""),
        "company_url": structured.get("_company_url", ""),
        "crawled_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "job_title": structured.get("job_name", ""),
        "company_name": structured.get("company_name", ""),
        "brand_id": structured.get("_brand_id", ""),
    }
    _write_json(os.path.join(job_dir, "meta.json"), meta)

    log.info(f"已保存到 {job_dir} (ID={next_id})")
    return job_dir, next_id


def get_scraped_job_ids(jobs_root="jobs"):
    """获取已采集过的职位 URL 集合 (用于去重)

    扫描 jobs 目录下的所有 meta.json, 提取 source_url。

    Args:
        jobs_root: jobs 根目录

    Returns:
        已采集的 source_url 集合
    """
    jobs_root = os.path.abspath(jobs_root)
    scraped = set()
    if not os.path.exists(jobs_root):
        return scraped

    for name in os.listdir(jobs_root):
        meta_path = os.path.join(jobs_root, name, "meta.json")
        if not os.path.exists(meta_path):
            continue
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            url = meta.get("source_url", "")
            if url:
                # 从 URL 中提取 encryptJobId 作为去重键
                # URL 格式: https://www.zhipin.com/job_detail/{encryptJobId}.html
                match = re.search(r"/job_detail/([^/.]+)", url)
                if match:
                    scraped.add(match.group(1))
                else:
                    scraped.add(url)
        except (json.JSONDecodeError, OSError):
            continue

    log.debug(f"已采集职位数: {len(scraped)}")
    return scraped


def is_job_scraped(encrypt_job_id, jobs_root="jobs"):
    """检查某个职位是否已经被采集过

    Args:
        encrypt_job_id: 加密的职位 ID
        jobs_root: jobs 根目录

    Returns:
        True 如果已采集, False 否则
    """
    scraped = get_scraped_job_ids(jobs_root)
    return encrypt_job_id in scraped


def save_job_list_raw(job_list_data, page_num, jobs_root="jobs"):
    """保存原始 API 响应数据 (用于调试和回溯)

    Args:
        job_list_data: API 响应的 zpData 字典
        page_num: 页码
        jobs_root: jobs 根目录
    """
    debug_dir = os.path.join(os.path.abspath(jobs_root), "_debug")
    os.makedirs(debug_dir, exist_ok=True)
    path = os.path.join(debug_dir, f"joblist_page_{page_num:02d}.json")
    _write_json(path, job_list_data)
    log.debug(f"原始 API 响应已保存: {path}")
