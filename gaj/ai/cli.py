"""AI 打分 CLI。

用法:
    # 构建提示词但不调用大模型 (dry-run, 不需要浏览器)
    python -m gaj.ai.cli --dry-run --job <job_id>

    # 单个职位 AI 打分
    python -m gaj.ai.cli --job <job_id> --provider deepseek
    python -m gaj.ai.cli --job <job_id> --provider deepseek --deep

    # 查看 backlog 候选名单 (不调用大模型)
    python -m gaj.ai.cli --dry-run --pool all

    # 查看可用 provider
    python -m gaj.ai.cli --list-providers
"""

from __future__ import annotations

import argparse
import sys

from ..logging_setup import get_logger, setup

log = get_logger("ai.cli")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AI 打分 (网页版大模型)")
    ap.add_argument("--job", metavar="JOB_ID", help="单个职位 AI 打分")
    ap.add_argument("--provider", default="deepseek", help="大模型 provider")
    ap.add_argument("--deep", action="store_true", help="生成深度分析报告")
    ap.add_argument(
        "--pool", default="", choices=["", "backfill", "rescore", "all"],
        help="走打分 backlog 队列: backfill=补历史未打分, rescore=重打过分,"
             " all=两者合并。配合 --dry-run 查看候选名单",
    )
    ap.add_argument("--min-rule-score", type=float, default=None, help="规则分下限, 低于不打")
    ap.add_argument("--cooldown-hours", type=float, default=None, help="重打冷却 (小时)")
    ap.add_argument("--max-age-days", type=float, default=None, help="分数保鲜期 (天)")
    ap.add_argument("--include-rejected", action="store_true", help="包含 REJECTED 岗位")
    ap.add_argument(
        "--dry-run", action="store_true",
        help="只构建提示词/挑候选, 不调用大模型 (--job 看提示词, --pool 看候选名单)",
    )
    ap.add_argument("--list-providers", action="store_true", help="列出可用 provider")
    args = ap.parse_args(argv)

    if args.list_providers:
        from ..browser import available_providers

        print("可用 provider:", ", ".join(available_providers()))
        return 0

    if args.dry_run and args.pool and not args.job:
        from .backlog import pick_backlog

        cands = pick_backlog(
            args.pool,
            min_rule_score=args.min_rule_score,
            include_rejected=args.include_rejected,
            cooldown_hours=args.cooldown_hours,
            max_age_days=args.max_age_days,
        )
        print(f"pool={args.pool} 候选 {len(cands)} 个:")
        for c in cands:
            print(
                f"  [{c['pool']}] {c['job_id']}  rule={c['rule_total']}"
                f"  {c['title']} @ {c['company_name']}  ({c['reason']})"
            )
        return 0

    if args.dry_run:
        if not args.job:
            print("--dry-run 需要 --job 或 --pool", file=sys.stderr)
            return 1
        from .runner import build_prompt

        prompt = build_prompt(args.job, deep=args.deep)
        print(prompt)
        print(f"\n\n--- prompt 共 {len(prompt)} 字 ---", file=sys.stderr)
        return 0

    if args.job:
        from .runner import score_with_ai

        out = score_with_ai(args.job, args.provider, deep=args.deep)
        if out["success"]:
            r = out["result"]
            print(
                f"\n✓ {r['status']}  {r['total_score']}/10  {r['recommendation']}\n"
                f"  {r['recommendation_reason']}\n"
                f"  财务 {r['dimension_scores']['finance']} / "
                f"成长 {r['dimension_scores']['growth']} / "
                f"资源 {r['dimension_scores']['resource']} / "
                f"WLB {r['dimension_scores']['wlb']}"
            )
            if r.get("highlights"):
                print("\n  亮点:")
                for h in r["highlights"]:
                    print(f"    + {h}")
            if r.get("risks"):
                print("\n  风险:")
                for r2 in r["risks"]:
                    print(f"    ! {r2}")
            if r.get("deep_analysis_report"):
                print(f"\n  深度分析:\n{r['deep_analysis_report']}")
            print(f"\n  耗时 {out['elapsed']}s")
            return 0
        else:
            print(f"\n✗ 失败: {out['error']}", file=sys.stderr)
            return 1

    ap.print_help()
    return 0


if __name__ == "__main__":
    setup()
    sys.exit(main())
