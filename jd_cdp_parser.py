#!/usr/bin/env python3
"""
BOSS直聘自动化岗位采集工具 (CDP版)

通过 CDP 连接 Chrome, 支持两种采集模式:

  1. 单职位采集 (原有功能):
     python3 jd_cdp_parser.py "https://www.zhipin.com/job_detail/xxx.html"

  2. 批量自动采集 (新功能):
     从职位列表页 URL 出发, 自动翻页采集所有职位详情和公司信息。
     python3 jd_cdp_parser.py --crawl "https://www.zhipin.com/web/geek/jobs?city=..."
     python3 jd_cdp_parser.py --crawl "https://..." --har har_file.har --max-pages 5

环境准备:
  python3 jd_cdp_parser.py --setup-chrome   # 启动 Chrome CDP 调试模式
  python3 jd_cdp_parser.py --check           # 检查环境

模块架构 (boss_scraper/):
  - cdp_session:    CDP 协议会话管理
  - har_parser:     HAR 文件解析与验证
  - network:        网络请求处理 (分页 API 调用)
  - page_parser:    页面解析 (DOM 提取、薪资解析)
  - storage:        数据持久化存储
  - chrome_manager: Chrome 环境管理
  - crawler:        主控制模块 (采集流程编排)
  - logger:         日志系统
"""
__version__ = "5.0.0"

import sys
import os
import argparse
import json

# 确保能找到 boss_scraper 包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from boss_scraper import __version__ as pkg_version
from boss_scraper.logger import get_logger
from boss_scraper.cdp_session import DEFAULT_CDP_PORT
from boss_scraper.chrome_manager import (
    run_setup_chrome,
    run_check,
    is_cdp_ready,
    check_login_state,
)
from boss_scraper.crawler import JobCrawler, crawl_single_jd
from boss_scraper.crawler import sanitize_url
from boss_scraper.har_parser import parse_har_file, print_har_summary
from boss_scraper.page_parser import build_structured

log = get_logger("main")


def main():
    parser = argparse.ArgumentParser(
        description=f"BOSS直聘自动化岗位采集工具 v{__version__}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 环境准备
  python3 jd_cdp_parser.py --setup-chrome
  python3 jd_cdp_parser.py --check

  # 单职位采集
  python3 jd_cdp_parser.py "https://www.zhipin.com/job_detail/xxx.html"
  python3 jd_cdp_parser.py "https://www.zhipin.com/job_detail/xxx.html" --debug
  python3 jd_cdp_parser.py "https://www.zhipin.com/job_detail/xxx.html" --no-company

  # 批量自动采集
  python3 jd_cdp_parser.py --crawl "https://www.zhipin.com/web/geek/jobs?city=101190200&..."
  python3 jd_cdp_parser.py --crawl "https://..." --har har_file.har
  python3 jd_cdp_parser.py --crawl "https://..." --max-pages 5 --delay-min 5 --delay-max 10

  # HAR 文件分析
  python3 jd_cdp_parser.py --har har_file.har
        """,
    )

    # 互斥的操作模式
    parser.add_argument("url", nargs="?", help="职位详情页 URL (单职位采集模式)")
    parser.add_argument(
        "--crawl",
        metavar="URL",
        help="批量采集模式: 从职位列表页 URL 出发, 自动翻页采集",
    )
    parser.add_argument(
        "--har",
        metavar="PATH",
        help="HAR 文件路径 (用于分析分页 API 结构或验证)",
    )
    parser.add_argument(
        "--setup-chrome", action="store_true", help="启动 Chrome CDP 调试模式"
    )
    parser.add_argument(
        "--check", action="store_true", help="运行环境检查"
    )

    # 通用选项
    parser.add_argument(
        "--cdp-port",
        type=int,
        default=DEFAULT_CDP_PORT,
        help=f"CDP 端口 (默认 {DEFAULT_CDP_PORT})",
    )
    parser.add_argument("--debug", action="store_true", help="打印详细提取过程")
    parser.add_argument(
        "--no-company", action="store_true", help="不抓取公司页面 (仅 JD 页)"
    )
    parser.add_argument(
        "--jobs-dir", default="jobs", help="输出目录 (默认 jobs)"
    )

    # 批量采集选项
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="最大翻页数 (默认不限制)",
    )
    parser.add_argument(
        "--delay-min",
        type=float,
        default=3.0,
        help="翻页最小延迟秒数 (默认 3)",
    )
    parser.add_argument(
        "--delay-max",
        type=float,
        default=8.0,
        help="翻页最大延迟秒数 (默认 8)",
    )

    args = parser.parse_args()

    # --- URL 净化 (移除反斜杠污染) ---
    if args.crawl:
        args.crawl = sanitize_url(args.crawl)
    if args.url:
        args.url = sanitize_url(args.url)

    # --- 模式分发 ---

    # 1. 启动 Chrome
    if args.setup_chrome:
        run_setup_chrome(args.cdp_port)
        return

    # 2. 环境检查
    if args.check:
        run_check(args.cdp_port)
        return

    # 3. HAR 文件分析 (独立模式)
    if args.har and not args.crawl and not args.url:
        try:
            result = parse_har_file(args.har)
            print_har_summary(result)
        except Exception as e:
            print(f"HAR 分析失败: {e}")
            sys.exit(1)
        return

    # 4. 批量采集模式
    if args.crawl:
        if not is_cdp_ready(args.cdp_port):
            print(f"❌ CDP 未就绪, 请先运行 --setup-chrome 启动 Chrome")
            sys.exit(1)
        if not check_login_state(args.cdp_port):
            print("❌ 未检测到登录状态, 请在 Chrome 中登录 zhipin.com")
            sys.exit(1)

        crawler = JobCrawler(
            cdp_port=args.cdp_port,
            jobs_dir=args.jobs_dir,
            max_pages=args.max_pages,
            delay_min=args.delay_min,
            delay_max=args.delay_max,
            fetch_company=not args.no_company,
            debug=args.debug,
        )
        crawler.crawl_from_url(args.crawl, har_path=args.har)
        return

    # 5. 单职位采集模式 (原有功能)
    if args.url:
        if not args.url.startswith("https://www.zhipin.com/"):
            print("错误: 请输入有效的 BOSS直聘链接")
            sys.exit(1)
        if not is_cdp_ready(args.cdp_port):
            print(f"❌ CDP 未就绪, 请先运行 --setup-chrome 启动 Chrome")
            sys.exit(1)
        if not check_login_state(args.cdp_port):
            print("❌ 未检测到登录状态, 请在 Chrome 中登录 zhipin.com")
            sys.exit(1)

        print(f"开始抓取: {args.url}")
        fetch_company = not args.no_company
        if fetch_company:
            print(f"  → 将同时抓取公司页面")
        else:
            print(f"  → 仅抓取 JD 页面")

        job_dir, job_id = crawl_single_jd(
            args.url,
            cdp_port=args.cdp_port,
            debug=args.debug,
            fetch_company=fetch_company,
            jobs_dir=args.jobs_dir,
        )

        if job_dir:
            print(f"\n✅ 抓取成功! 已保存到: {job_dir}")
            print(f"   ID: {job_id}")

            # 读取并显示摘要
            structured_path = os.path.join(job_dir, "structured.json")
            if os.path.exists(structured_path):
                with open(structured_path, "r", encoding="utf-8") as f:
                    structured = json.load(f)
                print(f"   职位: {structured.get('job_name', 'N/A')}")
                print(f"   公司: {structured.get('company_name', 'N/A')}")
                salary_low = structured.get("job_salary_low_10k")
                salary_high = structured.get("job_salary_high_10k")
                if salary_low and salary_high:
                    print(
                        f"   薪资: {salary_low}-{salary_high}万/年 "
                        f"{structured.get('salary_composition', '')}"
                    )
                print(f"   地址: {structured.get('company_address', 'N/A')}")
                skills = structured.get("skill_tags", [])
                print(f"   技术栈: {', '.join(skills) if skills else 'N/A'}")

                jd_len = len(
                    structured.get("job_responsibility", "")
                    + structured.get("job_requirement", "")
                )
                print(f"   JD 长度: {jd_len} 字")

            print(f"\n文件列表:")
            for f_name in os.listdir(job_dir):
                fpath = os.path.join(job_dir, f_name)
                size = os.path.getsize(fpath)
                print(f"   {f_name} ({size} bytes)")
        else:
            print(f"\n❌ 抓取失败")
            sys.exit(1)
        return

    # 无参数时显示帮助
    parser.print_help()


if __name__ == "__main__":
    main()
