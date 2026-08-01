"""统一 CLI —— 所有功能的入口。

用法:
    python3 -m gaj crawl <url>              # 从 BOSS 列表页 URL 爬取
    python3 -m gaj score [--all|--job ID]   # 规则打分
    python3 -m gaj ai-score [--job ID|--batch] [--provider deepseek]
    python3 -m gaj resume --job ID          # 生成针对性简历
    python3 -m gaj web [--port 8765]        # 启动 Web 看板
    python3 -m gaj reindex                  # 重建索引
    python3 -m gaj migrate [--src jobs]     # 迁移老数据
    python3 -m gaj fix-conflicts [--dry-run]  # 自动修复 brand_id 串号
    python3 -m gaj setup-chrome             # 启动 Chrome CDP 调试模式
    python3 -m gaj check                    # 检查环境
"""

from __future__ import annotations

import argparse
import sys

from .logging_setup import get_logger, setup

log = get_logger("cli")


def main(argv: list[str] | None = None) -> int:
    setup()
    ap = argparse.ArgumentParser(
        prog="gaj",
        description="Get A Job — 个人猎头系统",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    # ---- crawl ----
    p = sub.add_parser("crawl", help="从 BOSS 列表页 URL 爬取")
    p.add_argument("url", help="BOSS直聘筛选过的列表页 URL")
    p.add_argument("--max-pages", type=int, default=None, help="最大翻页数")
    p.add_argument("--no-company", action="store_true", help="不抓公司详情页")
    p.add_argument("--no-score", action="store_true", help="不自动打分")

    # ---- score ----
    p = sub.add_parser("score", help="规则打分")
    p.add_argument("--all", action="store_true", help="批量打分")
    p.add_argument("--job", metavar="ID", help="单个职位打分")
    p.add_argument("--force", action="store_true", help="重新打分")

    # ---- ai-score ----
    p = sub.add_parser("ai-score", help="AI 打分 (网页版大模型)")
    p.add_argument("--job", metavar="ID", help="单个职位")
    p.add_argument("--batch", action="store_true", help="批量 (只打需 AI 介入的)")
    p.add_argument("--provider", default="deepseek")
    p.add_argument("--deep", action="store_true", help="深度分析")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--dry-run", action="store_true", help="只构建提示词不调用")

    # ---- resume ----
    p = sub.add_parser("resume", help="简历生成")
    p.add_argument("--job", metavar="ID", required=True, help="目标职位 ID")
    p.add_argument("--provider", default="deepseek")
    p.add_argument("--style", choices=["optimize", "rewrite"], default="optimize")
    p.add_argument("--set-master", metavar="FILE", help="设置主简历")
    p.add_argument("--list", action="store_true", help="列出已有定制简历")

    # ---- web ----
    p = sub.add_parser("web", help="启动 Web 看板")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--reload", action="store_true", default=True, help="代码变更自动重启 (默认开启)")
    p.add_argument("--no-reload", action="store_false", dest="reload", help="关闭自动重启")

    # ---- reindex ----
    sub.add_parser("reindex", help="重建索引")

    # ---- migrate ----
    p = sub.add_parser("migrate", help="迁移老数据 (jobs/ → data/jobs/)")
    p.add_argument("--src", default="jobs", help="源目录")
    p.add_argument("--dry-run", action="store_true")

    # ---- fix-conflicts ----
    p = sub.add_parser("fix-conflicts", help="自动修复 brand_id 串号遗留的脏数据")
    p.add_argument("--dry-run", action="store_true", help="只看报告, 不落盘")
    p.add_argument("--no-index", action="store_true", help="跳过索引重建")

    # ---- setup-chrome ----
    p = sub.add_parser("setup-chrome", help="启动 Chrome CDP 调试模式")
    p.add_argument("--port", type=int, default=9222)

    # ---- check ----
    sub.add_parser("check", help="检查环境")

    args = ap.parse_args(argv)

    if args.command == "crawl":
        from .scraper import crawl

        out = crawl(
            args.url,
            max_pages=args.max_pages,
            fetch_company=not args.no_company,
            auto_score=not args.no_score,
        )
        if "error" in out:
            print(f"\n❌ {out['error']}", file=sys.stderr)
            return 1
        print(f"\n✓ 采集完成, 耗时 {out.get('elapsed', 0)}s")
        if "migrated" in out:
            m = out["migrated"]
            print(f"  迁移: {m.get('migrated', 0)} 职位, {m.get('companies', 0)} 公司")
        if "scored" in out:
            s = out["scored"]
            print(f"  打分: 通过 {s.get('PASS', 0)} / 复核 {s.get('REVIEW', 0)} / 淘汰 {s.get('REJECTED', 0)}")
        return 0

    if args.command == "score":
        from .core.score_runner import score_all
        from .core.scoring import STATUS_PASS, STATUS_REJECTED, STATUS_REVIEW, explain

        if args.job:
            out = score_all(force=True, only=args.job)
            for _job, result in out["results"]:
                print(explain(result))
        else:
            out = score_all(force=args.force)
            stats = out["stats"]
            print(
                f"\n打分完成: 通过 {stats.get(STATUS_PASS, 0)} / "
                f"待复核 {stats.get(STATUS_REVIEW, 0)} / "
                f"淘汰 {stats.get(STATUS_REJECTED, 0)} / "
                f"跳过 {stats.get('skipped', 0)}\n"
            )
        return 0

    if args.command == "ai-score":
        from .ai.cli import main as ai_main

        cli_args = []
        if args.dry_run:
            cli_args.extend(["--dry-run", "--job", args.job or ""])
        elif args.batch:
            cli_args.extend(["--batch", "--provider", args.provider])
            if args.limit:
                cli_args.extend(["--limit", str(args.limit)])
        elif args.job:
            cli_args.extend(["--job", args.job, "--provider", args.provider])
            if args.deep:
                cli_args.append("--deep")
        else:
            print("请指定 --job 或 --batch", file=sys.stderr)
            return 1
        return ai_main(cli_args)

    if args.command == "resume":
        from .resume.cli import main as resume_main

        cli_args = ["--job", args.job, "--provider", args.provider, "--style", args.style]
        if args.set_master:
            cli_args = ["--set-master", args.set_master]
        elif args.list:
            cli_args = ["--list", "--job", args.job]
        return resume_main(cli_args)

    if args.command == "web":
        from .web import run

        run(host=args.host, port=args.port, reload=args.reload)
        return 0

    if args.command == "reindex":
        from .store import index

        out = index.reindex()
        print(f"✓ 索引重建: {out['jobs']} 职位, {out['companies']} 公司, {out['seconds']}s")
        return 0

    if args.command == "migrate":
        from pathlib import Path

        from .store.migrate import migrate

        report = migrate(src=Path(args.src), dry_run=args.dry_run)
        print(report)
        return 0

    if args.command == "fix-conflicts":
        from .store.migrate import fix_conflicts

        report = fix_conflicts(
            dry_run=args.dry_run,
            rebuild_index=not args.no_index,
        )
        print(report.render())
        if args.dry_run:
            print("  (dry-run, 未写入任何文件)\n")
        return 0

    if args.command == "setup-chrome":
        from boss_scraper.chrome_manager import run_setup_chrome

        run_setup_chrome(args.port)
        return 0

    if args.command == "check":
        from .scraper import check_chrome

        ok = check_chrome()
        if ok:
            print("✓ Chrome CDP 就绪")
            return 0
        else:
            print("✗ Chrome CDP 未运行, 请先执行: python3 -m gaj setup-chrome")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
