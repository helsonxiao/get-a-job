"""AI 打分 CLI。

用法:
    # 构建提示词但不调用大模型 (dry-run, 不需要浏览器)
    python -m gaj.ai.cli --dry-run --job <job_id>

    # 单个职位 AI 打分
    python -m gaj.ai.cli --job <job_id> --provider deepseek
    python -m gaj.ai.cli --job <job_id> --provider deepseek --deep

    # 批量打分 (默认只处理规则引擎标记需要 AI 介入的)
    python -m gaj.ai.cli --batch --provider deepseek
    python -m gaj.ai.cli --batch --provider doubao --limit 20

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
    ap.add_argument("--batch", action="store_true", help="批量打分")
    ap.add_argument("--provider", default="deepseek", help="大模型 provider")
    ap.add_argument("--deep", action="store_true", help="生成深度分析报告")
    ap.add_argument("--limit", type=int, default=None, help="批量打分上限")
    ap.add_argument(
        "--all", action="store_true",
        help="批量时不过滤 ai_intervention_needed, 所有职位都打",
    )
    ap.add_argument("--dry-run", action="store_true", help="只构建提示词, 不调用大模型")
    ap.add_argument("--list-providers", action="store_true", help="列出可用 provider")
    args = ap.parse_args(argv)

    if args.list_providers:
        from ..browser import available_providers

        print("可用 provider:", ", ".join(available_providers()))
        return 0

    if args.dry_run:
        if not args.job:
            print("--dry-run 需要配合 --job 使用", file=sys.stderr)
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

    if args.batch:
        from .runner import batch_score

        out = batch_score(
            args.provider,
            only_triggered=not args.all,
            limit=args.limit,
            deep=args.deep,
        )
        print(
            f"\n批量打分完成: 成功 {out['success']} / 失败 {out['failed']} / 共 {out['total']}"
        )
        for d in out["details"]:
            mark = "✓" if d.get("success") else "✗"
            err = d.get("error", "")
            print(f"  {mark} {d['job_id']}  {err}")
        return 0 if out["failed"] == 0 else 1

    ap.print_help()
    return 0


if __name__ == "__main__":
    setup()
    sys.exit(main())
