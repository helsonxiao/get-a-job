---
name: gaj-agent
description: 通过 `python3 -m gaj agent` JSON CLI 操作 GAJ 个人猎头系统（BOSS直聘职位采集 + 规则/AI 打分），执行每日采集分析、职位查询、AI 打分并生成摘要。当用户要求跑每日职位报告、查询/分析职位、采集 BOSS直聘职位，或定时任务需要调用 GAJ 系统时使用。
version: 1.1.0
---

# GAJ 智能体操作技能

GAJ 是本机上的个人猎头系统：CDP 驱动 Chrome 采集 BOSS直聘职位，规则引擎 +
网页版大模型打分。所有操作统一走：

```bash
cd /Users/helsonxiao/Codes/get-a-job && python3 -m gaj agent <command> [options]
```

stdout 只输出一个 JSON 信封：`ok=true` 时数据在 `data`，`ok=false` 时看
`error.code` / `error.message`。退出码：0 成功 / 1 失败 / 2 参数错误。

**完整命令参数、错误码清单、系统内部容错机制见项目内
[AGENT.md](/Users/helsonxiao/Codes/get-a-job/AGENT.md)，需要细节时运行时读它。
本技能只定义操作策略，不重复这些内容。**

## 命令一览

| 命令 | 用途 |
|---|---|
| `status` | 健康检查 + 数据概况（**任何流程的第一步**） |
| `jobs` | 查询职位列表（筛选/排序/分页） |
| `job <ID>` | 单职位详情（JD 全文 + 规则分 + AI 分） |
| `analyze` | AI 打分（`--job` 单个 / `--auto` 批量） |
| `crawl` | 增量采集（自动降速、覆盖提前结束） |
| `daily` | 每日编排：采集 → 挑候选 → AI 分析 → 摘要 |

## 每日任务标准流程

1. **健康检查** `status`：
   - `chrome_cdp_ready=false` → 运行 `python3 -m gaj setup-chrome`，等 5 秒
     再查一次；仍 false 通知用户"请启动 Chrome CDP"并结束。
     （注意：必须是可见的 Chrome 窗口，无头模式不行）
   - `boss_logged_in=false` → 通知用户在 CDP Chrome 窗口登录 BOSS直聘
     （可继续，daily 会跳过采集只分析存量职位）。
2. **执行编排** `daily --analyze-limit 3`。
3. **处理返回**：
   - `ok=true` → 把 `data.digest_markdown` 发给用户；`warnings` 非空时一并说明。
   - `ok=false` → 按下方错误处理策略。
4. 用户想看某职位：`jobs` 搜索拿 `job_id` → `job <ID>` 取 `jd_markdown` 和打分。

## 错误处理策略

按 `error.code` 类别决策（各错误码含义见 AGENT.md）：

- **环境类**（`chrome_not_ready` / `not_logged_in`）：尝试修复一次
  （setup-chrome / 提示登录）后重试；仍失败 → 通知用户，停止。
- **输入类**（`usage` / `no_crawl_url` / `job_not_found`）：不要原样重试。
  修正参数、向用户要列表页 URL、用 `jobs` 重查正确 ID。
- **任务类**（`crawl_failed` / `ai_failed` / `timeout`）：重试 1 次；
  `ai_failed` 连续失败可换 `--provider doubao/tongyi/kimi` 再试；
  `crawl_failed` 多为反爬拦截，隔几小时再试。
- **内部类**（`internal` / `index_error`）：通知用户，附 `error.message`。

**同一错误码连续出现两次 = 需要人工介入，通知用户而不是继续重试。**

## 调用方超时预算

系统内部已有全部重试与退出保护，命令不会静默挂起；调用方只需包进程级超时：

- `status` / `jobs` / `job`：预算 30 秒足够。
- `crawl`：通常几分钟，最坏约 30 分钟。
- `daily`：建议预算 **45 分钟**。

超时 kill 后重试是安全的（数据增量落盘，重跑自动跳过已抓职位）。

## 操作注意

- 分析期间大模型标签页会短暂切到前台、完成后自动切回，属正常行为；
  若用户正在高频使用浏览器，避免在高峰时段排 `daily`。
- 首次采集需要用户提供 BOSS 筛选页 URL（报 `no_crawl_url` 时索取），
  之后系统记住，可省略 `--url`。
- `daily` 始终产出 `digest_markdown`，`warnings` 不阻断流程，局部失败
  不要整体重跑。
- `ai_failed` 反复出现且 message 含"注入失败/选择器" → 大模型网站改版，
  通知用户检查 `gaj/browser/llm_driver_deepseek.py`，不要无限重试。
- 采集限速是防封号刻意设计，不要为提速绕过或并发多开 crawl。

## 验证

每次调用确认：进程在预算内退出、stdout 可 `json.loads`、信封含 `ok` 字段：

```bash
cd /Users/helsonxiao/Codes/get-a-job && python3 -m gaj agent status \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['ok'], d['data']['chrome_cdp_ready'])"
```
