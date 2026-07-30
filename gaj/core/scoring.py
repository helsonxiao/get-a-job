"""规则打分引擎 (scoring_rules.md v3 的代码化实现)。

设计要点
--------

**1. 每一分都要能追溯。**
不返回一个孤零零的 7.7 分, 而是返回每个评分项的 ``ScoreItem``
(得了几分 / 满分几分 / 为什么)。可视化页面能直接展开成一棵账目树,
你能一眼看出"这 7.7 分是怎么来的"、"哪一项拖了后腿"。

**2. 硬性淘汰要看证据置信度。**
原规则写的是"加班强度 == 996 则淘汰"。但加班强度是从 JD 文本反推出来的,
推断错了就会静默杀掉一个好机会 —— 这是最危险的失败模式, 因为你根本
不会知道自己错过了什么。所以这里把淘汰分成两级:

- 证据确凿 (confidence >= 0.6) -> ``REJECTED``, 直接出局
- 证据不足 (confidence <  0.6) -> ``REVIEW``, 不出局, 但标记需要 AI 复核

**3. 权重取自画像。**
scoring_rules.md 写的是 40/30/20/10, profile.md 里用户自己填的是
30/30/30/10 (更看重生活平衡)。以用户自己填的为准 —— 规则文件是模板,
画像才是本人意志。可通过 ScoringConfig.use_profile_weights 切换。
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .. import config as cfg
from ..logging_setup import get_logger
from .models import Company, Job
from .profile import Profile
from .scoring_config import ScoringOverrides, load_overrides

log = get_logger("scoring")

RULES_VERSION = "3.1"

#: 硬性淘汰所需的最低证据置信度 (代码默认值, 可被 scoring_config.json 覆盖)。
REJECT_CONFIDENCE_FLOOR = 0.6

STATUS_PASS = "PASS"
STATUS_REVIEW = "REVIEW"
STATUS_REJECTED = "REJECTED"


def _load_active_overrides() -> ScoringOverrides:
    """读取当前生效的覆盖项。失败时返回空对象 (全用代码默认值)。"""
    return load_overrides()


# ---------------------------------------------------------------- 结果结构


@dataclass
class ScoreItem:
    """一个评分项的明细。"""

    code: str
    label: str
    points: float
    max_points: float
    detail: str = ""
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DimensionScore:
    key: str
    label: str
    score: float = 0.0
    max_score: float = 10.0
    items: list[ScoreItem] = field(default_factory=list)

    def add(self, item: ScoreItem) -> None:
        self.items.append(item)

    def finalize(self) -> "DimensionScore":
        raw = sum(i.points for i in self.items)
        self.score = round(min(raw, self.max_score), 2)
        return self

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "score": self.score,
            "max_score": self.max_score,
            "items": [i.to_dict() for i in self.items],
        }


@dataclass
class HardCheck:
    """一条硬性淘汰规则的判定结果。"""

    code: str
    label: str
    hit: bool = False
    confidence: float = 1.0
    reason: str = ""
    evidence: list[str] = field(default_factory=list)
    #: 最低证据置信度, 低于此值只标 REVIEW。由 run_hard_checks 注入。
    floor: float = REJECT_CONFIDENCE_FLOOR

    @property
    def is_fatal(self) -> bool:
        """命中且证据可信 -> 真淘汰。"""
        return self.hit and self.confidence >= self.floor

    @property
    def is_suspicion(self) -> bool:
        """命中但证据不足 -> 存疑, 交给 AI。"""
        return self.hit and self.confidence < self.floor

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fatal"] = self.is_fatal
        return d


@dataclass
class ScoreResult:
    job_id: str = ""
    status: str = STATUS_PASS
    reject_reason: str | None = None
    hard_checks: list[HardCheck] = field(default_factory=list)
    dimensions: dict[str, DimensionScore] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    total_score: float = 0.0          # 0~10
    ai_intervention_needed: bool = False
    triggered_ai_rules: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw_data: dict[str, Any] = field(default_factory=dict)
    rules_version: str = RULES_VERSION
    created_at: str = ""

    @property
    def total_100(self) -> float:
        return round(self.total_score * 10, 1)

    @property
    def dimension_scores(self) -> dict[str, float]:
        return {k: v.score for k, v in self.dimensions.items()}

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "reject_reason": self.reject_reason,
            "total_score": self.total_score,
            "total_100": self.total_100,
            "dimension_scores": self.dimension_scores,
            "weights": self.weights,
            "dimensions": {k: v.to_dict() for k, v in self.dimensions.items()},
            "hard_checks": [h.to_dict() for h in self.hard_checks],
            "ai_intervention_needed": self.ai_intervention_needed,
            "triggered_ai_rules": self.triggered_ai_rules,
            "warnings": self.warnings,
            "raw_data": self.raw_data,
            "rules_version": self.rules_version,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------- 匹配工具

_TOKEN_CLEAN_RE = re.compile(r"[^0-9a-z\u4e00-\u9fa5+#.]")


def _norm_token(text: str) -> str:
    return _TOKEN_CLEAN_RE.sub("", (text or "").lower())


def match_skills(jd_skills: Iterable[str], profile_skills: Iterable[str]) -> list[str]:
    """技术栈重合度。

    不能用集合直接取交集 —— BOSS 的标签是 "Node.js"、简历里写 "NodeJS",
    "React Native" 也应该算命中 "React"。这里做归一化后的双向包含匹配,
    并要求至少 2 个字符, 避免 "C" 匹配上一切含 c 的词。
    """
    jd_norm = [(s, _norm_token(s)) for s in jd_skills if s]
    pf_norm = [(s, _norm_token(s)) for s in profile_skills if s]
    matched: list[str] = []
    for raw, j in jd_norm:
        if len(j) < 2:
            continue
        for _praw, p in pf_norm:
            if len(p) < 2:
                continue
            if j == p or (len(j) >= 3 and j in p) or (len(p) >= 3 and p in j):
                matched.append(raw)
                break
    return matched


def match_industry(jd_industry: str, tags: Iterable[str]) -> list[str]:
    """行业 / 业务方向的字符串重合 (规则层只做字面匹配, 语义交给 AI)。"""
    text = _norm_token(jd_industry)
    if not text:
        return []
    hits = []
    for tag in tags:
        t = _norm_token(tag)
        if t and (t in text or text in t):
            hits.append(tag)
    return hits


#: A-03 用: 字面不匹配但语义相近的行业词, 命中就提示 AI 去做相似度判断
_SEMANTIC_HINTS: dict[str, tuple[str, ...]] = {
    "在线教育": ("教育", "培训", "学习", "知识付费", "课程", "школ"),
    "编程教育": ("编程", "少儿编程", "IT培训", "开发者教育"),
    "音频直播": ("音频", "直播", "语音", "社交", "泛娱乐", "内容社区"),
    "低代码平台": ("低代码", "无代码", "aPaaS", "搭建平台", "研发效能"),
    "跨端APP": ("跨端", "移动应用", "小程序", "混合开发", "APP"),
    "SCRM": ("CRM", "私域", "营销", "客户管理", "企微"),
}


def semantic_industry_hint(jd_industry: str, tags: Iterable[str]) -> list[str]:
    """字面没匹配上, 但可能语义相关 —— 返回值非空则触发 A-03。"""
    text = (jd_industry or "").lower()
    if not text:
        return []
    hints = []
    for tag in tags:
        for word in _SEMANTIC_HINTS.get(tag, ()):
            if word.lower() in text:
                hints.append(f"{tag} ~ {jd_industry}")
                break
    return hints


def _sig(job: Job, name: str) -> dict:
    return (job.signals or {}).get(name) or {}


def _sig_value(job: Job, name: str) -> Any:
    return _sig(job, name).get("value")


def _sig_conf(job: Job, name: str) -> float:
    try:
        return float(_sig(job, name).get("confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _sig_evidence(job: Job, name: str) -> list[str]:
    ev = _sig(job, name).get("evidence")
    return list(ev) if isinstance(ev, list) else []


# ---------------------------------------------------------------- 硬性淘汰


def run_hard_checks(job: Job, company: Company, profile: Profile, overrides: ScoringOverrides | None = None) -> list[HardCheck]:
    checks: list[HardCheck] = []
    overrides = overrides or ScoringOverrides()
    floor = overrides.get_floor(REJECT_CONFIDENCE_FLOOR)

    # H-01 城市
    acceptable = profile.all_acceptable_cities()
    city = job.city or ""
    h1 = HardCheck("H-01", "城市不可接受", floor=floor)
    if not city:
        h1.reason = "职位城市未知, 无法判断"
        h1.confidence = 0.0
        h1.evidence.append("city 字段为空")
    elif acceptable and city not in acceptable:
        h1.hit = True
        h1.reason = f"{city} 不在可接受城市 {'/'.join(acceptable)} 内"
        # 城市来自地址反解或列表 API, 基本可信; 但迁移时"猜"的城市不算数
        h1.confidence = 0.4 if job.provenance.get("city_source") == "assumed" else 1.0
        if h1.confidence < 1.0:
            h1.evidence.append("城市为迁移时人工假定, 非抓取所得")
    checks.append(h1)

    # H-02 行业
    h2 = HardCheck("H-02", "行业被拒绝", floor=floor)
    rejected = [r for r in profile.reject_industries if r and r in (company.industry or "")]
    if rejected:
        h2.hit = True
        h2.reason = f"公司行业「{company.industry}」命中拒绝行业 {rejected}"
        h2.evidence.append(company.industry)
    checks.append(h2)

    # H-03 公司规模过小
    h3 = HardCheck("H-03", "公司规模过小", floor=floor)
    scale_floor = profile.reject_scale_below
    if company.scale_max is not None and scale_floor and company.scale_max < scale_floor:
        h3.hit = True
        h3.reason = f"公司规模「{company.scale_raw}」低于下限 {scale_floor} 人"
        h3.evidence.append(company.scale_raw)
    checks.append(h3)

    # H-04 薪资不达标
    h4 = HardCheck("H-04", "薪资低于硬性下限", floor=floor)
    smin = job.salary.get("min_10k")
    hard_min = profile.hard_min_salary_10k
    if smin is None:
        if job.salary.get("negotiable"):
            h4.reason = "薪资面议, 无法判断"
        else:
            h4.reason = "薪资未知, 无法判断"
        h4.confidence = 0.0
    elif smin < hard_min:
        h4.hit = True
        h4.reason = (
            f"年薪下限 {smin} 万 < 硬性底线 {hard_min} 万 "
            f"(原文 {job.salary.get('raw', '')})"
        )
        h4.evidence.append(job.salary.get("raw", ""))
    checks.append(h4)

    # H-05 长期驻场出差
    h5 = HardCheck("H-05", "出差强度超标", floor=floor)
    travel = _sig_value(job, "travel")
    if not profile.accept_travel and travel == "long_term":
        h5.hit = True
        h5.confidence = _sig_conf(job, "travel")
        h5.reason = "JD 要求长期驻场/外派, 而画像明确不接受出差"
        h5.evidence = _sig_evidence(job, "travel")
    checks.append(h5)

    # H-06 加班强度
    h6 = HardCheck("H-06", "加班强度超标", floor=floor)
    if _sig_value(job, "overtime") == "heavy":
        h6.hit = True
        h6.confidence = _sig_conf(job, "overtime")
        h6.reason = "推断为 996 / 大小周 / 单休一类的高强度作息"
        h6.evidence = _sig_evidence(job, "overtime")
    checks.append(h6)

    # H-07 黑名单关键词
    h7 = HardCheck("H-07", "命中排除关键词", floor=floor)
    hits = list((job.signals or {}).get("blacklist_hits") or [])
    if hits:
        h7.hit = True
        h7.reason = f"JD 命中排除关键词: {', '.join(hits)}"
        h7.evidence = hits
    checks.append(h7)

    # H-08 外包 (画像里写了不接受外包, 原规则表漏了这条)
    h8 = HardCheck("H-08", "外包/驻场岗位", floor=floor)
    if not profile.accept_outsourcing and _sig_value(job, "outsourcing") is True:
        h8.hit = True
        h8.confidence = _sig_conf(job, "outsourcing")
        h8.reason = "推断为外包/劳务派遣/驻场岗位, 画像明确不接受"
        h8.evidence = _sig_evidence(job, "outsourcing")
    checks.append(h8)

    return checks


# ---------------------------------------------------------------- 四个维度


def score_finance(job: Job, company: Company, profile: Profile, overrides: ScoringOverrides | None = None) -> DimensionScore:
    ov = overrides or ScoringOverrides()
    dim = DimensionScore("finance", "财务回报")
    smin = job.salary.get("min_10k")
    smax = job.salary.get("max_10k")
    smid = job.salary.get("mid_10k")
    months = job.salary.get("months") or 12

    f01_max = ov.get("F-01", 4.0)
    dim.add(
        ScoreItem(
            "F-01",
            "薪资下限达标",
            f01_max if (smin is not None and smin >= profile.expect_min_salary_10k) else 0.0,
            f01_max,
            f"年薪下限 {smin if smin is not None else '未知'} 万 "
            f"vs 期望下限 {profile.expect_min_salary_10k} 万"
            + (f" (按 {months} 薪折算)" if months != 12 else ""),
        )
    )
    f02_max = ov.get("F-02", 4.0)
    dim.add(
        ScoreItem(
            "F-02",
            "薪资中位数达标",
            f02_max if (smid is not None and smid >= profile.expect_mid_salary_10k) else 0.0,
            f02_max,
            f"中位 {smid if smid is not None else '未知'} 万 "
            f"vs 期望中位 {profile.expect_mid_salary_10k} 万",
        )
    )
    surprise = profile.salary_surprise_threshold_10k
    f03_max = ov.get("F-03", 1.0)
    dim.add(
        ScoreItem(
            "F-03",
            "薪资上限惊喜",
            f03_max if (smax is not None and smax >= surprise) else 0.0,
            f03_max,
            f"上限 {smax if smax is not None else '未知'} 万 vs 惊喜线 {surprise} 万",
        )
    )

    welfare_blob = " ".join(job.welfare)
    equity = any(k in welfare_blob for k in ("股票", "期权", "股权", "限制性股票"))
    f04_max = ov.get("F-04", 0.5)
    dim.add(
        ScoreItem(
            "F-04",
            "股票/期权激励",
            f04_max if equity else 0.0,
            f04_max,
            "福利含股权激励" if equity else "未提及股权激励",
            [w for w in job.welfare if any(k in w for k in ("股票", "期权", "股权"))],
        )
    )

    valued = profile.valued_benefits or ["补充公积金", "补充医疗", "企业年金", "房补"]
    hit_benefits = [v for v in valued if any(v in w or w in v for w in job.welfare)]
    f05_max = ov.get("F-05", 0.5)
    dim.add(
        ScoreItem(
            "F-05",
            "高价值福利",
            min(0.25 * len(hit_benefits), f05_max),
            f05_max,
            f"命中 {len(hit_benefits)} 项看重的福利" if hit_benefits else "未命中高价值福利",
            hit_benefits,
        )
    )
    return dim.finalize()


def score_growth(job: Job, company: Company, profile: Profile, overrides: ScoringOverrides | None = None) -> DimensionScore:
    ov = overrides or ScoringOverrides()
    dim = DimensionScore("growth", "职业发展")

    matched = match_skills(job.skills, profile.skills)
    n = len(set(matched))
    g01_max = ov.get("G-01", 3.0)
    # 按 max 等比例缩放: 0→0, 1→1/6, 2→1/3, 3→2/3, ≥4→1
    stack_ratio = {0: 0.0, 1: 1/6, 2: 1/3, 3: 2/3}.get(n, 1.0)
    dim.add(
        ScoreItem(
            "G-01",
            "技术栈重合度",
            round(g01_max * stack_ratio, 2),
            g01_max,
            f"重合 {n} 项" if n else "技术栈无重合",
            sorted(set(matched)),
        )
    )

    stage = company.stage or ""
    stage_ratio = 0.0
    stage_detail = []
    if any(s in stage for s in ("B轮", "C轮")):
        stage_ratio = 1.0
        stage_detail.append(f"融资阶段 {stage}")
    elif any(s in stage for s in ("D轮", "E轮", "F轮", "已上市", "上市")):
        stage_ratio = 0.5
        stage_detail.append(f"融资阶段 {stage}")
    if company.nature == "国企":
        stage_ratio = min(stage_ratio + 0.5, 1.0)
        stage_detail.append("国有企业")
    g02_max = ov.get("G-02", 2.0)
    dim.add(
        ScoreItem(
            "G-02",
            "公司阶段价值",
            round(g02_max * stage_ratio, 2),
            g02_max,
            " + ".join(stage_detail) or f"阶段「{stage or '未知'}」不加分",
        )
    )

    biz_hits = match_industry(company.industry, profile.industry_tags) or match_industry(
        company.industry, profile.preferred_directions
    )
    g03_max = ov.get("G-03", 2.0)
    dim.add(
        ScoreItem(
            "G-03",
            "业务方向匹配",
            g03_max if biz_hits else 0.0,
            g03_max,
            f"行业「{company.industry}」命中 {biz_hits}" if biz_hits
            else f"行业「{company.industry or '未知'}」与既往经验无字面重合",
            biz_hits,
        )
    )

    team = _sig_value(job, "team_size")
    tmin = profile.team_size_min or 10
    tmax = profile.team_size_max or 50
    in_range = isinstance(team, int) and tmin <= team <= tmax
    g04_max = ov.get("G-04", 1.0)
    dim.add(
        ScoreItem(
            "G-04",
            "团队规模契合",
            g04_max if in_range else 0.0,
            g04_max,
            f"团队约 {team} 人, 偏好 {tmin}~{tmax} 人" if isinstance(team, int)
            else "JD 未提及团队规模",
            _sig_evidence(job, "team_size"),
        )
    )

    exp = job.experience or {}
    edu = job.education or {}
    years = profile.total_years or 0
    exp_ok = (
        exp.get("unlimited")
        or exp.get("min_years") is None
        or float(exp.get("min_years") or 0) <= years
    )
    edu_ok = (
        edu.get("unlimited")
        or not edu.get("level")
        or (edu.get("rank") is not None and profile.education and edu.get("rank", 9) <= 3)
    )
    g05_max = ov.get("G-05", 1.0)
    dim.add(
        ScoreItem(
            "G-05",
            "经验学历兼容",
            g05_max if (exp_ok and edu_ok) else 0.0,
            g05_max,
            f"要求 {exp.get('raw') or '不限'} / {edu.get('raw') or '不限'}; "
            f"本人 {years} 年 / {profile.education or '未填'}",
        )
    )

    depth = _sig_value(job, "tech_depth")
    g06_max = ov.get("G-06", 1.0)
    dim.add(
        ScoreItem(
            "G-06",
            "技术深度信号",
            g06_max if depth else 0.0,
            g06_max,
            "JD 提到架构设计/性能优化/技术选型等深度工作" if depth
            else "JD 未见明显技术深度信号",
            _sig_evidence(job, "tech_depth"),
        )
    )
    return dim.finalize()


def score_resource(job: Job, company: Company, profile: Profile, overrides: ScoringOverrides | None = None) -> DimensionScore:
    ov = overrides or ScoringOverrides()
    dim = DimensionScore("resource", "资源匹配")

    r01_max = ov.get("R-01", 5.0)
    city = job.city or ""
    if city and city == profile.current_city:
        dim.add(ScoreItem("R-01", "城市完美匹配", r01_max, r01_max, f"{city} 即常住城市"))
    elif city and city in profile.all_acceptable_cities():
        # 次优匹配 = 满分的 40%
        dim.add(ScoreItem("R-01", "城市次优匹配", round(r01_max * 0.4, 2), r01_max, f"{city} 属可接受城市"))
    else:
        dim.add(
            ScoreItem("R-01", "城市匹配", 0.0, r01_max, f"城市「{city or '未知'}」不加分")
        )

    ind_hits = match_industry(company.industry, profile.industry_tags)
    r02_max = ov.get("R-02", 2.0)
    dim.add(
        ScoreItem(
            "R-02",
            "行业经验重叠",
            r02_max if ind_hits else 0.0,
            r02_max,
            f"命中既往行业 {ind_hits}" if ind_hits else "无行业经验重叠",
            ind_hits,
        )
    )

    big = (company.scale_min is not None and company.scale_min >= 500) or (
        "上市" in (company.stage or "")
    )
    r03_max = ov.get("R-03", 1.0)
    dim.add(
        ScoreItem(
            "R-03",
            "公司体量偏好",
            r03_max if big else 0.0,
            r03_max,
            f"规模「{company.scale_raw or '未知'}」/ 阶段「{company.stage or '未知'}」",
        )
    )

    r04_max = ov.get("R-04", 2.0)
    dim.add(ScoreItem("R-04", "特殊资源(手动)", 0.0, r04_max, "预留手动加分位, 默认 0"))
    return dim.finalize()


def score_wlb(job: Job, company: Company, profile: Profile, overrides: ScoringOverrides | None = None) -> DimensionScore:
    ov = overrides or ScoringOverrides()
    dim = DimensionScore("wlb", "工作生活平衡")

    w01_max = ov.get("W-01", 3.0)
    mode = _sig_value(job, "work_mode")
    # remote→满分, hybrid→2/3, 现场→0
    mode_ratio = {"remote": 1.0, "hybrid": 2/3}.get(mode or "", 0.0)
    dim.add(
        ScoreItem(
            "W-01",
            "工作模式弹性",
            round(w01_max * mode_ratio, 2),
            w01_max,
            {"remote": "全远程", "hybrid": "混合办公"}.get(mode or "", "现场办公"),
            _sig_evidence(job, "work_mode"),
        )
    )

    flexible = [w for w in job.welfare if any(k in w for k in ("弹性", "不打卡", "自由"))]
    w02_max = ov.get("W-02", 2.0)
    dim.add(
        ScoreItem(
            "W-02",
            "弹性福利信号",
            w02_max if flexible else 0.0,
            w02_max,
            f"福利含 {flexible}" if flexible else "无弹性工作相关福利",
            flexible,
        )
    )

    regulated = (company.scale_min is not None and company.scale_min >= 2000) or (
        "上市" in (company.stage or "")
    )
    w03_max = ov.get("W-03", 1.0)
    dim.add(
        ScoreItem(
            "W-03",
            "企业规范性",
            w03_max if regulated else 0.0,
            w03_max,
            "大型/上市公司, 制度相对规范" if regulated else "规模或阶段不足以佐证规范性",
        )
    )

    w04_max = ov.get("W-04", 3.0)
    overtime = _sig_value(job, "overtime")
    # light→满分, moderate/unknown→1/3, heavy→0
    ot_ratio = {"light": 1.0, "moderate": 1/3, "unknown": 1/3, "heavy": 0.0}.get(
        overtime or "unknown", 1/3
    )
    dim.add(
        ScoreItem(
            "W-04",
            "加班强度",
            round(w04_max * ot_ratio, 2),
            w04_max,
            {
                "light": "双休/弹性, 作息健康",
                "moderate": "作息中等或存在加班信号",
                "heavy": "高强度作息",
                "unknown": "无法判断, 按中性处理",
            }.get(overtime or "unknown", "无法判断"),
            _sig_evidence(job, "overtime"),
        )
    )

    w05_max = ov.get("W-05", 1.0)
    dim.add(
        ScoreItem(
            "W-05",
            "通勤便利",
            w05_max if job.city and job.city == profile.current_city else 0.0,
            w05_max,
            f"{job.city or '未知'} vs 常住 {profile.current_city or '未填'}",
        )
    )
    return dim.finalize()


# ---------------------------------------------------------------- AI 触发


def eval_ai_triggers(
    job: Job,
    company: Company,
    profile: Profile,
    dims: dict[str, DimensionScore],
    hard_checks: list[HardCheck],
    total: float,
    *,
    user_requested: bool = False,
) -> list[dict]:
    """判断哪些条件需要 AI 介入, 返回 [{code, reason, ask}]。

    ``ask`` 是给 AI 的具体问题 —— 后面 AI 打分链路会直接拿它拼提示词,
    比笼统地丢一句"帮我评估一下"要有效得多。
    """
    triggers: list[dict] = []
    wlb = dims["wlb"].score
    growth = dims["growth"]

    if wlb <= 3:
        triggers.append({
            "code": "A-01",
            "reason": f"WLB 仅 {wlb} 分, 可能是 JD 没写清楚而非真的差",
            "ask": f"请联网查一下「{company.name or job.company_name}」的真实作息口碑: "
                   "是否强制加班、几点下班、周末是否单休。",
        })
    elif wlb >= 7:
        triggers.append({
            "code": "A-02",
            "reason": f"WLB 高达 {wlb} 分, 需要交叉验证是不是纸面福利",
            "ask": f"「{company.name or job.company_name}」JD 上的弹性/双休承诺是否属实? "
                   "有无隐性加班文化(员工评价、脉脉/看准网口碑)?",
        })

    biz_item = next((i for i in growth.items if i.code == "G-03"), None)
    if biz_item and biz_item.points == 0:
        hints = semantic_industry_hint(
            company.industry, list(profile.industry_tags) + list(profile.preferred_directions)
        )
        if hints:
            triggers.append({
                "code": "A-03",
                "reason": f"行业字面不匹配但疑似语义相关: {hints}",
                "ask": f"「{company.industry}」与我的既往经验"
                       f"({', '.join(profile.industry_tags)})是否属于同一大类? "
                       "经验能否迁移?",
            })

    if 6.0 <= total <= 7.5:
        triggers.append({
            "code": "A-04",
            "reason": f"总分 {total} 落在可去可不去的模糊区间",
            "ask": "结合公司前景、岗位含金量和我的画像, 给一个去或不去的明确结论和理由。",
        })

    if not (job.jd or {}).get("responsibility") or len((job.jd or {}).get("full", "")) < 120:
        triggers.append({
            "code": "A-05",
            "reason": "JD 内容过短或缺少岗位职责, 规则无法提取有效信号",
            "ask": f"请补充搜索「{company.name or job.company_name}」的"
                   f"「{job.title}」岗位实际做什么、汇报关系和团队情况。",
        })

    if user_requested:
        triggers.append({
            "code": "A-06",
            "reason": "用户主动要求深度分析",
            "ask": "请对这个机会做一次完整的尽调式分析。",
        })

    # A-07: 硬性规则命中但证据不足 —— 这是新增的, 也是最该问 AI 的情况
    for hc in hard_checks:
        if hc.is_suspicion:
            triggers.append({
                "code": "A-07",
                "reason": f"{hc.code} {hc.label} 命中但证据置信度仅 {hc.confidence:.0%}",
                "ask": f"请核实: {hc.reason}。证据是否成立?",
            })

    # A-08: 匿名雇主, 公司维度全靠猜
    if company.anonymous or job.provenance.get("employer_anonymous"):
        triggers.append({
            "code": "A-08",
            "reason": "BOSS 匿名雇主, 公司相关维度缺乏依据",
            "ask": f"根据 JD 内容「{job.title}」和描述特征, 推测这可能是哪一类/哪一家公司, "
                   "并说明判断依据。",
        })

    return triggers


# ---------------------------------------------------------------- 主入口


def score_job(
    job: Job,
    company: Company | None,
    profile: Profile,
    *,
    config: cfg.ScoringConfig | None = None,
    user_requested_ai: bool = False,
    overrides: ScoringOverrides | None = None,
) -> ScoreResult:
    company = company or Company()
    conf = config or cfg.SETTINGS.scoring
    ov = overrides or _load_active_overrides()

    result = ScoreResult(job_id=job.job_id, created_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    result.weights = (
        profile.normalized_weights if conf.use_profile_weights else conf.as_dict()
    )

    result.hard_checks = run_hard_checks(job, company, profile, ov)
    fatal = [h for h in result.hard_checks if h.is_fatal]
    suspicion = [h for h in result.hard_checks if h.is_suspicion]

    # 四个维度照常算 —— 即便被淘汰也要留下分数, 方便你回头复盘
    # "我到底否掉了什么样的机会"。
    dims = {
        "finance": score_finance(job, company, profile, ov),
        "growth": score_growth(job, company, profile, ov),
        "resource": score_resource(job, company, profile, ov),
        "wlb": score_wlb(job, company, profile, ov),
    }
    result.dimensions = dims

    weighted = sum(dims[k].score * result.weights.get(k, 0) for k in dims)
    result.total_score = round(weighted, 2)

    if fatal:
        result.status = STATUS_REJECTED
        result.reject_reason = "; ".join(f"{h.code}: {h.reason}" for h in fatal)
        # 淘汰后总分归零, 但 dimensions 里的明细保留
        result.total_score = 0.0
    elif suspicion:
        result.status = STATUS_REVIEW
        result.reject_reason = None
        result.warnings.extend(
            f"{h.code} {h.label} 命中但证据不足({h.confidence:.0%}), 未直接淘汰" for h in suspicion
        )
    else:
        result.status = STATUS_PASS

    # 数据质量警告
    q = job.quality or {}
    if q.get("missing"):
        result.warnings.append(f"数据缺失字段: {', '.join(q['missing'])}")
    if q.get("polluted"):
        result.warnings.append("JD 文本疑似仍含反爬污染, 信号推断可能失真")
    if job.provenance.get("city_source") == "assumed":
        result.warnings.append("城市为人工假定值, 非抓取所得")

    triggers = eval_ai_triggers(
        job, company, profile, dims, result.hard_checks,
        result.total_score if result.status != STATUS_REJECTED else weighted,
        user_requested=user_requested_ai,
    )
    result.triggered_ai_rules = triggers
    result.ai_intervention_needed = bool(triggers) and result.status != STATUS_REJECTED

    result.raw_data = {
        "jd_city": job.city,
        "jd_industry": company.industry,
        "jd_salary_min": job.salary.get("min_10k"),
        "jd_salary_max": job.salary.get("max_10k"),
        "jd_salary_raw": job.salary.get("raw"),
        "jd_months": job.salary.get("months"),
        "company_name": company.name or job.company_name,
        "company_scale": company.scale_raw,
        "company_stage": company.stage,
        "company_nature": company.nature,
        "company_anonymous": company.anonymous,
        "overtime": _sig_value(job, "overtime"),
        "work_mode": _sig_value(job, "work_mode"),
        "outsourcing": _sig_value(job, "outsourcing"),
        "profile_current_city": profile.current_city,
        "profile_expected_min": profile.expect_min_salary_10k,
        "profile_expected_max": profile.expect_max_salary_10k,
        "profile_hard_min": profile.hard_min_salary_10k,
    }
    return result


def explain(result: ScoreResult) -> str:
    """把打分结果渲染成一段人类可读的说明, 终端和 Web 都能直接用。"""
    icon = {STATUS_PASS: "✓", STATUS_REVIEW: "?", STATUS_REJECTED: "✗"}.get(result.status, "·")
    lines = [
        f"{icon} {result.status}  总分 {result.total_score}/10 ({result.total_100} 分)",
    ]
    if result.reject_reason:
        lines.append(f"  淘汰原因: {result.reject_reason}")
    for key, dim in result.dimensions.items():
        w = result.weights.get(key, 0)
        lines.append(f"  [{dim.label}] {dim.score}/10  ×{w:.0%}")
        for item in dim.items:
            if item.points or item.max_points >= 2:
                mark = "+" if item.points else " "
                lines.append(
                    f"     {mark}{item.points:>4.2f}/{item.max_points:<4.2f} {item.label}"
                    f" — {item.detail}"
                )
    for w in result.warnings:
        lines.append(f"  ! {w}")
    if result.triggered_ai_rules:
        codes = ", ".join(t["code"] for t in result.triggered_ai_rules)
        lines.append(f"  → 建议 AI 介入: {codes}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 规则目录


#: 规则项的代码默认满分 (与打分函数一一对应, 修改打分逻辑时同步更新)。
_ITEM_DEFAULTS: dict[str, float] = {
    "F-01": 4.0, "F-02": 4.0, "F-03": 1.0, "F-04": 0.5, "F-05": 0.5,
    "G-01": 3.0, "G-02": 2.0, "G-03": 2.0, "G-04": 1.0, "G-05": 1.0, "G-06": 1.0,
    "R-01": 5.0, "R-02": 2.0, "R-03": 1.0, "R-04": 2.0,
    "W-01": 3.0, "W-02": 2.0, "W-03": 1.0, "W-04": 3.0, "W-05": 1.0,
}

#: 规则目录的静态描述部分 (不含动态值)。
_CATALOG_STATIC: dict[str, Any] = {
    "version": RULES_VERSION,
    "weights_source": "画像 (profile.md 的价值观权重, 归一化后使用)",
    "hard_checks": [
        {"code": "H-01", "label": "城市不可接受", "detail": "职位城市不在画像的可接受城市列表内。迁移时人工假定的城市置信度仅 40%, 命中只标 REVIEW 不淘汰。"},
        {"code": "H-02", "label": "行业被拒绝", "detail": "公司行业命中画像的拒绝行业列表。"},
        {"code": "H-03", "label": "公司规模过小", "detail": "公司规模上限低于画像的拒绝规模下限 (默认 20 人)。"},
        {"code": "H-04", "label": "薪资低于硬性下限", "detail": "年薪下限 < 画像的硬性最低可接受年薪 (默认期望下限的 85%)。面议/未知不淘汰。"},
        {"code": "H-05", "label": "出差强度超标", "detail": "JD 要求长期驻场/外派, 且画像明确不接受出差。证据置信度低于阈值只标 REVIEW。"},
        {"code": "H-06", "label": "加班强度超标", "detail": "推断为 996/大小周/单休等高强度作息。证据置信度低于阈值只标 REVIEW。"},
        {"code": "H-07", "label": "命中排除关键词", "detail": "JD 文本命中画像的排除关键词黑名单 (如大小周、995、外包驻场)。"},
        {"code": "H-08", "label": "外包/驻场岗位", "detail": "推断为外包/劳务派遣/驻场岗位, 且画像不接受外包。证据置信度低于阈值只标 REVIEW。"},
    ],
    "dimensions": [
        {
            "key": "finance", "label": "财务回报",
            "items": [
                {"code": "F-01", "label": "薪资下限达标", "detail": "年薪下限 >= 期望最低年薪 → 满分"},
                {"code": "F-02", "label": "薪资中位数达标", "detail": "薪资中位 >= 期望中位 → 满分"},
                {"code": "F-03", "label": "薪资上限惊喜", "detail": "年薪上限 >= 期望上限×1.2 → 满分"},
                {"code": "F-04", "label": "股票/期权激励", "detail": "福利含股票/期权/股权 → 满分"},
                {"code": "F-05", "label": "高价值福利", "detail": "每命中 1 项看重的福利 +满分的50%, 封顶满分"},
            ],
        },
        {
            "key": "growth", "label": "职业发展",
            "items": [
                {"code": "G-01", "label": "技术栈重合度", "detail": "重合 1→17%, 2→33%, 3→67%, ≥4→满分"},
                {"code": "G-02", "label": "公司阶段价值", "detail": "B/C轮→满分, D轮+/上市→50%, 国企→+50% (封顶满分)"},
                {"code": "G-03", "label": "业务方向匹配", "detail": "行业与既往经验标签字面重合 → 满分"},
                {"code": "G-04", "label": "团队规模契合", "detail": "JD 团队规模落在偏好区间内 → 满分"},
                {"code": "G-05", "label": "经验学历兼容", "detail": "经验/学历要求均兼容本人 → 满分"},
                {"code": "G-06", "label": "技术深度信号", "detail": "JD 含架构/性能/选型等深度关键词 → 满分"},
            ],
        },
        {
            "key": "resource", "label": "资源匹配",
            "items": [
                {"code": "R-01", "label": "城市匹配", "detail": "常住城市→满分, 可接受城市→40%, 其他→0"},
                {"code": "R-02", "label": "行业经验重叠", "detail": "公司行业命中既往行业标签 → 满分"},
                {"code": "R-03", "label": "公司体量偏好", "detail": "规模≥500人 或 上市 → 满分"},
                {"code": "R-04", "label": "特殊资源(手动)", "detail": "预留手动加分位, 默认 0"},
            ],
        },
        {
            "key": "wlb", "label": "工作生活平衡",
            "items": [
                {"code": "W-01", "label": "工作模式弹性", "detail": "远程→满分, 混合→67%, 现场→0"},
                {"code": "W-02", "label": "弹性福利信号", "detail": "福利含弹性/不打卡/自由 → 满分"},
                {"code": "W-03", "label": "企业规范性", "detail": "规模≥2000人 或 上市 → 满分"},
                {"code": "W-04", "label": "加班强度", "detail": "light→满分, moderate/unknown→33%, heavy→0"},
                {"code": "W-05", "label": "通勤便利", "detail": "职位城市 = 常住城市 → 满分"},
            ],
        },
    ],
    "ai_triggers": [
        {"code": "A-01", "label": "WLB 偏低需核实", "detail": "WLB ≤ 3 分, 可能是 JD 没写清楚而非真的差, 请 AI 查公司真实作息口碑"},
        {"code": "A-02", "label": "WLB 偏高需交叉验证", "detail": "WLB ≥ 7 分, 需核实纸面福利是否属实"},
        {"code": "A-03", "label": "行业语义相关", "detail": "字面不匹配但疑似语义相近 (如在线教育 vs 编程教育), 请 AI 判断经验能否迁移"},
        {"code": "A-04", "label": "总分模糊区间", "detail": "总分 6.0~7.5, 可去可不去, 请 AI 给明确结论"},
        {"code": "A-05", "label": "JD 内容过短", "detail": "JD 缺岗位职责或正文过短, 规则无法提取有效信号, 请 AI 补充"},
        {"code": "A-06", "label": "用户主动深度分析", "detail": "用户勾选深度分析时触发"},
        {"code": "A-07", "label": "硬规则命中但证据不足", "detail": "某条硬性规则命中但置信度 < 60%, 请 AI 核实证据是否成立"},
        {"code": "A-08", "label": "匿名雇主", "detail": "BOSS 匿名雇主, 公司维度全靠猜, 请 AI 推测公司类型"},
    ],
}


def build_rules_catalog(overrides: ScoringOverrides | None = None) -> dict[str, Any]:
    """构建规则目录, 注入当前生效的 max 和 default_max。

    每个评分项额外返回:
      - max: 当前生效满分 (用户覆盖值或代码默认值)
      - default_max: 代码默认满分 (供前端显示"恢复默认")
    """
    import copy

    ov = overrides or _load_active_overrides()
    catalog = copy.deepcopy(_CATALOG_STATIC)
    catalog["reject_confidence_floor"] = ov.get_floor(REJECT_CONFIDENCE_FLOOR)
    catalog["default_reject_confidence_floor"] = REJECT_CONFIDENCE_FLOOR
    for dim in catalog["dimensions"]:
        dim_total = 0.0
        for item in dim["items"]:
            code = item["code"]
            default = _ITEM_DEFAULTS.get(code, 0.0)
            item["default_max"] = default
            item["max"] = ov.get(code, default)
            # 前端编辑用的字段名 (F-01 → f01_max)
            item["override_key"] = code.lower().replace("-", "") + "_max"
            dim_total += item["max"]
        dim["max_score"] = round(dim_total, 2)
        dim["default_max_score"] = round(sum(_ITEM_DEFAULTS.get(i["code"], 0.0) for i in dim["items"]), 2)
    return catalog


# 向后兼容: 模块加载时构建一次 (未读取覆盖文件, 用默认值)
RULES_CATALOG: dict[str, Any] = build_rules_catalog(ScoringOverrides())
