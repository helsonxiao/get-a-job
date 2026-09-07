# 数据分析可信度与统计维度改进方案（待排期）

> 状态：**待办 backlog**。数据分析是项目的立身之本，本方案把"数据准确可靠、可维护、
> 对求职者更有价值"拆成可执行的小步；没有一步涉及推翻现有模型，都是"口径固化 +
> 可信度基础设施 + 求职者价值维度"。

---

## 一句话总结

系统已有一批好用的统计视图（薪资定价 / 技能榜 / 红旗雷达 / 行业下钻 / 公司双榜 / 快照），
报告与 Web 也大多复用同一批函数（一致性有基础）。真正的问题不在"有没有"，而在三处：

1. **口径会漂移**：同一字段名 `avg_salary` 在同一统计引擎里，一半视图是均值、一半是中位数，
   且没有任何自动保障能拦住这种漂移（只有人工注释）。
2. **没有可信度透出**：多数视图不告诉用户"样本几个、数据几号采集的、门槛是多少"，求职者无法区分可靠数字与孤例。
3. **工程保障薄弱**：观察台没有一个黄金测试锁死口径；快照只有靠人工导出才会产生，
   趋势类分析没有数据基建；没有数据质量体检入口。

改进分三层：**P0 口径固化（正确性）→ P1 可信度基础设施（可靠性/可维护）→ P2 求职者价值维度（新增统计）。**

---

## 一、现状盘点

### 已有统计维度（`gaj/store/observatory.py` + 快照 + reportbundle）

| 维度 | 函数 | 消费方 |
|---|---|---|
| 薪资定价曲线（overall 分位 + by_exp/by_edu/by_industry/by_stage） | `observatory_salary_pricing` | Web 观察台 / 报告 / calibrator / strategy 谈薪 |
| 地理机会热力图（网格聚合） | `observatory_geo_heatmap` | Web 观察台 |
| 热点区域 TopN（district×industry 切片） | `observatory_district_top` | Web 观察台 / 报告 |
| 红旗信号雷达（summary/按行业/红旗公司榜） | `observatory_signal_radar` | Web 观察台 / 报告 / calibrator |
| 技能热度榜（需求/公司/溢价/Top行业） | `observatory_skill_leaderboard` | Web 观察台 / 报告 / calibrator / strategy 技能缺口 |
| 公司双榜（招聘力度 + 薪资，动态样本门槛） | `observatory_company_boards` | Web 观察台 / 报告 / strategy 投递优先级 |
| 行业总览 / 行业详情 | `observatory_industry_list/detail` | Web 行业下钻 / 报告 |
| 雇主画像（月薪构成/工时/规模等） | reportbundle `_employer_block` | 报告 / strategy 谈薪 |
| 采集纪元 + 快照（4 视图固化 + 快照 diff） | `observatory_snapshot` | 报告导出钩子 / Web 快照切换 |

### 值得肯定（勿破坏）

- **报告与 Web 共用同一批函数**，`test_report_vs_observatory.py` 已锁定"共享维度数字一致"，有防漂移意识。
- **快照 + diff**（`collection_epochs` / `snapshot_members`）已具备趋势分析的数据模型，只是"没有规律地产生"。
- **skill 归一化**（`normalize_skill` + 别名/停用词表）专门处理了平台脏标签，是高质量基础。
- **动态样本门槛**（公司双榜 `min_jobs`）已有"样本不足要诚实标注"的先例。

---

## 二、问题诊断

### A. 数据准确性（P0）—— 口径一致性与明显错误

**A1 · `avg_salary` 均/中位语义混用（最优先）**
同一函数族里，字段名 `avg_salary` 两种公式并存：

- **中位数**：`skill_leaderboard` 榜项（observatory.py L581）、`market_avg_salary`（L558/L598）、`company_boards`（L650，注释明确"语义=薪资中位数"）
- **均值**：`geo_heatmap`（L321）、`district_top`（L399）、`red_flag_companies`（L524）、`industry_detail` 的 `top_skills`/`top_companies`/`top_districts`（L841/L878/L902）

同一个"均薪"键，前端与报告消费时无从分辨公式。已在注释里承诺的"v3.0 统一中位口径"
只是约定，没有任何机制防止下一次新视图又写均值。对求职者，这是**期望偏差**的直接来源
（均值被高薪孤例拉高，中位数才代表"普通拿到手的数"）。

**A2 · `industry_detail` 代表公司双重排序 bug**
L881-L882 连续两次 `sort`，第二个按键把第一个覆盖：
```python
top_companies.sort(key=lambda x: (x["best_score"] is not None, x["best_score"]), reverse=True)
top_companies.sort(key=lambda x: x["job_count"], reverse=True)   # ← 覆盖了上一行
```
注释意图"在招数优先、有公司分的按分数排前"实际未生效，目前只是纯按岗位数排。

**A3 · 样本门槛不一致**
- `salary_pricing` 的 by_exp/by_edu：有样本就出，无最小门槛；
- `industry_detail` 的 by_exp/by_edu：要求样本 ≥2；signal ≥3；
- 阈值魔法数散落：`_MIN_SALARY = 2`、`_MIN_SIGNAL = 3`、`MIN_BOARD_ENTRIES = 5`（observatory.py L607/L678/L679）。
- 后果：同一维度的中位数（如"本科薪资"）在总览页和行业页口径不同，且无解释。

**A4 · 可信度不随数据透出**
除公司双榜 `min_jobs` 外，多数视图不标注样本量/数据日期/门槛。一个 2 样本的中位数和
200 样本的中位数在页面展示上没有差别。

**A5 · AI 字段进统计无标记**
统计消耗了 AI 产出：岗位 `best_total`（行业详情 top_companies 的 best_score）、
公司分 AI 兜底（`company_score_ai`，index.py L396-407）。但 AI 修正（`ai_corrections`）、
置信度、是否 AI 生成等信息没有随行携带到统计层，"统计数字里混了多少 AI 判断"不可见。

### B. 数据可靠性（P1）—— 防退化、可观测

**B1 · 统计函数无黄金测试**
现有测试是"报告↔观察台一致性""快照 diff""报告 bundle"，没有对单个聚合函数
（如 `observatory_salary_pricing`、`observatory_industry_detail`）的**固定 fixture 期望值断言**。
口径漂移只能靠人肉发现。

**B2 · 无数据质量体检**
没有一处入口检查：岗位重复率、字段空值率、异常薪资（如解析错的 999 万）、JD 污染
（`looks_polluted` 只用于采集时单条判断）。观察台展示的数据"健不健康"不可观测。

**B3 · 快照只在人工导出时产生**
快照唯一触发点是"带 scope 的报告导出"（observatory_snapshot.py L4-8 语义）。没有规律快照，
就没有"本月 vs 上月"的趋势数据。

**B4 · 无数据日期透出**
各视图没有"数据采集于 X"字段，求职者无法判断行情新鲜度（跟 A4 同类，但日期独立于样本量）。

### C. 可维护性（P1/P2）—— 口径单一事实源

- **C1** `exp_buckets`（L204-211 与 L802-809）、`edu_labels`（L218-224 与 L816-822）桶定义整段复制。
- **C2** `company_score` 公式在 `apply_scope` 与 `refresh_company_stats` 复制（BOARD 已有 P0 条目，继续有效）。
- **C3** 指标定义（公式/门槛/单位/样本标注）散落在各函数注释里，没有单一事实源（口径注册表）。
- **C4** `observatory.py` 单文件 921 行，全部是手写 SQL + Python 聚合，无"口径层 / 聚合层 / 组装层"分层。
- **C5** `_VISIBLE` 过滤串既定义了模块常量（L13）又在 `salary_pricing` 内内联一份（L183）。

### D. 求职者价值缺口（P2）—— 缺什么统计维度

现有维度覆盖"水位"（薪资）、"热度"（技能/公司/区域）、"风险"（红旗）。缺的是**决策型维度**：

| # | 缺失维度 | 对求职者的价值 | 是否已有数据基建 |
|---|---|---|---|
| D1 | **行情趋势**：岗位量/薪资水位/技能热度 环比变化、新岗率 | 判断"该行业在扩张还是收缩、现在是不是好时机" | 快照 diff 已就绪，只差"规律产生 + 端点" |
| D2 | **岗位生命周期**：新岗占比、7/14 天下架率 | 判断竞争激烈度与投递窗口 | `first_seen / last_seen / online` 已有 |
| D3 | **技能组合画像**：技能×技能共现、0-3 年入门技能榜 | 比单技能榜更能指导学习路径与简历切题 | 归一化管线已就绪 |
| D4 | **薪资成长曲线**：同行业/技能下 0-3→3-5→5-8 跳升 | 判断"这个方向的成长空间" | by_exp 数据已有，只需串联展示 |
| D5 | **期望校准闭环**：你的目标薪资落在市场 P25-P75 的位置 + 建议 | 直接对求职者最有用的"期望值校准" | calibrator 机制已就绪，`data/calibration/` 仍为空；strategy 有谈薪逻辑可迁 | 
| D6 | **公司通勤距离**（BOARD 长期方向） | 真实生活成本维度 | `profile.md` 只有上限规则，坐标未落 |

---

## 三、改进方案设计

三层推进，每层做完都有独立价值，不互相阻塞。优先级沿用 BOARD 约定：P0 影响正确性/数据，
P1 影响可用性，P2 一般优化，P3 规模大了再说。

### 第一层 · 口径固化（P0，正确性）—— 建议下一轮动手

| 项 | 改动 | 涉及 |
|---|---|---|
| 1.0 统一薪资聚合语义 | 默认取**中位数**（与 v3.0 约定、salary_pricing、公司双榜一致）；只留「均值」给明确需要的地方并改名 `avg_salary_mean`；字段级语义标注（`_sem` 或注册表字段）。键名保留兼容前端的取法由注册表统一，避免重复造第三次 | observatory.py 全部收益链 |
| 1.1 修复双 sort | 单一排序键 `(job_count desc, 有 best_score 在前, best_score desc)` | observatory.py L881-882 |
| 1.2 门槛收敛 | 把 `_MIN_SALARY/_MIN_SIGNAL/MIN_BOARD_ENTRIES` 抽成公共同名变量；同一维度（by_exp/by_edu）总览与行业页用同一门槛；门槛随结果透出 | observatory.py |
| 1.3 口径注册表 | 建 `OBSERVATORY_METRICS` 常量：每个指标的 {字段名, 公式语义（median/mean/count）, 最小样本门槛, 单位, 消费方}；函数与测试都引用它 | observatory.py + 测试 |

**验收**：`tests/test_observatory_metrics.py` 黄金测试——固定 fixture 数据，断言每个聚合函数
的完整输出（含 `avg_salary` 语义、门槛、空样本降级）；`avg_salary` 语义若再次混写，测试直接红。

### 第二层 · 可信度基础设施（P1，可靠性/可维护）

| 项 | 改动 | 涉及 |
|---|---|---|
| 2.0 快照自动化 + 趋势数据 | 报告导出之外，增加周期性（如每日）自动 capture；快照 diff 结果持久化（新表或复用），Web 观察台出 `/api/observatory/trend?metric=job_count|salary_p50|skills` 环比端点 | observatory_snapshot.py + web/routes/observatory.py + 新增趋势表 |
| 2.1 数据质量体检 CLI | `gaj qa data`：重复岗位率、关键字段空值率、异常薪资（>3σ 或超白名单）、JD 污染率、快照新鲜度；输出体检报告，观察台上线前哨兵 | 新增 `gaj/cli` 子命令（复用 denoise 的 `looks_polluted`） |
| 2.2 可信度透出 | 视图统一携带 `{sample_count, data_as_of, min_threshold}` 元数据（注册表驱动）；前端低样本视图显示"样本 N，仅供参考" | observatory.py 返回值 + 前端 |
| 2.3 桶定义去重 | `exp_buckets / edu_labels / HOURS_ORDER / SCALE_ORDER` 抽模块常量共用 | observatory.py（顺带完成 C1/C5） |
| 2.4 AI 字段来源标记 | 统计聚合带上 AI 生成占比（如 `ai_scored_count/job_count`）与置信度下限，供前端/报告标注"含 AI 判断" | index.py + observatory 消费端 |

### 第三层 · 求职者价值维度（P2，新增统计）

按 投入产出 排序，前 4 项基本复用现有数据与管线，成本低：

| # | 维度 | 落地方式 | 复用 |
|---|---|---|---|
| D1 行情趋势 | 快照 diff → 趋势端点 + 观察台"市场温度"卡片（岗位量环比/薪资 P50 环比/新岗占比） | 第二层 2.0 的产物 |
| D4 薪资成长曲线 | by_exp 分位串联成曲线 + 行业/技能交叉；报告与 web 同组件 | salary_pricing/industry_detail 已有数据 |
| D3 技能组合画像 | 技能共现矩阵 topN（python×机器学习）+ 0-3 年经验技能榜；归一化后聚合即可 | skill_leaderboard 管线 |
| D2 岗位生命周期 | `first_seen→last_seen` 计算新岗占比、7 天下架率；行业维度下钻 | jobs 表字段已有 |
| D5 期望校准闭环落地 | calibrator 从"机制"到"落地历史"（data/calibration 产历史）+ web 输出"你的期望 vs 市场 P25/P50/P75 位置与建议" | calibrator.py + strategy.py 谈薪逻辑 |
| D6 公司通勤距离 | profile 坐标 + 公司坐标 → 距离/通勤估算，评分与公司卡展示 | profile.md 占位 + 公司数据 |

### 明确不做（记录取舍）

- **置信区间 / 显著性检验**：样本量级（~600 岗）不足以支撑，展示"样本量"比"区间"更诚实。
- **推翻 JSON 列、做关系范式化**：规模换简单，维持现状（与 db-sqlite-refactor-plan 附则一致）。
- **对均值/中位数做自动纠偏**：只统一口径与标注，不偷偷"修正"数值——保持数据原始诚实。

---

## 四、BOARD 挂载建议

| 优先级 | 项 | 一句话 |
|---|---|---|
| P0 | 薪资口径统一（avg_salary 均/中位混用 + 行业代表公司双 sort）+ 黄金测试 | 正确性；不修，每个新视图都可能继续漂移 |
| P1 | 数据质量体检 CLI + 快照自动化 + 可信度透出 | 可靠性；让"数字可信"可观测可验证 |
| P2 | 行情趋势 / 薪资成长曲线 / 技能组合画像 / 岗位生命周期 / 校准闭环落地 | 求职者价值；多数复用现有数据 |

---

## 更新日志

- 2026-09-07：建档。基于现状代码盘点（observatory.py / snapshot / reportbundle / calibrator / tests）。