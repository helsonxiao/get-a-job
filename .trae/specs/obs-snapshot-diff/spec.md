# 同口径多采集快照 + 月度/季度 Diff Spec

## Why

同一个「口径」（BOSS 列表筛选链接，即 `source_link`）每隔一两个月会重新采集。
当前 `jobs` 表以 `job_id` 为主键，增量用 `INSERT OR REPLACE`、`reindex()` 又是「先清空再重插」，
**同一口径的历史采集会被覆盖、无法按月份区分**，更无法做跨月/季对比。

同时，若不复位/隔离：**11 月导出的快照会把数月前采集、之后消失但未删除的滞留行带进去**，使聚合与 diff 出现「隔月串数据」。
因此快照必须基于**采集纪元（epoch）隔离**：每一次 report 导出后，后续新采集的数据进入**新纪元**，下一份快照只统计新纪元内的岗位，绝不混入前一纪元的遗留数据。

**触发约定（依据用户确认）**：

- **仅以 report 导出捕获快照**，不需要额外的显式 capture/backfill 命令；第一次导出某口径时自动开启首纪元并固化首份快照。

- **daily 与 full 采集不加区别**：两者都只是把岗位写入当前活跃纪元；快照/纪元切换**只由 report 导出触发**。

## What Changes

- **采集纪元（epoch）隔离**（核心）：每口径维护一个活跃纪元；所有采集（含 daily/full）都打进活跃纪元；report 导出某口径时用活跃纪元内岗位固化快照，成功后闭合旧纪元、开启新纪元 → 后续数据隔离。

- 新增三张表：`collection_epochs`（口径→活跃纪元）、`observatory_snapshots`（快照头 + 聚合指标 JSON）、`snapshot_members`（快照岗位成员）。

- `jobs` 表新增 `collection_epoch` 列，采集写入时打上活跃纪元；快照只统计 `source_link + 活跃纪元`。

- **快照触发：仅** **`report-bundle`** **导出（带 scope）**。

- 快照基于**口径影子 + 纪元过滤**复用现有 `observatory_*` 聚合，数字与 `?scope=<link>` 一致，快照不可变。

- **中断恢复**：沿用现有续翻机制（`crawl_state` 的 `resume_page`/`last_dup_page`/`dup_stop_pages`），中断只把已采岗位留在活跃纪元，不落快照、不切纪元。

- **历史纪元可回溯**：旧纪元 `jobs` 行不删除；`collection_epoch` 持久化于 `job.json`，`reindex` 不丢失；可按 `source_link + collection_epoch` 加载旧纪元完整岗位行；diff 直接读两份快照的 `snapshot_members`/`metrics`。

- 新增快照列表与 **Diff**（同一口径两个快照：聚合 delta + 岗位新增/消失）。

- **文档更新**：`AGENT.md` 与 `gaj-agent/SKILL.md` 补充「多采集快照 / report 导出固化快照 / 月度季度 diff」说明。

- **不**改动 `jobs` 表主键、**不**改动现有列表/观察台/报告主查询路径（快照侧独立过滤，零回归）。

## Impact

- Affected specs: 采集流程、report 导出、市场观察台（只读复用）。

- Affected code:

  - `gaj/store/index.py` — schema 迁移（三张新表 + `jobs.collection_epoch`）+ 快照查询函数。

  - `gaj/store/observatory.py` — 复用，不改（被快照捕获调用）。

  - `gaj/store/observatory_snapshot.py`（新增）— 纪元管理 / 快照捕获 / 列表 / diff / 回溯。

  - `gaj/store/reportbundle.py` — `build_report_bundle` 带 scope 时捕获快照并推进纪元（不改返回契约）。

  - `gaj/scraper/adapter.py` / `index.touch_job_source_link` / `migrate.reassign_source_links` — 写入时打纪元标记。

  - `gaj/repo.update_source_link` 旁路 — `job.json` 持久化 `collection_epoch`（类似 `source_link`）。

  - 文档：`get-a-job/AGENT.md`、`get-a-job/gaj-agent/SKILL.md`。

## ADDED Requirements

### Requirement: 采集纪元隔离（核心）

系统 SHALL 以「采集纪元」为单位隔离同一口径不同时段的数据，保证快照不串期。

#### Scenario: 导出后新采集进入新纪元

- **WHEN** 对某口径执行 report 导出（带 scope）

- **THEN** 该导出：① 以当前**活跃纪元**内 `source_link` 的岗位计算并落一份快照（聚合 + 成员）；② 成功后**闭合该纪元并开启新纪元**。

- **AND WHEN** 该导出之后有新采集（daily 或 full）

- **THEN** 新采集岗位打入**新纪元**；下一次导出只统计新纪元内岗位 → **11 月快照不会带入 9 月遗留数据**。

#### Scenario: 旧纪元数据不移除但不再进新快照、仍可回溯

- **WHEN** 已闭合纪元内仍有岗位留在活库 `jobs`

- **THEN** 这些行**不删除**（保留收藏/忽略/历史），**不计入**后续纪元的快照聚合与成员；旧纪元数据按「历史纪元可回溯」可随时调出查看/做 diff。

### Requirement: 历史纪元数据可回溯查看

系统 SHALL 让某口径的**旧纪元数据**事后仍可调出（Diff 依赖此能力）。

#### Scenario: 查看某快照成员与聚合

- **WHEN** 指定某口径 + 某快照（或某纪元）

- **THEN** 可读其冻结聚合指标（`observatory_snapshots.metrics`）与岗位成员（`snapshot_members`）。

#### Scenario: 加载历史纪元完整岗位行

- **WHEN** 需基于某历史纪元做深查/按新条件重算

- **THEN** 可用 `WHERE source_link=? AND collection_epoch=<历史纪元>` 读取该纪元完整岗位行；`collection_epoch` 持久化于 `job.json`，`reindex` 后重建不丢失。

### Requirement: 快照捕获（仅由 report 导出触发）

系统 SHALL 在带 `scope_link` 的 report 导出时，为该拨款口径固化为当前纪元的不可变快照并推进纪元；无需独立的捕获命令。

#### Scenario: report 导出即固化为快照

- **WHEN** 对某 `source_link` 生成/导出 report（report-bundle，带 scope）

- **THEN** 导出末尾自动捕获该口径活跃纪元快照（聚合 + 成员）并推进入口纪元；此快照即「权威最终快照」。

#### Scenario: 无 scope 导出不受影响

- **WHEN** 导出不指定 `scope_link`（全量）

- **THEN** 不触发单口径快照/纪元切换，避免多口径混合导出落语义混乱快照。

#### Scenario: 首次导出自动开启首纪元

- **WHEN** 某口径尚未有纪元时首次带 scope 导出

- **THEN** 自动开启首纪元，并以当前活跃纪元（初建）内的该口径岗位固化首份快照；无需单独回填。

#### Scenario: daily/full 只并入活跃纪元，不单独触发快照

- **WHEN** 任意采集（daily 增量或 full 批量）写入岗位

- **THEN** 岗位只并入当前活跃纪元，**不触发快照、不切换纪元**；快照/纪元切换仅由上述带 scope 的 report 导出触发。

### Requirement: 中断恢复

系统 SHALL 支持采集异常中断后的续跑，不留过期快照、不误切纪元。

#### Scenario: 中断后续跑

- **WHEN** 一次采集被中断（Ctrl+C / 浏览器断开 / 异常退出）

- **THEN** 已增量入库的岗位留在当前活跃纪元；下次沿用现有续翻机制（`resume_page`/`last_dup_page`）继续，不立即重开全量。

- **AND** 由于快照/纪元切换只在带 scope 的 report 导出时发生，中断不落半截快照、不切纪元。

### Requirement: 快照内容（聚合指标 + 岗位成员）

系统 SHALL 固化快照时，基于「口径影子 + 活跃纪元」计算聚合指标与岗位成员。

#### Scenario: 聚合指标

- **WHEN** 固化某口径活跃纪元快照

- **THEN** 快照记录 `job_count`/`company_count`，并以 JSON 保存该视野聚合：薪资定价（`observatory_salary_pricing`）、信号雷达（`observatory_signal_radar`）、技能榜（`observatory_skill_leaderboard`）、公司双榜（`observatory_company_boards`）、行业列表（`observatory_industry_list`）、热力（`observatory_geo_heatmap`）；只统计活跃纪元内岗位。

#### Scenario: 岗位成员

- **WHEN** 固化快照

- **THEN** 记录该纪元可见岗位成员行（`job_id` + title / company\_name / city / district / industry / salary\_mid / exp\_min / edu\_level / overtime / outsourcing / best\_total / online），供岗位级「新增/消失」与按维重算。

### Requirement: 快照列表与 Diff 对比

系统 SHALL 支持按 `source_link` 列快照，并对同一口径的两个快照做差异对比。

#### Scenario: 列出快照

- **WHEN** 指定一个 `source_link`

- **THEN** 返回该口径快照列表：`snapshot_id`、`captured_at`、`period_month`、`period_quarter`、`job_count`、`company_count`，按时间倒序。

#### Scenario: 聚合指标 Diff

- **WHEN** 指定 `source_link` 与两个快照（或两个 `period_month`/`period_quarter`）

- **THEN** 返回各观察台视图 delta：薪资分位平移、技能需求增减、公司榜进出、行业分布、红旗信号变化等。

#### Scenario: 岗位成员 Diff

- **WHEN** 进行上述对比

- **THEN** 返回该口径「新增岗位 / 消失岗位」列表与计数（成员差集），并附带差异采样。

### Requirement: 文档更新

系统 SHALL 同步更新操作文档，便于后续通过 agent 触达快照/diff 能力。

#### Scenario: agent 文档说明新能力

- **WHEN** 快照/diff 能力落地后

- **THEN** `get-a-job/AGENT.md` 与 `get-a-job/gaj-agent/SKILL.md` 补充：report 导出会固化口径快照、如何通过接口列表/做同口径月度季度 diff 的用法与示例。

## MODIFIED Requirements

### Requirement: report 导出的快照副作用与纪元推进

`report-bundle` 导出（带 scope）不再只是纯只读输出 —— 需固化快照并推进入口纪元。

- `gaj/store/reportbundle.py:build_report_bundle(conn, scope_link=...)`：当 `scope_link` 非空时，导出末尾调用快照捕获（活跃纪元）+ 推进纪元；返回契约仍为原 bundle JSON，快照/纪元推进为副作用。

- 无 `scope_link` 的全量导出不触发单口径快照。

### Requirement: 采集写入打纪元标记

所有把岗位写入某 `source_link` 的采集路径，需同时打上该口径**当前活跃纪元**的 `collection_epoch`。

- `gaj/scraper/adapter.py` 增量入库（`_on_job_saved`/`_on_page_seen`）与 `gaj/store/index.py:touch_job_source_link`、`gaj/store/migrate.py:reassign_source_links` 联动时，把 `jobs.collection_epoch`（与 `job.json`）置为当前活跃纪元。

- `jobs.collection_epoch` 持久化到 `job.json`（类似 `source_link`），`upsert_job`/`reindex` 后不丢失。

- 不新增 full/daily 采集模式参数 —— daily 与 full 行为一致，仅写入活跃纪元。

## REMOVED Requirements

无。
