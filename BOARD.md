# 坑位图鉴（GAJ）工程看板 · BOARD.md

> **人和 agent 共用的工程操作台。** 顶部两区（当前优先队列 / 等你动手）是接下来要动的活，
> 往下是分组 backlog 与归档。本文件取代粗糙的 `TODO.md`（旧条目已归并到下方"方向/归档"区）。
>
> 用法约定：**安排、开工、完成一项就更新本文件一次**；别把它当永久 TODO，做完即下沉归档。
> 优先级沿用 P0/P1/P2/P3：P0 影响正确性/数据，P1 影响可用性/防丢，P2 一般优化，P3 规模大了再说。

---

## ⏳ 当前优先队列（下次动手从这里挑）

| 主题 | 优先级 | 卡在哪 / 状态 | 详见 |
|---|---|---|---|
| 数据分析口径一致：均/中位混用 + 双 sort + 黄金测试 | **P0** | 未开工，方案已出，建议下一轮动手 | [data-analysis-improvement-plan.md](docs/data-analysis-improvement-plan.md#第一层--口径固化p0正确性) |
| SQLite 公司分公式去重 | P0 | 未开工，改动小收益高 | [db-sqlite-refactor-plan.md](docs/db-sqlite-refactor-plan.md#p0--公司分公式去重首推) |
| 基础工程：venv + 依赖锁定 | P1 | 未开工，等你确认方案 | §工程基础设施 |
| 整库冷备 + WAL checkpoint | P1 | 未开工 | [db-sqlite-refactor-plan.md](docs/db-sqlite-refactor-plan.md#p1--整库冷备--wal-checkpoint) |

> 建议一次只做一项，做完回填状态再挪下一个。数据分析是项目立身之本，其 P0 口径一致项
> 优先于其他 P0（当前无阻塞项，但不可再让口径继续漂移）。

---

## 工程基础设施 backlog（基础工程需求）

> 你提到"venv 之类的基础工程需求"，先列在这里，按优先级慢慢做。当前 `requirements.txt` 是裸跑无锁。

- [ ] **P1 venv + 一键安装**
  `get-a-job` 目前直接 `python3 -m gaj ...`，无虚拟环境、无可复现安装路径。
  目标：`setup.sh`（或等价）＝ `python3 -m venv .venv` + `.venv/bin/pip install -r requirements.txt`，README/AGENT 首装步骤收敛到一条命令。
- [ ] **P1 依赖版本锁定**
  `requirements.txt` 全无版本号（`fastapi>=0.100.0` 之类只是下限）。建议分 `requirements.txt`（运行）+ `requirements-dev.txt`（测试/开发），运行侧锁到可用版本区间。
- [ ] **P1 数据冷备 + WAL checkpoint**
  见 refactor plan §P1：`index.db` 启用了 WAL，直接拷 `index.db` 会漏 `.wal` 数据；需先 `PRAGMA wal_checkpoint(TRUNCATE)` 或 `backup()`。提供带时间戳的冷备脚本。
- [ ] **P2 Python 版本固定**
  运行时 Python 版本没写死（AGENT 里用的是 `python3`）。建议加 `.python-version` 或在 README 注明支持版本，避免环境漂移。
- [ ] **P2 首次安装文档联动**
  基础项落地后，把 README「安装」段、AGENT.md「前置条件」、`setup-chrome` 串成一条可照做的路径。
- [ ] **P2（候选）日志轮转**
  `logs/` 需核对是否无限增长；若是，加大小/天数轮转。此项先复核再决定。
- [ ] **P3（可选）打包成 pyproject**
  若后续要多机部署 / 提供稳定 CLI 分发，再做 packaging；个人工具阶段可长期缓。

---

## 数据库使用改进（P0–P3 backlog）

> 完整方案与改动点见 [db-sqlite-refactor-plan.md](docs/db-sqlite-refactor-plan.md)。这里只放清单。

| 优先级 | 项 | 一句话 |
|---|---|---|
| P0 | 公司分公式去重 | `apply_scope` 与 `refresh_company_stats` 各复制一份公式，抽公共函数 |
| P1 | 派生缓存 vs 历史快照 分离声明 | `index.db` 混装"可重建缓存"与"不可重建快照/纪元"，删除损失不一致 |
| P1 | 整库冷备 + WAL checkpoint | 避免拷贝丢 `.wal`，补日常备份 |
| P2 | 口径影子"写陷阱"防御 | scope 连接上写会落 temp 表丢失，防御后台线程误写 |
| P2 | 口径视图读文件（性能） | 每个带口径请求对 ~470 家公司读 `company.json`，应物化防抖 |
| P3 | FTS 只索引 JD 前 20000 字符 | 超长 JD 尾部不可搜，规模小先记录 |

---

## 数据分析可信度与统计维度（P0–P2 backlog）

> 完整方案见 [data-analysis-improvement-plan.md](docs/data-analysis-improvement-plan.md)。
> 数据分析是项目最重要的能力，工程保障此前偏薄，这里按三层推进。

| 优先级 | 项 | 一句话 |
|---|---|---|
| P0 | 薪资口径统一 | 同一字段 `avg_salary` 在统计引擎内一半是均值一半是中位数（observatory 多处），统一为中位 + 改名均值 + 注册表锁死 |
| P0 | 行业代表公司双 sort 修复 | `industry_detail` 两次 sort 互相覆盖，"有公司分按分排前"实际未生效 |
| P0 | 统计黄金测试 | `tests/test_observatory_metrics.py`：固定 fixture 断言每个聚合函数输出，防口径漂移 |
| P1 | 数据质量体检 CLI | `gaj qa data`：重复率/空值率/异常薪资/污染率/快照新鲜度 |
| P1 | 快照自动化 + 行情趋势 | 周期自动 capture + diff 持久化 + 趋势端点（岗位量/薪资 P50/技能热度环比） |
| P1 | 可信度透出 | 视图统一标注样本量 / 数据日期 / 门槛，低样本视图前端提示 |
| P2 | 求职者价值维度 | 薪资成长曲线 / 技能组合画像 / 岗位生命周期 / 期望校准闭环落地（calibrator 产历史）/ 公司通勤距离 |

---

## 长期方向（旧 TODO.md 归并，附复核标注）

- [ ] **行业政策分析**（未做）
  行业观察/下钻已上线，但"结合行业政策做分析"这一后续能力尚未实现。——对应旧 TODO 第 2 条后半。
- [ ] **市场观察数据 → 优化规则 / 画像预期**（模块在，未落历史）
  `gaj/ai/calibrator.py` 已存在，但 `data/calibration/` 为空——矫正机制已就位、尚未产生落地历史。→ 旧 TODO 第 3 条。
- [ ] **公司 - 本人通勤距离**（未实现）
  `profile.md` 只有"单程通勤上限"评分规则 + 居住地坐标占位（注释"后续用于计算通勤时间"）；公司-本地的距离/通勤时长计算尚未实现。→ 旧 TODO 第 4 条。

---

## 已完成归档（旧条目，莫再当 TODO）

- ✅ **模块 / 仓库拆分**：`get-a-job`（数据+采集+评分+图鉴 Web）与 `gaj-reporter`（报告/内容线）已分仓库，`gaj/store` 内有 `repo / index / observatory / reportbundle` 分层。——旧 TODO 第 1 条。
- ✅ **行业观察 + 下钻查询**：`observatory_industry_list` + Web `/industry/{name}` 下钻已上线。——旧 TODO 第 2 条前半。

---

## 更新日志

- 2026-09-07：新增「数据分析可信度与统计维度」backlog（P0 口径一致挂入当前优先队列首位），完整方案见 [data-analysis-improvement-plan.md](docs/data-analysis-improvement-plan.md)。
- 2026-09-06：建档，取代粗糙 `TODO.md`；归并旧条目并标注实际完成状态；挂入基础工程 backlog 与 SQLite 改进 backlog。