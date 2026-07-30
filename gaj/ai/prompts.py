"""AI 提示词构建。

把 Job / Company / Profile / 规则打分结果 组装成给网页版大模型的提示词。
设计原则:
  - 上下文要全但不冗余 —— JD 全文给, 但公司简介截断到 500 字
  - 问题要具体 —— 每条 AI 触发规则都带一个明确的 ask, 不是笼统的"帮我看看"
  - 输出要可控 —— 强制要求 JSON, 给出 schema 和示例, 解析器才好提取
"""

from __future__ import annotations

import json
from typing import Any

from ..core.models import Company, Job
from ..core.profile import Profile

# ---------------------------------------------------------------- 输出 schema

OUTPUT_SCHEMA = """\
请严格按照以下 JSON 格式输出, 不要在 JSON 外面加任何文字:

```json
{
  "status": "PASS | REVIEW | REJECTED",
  "total_score": 7.5,
  "dimension_scores": {
    "finance": 8.0,
    "growth": 7.0,
    "resource": 7.5,
    "wlb": 6.0
  },
  "recommendation": "强烈推荐 | 可以考虑 | 不建议",
  "recommendation_reason": "200 字以内的结论和理由",
  "rule_answers": {
    "A-01": "针对该触发规则的具体回答",
    "A-04": "..."
  },
  "ai_corrections": {
    "wlb_corrected": null,
    "wlb_correction_reason": "",
    "growth_business_semantic_score": null,
    "jd_enriched": {
      "description": "",
      "team_intro": ""
    }
  },
  "highlights": ["匹配亮点 1", "匹配亮点 2"],
  "risks": ["风险点 1", "风险点 2"],
  "interview_tips": ["面试准备建议"],
  "deep_analysis_report": ""
}
```

字段说明:
- status: 最终判定。PASS=通过, REVIEW=存疑需进一步了解, REJECTED=不建议
- total_score: 你的综合评分 (0~10), 不需要和规则打分一致
- dimension_scores: 四个维度你的独立评分 (各 0~10)
- recommendation: 三选一的简短结论
- recommendation_reason: 支撑结论的核心理由
- rule_answers: 对每条触发规则的逐一回答, key 是规则编号
- ai_corrections: 仅在对应规则触发时填写, 否则留 null/空串
  - wlb_corrected: A-01/A-02 触发时填你修正后的 WLB 分数
  - growth_business_semantic_score: A-03 触发时填 0~1 的语义匹配度
  - jd_enriched: A-05 触发时填你补充的信息
- highlights/risks/interview_tips: 各列 2~5 条
- deep_analysis_report: 仅在用户要求深度分析时填写 (300 字以上), 否则留空串
"""


def _truncate(text: str, limit: int = 500) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _format_signals(job: Job) -> str:
    sig = job.signals or {}
    lines = []
    for key, label in (
        ("overtime", "加班强度"),
        ("work_mode", "工作模式"),
        ("outsourcing", "外包"),
        ("travel", "出差"),
        ("team_size", "团队规模"),
        ("tech_depth", "技术深度"),
    ):
        s = sig.get(key) or {}
        val = s.get("value", "未知")
        conf = s.get("confidence", 0)
        ev = s.get("evidence", [])
        ev_str = f" (依据: {', '.join(ev[:2])})" if ev else ""
        lines.append(f"  - {label}: {val} [置信度 {conf:.0%}]{ev_str}")
    blacklist = sig.get("blacklist_hits") or []
    if blacklist:
        lines.append(f"  - 命中排除关键词: {', '.join(blacklist)}")
    return "\n".join(lines)


def _format_rule_result(rule: dict | None) -> str:
    if not rule:
        return "  (尚未进行规则打分)"
    dims = rule.get("dimension_scores", {}) or {}
    lines = [
        f"  状态: {rule.get('status', '未知')}",
        f"  总分: {rule.get('total_score', 0)}/10",
    ]
    if rule.get("reject_reason"):
        lines.append(f"  淘汰原因: {rule['reject_reason']}")
    for key, label in (
        ("finance", "财务回报"),
        ("growth", "职业成长"),
        ("resource", "平台资源"),
        ("wlb", "工作生活平衡"),
    ):
        lines.append(f"  {label}: {dims.get(key, 0)}/10")
    triggers = rule.get("triggered_ai_rules") or []
    if triggers:
        lines.append(f"  触发的 AI 规则: {', '.join(t.get('code', '') for t in triggers)}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 打分提示词


def build_scoring_prompt(
    job: Job,
    company: Company | None,
    profile: Profile,
    rule_result: dict | None = None,
    triggers: list[dict] | None = None,
    *,
    deep: bool = False,
    resume: str = "",
) -> str:
    """构建 AI 打分 / 复核提示词。

    Args:
        job: 职位对象
        company: 公司对象 (可能为 None)
        profile: 用户画像
        rule_result: 规则打分结果 (dict, 即 ScoreResult.to_dict())
        triggers: AI 触发规则列表, 每项 {code, reason, ask}; None 则从 rule_result 取
        deep: 是否生成深度分析报告 (A-06)
        resume: 用户主简历全文 (Markdown), 提供给 AI 做人岗匹配参考
    """
    company = company or Company()
    if triggers is None:
        triggers = (rule_result or {}).get("triggered_ai_rules") or []

    sections: list[str] = []

    sections.append(
        "你是一位资深的职业顾问和猎头, 请基于以下信息对一个求职机会做评估。\n"
        "你的回答将直接影响求职者的职业决策, 请认真、客观、有依据。"
    )

    # ---- 1. 求职者画像 ----
    sections.append("## 求职者画像\n\n" + profile.summary_for_ai())

    # ---- 1b. 求职者简历 (可选) ----
    resume = (resume or "").strip()
    if resume:
        sections.append(
            "## 求职者简历 (人岗匹配参考)\n\n"
            "以下是求职者的真实简历, 请结合简历内容评估人岗匹配度, "
            "在 highlights 中点出真正契合的经历/技能, 在 risks 中点出能力差距。\n\n"
            + _truncate(resume, 6000)
        )

    # ---- 2. 职位信息 ----
    salary = job.salary or {}
    exp = job.experience or {}
    edu = job.education or {}
    jd = job.jd or {}

    job_lines = [
        f"### 职位: {job.title}",
        f"- 公司: {company.name or job.company_name or '未知'}"
        + (f" ({company.industry})" if company.industry else ""),
        f"- 薪资: {salary.get('raw', '未知')}"
        f" (折算年薪 {salary.get('min_10k', '?')}~{salary.get('max_10k', '?')} 万,"
        f" {salary.get('months', 12)} 薪)",
        f"- 城市: {job.city or '未知'} {job.district or ''}",
        f"- 经验要求: {exp.get('raw', '不限')}; 学历要求: {edu.get('raw', '不限')}",
    ]
    if job.skills:
        job_lines.append(f"- 技能标签: {', '.join(job.skills)}")
    if job.welfare:
        job_lines.append(f"- 福利: {', '.join(job.welfare[:15])}")
    sections.append("\n".join(job_lines))

    # ---- 3. JD 正文 ----
    jd_full = jd.get("full", "")
    if jd.get("responsibility"):
        jd_text = (
            f"**岗位职责:**\n{jd['responsibility']}\n\n"
            f"**任职要求:**\n{jd.get('requirement', '')}\n"
        )
        if jd.get("bonus"):
            jd_text += f"\n**加分项:**\n{jd['bonus']}\n"
    else:
        jd_text = jd_full
    sections.append("### 岗位描述\n\n" + _truncate(jd_text, 3000))

    # ---- 4. 公司信息 ----
    if company.name or company.intro:
        co_lines = []
        if company.stage:
            co_lines.append(f"- 融资阶段: {company.stage}")
        if company.scale_raw:
            co_lines.append(f"- 规模: {company.scale_raw}")
        if company.nature:
            co_lines.append(f"- 性质: {company.nature}")
        if company.founded:
            co_lines.append(f"- 成立: {company.founded}")
        if company.hours_per_day:
            co_lines.append(f"- 公示工时: {company.hours_per_day} 小时/天")
        if company.intro:
            co_lines.append(f"- 简介: {_truncate(company.intro, 500)}")
        if company.business_scope:
            co_lines.append(f"- 经营范围: {_truncate(company.business_scope, 300)}")
        if co_lines:
            sections.append("### 公司信息\n\n" + "\n".join(co_lines))

    # ---- 5. 信号推断 ----
    sections.append("### 爬虫推断的信号 (置信度仅供参考)\n" + _format_signals(job))

    # ---- 6. 规则打分结果 ----
    sections.append("### 规则引擎打分结果\n\n" + _format_rule_result(rule_result))

    # ---- 7. 需要你回答的问题 ----
    if triggers:
        q_lines = ["规则引擎标记了以下需要你判断的问题, 请逐一回答:"]
        for t in triggers:
            q_lines.append(f"\n**{t.get('code', '?')}** {t.get('reason', '')}")
            q_lines.append(f"  → {t.get('ask', '')}")
        sections.append("\n".join(q_lines))
    else:
        sections.append("规则引擎未触发 AI 复核, 请给出你的独立评估。")

    if deep:
        sections.append(
            "\n**用户要求深度分析**: 请在 deep_analysis_report 字段中给出 300 字以上的"
            "完整尽调式分析, 包括匹配亮点、潜在风险、面试准备建议和薪资谈判空间。"
        )

    # ---- 8. 输出要求 ----
    sections.append("## 输出要求\n\n" + OUTPUT_SCHEMA)

    return "\n\n---\n\n".join(sections)


# ---------------------------------------------------------------- 简历提示词


def build_resume_prompt(
    job: Job,
    company: Company | None,
    master_resume: str,
    profile: Profile,
    *,
    style: str = "optimize",
) -> str:
    """构建简历针对性优化提示词。

    Args:
        job: 目标职位
        company: 目标公司
        master_resume: 用户的主简历全文
        profile: 用户画像
        style: optimize=在原简历基础上优化; rewrite=完全重写
    """
    company = company or Company()
    jd = job.jd or {}
    salary = job.salary or {}

    sections: list[str] = []

    sections.append(
        "你是一位资深简历顾问。请根据目标岗位的 JD, 对求职者的简历进行针对性优化。\n"
        "原则:\n"
        "1. **真实优先**: 不编造经历、不夸大技能, 只做重新组织和强调\n"
        "2. **JD 对齐**: 把与岗位最相关的经验前置、加粗, 删减无关内容\n"
        "3. **量化成果**: 尽量用数字和结果描述, 而非职责罗列\n"
        "4. **关键词**: 在自然的前提下融入 JD 中的关键技术词和业务词\n"
        "5. **格式**: 输出 Markdown 格式, 保持简历结构清晰"
    )

    # ---- 目标岗位 ----
    job_lines = [
        f"### 目标岗位: {job.title}",
        f"- 公司: {company.name or job.company_name or '未知'}"
        + (f" ({company.industry})" if company.industry else ""),
        f"- 薪资: {salary.get('raw', '未知')}",
        f"- 城市: {job.city or '未知'}",
        f"- 经验/学历: {(job.experience or {}).get('raw', '不限')}"
        f" / {(job.education or {}).get('raw', '不限')}",
    ]
    if job.skills:
        job_lines.append(f"- 关键技能: {', '.join(job.skills)}")
    sections.append("\n".join(job_lines))

    # ---- JD ----
    jd_text = jd.get("responsibility", "") or jd.get("full", "")
    sections.append("### 岗位描述\n\n" + _truncate(jd_text, 2500))

    # ---- 求职者核心信息 ----
    sections.append("### 求职者画像\n\n" + profile.summary_for_ai())

    # ---- 原简历 ----
    sections.append("### 当前简历\n\n" + _truncate(master_resume, 6000))

    # ---- 输出要求 ----
    action = "优化" if style == "optimize" else "重写"
    sections.append(
        f"## 输出要求\n\n"
        f"请{action}这份简历, 使其更匹配上述岗位。直接输出 Markdown 格式的完整简历,\n"
        f"不要加解释性文字。简历应包含: 个人信息、专业技能、工作经历、项目经历、\n"
        f"教育背景等模块。可以在简历末尾用 `---` 分隔后, 简要列出你做了哪些关键调整 (3 条以内)。"
    )

    return "\n\n---\n\n".join(sections)


# ---------------------------------------------------------------- 调试用


def preview_prompt(prompt: str, max_chars: int = 2000) -> str:
    """截断 prompt 用于调试打印。"""
    if len(prompt) <= max_chars:
        return prompt
    return prompt[:max_chars] + f"\n\n… (共 {len(prompt)} 字, 已截断)"
