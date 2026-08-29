"""个性化求职策略生成（纯规则，确定性输出，无 AI 依赖）。

数据源：报告数据契约 bundle JSON（reporter --save-bundle 导出，随付费报告交付）。
即使本地没有爬取数据，也能基于付费报告与个人预期产出扎实的策略信息。

用法:
    python3 -m gaj strategy --bundle artifacts/xxx.bundle.json \\
        --profile data/profile.md --out strategy.md

规则（全部确定性，可复现）:
- 谈薪：个人期望年薪换算后与市场 P50/P75 对比，给出报价区间建议
- 投递优先级：招聘力度榜 Top10（真名版 bundle 才含公司名，lite bundle 显示代号）
- 技能缺口：画像技能与技能需求榜 Top10 求交集/差集
- 避雷：信号公司榜 Top10（长工时判定，含公示工时）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def parse_profile(profile_md: str) -> dict:
    """从画像 Markdown 宽松解析期望薪资/期望城市/技能/岗位方向。

    未解析到的字段为 None，策略输出中会提示补充。
    """
    text = profile_md
    result: dict = {"salary_k_min": None, "salary_k_max": None,
                    "city": None, "skills": [], "direction": None}

    m = re.search(r"(\d{1,3})\s*[-~至]\s*(\d{1,3})\s*[Kk]", text)
    if m:
        result["salary_k_min"], result["salary_k_max"] = int(m.group(1)), int(m.group(2))

    m = re.search(r"期望城市[：:]\s*(\S+)", text)
    if m:
        result["city"] = m.group(1).strip("，。;")

    m = re.search(r"(?:技能|技术栈|skills?)[：:]\s*([^\n]+)", text, re.I)
    if m:
        result["skills"] = [s.strip() for s in re.split(r"[,，/、|+]", m.group(1)) if s.strip()][:12]

    m = re.search(r"(?:岗位方向|求职方向|目标岗位)[：:]\s*([^\n]+)", text)
    if m:
        result["direction"] = m.group(1).strip("，。;")[:40]
    return result


def _pct(v) -> str:
    return f"{v * 100:.1f}%" if v is not None else "—"


def _k_to_wan_year(k: int | None) -> float | None:
    """月薪 K 换算为年薪（万元），按 12 薪保守折算。"""
    return round(k * 12 / 10, 1) if k is not None else None


def build_strategy(bundle: dict, profile: dict) -> str:
    """由 bundle + 画像生成个性化策略 Markdown（确定性）。"""
    market = bundle.get("market") or {}
    pricing = market.get("salary_pricing") or {}
    overall = pricing.get("overall") or {}
    p50, p75 = overall.get("p50"), overall.get("p75")
    boards = market.get("company_boards") or {}
    radar = (market.get("signal_radar") or {})
    red_flags = radar.get("red_flag_companies") or []
    skills_top = (market.get("skill_leaderboard") or {}).get("items") or []
    hiring = boards.get("hiring") or []
    name_key = "company" if hiring and "company" in hiring[0] else "alias"
    anon_note = ("（bundle 为 lite 版，公司以代号呈现；真名对照见付费完整版）"
                 if name_key == "alias" else "")

    lines: list[str] = []
    lines.append("# 个性化求职策略")
    lines.append("")
    lines.append(f"> 数据指纹：`{bundle.get('data_fingerprint')}` · "
                 f"样本 {((bundle.get('meta') or {}).get('job_count'))} 岗 · "
                 f"由报告数据契约生成（纯规则，无 AI 参与，可复现）")
    lines.append("")

    # 1) 谈薪
    lines.append("## 1. 谈薪策略")
    if p50 and p75:
        lines.append(f"- 全市场薪资中位 **{p50} 万/年**，P75 = **{p75} 万/年**"
                     f"（P25 = {overall.get('p25')}，P90 = {overall.get('p90')}）。")
    kmin, kmax = profile.get("salary_k_min"), profile.get("salary_k_max")
    if kmin is not None and kmax is not None:
        wmin, wmax = _k_to_wan_year(kmin), _k_to_wan_year(kmax)
        lines.append(f"- 你的期望 {kmin}-{kmax}K ≈ 年薪 **{wmin}-{wmax} 万**（按 12 薪保守折算）。")
        if p50 and p75:
            anchor = min(max(wmax, p50), p75) if wmax else p50
            lines.append(f"- 报价建议：开价对齐 **{anchor} 万/年**（落在 P50-P75 区间内），"
                         f"底线设为 {wmin} 万；对方报价低于 P50 时谨慎评估。")
        months = (market.get("employer_profile") or {}).get("months_mix") or []
        if months:
            mix = "、".join(f"{m['months']}薪占 {_pct(m.get('ratio'))}" for m in months[:3])
            mm = (market.get("employer_profile") or {}).get("months_mix") or []
            if mm:
                pass
            lines.append(f"- 注意薪资月数构成：本批次 {mix}——同样「24 万」在 12 薪与 15 薪下月发差 25%。")
    else:
        lines.append("- 画像未标注期望薪资，建议在 profile.md 补充「期望薪资：25-35K」后重新生成。")
    lines.append("")

    # 2) 投递优先级
    lines.append("## 2. 投递优先级（招聘力度 Top10）")
    if hiring:
        lines.append(f"按在招岗位数排序{anon_note}：")
        lines.append("")
        lines.append("| # | 公司 | 行业 | 在招 | 均薪(万/年) |")
        lines.append("|---|---|---|---|---|")
        for i, c in enumerate(hiring[:10], 1):
            lines.append(f"| {i} | {c.get(name_key, '—')} | {c.get('industry', '—')} | "
                         f"{c.get('job_count')} | {c.get('avg_salary') or '—'} |")
        lines.append("")
        lines.append("行动：①优先投 Top5；②新岗发布前 2-3 周投递筛选最松；③每家 15 天内只投一个最匹配岗。")
    else:
        lines.append("- 本 bundle 未含双榜数据，请使用完整版 bundle 重新生成。")
    lines.append("")

    # 3) 技能缺口
    lines.append("## 3. 技能缺口")
    p_skills = [s.lower() for s in profile.get("skills") or []]
    if skills_top and p_skills:
        have = [s for s in skills_top if s["skill"].lower() in p_skills]
        need = [s for s in skills_top if s["skill"].lower() not in p_skills]
        if have:
            lines.append("- 你已具备市场需求技能：" + "、".join(
                f"**{s['skill']}**（需求 {s['demand_count']} 岗）" for s in have[:5]) + "。")
        if need:
            lines.append("- 建议补充（需求量大）："
                         + "、".join(f"**{s['skill']}**（需求 {s['demand_count']} 岗，均薪 {s['avg_salary']} 万）"
                                     for s in need[:5]) + "。")
        lines.append("- JD 出现「架构设计/高并发/分布式」等深度词越多，越可能给高级别定价——简历对齐这些措辞。")
    elif skills_top:
        lines.append("- 市场技能需求 Top5：" + "、".join(
            f"**{s['skill']}**（{s['demand_count']} 岗）" for s in skills_top[:5]) + "。")
        lines.append("- 画像未标注技能，建议在 profile.md 的「技能：」行补充后重新生成，以获得缺口分析。")
    lines.append("")

    # 4) 避雷
    lines.append("## 4. 工时避雷（信号公司 Top10）")
    if red_flags:
        lines.append("| 公司 | 公示工时(h/天) | 长工时 | 外包表述 | 出差表述 |")
        lines.append("|---|---|---|---|---|")
        for c in red_flags[:10]:
            h = c.get("hours_per_day")
            lines.append(f"| {c.get(name_key, '—')} | {h if h is not None else '未公示'} | "
                         f"{c.get('heavy_overtime', 0)} | {c.get('outsourcing', 0)} | {c.get('travel', 0)} |")
        lines.append("")
        lines.append("判定为关键词规则（JD 文本+公示信息），存在误判可能；公示工时为公司自报口径。")
    else:
        lines.append("- 本批次无信号公司样本。")
    lines.append("")

    # 5) 行动清单
    lines.append("## 5. 本周行动清单")
    lines.append("1. 把招聘力度 Top5 加入投递清单，今天投出第一份定制简历；")
    lines.append("2. 按第 1 节建议更新期望报价话术（对齐 P50-P75）；")
    lines.append("3. 从第 3 节挑一个缺口技能，本周完成一个可展示的小项目；")
    lines.append("4. 面试前查一遍避雷名单，准备工时相关的反问；")
    lines.append("5. 每周日对照新版报告复盘一次（订阅月更快照会自动带来增量）。")
    lines.append("")
    lines.append("---")
    lines.append("*本策略由坑位图鉴基于报告数据契约确定性生成，不涉及 AI；数据口径与局限见完整版报告。*")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="gaj strategy",
        description="基于付费报告 bundle 与个人预期生成个性化求职策略（纯规则，无 AI）",
    )
    ap.add_argument("--bundle", required=True, help="报告 bundle JSON 路径（reporter --save-bundle 导出）")
    ap.add_argument("--profile", default="data/profile.md", help="个人画像 Markdown 路径")
    ap.add_argument("--out", default=None, help="输出路径（默认 stdout）")
    args = ap.parse_args(argv)

    bundle_path = Path(args.bundle)
    if not bundle_path.exists():
        print(f"✗ bundle 不存在: {bundle_path}", file=sys.stderr)
        return 1
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"✗ bundle 不是合法 JSON: {exc}", file=sys.stderr)
        return 1

    profile_text = ""
    profile_path = Path(args.profile)
    if profile_path.exists():
        profile_text = profile_path.read_text(encoding="utf-8")
    else:
        print(f"⚠ 画像文件不存在: {profile_path}，将按未标注画像生成", file=sys.stderr)

    strategy = build_strategy(bundle, parse_profile(profile_text))
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(strategy, encoding="utf-8")
        print(f"✓ 策略已生成: {out}")
    else:
        print(strategy)
    return 0


if __name__ == "__main__":
    sys.exit(main())
