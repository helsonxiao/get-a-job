"""打分上下文指纹 (context fingerprint)。

指纹输入 = data/profile.md 全文 + data/scoring_config.json 全文 + RULES_VERSION。
三者任一变化都意味着"用户的打分预期变了", 此前打出的 AI 分数应被标记为
stale (过时), 进入重打分队列 (见 gaj/ai/backlog.py)。

设计取舍:
  - 指纹只描述"打分时的预期快照", 不描述岗位本身, 所以重抓 JD 不影响它。
  - 旧数据 (没有 context_fingerprint 字段的 AI 打分文件) 不自动判 stale,
    只受 TTL 约束 —— 避免给存量数据一次性挂上还不上的重打分债。
"""

from __future__ import annotations

import hashlib
import time

from .. import config as cfg

# AI 分数保鲜期 (天): 超过这个时间的分数即使上下文没变也视为过期。
# 定得长是因为打分是低频行为, 短 TTL 会制造还不完的过期债。
DEFAULT_STALE_TTL_DAYS = 90.0

# 重打冷却 (小时): 同一岗位在冷却期内不进入重打队列, 防止反复烧预算。
DEFAULT_RESCORE_COOLDOWN_HOURS = 72.0


def compute_context_fingerprint() -> str:
    """计算当前打分上下文指纹 (md5 前 12 位)。"""
    from .scoring import RULES_VERSION
    from .scoring_config import SCORING_CONFIG_PATH

    h = hashlib.md5()
    profile_text = ""
    if cfg.PROFILE_PATH.exists():
        try:
            profile_text = cfg.PROFILE_PATH.read_text(encoding="utf-8")
        except OSError:
            profile_text = ""
    h.update(profile_text.encode("utf-8"))
    h.update(b"\x00")

    config_text = ""
    if SCORING_CONFIG_PATH.exists():
        try:
            config_text = SCORING_CONFIG_PATH.read_text(encoding="utf-8")
        except OSError:
            config_text = ""
    h.update(config_text.encode("utf-8"))
    h.update(b"\x00")

    h.update(str(RULES_VERSION).encode("utf-8"))
    return h.hexdigest()[:12]


def parse_iso(ts: str) -> float | None:
    """把 ISO 时间串转成本地时间戳, 解析失败返回 None。

    兼容 "YYYY-MM-DDTHH:MM:SS" 与 "YYYY-MM-DD HH:MM:SS" 两种历史格式。
    """
    if not ts:
        return None
    try:
        return time.mktime(time.strptime(ts.replace(" ", "T")[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return None


def stale_info(
    latest_ai: dict | None,
    *,
    current_fp: str | None = None,
    ttl_days: float = DEFAULT_STALE_TTL_DAYS,
    now: float | None = None,
) -> tuple[int, str]:
    """判定某个岗位的最新 AI 打分是否过时。

    Args:
        latest_ai: repo.latest_ai_score 返回的最新 AI 打分记录 (None=从未打分)
        current_fp: 当前上下文指纹; None 时自动计算
        ttl_days: 分数保鲜期 (天)
        now: 当前时间戳 (测试注入用)

    Returns:
        (ai_stale, ai_stale_reason):
          (0, "")               未过时 / 从未打分
          (1, "context_changed") 打分时的预期与现在不一致
          (1, "expired")         分数超过保鲜期
    """
    if not latest_ai:
        return 0, ""
    now = now if now is not None else time.time()

    # 规则 1: 上下文指纹不一致 (旧数据没有指纹字段, 跳过此规则)
    fp = latest_ai.get("context_fingerprint")
    if fp:
        if current_fp is None:
            current_fp = compute_context_fingerprint()
        if fp != current_fp:
            return 1, "context_changed"

    # 规则 2: TTL 过期
    scored_at = parse_iso(latest_ai.get("created_at", ""))
    if scored_at is not None and (now - scored_at) > ttl_days * 86400:
        return 1, "expired"

    return 0, ""
