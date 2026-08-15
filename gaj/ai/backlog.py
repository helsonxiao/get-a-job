"""打分待办队列 (backlog)。

"给哪些岗位喂 AI"的统一决策层, daily 兜底 / 批量补分 / Web 补分按钮都从这里
取候选, 不再各写各的选择逻辑。

队列档位 (见 design/图鉴进化方案.md Part A):
  backfill (P1): 有规则分、从未 AI 打分、未忽略的岗位。
                 规则分降序 → 收藏优先 → 越老越优先清欠。
  rescore  (P2): 已有 AI 分但被判 stale (上下文变了 / 超过保鲜期),
                 且最新一次打分已过冷却期。最旧的优先。
  all: P1 全量接 P2。

P0 新岗首打由 daily 自己的 _pick_candidates 处理 (它有"本次新抓"信息),
P3 手动指定就是 analyze --job, 都不经过这里。
"""

from __future__ import annotations

import time

from .. import config as cfg
from ..logging_setup import get_logger
from ..store import index

log = get_logger("ai.backlog")

POOL_BACKFILL = "backfill"
POOL_RESCORE = "rescore"
POOL_ALL = "all"
VALID_POOLS = (POOL_BACKFILL, POOL_RESCORE, POOL_ALL)

_NOT_IGNORED = "(ignored = 0 OR ignored IS NULL)"


def backlog_stats() -> dict:
    """backlog 计数器, 供 agent status / Web 统计展示。

    Returns:
        {
          "unscored": int,              # 有规则分但从未 AI 打分 (可补分)
          "stale_total": int,           # 需重打分总数
          "stale_context_changed": int, # 其中: 打分预期已变
          "stale_expired": int,         # 其中: 分数超过保鲜期
        }
    """
    with index.session() as conn:
        index.refresh_ai_stale(conn)
        row = conn.execute(
            "SELECT"
            " COALESCE(SUM(rule_total IS NOT NULL AND ai_count = 0"
            f"   AND {_NOT_IGNORED}), 0) AS unscored,"
            " COALESCE(SUM(ai_count > 0 AND ai_stale = 1"
            f"   AND {_NOT_IGNORED}), 0) AS stale_total,"
            " COALESCE(SUM(ai_count > 0 AND ai_stale = 1"
            f"   AND ai_stale_reason = 'context_changed' AND {_NOT_IGNORED}), 0) AS ctx,"
            " COALESCE(SUM(ai_count > 0 AND ai_stale = 1"
            f"   AND ai_stale_reason = 'expired' AND {_NOT_IGNORED}), 0) AS expired"
            " FROM jobs"
        ).fetchone()
    return {
        "unscored": row["unscored"],
        "stale_total": row["stale_total"],
        "stale_context_changed": row["ctx"],
        "stale_expired": row["expired"],
    }


def pick_backlog(
    pool: str = POOL_ALL,
    *,
    limit: int | None = None,
    min_rule_score: float | None = None,
    include_rejected: bool = False,
    include_ignored: bool = False,
    cooldown_hours: float | None = None,
    max_age_days: float | None = None,
    prefer_triggered: bool = False,
) -> list[dict]:
    """按优先级挑选打分候选。

    Args:
        pool: backfill | rescore | all
        limit: 最多返回多少个 (None=不限)
        min_rule_score: 规则分下限, 低于它的不值得消耗预算
        include_rejected: 是否包含 REJECTED 岗位 (默认不含)
        include_ignored: 是否包含已忽略岗位 (默认不含)
        cooldown_hours: 重打冷却 (小时), 默认取 AIConfig.rescore_cooldown_hours
        max_age_days: 分数保鲜期 (天), 默认取 AIConfig.stale_ttl_days;
                      会先以此刷新 ai_stale 标记
        prefer_triggered: backfill 排序时把"规则引擎标记需 AI 介入"的排前面
                          (daily 兜底用)

    Returns:
        [{job_id, title, company_name, city, pool, rule_total, ai_needed,
          latest_ai_at, reason}, ...]
    """
    if pool not in VALID_POOLS:
        raise ValueError(f"非法 pool: {pool}, 可选 {VALID_POOLS}")

    cooldown = (
        cfg.SETTINGS.ai.rescore_cooldown_hours if cooldown_hours is None else cooldown_hours
    )
    cooldown_cutoff = time.strftime(
        "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - cooldown * 3600)
    )

    ignored_clause = "1=1" if include_ignored else _NOT_IGNORED
    status_clause = "1=1" if include_rejected else "rule_status != 'REJECTED'"

    extra_where: list[str] = []
    extra_params: list = []
    if min_rule_score is not None:
        extra_where.append("rule_total >= ?")
        extra_params.append(float(min_rule_score))
    extra_sql = (" AND " + " AND ".join(extra_where)) if extra_where else ""

    cols = (
        "job_id, title, company_name, city, rule_total, ai_needed,"
        " favorite, first_seen, latest_ai_at, ai_stale_reason"
    )

    out: list[dict] = []
    with index.session() as conn:
        # 预期可能刚变过, 先刷新 stale 标记再选
        index.refresh_ai_stale(conn, ttl_days=max_age_days)

        if pool in (POOL_BACKFILL, POOL_ALL):
            triggered_order = "ai_needed DESC, " if prefer_triggered else ""
            rows = conn.execute(
                f"SELECT {cols} FROM jobs"
                f" WHERE rule_total IS NOT NULL AND ai_count = 0"
                f" AND {ignored_clause} AND {status_clause}{extra_sql}"
                f" ORDER BY {triggered_order}(rule_total IS NULL),"
                f" rule_total DESC, favorite DESC,"
                f" REPLACE(first_seen, ' ', 'T') ASC",
                extra_params,
            ).fetchall()
            for r in rows:
                reason = "历史未打分"
                if r["ai_needed"]:
                    reason += " + 规则引擎标记需 AI 介入"
                out.append(_candidate(r, POOL_BACKFILL, reason))

        if pool in (POOL_RESCORE, POOL_ALL):
            rows = conn.execute(
                f"SELECT {cols} FROM jobs"
                f" WHERE ai_count > 0 AND ai_stale = 1"
                f" AND {ignored_clause} AND {status_clause}{extra_sql}"
                # 冷却期内不重复重打
                " AND (latest_ai_at IS NULL OR latest_ai_at = ''"
                "      OR latest_ai_at <= ?)"
                f" ORDER BY (latest_ai_at IS NULL) DESC, latest_ai_at ASC,"
                f" rule_total DESC",
                [*extra_params, cooldown_cutoff],
            ).fetchall()
            for r in rows:
                why = r["ai_stale_reason"] or "stale"
                reason = {
                    "context_changed": "打分预期已变化",
                    "expired": "分数超过保鲜期",
                }.get(why, f"分数过时({why})")
                out.append(_candidate(r, POOL_RESCORE, reason))

    if limit is not None:
        out = out[: max(0, int(limit))]
    log.info(
        f"backlog 挑选: pool={pool} 返回 {len(out)} 个候选"
        f" (min_rule_score={min_rule_score}, include_rejected={include_rejected})"
    )
    return out


def _candidate(row, pool: str, reason: str) -> dict:
    return {
        "job_id": row["job_id"],
        "title": row["title"],
        "company_name": row["company_name"],
        "city": row["city"],
        "pool": pool,
        "rule_total": row["rule_total"],
        "ai_needed": bool(row["ai_needed"]),
        "latest_ai_at": row["latest_ai_at"],
        "reason": reason,
    }
