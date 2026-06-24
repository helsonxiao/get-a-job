# AI Fallback 逻辑（v2 - 独立修正模块）

## 模块定位
- **输入**：`scoring_rules.md（v3）` 输出的 JSON（含各维度得分、触发标记、原始数据快照）。
- **输出**：修正后的 JSON（可覆盖或补充原得分），以及文字说明。
- **触发**：仅当输入 JSON 中 `ai_intervention_needed == true` 时才执行，否则直接透传原始结果。
- **目标**：对规则无法覆盖的“语义判断”和“信息缺失”进行修正，不修改任何固定规则逻辑。

---

## AI 任务列表（按触发编号映射）

| 触发编号 | AI 任务 | 输入数据 | 输出数据 | 写入字段 |
| :--- | :--- | :--- | :--- | :--- |
| A-01 / A-02 | **WLB 真实情况评估** | `JD.公司全称` + `JD.公司所在城市` + 当前规则 WLB 得分 | 修正后的 WLB 得分（0~10） + 修正理由（100字内） | `dimension_scores.wlb_corrected` <br>`wlb_correction_reason` |
| A-03 | **业务方向语义匹配** | `Profile.偏好业务方向` + `JD.公司所属行业` + `JD.业务方向或产品` | 0~1 语义相似度分数 | `dimension_scores.growth_business_semantic_score` |
| A-04 | **模糊区间综合建议** | 完整个人画像 + 完整 JD + 所有维度得分 | 最终推荐结论（“强烈推荐/可以考虑/不建议”）+ 200字分析 | `final_recommendation` <br>`recommendation_reason` |
| A-05 | **信息补全（联网搜索）** | `JD.公司全称` + `JD.职位名称` | 补充的“岗位职责”或“团队介绍”文本摘要 | `jd_enriched.description` <br>`jd_enriched.team_intro` |
| A-06 | **深度匹配分析报告** | 完整个人画像 + 完整 JD + 所有维度得分 | 300字以上分析报告，含匹配亮点、风险点、面试准备建议 | `deep_analysis_report` |

---

## AI Fallback 输出 JSON 格式

```json
{
  // 继承原规则打分的所有字段
  "status": "PASS",
  "reject_reason": null,
  "dimension_scores": {
    "finance": 8.5,
    "growth": 7.0,
    "resource": 9.0,
    "wlb": 6.5
  },
  "total_score": 7.7,
  "ai_intervention_needed": true,
  "triggered_ai_rules": ["A-01", "A-04"],

  // ⬇️ 以下为 AI Fallback 新增/修正字段 ⬇️
  "ai_corrections": {
    "wlb_corrected": 4.5,               // A-01: AI 修正后的 WLB 得分
    "wlb_correction_reason": "经查询该公司在脉脉和知乎的口碑，实际加班强度较大（平均下班时间21:00），且大小周制度存在，故将 WLB 得分从 6.5 下调至 4.5。",
    "growth_business_semantic_score": 0.85,  // A-03: 业务方向语义匹配度
    "final_recommendation": "可以考虑",      // A-04: 最终推荐结论
    "recommendation_reason": "虽然 WLB 实际表现低于预期，但薪资上限达到55万，技术栈高度匹配（React + Node.js），且该公司在音频赛道处于头部位置，职业发展价值较高。建议面试时重点确认加班补偿和项目周期。",
    "jd_enriched": {                      // A-05: 信息补全
      "description": "负责音频直播互动功能的架构设计与核心模块开发...",
      "team_intro": "隶属于直播中台团队，约15人，负责猫耳FM Web端..."
    }
  },
  "deep_analysis_report": null,  // A-06: 用户触发时才生成，避免成本浪费
  "ai_usage_log": {
    "tokens_used": 1250,
    "cost_usd": 0.025,
    "model_used": "gpt-4o-mini",
    "timestamp": "2026-06-21T16:30:00Z"
  }
}
