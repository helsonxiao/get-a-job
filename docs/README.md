# docs/ —— 工程/设计文档唯一家

本目录存放 **get-a-job 项目的工程与设计文档**。`docs/` 不是公网站点发布面，不含 `index.html` / `assets/`。

- **公网站点源码**在 [`landing/`](../landing/index.html)，由 GitHub Actions 自动发布到 `gh-pages` 分支，**agent 勿往 landing 或本目录塞站点/图片**。
- 一次性过程产物（审计 / 交接 / 批次说明）→ `archive/YYYY-MM/`，文件名带日期前缀，只增不改。
- 命名约定：长期文档可直放根或按类型，建议带 `YYYY-MM-DD-<类型>-<主题>.md` 前缀。

## 目录约定

| 位置 | 类型 |
|---|---|
| `./`（根） | 当前活跃的工程方案 / 设计说明（如 `career-path-plan.md`、`report-lite-design.md`） |
| `adr/` | 架构决策（`adr-00x-*.md`，编号复用后不复用） |
| `proposals/` | 待决策 / 待排期的方案 |
| `archive/YYYY-MM/` | 一次性过程产物，按月度分桶 |

## 写给智能体

- 写文档一律进 `docs/`（含 `adr/` / `proposals/` / `archive/`）。
- **不要**写进 `landing/`、仓库根、或 `.git` 外散落目录。
- `.trae/specs/` 是 Trae 在制工作区，仅放进行中的 spec，完成后归 `docs/`。