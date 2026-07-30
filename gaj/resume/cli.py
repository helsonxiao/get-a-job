"""简历生成 CLI。

用法:
    # 生成针对某岗位的优化简历
    python -m gaj.resume.cli --job <job_id> --provider deepseek

    # 完全重写
    python -m gaj.resume.cli --job <job_id> --style rewrite

    # 查看已有的定制简历
    python -m gaj.resume.cli --list
    python -m gaj.resume.cli --list --job <job_id>

    # 设置主简历
    python -m gaj.resume.cli --set-master resume.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..logging_setup import get_logger, setup
from ..store import repo

log = get_logger("resume.cli")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="简历生成")
    ap.add_argument("--job", metavar="JOB_ID", help="目标职位 ID")
    ap.add_argument("--provider", default="deepseek")
    ap.add_argument("--style", choices=["optimize", "rewrite"], default="optimize")
    ap.add_argument("--list", action="store_true", help="列出已有的定制简历")
    ap.add_argument("--set-master", metavar="FILE", help="从文件设置主简历")
    args = ap.parse_args(argv)

    if args.set_master:
        src = Path(args.set_master)
        if not src.exists():
            print(f"文件不存在: {src}", file=sys.stderr)
            return 1
        text = src.read_text(encoding="utf-8")
        path = repo.save_master_resume(text)
        print(f"主简历已保存: {path}")
        return 0

    if args.list:
        items = repo.list_tailored_resumes(args.job)
        if not items:
            print("暂无定制简历")
            return 0
        print(f"共 {len(items)} 份:")
        for it in items:
            print(f"  {it['name']}  job={it['job_id']}  provider={it['provider']}")
        return 0

    if args.job:
        from .generator import generate_resume

        out = generate_resume(args.job, args.provider, style=args.style)
        if out["success"]:
            print(f"\n✓ 简历已生成: {out['resume_path']}")
            print(f"  耗时 {out['elapsed']}s, {len(out['content'])} 字")
            return 0
        else:
            print(f"\n✗ 失败: {out['error']}", file=sys.stderr)
            return 1

    ap.print_help()
    return 0


if __name__ == "__main__":
    setup()
    sys.exit(main())
