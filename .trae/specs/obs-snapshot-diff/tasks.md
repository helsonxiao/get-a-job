# Tasks

- [x] Task 1: 数据表与迁移：新增 `collection_epochs` / `observatory_snapshots` / `snapshot_members`，并为 `jobs` 加 `collection_epoch` 列。
  - [ ] 1.1 `gaj/store/index.py` `_migrate` 建表 + 给 `jobs` 加 `collection_epoch TEXT`（补列模式）。

  - [ ] 1.2 列契约：`collection_epochs(epoch_id, source_link, opened_at, status, closed_at)`；(snapshots) `snapshot_id, source_link, epoch_id, captured_at, period_month=YYYY-MM, period_quarter=YYYY-Qn, job_count, company_count, metrics TEXT(JSON)`；(members) `(snapshot_id, job_id)` 复合主键 + 关键字段。

  - [ ] 1.3 快照侧查询函数：按 source\_link 列快照、按 snapshot\_id 取指标/成员、按口径取活跃纪元、按口径+纪元取岗位行。

- [x] Task 2: 纪元管理与快照捕获核心（`gaj/store/observatory_snapshot.py`）。
  - [ ] 2.1 活跃纪元获取 / 无则自动开启（每口径至多一个活跃纪元；首次即元）。

  - [ ] 2.2 `capture_snapshot(conn, source_link)`：口径影子 + 纪元过滤 → `observatory_*` 聚合 → 写 snapshots + members。

  - [ ] 2.3 原子「捕获 + 闭合旧纪元 + 开启新纪元」（同一连接/事务，失败不切纪元）。

  - [ ] 2.4 report 导出钩子：`reportbundle.build_report_bundle` 带 scope 时调用捕获+推进（不改返回契约；无 scope 不触发）。

- [x] Task 3: 采集写纪元标记 + 持久化。
  - [ ] 3.1 `adapter.py` 增量入库（`_on_job_saved`/`_on_page_seen`）与 `touch_job_source_link`/`reassign_source_links` 联动时，给 `jobs` 打活跃纪元。

  - [ ] 3.2 `jobs.collection_epoch` 持久化到 `job.json`（类似 `source_link`，`repo.update_source_link` 旁路），`upsert_job`/`reindex` 不丢失。

  - [ ] 3.3 不新增 full/daily 模式参数（daily/full 一致，仅写活跃纪元）。

- [x] Task 4: 中断恢复确认与加固。
  - [ ] 4.1 确认续翻/恢复机制（`resume_page`/`last_dup_page`/`dup_stop_pages`）在现有路径生效。

  - [ ] 4.2 中断路径明确不落快照、不切纪元（仅带 scope 的 report 导出触发）；异常分支加日志说明已采岗位留当前活跃纪元。

- [x] Task 5: 快照列表与 Diff 对比。
  - [ ] 5.1 列表：按 `source_link` 返回快照（含 period 标签）。

  - [ ] 5.2 Diff：聚合指标 delta（各观察台视图数值增减）。

  - [ ] 5.3 Diff：岗位成员差集 → 新增/消失岗位列表与计数。

  - [ ] 5.4 回溯查询：按 `source_link + collection_epoch` 加载历史纪元完整岗位行（供深查/重算）。

  - [ ] 5.5 只读入口（CLI 或 Web 只读 API）：list / diff（读取用，非捕获命令）。

- [x] Task 6: 文档更新。
  - [ ] 6.1 `get-a-job/AGENT.md` 补充「report 导出固化口径快照 / 同口径月度季度 diff」用法与示例。

  - [ ] 6.2 `get-a-job/gaj-agent/SKILL.md` 补充对应能力说明与调用示例。

- [x] Task 7: 测试与验证。
  - [ ] 7.1 带 scope 导出 → 落快照 + 推进入口纪元；无 scope → 不落（回归）。

  - [ ] 7.2 核心隔离：旧纪元遗留行不计入新纪元快照（11 月快照不带 9 月数据）。

  - [ ] 7.3 首次带 scope 导出自动开首纪元并落首份快照（无需单独回填）。

  - [ ] 7.4 daily/full 采集都只并入活跃纪元、不单独落快照/切纪元。

  - [ ] 7.5 中断不产生快照、不切纪元、可续跑。

  - [ ] 7.6 Diff：已知两批成员可算出进出与 delta。

  - [ ] 7.7 历史纪元数据可按 `source_link + collection_epoch` 回溯；`reindex` 后 `collection_epoch` 不丢失。

  - [ ] 7.8 现有列表/观察台/报告主路径回归不变。

# Task Dependencies

- \[Task 2] depends on \[Task 1]

- \[Task 3] depends on \[Task 1], \[Task 2]

- \[Task 5] depends on \[Task 2]

- \[Task 6] depends on \[Task 5]

- \[Task 7] depends on \[Task 2], \[Task 3], \[Task 4], \[Task 5]

