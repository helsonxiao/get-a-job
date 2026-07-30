"""
主控制模块 — 采集流程编排

JobCrawler 类负责完整的自动化采集流程:

  1. (可选) 解析 HAR 文件, 验证分页 API 结构
  2. 连接 Chrome CDP, 打开职位列表页面
  3. 从页面 URL 提取搜索参数
  4. 循环翻页:
     a. 在页面上下文中执行 fetch() 获取职位列表
     b. 逐个处理职位: 打开详情页 → 公司页 → 保存
     c. 随机延迟 3-8 秒后翻到下一页
  5. 汇总输出采集统计

错误处理:
  - 网络请求失败: 自动重试 (最多 3 次)
  - 单个职位抓取失败: 跳过并记录, 不影响整体流程
  - 公司页抓取失败: 仅保存 JD 页数据
  - 翻页失败: 重试后仍失败则终止翻页
"""

import time
import random
import json
from typing import Callable, Optional

from .logger import get_logger
from .cdp_session import CDPSession, human_simulate, DEFAULT_CDP_PORT
from .har_parser import (
    parse_har_file,
    extract_search_params_from_url,
    print_har_summary,
    HARAnalysisResult,
)
from .network import (
    fetch_job_list_with_retry,
    extract_job_items,
    build_job_detail_url,
    build_company_url,
    random_page_delay,
)
from .page_parser import (
    scrape_jd_page,
    scrape_company_page,
    build_structured,
    safe_json_loads,
)
from .storage import (
    save_to_jobs_dir,
    get_scraped_job_ids,
    save_job_list_raw,
)
from .chrome_manager import is_cdp_ready, check_login_state

log = get_logger("crawler")


def sanitize_url(url: str) -> str:
    """净化 URL, 移除查询参数中可能存在的反斜杠污染

    BOSS直聘的反爬机制或 Chrome 的 URL 规范化可能在查询字符串中
    插入反斜杠 (\\ → %5C), 导致参数解析错误。

    处理:
      - 移除 %5C / %5c (URL 编码的反斜杠)
      - 移除查询字符串中的字面反斜杠
      - 规范化路径: /jobs/? → /jobs? (移除 ? 前多余的 /)

    Args:
        url: 原始 URL

    Returns:
        净化后的 URL
    """
    from urllib.parse import urlparse, urlunparse

    original = url

    # Step 1: 移除 %5C (URL 编码的反斜杠, 不区分大小写)
    cleaned = url.replace("%5C", "").replace("%5c", "")

    # Step 2: 移除字面反斜杠 (仅在查询字符串部分)
    parsed = urlparse(cleaned)
    if "\\" in parsed.query:
        clean_query = parsed.query.replace("\\", "")
        parsed = parsed._replace(query=clean_query)
        cleaned = urlunparse(parsed)

    # Step 3: 规范化路径 — 移除 ? 前多余的 /
    # 例如: /web/geek/jobs/?city=... → /web/geek/jobs?city=...
    if "/?" in cleaned:
        cleaned = cleaned.replace("/?", "?")

    if cleaned != original:
        log.warning(f"URL 已净化:")
        log.warning(f"  原始: {original}")
        log.warning(f"  净化: {cleaned}")

    return cleaned


class CrawlStats:
    """采集统计"""

    def __init__(self):
        self.total_pages = 0
        self.total_jobs_found = 0
        self.jobs_scraped = 0
        self.jobs_skipped_dup = 0
        self.jobs_failed = 0
        self.start_time = None
        self.end_time = None

    def __str__(self):
        elapsed = 0
        if self.start_time and self.end_time:
            elapsed = self.end_time - self.start_time
        elif self.start_time:
            elapsed = time.time() - self.start_time
        mins, secs = divmod(int(elapsed), 60)
        return (
            f"采集统计:\n"
            f"  耗时: {mins}分{secs}秒\n"
            f"  翻页数: {self.total_pages}\n"
            f"  发现职位: {self.total_jobs_found}\n"
            f"  成功采集: {self.jobs_scraped}\n"
            f"  跳过(重复): {self.jobs_skipped_dup}\n"
            f"  失败: {self.jobs_failed}"
        )


class JobCrawler:
    """自动化岗位采集器

    通过 CDP 控制 Chrome 浏览器, 自动翻页采集 BOSS直聘职位信息。

    Args:
        cdp_port: CDP 端口号
        jobs_dir: 数据保存目录
        max_pages: 最大翻页数 (None=不限制)
        delay_min: 翻页最小延迟 (秒)
        delay_max: 翻页最大延迟 (秒)
        fetch_company: 是否抓取公司页
        debug: 调试模式
        on_job_saved: 单个职位保存后的回调, 签名 (job_dir: str, job_id: str) -> None。
                      适配层用它做增量迁移, 让 Web 端能实时看到新抓的职位。
    """

    def __init__(
        self,
        cdp_port=DEFAULT_CDP_PORT,
        jobs_dir="jobs",
        max_pages=None,
        delay_min=3,
        delay_max=8,
        fetch_company=True,
        debug=False,
        on_job_saved: Optional[Callable[[str, str], None]] = None,
        existing_ids: Optional[set] = None,
    ):
        self.cdp_port = cdp_port
        self.jobs_dir = jobs_dir
        self.max_pages = max_pages
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.fetch_company = fetch_company
        self.debug = debug
        self.on_job_saved = on_job_saved
        # 已采集过的职位 ID 集合 (来自 data/jobs/), 翻页时跳过避免重复抓取
        self.existing_ids = existing_ids or set()
        self.stats = CrawlStats()

    def crawl_from_url(self, list_url: str, har_path: Optional[str] = None):
        """从职位列表页 URL 启动采集

        Args:
            list_url: BOSS直聘职位列表页 URL
            har_path: HAR 文件路径 (可选, 用于验证 API 结构)
        """
        # 净化 URL: 移除反斜杠污染 (可能来自 Chrome 反爬 SDK 或 URL 规范化)
        list_url = sanitize_url(list_url)

        self.stats.start_time = time.time()
        log.info("=" * 60)
        log.info("BOSS直聘自动化岗位采集启动")
        log.info("=" * 60)
        log.info(f"列表页 URL: {list_url}")
        log.info(f"最大翻页: {self.max_pages or '不限'}")
        log.info(f"翻页延迟: {self.delay_min}-{self.delay_max}s")
        log.info(f"抓取公司页: {self.fetch_company}")
        log.info(f"保存目录: {self.jobs_dir}")

        # --- 步骤 1: (可选) 解析 HAR 文件 ---
        search_params = None
        if har_path:
            log.info("-" * 40)
            log.info("步骤 1: 解析 HAR 文件")
            try:
                har_result = parse_har_file(har_path)
                print_har_summary(har_result)
                # 从 HAR 提取请求参数作为模板
                search_params = self._params_from_har(har_result)
                log.info("HAR 解析成功, 参数已提取")
            except Exception as e:
                log.error(f"HAR 解析失败: {e}")
                log.warning("将使用 URL 参数代替")

        # 如果没有 HAR 或 HAR 解析失败, 从 URL 提取参数
        if search_params is None:
            search_params = extract_search_params_from_url(list_url)
            log.info(f"从 URL 提取搜索参数: {json.dumps(search_params, ensure_ascii=False)[:200]}")

        # --- 步骤 2: 检查环境 ---
        log.info("-" * 40)
        log.info("步骤 2: 检查环境")
        if not is_cdp_ready(self.cdp_port):
            log.error(f"CDP 未就绪 (端口 {self.cdp_port}), 请先运行 --setup-chrome")
            return
        if not check_login_state(self.cdp_port):
            log.error("未检测到登录状态, 请在 Chrome 中登录 zhipin.com")
            return
        log.info("环境检查通过: CDP 就绪 + 已登录")

        # --- 步骤 3: 打开列表页 + 翻页采集 ---
        log.info("-" * 40)
        log.info("步骤 3: 开始翻页采集")
        ws = None
        list_tid = None
        detail_tid = None

        try:
            ws = CDPSession(self.cdp_port)

            # 创建列表页标签 (保持打开, 用于 fetch 调用)
            log.info("创建列表页标签...")
            list_tid, list_sid = ws.create_target(list_url)
            # 等待页面加载完成 (让反爬 token 初始化)
            log.info("等待页面加载 (10s)...")
            time.sleep(10)

            # 创建详情页标签 (复用, 避免频繁创建/销毁)
            log.info("创建详情页标签...")
            detail_tid, detail_sid = ws.create_target("about:blank")

            # 已采集的职位 ID 集合 (去重)
            scraped_ids = get_scraped_job_ids(self.jobs_dir)
            log.info(f"已采集职位数: {len(scraped_ids)}")

            # --- 翻页循环 ---
            page = 1
            while True:
                if self.max_pages and page > self.max_pages:
                    log.info(f"已达到最大翻页数 {self.max_pages}, 停止")
                    break

                log.info(f"\n{'='*40}")
                log.info(f"正在采集第 {page} 页...")
                log.info(f"{'='*40}")
                self.stats.total_pages = page

                # 获取职位列表
                api_data = fetch_job_list_with_retry(
                    ws, list_sid, page, search_params
                )

                if api_data is None:
                    log.error(f"第 {page} 页获取失败, 终止翻页")
                    break

                # 保存原始 API 响应 (调试用)
                save_job_list_raw(api_data.get("zpData", {}), page, self.jobs_dir)

                # 提取职位列表
                job_list, has_more, res_count = extract_job_items(api_data)
                self.stats.total_jobs_found += len(job_list)
                log.info(
                    f"第 {page} 页: {len(job_list)} 个职位, "
                    f"hasMore={has_more}, resCount={res_count}"
                )

                if not job_list:
                    log.warning(f"第 {page} 页职位列表为空, 停止翻页")
                    break

                # 逐个处理职位
                for idx, job_item in enumerate(job_list, 1):
                    encrypt_job_id = job_item.get("encryptJobId", "")
                    job_name = job_item.get("jobName", "")
                    brand_name = job_item.get("brandName", "")

                    log.info(
                        f"\n  [{page}-{idx}/{len(job_list)}] "
                        f"{job_name} @ {brand_name}"
                    )

                    # 去重检查: 本次会话已采 + 历史已采 (data/jobs/)
                    if encrypt_job_id and (
                        encrypt_job_id in scraped_ids
                        or encrypt_job_id in self.existing_ids
                    ):
                        log.info(f"  → 已采集过, 跳过")
                        self.stats.jobs_skipped_dup += 1
                        continue

                    # 抓取职位详情
                    try:
                        self._scrape_one_job(
                            ws,
                            detail_sid,
                            job_item,
                            scraped_ids,
                        )
                        scraped_ids.add(encrypt_job_id)
                    except Exception as e:
                        log.error(f"  → 抓取失败: {e}")
                        self.stats.jobs_failed += 1
                        # 出错后等待一下, 避免连续错误
                        time.sleep(random.uniform(2, 5))

                # 判断是否继续翻页
                if not has_more:
                    log.info(f"第 {page} 页 hasMore=False, 采集完成")
                    break

                # 翻页前随机延迟
                page += 1
                random_page_delay(self.delay_min, self.delay_max)

        except KeyboardInterrupt:
            log.warning("\n用户中断采集 (Ctrl+C)")
        except Exception as e:
            log.error(f"采集流程异常: {e}")
            import traceback
            log.debug(traceback.format_exc())
        finally:
            # 清理标签页
            if ws:
                if list_tid:
                    try:
                        ws.close_target(list_tid)
                    except Exception:
                        pass
                if detail_tid:
                    try:
                        ws.close_target(detail_tid)
                    except Exception:
                        pass
                ws.close()

        # --- 汇总输出 ---
        self.stats.end_time = time.time()
        log.info("\n" + "=" * 60)
        log.info("采集完成")
        log.info("=" * 60)
        log.info(str(self.stats))

    def _scrape_one_job(self, ws, detail_sid, job_item, scraped_ids):
        """抓取单个职位 (详情页 + 公司页)

        Args:
            ws: CDPSession 实例
            detail_sid: 详情页标签的 sessionId
            job_item: API 返回的职位项字典
            scraped_ids: 已采集 ID 集合 (用于更新)
        """
        encrypt_job_id = job_item.get("encryptJobId", "")
        job_url = build_job_detail_url(encrypt_job_id)

        if not job_url or job_url == "https://www.zhipin.com/job_detail/.html":
            log.warning(f"  → 无效的职位 URL, 跳过")
            self.stats.jobs_failed += 1
            return

        # --- JD 详情页 ---
        jd_data = scrape_jd_page(ws, detail_sid, job_url, debug=self.debug)

        if not jd_data or not jd_data.get("job_name"):
            log.warning(f"  → JD 页数据为空, 跳过")
            self.stats.jobs_failed += 1
            return

        # --- 公司页 (可选) ---
        company_data = None
        if self.fetch_company:
            # 优先从 JD 页提取公司链接, 其次从 API 数据构建
            company_url = jd_data.get("company_url", "")
            if not company_url:
                encrypt_brand_id = job_item.get("encryptBrandId", "")
                company_url = build_company_url(encrypt_brand_id)

            if company_url:
                time.sleep(random.uniform(3, 6))
                try:
                    company_data = scrape_company_page(
                        ws, detail_sid, company_url, debug=self.debug
                    )
                except Exception as e:
                    log.warning(f"  → 公司页抓取失败: {e}")
            else:
                log.info(f"  → 无公司页链接, 跳过公司页")

        # --- 构建结构化数据 ---
        structured = build_structured(jd_data, company_data)

        # --- 保存 ---
        result = {
            "jd_data": jd_data,
            "company_data": company_data,
            "structured": structured,
        }
        job_dir, job_id = save_to_jobs_dir(result, self.jobs_dir)

        self.stats.jobs_scraped += 1
        log.info(
            f"  → ✅ 保存成功 (ID={job_id}): "
            f"{structured.get('job_name', '')} @ "
            f"{structured.get('company_name', '')}"
        )

        # 增量入库回调: 让适配层立刻迁移 + 入索引, Web 端能实时看到
        if self.on_job_saved:
            try:
                self.on_job_saved(job_dir, job_id)
            except Exception as e:
                log.warning(f"  → 增量入库回调失败 (不影响采集): {e}")

    @staticmethod
    def _params_from_har(har_result: HARAnalysisResult) -> dict:
        """从 HAR 分析结果提取搜索参数

        Args:
            har_result: HARAnalysisResult 对象

        Returns:
            搜索参数字典
        """
        params = dict(har_result.request_params)
        # 确保必要参数存在
        params.setdefault("pageSize", "15")
        params.setdefault("scene", "1")
        return params


def crawl_single_jd(url, cdp_port=DEFAULT_CDP_PORT, debug=False, fetch_company=True, jobs_dir="jobs"):
    """抓取单个职位。

    Args:
        url: JD 详情页 URL
        cdp_port: CDP 端口号
        debug: 调试模式
        fetch_company: 是否抓取公司页
        jobs_dir: 保存目录

    Returns:
        (job_dir, job_id) 元组, 失败返回 (None, None)
    """
    log.info(f"开始抓取单个职位: {url}")

    ws = None
    tid = None
    try:
        ws = CDPSession(cdp_port)
        tid, sid = ws.create_target("about:blank")

        # JD 页
        jd_data = scrape_jd_page(ws, sid, url, debug=debug)

        # 公司页
        company_data = None
        company_url = jd_data.get("company_url", "")
        if fetch_company and company_url:
            time.sleep(random.uniform(3, 6))
            try:
                company_data = scrape_company_page(ws, sid, company_url, debug=debug)
            except Exception as e:
                log.warning(f"公司页抓取失败: {e}")

        # 构建结构化数据
        structured = build_structured(jd_data, company_data)

        if debug:
            log.debug("=" * 60)
            log.debug("最终结构化结果:")
            log.debug(json.dumps(structured, ensure_ascii=False, indent=2))
            log.debug("=" * 60)

        result = {
            "jd_data": jd_data,
            "company_data": company_data,
            "structured": structured,
        }
        job_dir, job_id = save_to_jobs_dir(result, jobs_dir)

        log.info(f"抓取成功! 已保存到: {job_dir} (ID={job_id})")
        log.info(f"  职位: {structured.get('job_name', 'N/A')}")
        log.info(f"  公司: {structured.get('company_name', 'N/A')}")

        return job_dir, job_id

    except Exception as e:
        log.error(f"抓取失败: {e}")
        import traceback
        log.debug(traceback.format_exc())
        return None, None
    finally:
        if ws:
            if tid:
                try:
                    ws.close_target(tid)
                except Exception:
                    pass
            ws.close()
