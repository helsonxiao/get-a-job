"""批量规则打分: 遍历 data/jobs, 打分, 落盘, 更新索引。

用法::

    python -m gaj.core.score_runner                # 给所有职位打分
    python -m gaj.core.score_runner --force        # 已打过的也重打
    python -m gaj.core.score_runner --job <id>     # 只打一条并打印明细
    python -m gaj.core.score_runner --top 15       # 打完后列出前 15 名
"""

from __future__ import annotations

import argparse

from .. import config as cfg
from ..logging_setup import get_logger, setup
from ..store import index, repo
from .profile import load_profile
from .scoring import STATUS_PASS, STATUS_REJECTED, STATUS_REVIEW, explain, score_job

log = get_logger("score")


def score_all(*, force: bool = False, only: str = "") -> dict:
    profile = load_profile()
    stats = {STATUS_PASS: 0, STATUS_REVIEW: 0, STATUS_REJECTED: 0, "skipped": 0}
    results = []

    jobs = [repo.load_job(only)] if only else list(repo.iter_jobs())
    jobs = [j for j in jobs if j]

    companies: dict[str, object] = {}
    for job in jobs:
        if not force and not only and repo.load_rule_score(job.job_id):
            stats["skipped"] += 1
            continue
        if job.company_id not in companies:
            companies[job.company_id] = repo.load_company(job.company_id)
        result = score_job(job, companies[job.company_id], profile)  # type: ignore[arg-type]
        repo.save_rule_score(job.job_id, result.to_dict())
        stats[result.status] = stats.get(result.status, 0) + 1
        results.append((job, result))

    if results:
        with index.session() as conn:
            for job, _ in results:
                index.upsert_job(conn, job, companies.get(job.company_id))  # type: ignore[arg-type]

    return {"stats": stats, "results": results}


def main() -> None:
    setup()
    ap = argparse.ArgumentParser(description="规则打分")
    ap.add_argument("--force", action="store_true", help="重打已有分数")
    ap.add_argument("--job", default="", help="只打一条职位")
    ap.add_argument("--top", type=int, default=10, help="打印排名前 N")
    args = ap.parse_args()

    out = score_all(force=args.force or bool(args.job), only=args.job)
    stats = out["stats"]

    if args.job:
        for _job, result in out["results"]:
            print(explain(result))
        return

    print(
        f"\n打分完成: 通过 {stats.get(STATUS_PASS, 0)} / "
        f"待复核 {stats.get(STATUS_REVIEW, 0)} / "
        f"淘汰 {stats.get(STATUS_REJECTED, 0)} / "
        f"跳过 {stats.get('skipped', 0)}\n"
    )

    with index.session() as conn:
        rows = index.query_jobs(conn, sort="best_total", limit=args.top)
    print(f"{'分数':>6} {'状态':<9} {'年薪':>12}  {'城市':<5} {'职位':<26} 公司")
    print("-" * 100)
    for r in rows:
        smin, smax = r.get("salary_min"), r.get("salary_max")
        pay = f"{smin or 0:.0f}-{smax or 0:.0f}万" if smax else "面议"
        status = r.get("rule_status") or "-"
        print(
            f"{(r.get('best_total') or 0):>6.2f} {status:<9} {pay:>12}  "
            f"{(r.get('city') or ''):<5} {(r.get('title') or '')[:24]:<26} "
            f"{(r.get('company_name') or '')[:22]}"
        )
    ai_need = sum(1 for r in rows if r.get("ai_needed"))
    print(f"\n其中 {ai_need} 条建议 AI 介入复核。")


if __name__ == "__main__":
    main()
