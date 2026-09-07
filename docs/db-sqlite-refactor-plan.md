# SQLite 使用改进方案（待排期重构）

> 状态：**待办 backlog**，不阻塞当前功能。按下方优先级在需要时安排重构。
> 结论先写在最前：**架构方向合理，不换引擎**。以下均为"边界清晰化 + 去重"类优化，
> 无一处涉及数据迁移或推翻现有模型。量级参考：约 600 岗位 / 470 公司 / index.db 12MB。

---

## 一句话总结

`index.db` 当前混装了**两类性质完全不同的数据**：可重建的派生缓存 + 不可重建的历史快照。
改进核心只有三件事：**把可重建的与不可重建的分清楚、把重复的计算逻辑收拢、把备份补上。**

---

## 优先级总览

| 优先 | 主题 | 风险/收益 | 复杂度 |
|---|---|---|---|
| P0 | 公司分公式去重 | 高收益 · 消除真实重复 | 中 |
| P1 | 派生缓存 vs 历史快照 分离声明 | 中收益 · 防误删丢历史 | 小 |
| P1 | 整库冷备 + WAL checkpoint | 中收益 · 防手滑/盘损 | 小 |
| P2 | 口径影子"写陷阱"防御 | 低 · 当前单人够用 | 中 |
| P2 | 口径视图读文件（性能） | 低 · 规模小无感 | 中 |
| P3 | FTS 只索引 JD 前 20000 字符 | 低 · 规模下无感 | 小 |

---

## P0 · 公司分公式去重（首推）

**现状**：`company_score` 的完整计算（头部加权均值 + 活跃度修正 + 信息完整度加成）在
`gaj/store/index.py` 里被**复制成两份**：

- `apply_scope()` 口径影子重算 —— L373~L394
- `refresh_company_stats()` 物化生成 —— L771~L799

两处逻辑逐行几乎一致，但改动必须同步两处。

**改法**：抽一个纯函数，接收 `(job_rows, company) -> company_score`，两处共用。
公式结构（来自 `design/图鉴进化方案.md`）不变，只收敛实现。

**涉及文件**：`gaj/store/index.py`

---

## P1 · 派生缓存 vs 历史快照 分离声明

**现状**：`index.db` 一个文件同时承载：

- **可重建**（删了 `reindex()` 即恢复）：`jobs / companies / scores / company_stats / jobs_fts`
- **不可重建**（点时刻存档）：`collection_epochs / observatory_snapshots / snapshot_members / source_links.label`

当前用户/维护者若删除 `index.db`，对两类数据的损失预期不一致，易误伤历史。

**改法（二选一，推荐先做文档）**：
1. **低成本**：在 `gaj/config.py`、`gaj/store/index.py` 顶部注释中，明确区分"可重建派生"与"不可重建历史归档"两段，并在 README/AGENT.md 标注"删除 index.db 会永久丢失观察台历史快照与来源口径命名"。
2. **彻底**：拆成 `index.db`（可重建缓存）+ `observatory.db`（历史归档，独立文件名，单独备份）。

**涉及文件**：`gaj/config.py`、`gaj/store/index.py`、`gaj/store/observatory_snapshot.py`

---

## P1 · 整库冷备 + WAL checkpoint

**现状**：
- `index.db` 启用了 WAL（`PRAGMA journal_mode=WAL`），最近未 checkpoint 的数据在 `index.db-wal` 里；直接拷贝 `index.db` 会漏数据。
- 现有 `repo.backup_dir()` 只备份目录，用在迁移前，**没有面向日常的整库快照备份**。

**改法**：
- 提供一段冷备说明/脚本：`PRAGMA wal_checkpoint(TRUNCATE)` → 拷贝 `index.db (+ -shm/-wal)` → 落一份带时间戳的备份。
- 或直接用 sqlite `backup()` API 在线备份，避免停机。

**涉及文件**：`gaj/store/index.py`（或新增 `gaj/store/backup.py`）

---

## P2 · 口径影子"写陷阱"防御

**现状**：口径隔离靠 `temp.jobs` 影子表 + `contextvars.ContextVar`（`CURRENT_SCOPE`）。
`index.session()` / 代码注释已明确警告"**不要在口径连接上做写操作**，写会落进 temp 表并随连接销毁"。
属内存性的 footgun —— 未来若后台线程/定时任务在 scope 上下文中开连接写库，会静默写丢。

**改法（低优先）**：
- 给"写库入口"统一走独立（无 scope）连接，或断言 scope 为空；
- 或在 `session()` 检测到 scope + 写意图时报错。

**涉及文件**：`gaj/store/index.py` L284~L463

---

## P2 · 口径视图读文件（性能天花板）

**现状**：`apply_scope()` 对每个带口径的请求，遍历 `main.companies` 全表并对每家
调用 `repo.load_company()` 读 `company.json`（当前约 470 家公司/请求 → 470 次文件读）。
这违背"DB 是加速层"的初衷 —— scope 视图反而把读请求打回文件系统。规模小无感，但它是第一块天花板。

**改法（低优先）**：把公司只读字段（intro 有无 / 经营范围有无 / 匿名标记）物化进 `companies`/`company_stats`，
`apply_scope` 改为纯 SQL 读，不再逐家读文件。

**涉及文件**：`gaj/store/index.py`、`gaj/store/repo.py`

---

## P3 · FTS 只索引 JD 前 20000 字符

**现状**：`upsert_job()` 写入 FTS 时截断 `jd.full[:20000]`，超长 JD 的尾部不可全文检索。
规模小（~600 岗）可接受，先记录边界。

**涉及文件**：`gaj/store/index.py` L592~L602

---

## 附 · 明确接受的取舍（不列为待办）

- `skills / welfare / cities` 用 JSON 串存 TEXT、`welfare` 靠 `LIKE` 匹配 —— 规模换简单，符合场景，不追求关系纯度。
- 快照成员是冻结历史，`delete_job` 不改 `snapshot_members` —— 有意保留，语义是"历史快照 ≠ 当前库"，已归档此语义。
- 文件 + DB 双写非整体原子 —— 崩溃可能短暂不一致，靠"文件为真相 + reindex 自愈"兜底，属可接受设计。