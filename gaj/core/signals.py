"""软信号推断 —— 补齐打分规则需要、但 BOSS 不直接提供的字段。

scoring_rules.md 里的 H-05/H-06/H-07 和 WLB 维度依赖这些字段:
    实际加班强度 / 工作模式 / 出差频率 / 是否外包
但 BOSS 的接口和页面都没有这些结构化字段, 只能从三处反推:
    1. JD 正文关键词
    2. 福利标签
    3. 工作时间

一个容易被忽略的判断: **福利标签里的"加班补助""法定节假日三薪""包吃""免费班车"
其实是加班多的信号**, 而不是加分项 —— 公司愿意为加班付钱, 说明加班是常态。
这类反向信号在这里单独建模。

每个信号都返回置信度和证据, 低置信度的项会触发 AI 复核, 而不是硬拍一个结论。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Iterable

from .denoise import clean_text

# ---------------------------------------------------------------- 关键词表

# 加班强度: 明确写出来的作息制度
OVERTIME_PATTERNS: list[tuple[str, str, tuple[str, ...]]] = [
    ("996", "high", ("996", "9-9-6", "早9晚9")),
    ("大小周", "high", ("大小周", "单双休", "隔周双休")),
    ("单休", "high", ("单休", "每周休息一天", "做六休一")),
    ("995", "medium_high", ("995", "早9晚9双休")),
    ("弹性", "low", ("弹性工作", "弹性上下班", "不打卡", "弹性考勤", "自由上下班")),
    ("双休", "low", ("双休", "周末双休", "五天八小时", "965", "朝九晚六")),
]

# 反向信号: 表面是福利, 实际暗示长时间在岗
OVERTIME_PROXY_WELFARE: dict[str, str] = {
    "加班补助": "公司为加班单列补贴, 说明加班是常态",
    "加班费": "同上",
    "法定节假日三薪": "节假日排班常态化",
    "节假日加班费": "节假日排班常态化",
    "包吃": "供餐通常对应长时间在岗",
    "包住": "食宿一体, 工作与生活边界模糊",
    "免费班车": "通勤依赖班车, 下班时间往往固定得很晚",
    "夜班补助": "存在夜班安排",
    "免费工装": "偏制造业/现场岗, 作息刚性",
}

REMOTE_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("remote", ("全远程", "远程办公", "居家办公", "work from home", "wfh", "远程工作")),
    ("hybrid", ("混合办公", "远程与办公室", "每周可远程", "部分远程", "hybrid")),
]

OUTSOURCING_PATTERNS: tuple[str, ...] = (
    "外包",
    "驻场",
    "人力外包",
    "劳务派遣",
    "第三方派遣",
    "外派至",
    "客户现场办公",
    "项目制派驻",
    "服务外包",
    "人力资源外包",
)

OUTSOURCING_INDUSTRY_HINTS: tuple[str, ...] = (
    "人力资源服务",
    "专业服务",
    "IT服务",
    "计算机服务",
    "外包服务",
)

TRAVEL_PATTERNS: list[tuple[str, str, tuple[str, ...]]] = [
    ("long_term", "long_term", ("长期出差", "长期驻场", "常驻项目地", "长期外派", "全国驻场")),
    ("frequent", "frequent", ("经常出差", "频繁出差", "出差频繁", "接受出差")),
    ("occasional", "occasional", ("偶尔出差", "短期出差", "适应出差", "可接受少量出差")),
]

# 团队规模: "团队20人" / "10人左右的团队"
TEAM_SIZE_RES = (
    re.compile(r"团队\s*(?:规模)?\s*(?:约|大约|近)?\s*(\d+)\s*(?:余|多)?\s*人"),
    re.compile(r"(\d+)\s*(?:余|多)?\s*人\s*(?:的)?\s*(?:研发)?团队"),
    re.compile(r"团队\s*(\d+)\s*[-~]\s*(\d+)\s*人"),
)


# ---------------------------------------------------------------- 数据结构


@dataclass
class Signal:
    """单个推断信号。

    value      推断结果
    confidence 0~1, 低于 0.5 建议交给 AI 复核
    evidence   命中的原文片段, 保证结论可追溯
    """

    value: str | bool | int | None = None
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class JobSignals:
    overtime: Signal = field(default_factory=Signal)
    work_mode: Signal = field(default_factory=Signal)
    outsourcing: Signal = field(default_factory=Signal)
    travel: Signal = field(default_factory=Signal)
    team_size: Signal = field(default_factory=Signal)
    tech_depth: Signal = field(default_factory=Signal)
    blacklist_hits: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "overtime": self.overtime.to_dict(),
            "work_mode": self.work_mode.to_dict(),
            "outsourcing": self.outsourcing.to_dict(),
            "travel": self.travel.to_dict(),
            "team_size": self.team_size.to_dict(),
            "tech_depth": self.tech_depth.to_dict(),
            "blacklist_hits": list(self.blacklist_hits),
        }

    def needs_ai_review(self) -> list[str]:
        """返回置信度不足、建议 AI 复核的信号名。"""
        weak = []
        for name in ("overtime", "work_mode", "outsourcing", "travel"):
            sig: Signal = getattr(self, name)
            if sig.confidence < 0.5:
                weak.append(name)
        return weak


# ---------------------------------------------------------------- 工具


def _find_hits(text: str, keywords: Iterable[str]) -> list[str]:
    hits = []
    low = text.lower()
    for kw in keywords:
        if kw.lower() in low:
            hits.append(kw)
    return hits


def _snippet(text: str, keyword: str, radius: int = 18) -> str:
    idx = text.lower().find(keyword.lower())
    if idx < 0:
        return keyword
    start = max(0, idx - radius)
    end = min(len(text), idx + len(keyword) + radius)
    return ("…" if start > 0 else "") + text[start:end].replace("\n", " ") + (
        "…" if end < len(text) else ""
    )


# ---------------------------------------------------------------- 各信号推断


def infer_overtime(
    jd_text: str, welfare: list[str], hours_per_day: float | None
) -> Signal:
    """推断实际加班强度。

    返回值: heavy(996/大小周/单休) / moderate / light(双休弹性) / unknown
    """
    text = clean_text(jd_text)
    sig = Signal(value="unknown", confidence=0.0)

    # 1) JD 正文里明说的作息制度 —— 置信度最高
    for label, level, kws in OVERTIME_PATTERNS:
        hits = _find_hits(text, kws)
        if not hits:
            continue
        sig.evidence.append(f"JD 提到「{label}」: {_snippet(text, hits[0])}")
        if level == "high":
            sig.value = "heavy"
            sig.confidence = 0.9
            return sig
        if level == "medium_high":
            sig.value = "heavy"
            sig.confidence = 0.75
            return sig
        if level == "low":
            sig.value = "light"
            sig.confidence = 0.7

    # 2) 福利标签里的反向信号
    proxy_hits = [w for w in welfare if w in OVERTIME_PROXY_WELFARE]
    if proxy_hits:
        for w in proxy_hits:
            sig.evidence.append(f"福利「{w}」→ {OVERTIME_PROXY_WELFARE[w]}")
        # 三个以上反向信号叠加, 基本可以判定作息偏重
        if len(proxy_hits) >= 3 and sig.value != "light":
            sig.value = "heavy"
            sig.confidence = max(sig.confidence, 0.6)
        elif sig.value == "unknown":
            sig.value = "moderate"
            sig.confidence = max(sig.confidence, 0.4)
        elif sig.value == "light":
            # 正反信号打架, 降低置信度让 AI 去查口碑
            sig.confidence = min(sig.confidence, 0.45)

    # 3) 工时时长
    if hours_per_day is not None:
        sig.evidence.append(f"公示工时 {hours_per_day} 小时/天")
        if hours_per_day >= 10 and sig.value in ("unknown", "moderate"):
            sig.value = "heavy"
            sig.confidence = max(sig.confidence, 0.55)
        elif hours_per_day <= 8.5 and sig.value == "unknown":
            sig.value = "light"
            sig.confidence = max(sig.confidence, 0.45)

    return sig


def infer_work_mode(jd_text: str, welfare: list[str]) -> Signal:
    """推断工作模式: remote / hybrid / onsite。"""
    text = clean_text(jd_text)
    blob = text + " " + " ".join(welfare)
    sig = Signal(value="onsite", confidence=0.3)
    for mode, kws in REMOTE_PATTERNS:
        hits = _find_hits(blob, kws)
        if hits:
            sig.value = mode
            sig.confidence = 0.8
            sig.evidence.append(f"命中「{hits[0]}」: {_snippet(blob, hits[0])}")
            return sig
    sig.evidence.append("未发现远程/混合办公表述, 默认按现场办公处理")
    return sig


def infer_outsourcing(jd_text: str, company_industry: str, company_name: str) -> Signal:
    """判断是否外包/驻场岗位。用户画像里明确写了不接受外包, 这是硬性淘汰项。"""
    text = clean_text(jd_text)
    sig = Signal(value=False, confidence=0.4)

    hits = _find_hits(text, OUTSOURCING_PATTERNS)
    if hits:
        sig.value = True
        sig.confidence = 0.85
        for h in hits[:3]:
            sig.evidence.append(f"JD 出现「{h}」: {_snippet(text, h)}")
        return sig

    industry_hit = [h for h in OUTSOURCING_INDUSTRY_HINTS if h in (company_industry or "")]
    name_hit = [h for h in ("外包", "人力", "劳务", "派遣") if h in (company_name or "")]
    if industry_hit or name_hit:
        sig.value = True
        sig.confidence = 0.5
        if industry_hit:
            sig.evidence.append(f"公司所属行业为「{company_industry}」, 外包可能性高")
        if name_hit:
            sig.evidence.append(f"公司名含「{name_hit[0]}」")
        return sig

    sig.evidence.append("未发现外包/驻场特征")
    return sig


def infer_travel(jd_text: str) -> Signal:
    """推断出差强度: long_term / frequent / occasional / none。"""
    text = clean_text(jd_text)
    sig = Signal(value="none", confidence=0.35)
    for _, level, kws in TRAVEL_PATTERNS:
        hits = _find_hits(text, kws)
        if hits:
            sig.value = level
            sig.confidence = 0.8
            sig.evidence.append(f"命中「{hits[0]}」: {_snippet(text, hits[0])}")
            return sig
    if "出差" in text:
        sig.value = "occasional"
        sig.confidence = 0.5
        sig.evidence.append(f"提及出差: {_snippet(text, '出差')}")
        return sig
    sig.evidence.append("JD 未提及出差")
    return sig


def infer_team_size(jd_text: str, company_intro: str = "") -> Signal:
    """从 JD 或公司介绍里抠出团队人数。"""
    text = clean_text(jd_text) + "\n" + clean_text(company_intro)
    sig = Signal(value=None, confidence=0.0)
    for pattern in TEAM_SIZE_RES:
        m = pattern.search(text)
        if not m:
            continue
        groups = [int(g) for g in m.groups() if g]
        if not groups:
            continue
        sig.value = sum(groups) // len(groups)
        sig.confidence = 0.7
        sig.evidence.append(f"文本提到团队规模: {m.group(0)}")
        return sig
    sig.evidence.append("未找到团队规模描述")
    return sig


TECH_DEPTH_KEYWORDS: tuple[str, ...] = (
    "架构设计",
    "架构演进",
    "性能优化",
    "技术选型",
    "高并发",
    "分布式",
    "技术方案设计",
    "系统重构",
    "稳定性建设",
    "工程化",
    "技术攻坚",
    "底层原理",
    "源码",
    "开源",
)


def infer_tech_depth(jd_text: str) -> Signal:
    """判断 JD 是否体现技术深度诉求 (对应打分规则的"技术深度信号")。"""
    text = clean_text(jd_text)
    hits = _find_hits(text, TECH_DEPTH_KEYWORDS)
    sig = Signal(value=len(hits), confidence=0.8 if hits else 0.6)
    for h in hits[:4]:
        sig.evidence.append(f"「{h}」: {_snippet(text, h)}")
    if not hits:
        sig.evidence.append("JD 未体现架构/性能/选型等技术深度要求")
    return sig


def find_blacklist_hits(jd_text: str, blacklist: Iterable[str]) -> list[str]:
    """匹配用户画像里的硬性排除关键词。"""
    text = clean_text(jd_text)
    return _find_hits(text, [k for k in blacklist if k])


# ---------------------------------------------------------------- 汇总入口


def extract_signals(
    *,
    jd_text: str,
    welfare: list[str] | None = None,
    hours_per_day: float | None = None,
    company_industry: str = "",
    company_name: str = "",
    company_intro: str = "",
    blacklist: Iterable[str] = (),
) -> JobSignals:
    """一次性抽取一个职位的全部软信号。"""
    welfare = welfare or []
    return JobSignals(
        overtime=infer_overtime(jd_text, welfare, hours_per_day),
        work_mode=infer_work_mode(jd_text, welfare),
        outsourcing=infer_outsourcing(jd_text, company_industry, company_name),
        travel=infer_travel(jd_text),
        team_size=infer_team_size(jd_text, company_intro),
        tech_depth=infer_tech_depth(jd_text),
        blacklist_hits=find_blacklist_hits(jd_text, blacklist),
    )
