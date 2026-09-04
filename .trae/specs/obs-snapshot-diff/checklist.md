# Checklist

- [x] 三张表 + `jobs.collection_epoch` 已按 spec 迁移建好（Task 1）。

- [x] 每口径存在且最多一个活跃纪元；首次带 scope 导出自动开首纪元（Task 2）。

- [x] report 导出（带 scope）自动固化快照并推进入口纪元；无 scope 不落（Task 2）。

- [x] 快照只统计活跃纪元的岗位，**11 月快照不带入 9 月遗留数据**（核心隔离用例，Task 2/7）。

- [x] 快照包含各观察台视图聚合指标（Task 2）。

- [x] 快照包含该口径该纪元可见岗位成员行（Task 2）。

- [x] 采集写入时给 `jobs` / `job.json` 打上活跃纪元标记（Task 3）。

- [x] `reindex` 后 `collection_epoch` 不丢失（持久化于 job.json）（Task 3）。

- [x] daily 与 full 采集行为一致，都只并入活跃纪元、不单独落快照/切纪元（Task 3）。

- [x] 不存在独立 capture/backfill 命令 —— 快照仅由带 scope 的报告导出触发（Task 2）。

- [x] 中断不产生快照、不切纪元、可续跑不重启新一轮（Task 4）。

- [x] 可列出某口径全部快照（含 period\_month / period\_quarter）（Task 5）。

- [x] 可对同口径两个快照输出聚合指标 delta 与岗位新增/消失（Task 5，只读入口验证）。

- [x] 可按 `source_link + collection_epoch` 回溯加载**历史纪元**完整岗位行（Task 5.4）。

- [x] `AGENT.md` 与 `gaj-agent/SKILL.md` 已补充快照/diff 用法（Task 6）。

- [x] 现有列表/观察台/报告主路径行为保持不变（Task 7 回归）。

