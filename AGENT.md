# GAJ 智能体操作接口 (AGENT.md)

本文档面向 **AI 智能体**（以及想让智能体自动操作本系统的人）。
所有机器接口统一走：

```bash
cd /Users/helsonxiao/Codes/get-a-job
python3 -m gaj agent <command> [options]
```

## 1. 协议约定

- **stdout 只输出一个 JSON 对象**（信封），日志全部走 stderr 和 `logs/`。
  智能体可以直接 `json.loads(stdout)`。
- **退出码**：`0` 成功，`1` 执行失败，`2` 参数错误。
- 信封格式：

```json
{"ok": true,  "command": "status", "version": "1.0.0", "data": { ... }}
```

```json
{"ok": false, "command": "crawl",  "version": "1.0.0",
 "error": {"code": "chrome_not_ready", "message": "Chrome CDP 未运行..."},
 "data": { ... }}
```

### 错误码一览

| code | 含义 | 建议动作 |
|---|---|---|
| `usage` | 参数错误 | 检查参数，不要原样重试 |
| `chrome_not_ready` | Chrome CDP 未运行 | 执行 `python3 -m gaj setup-chrome` 后重试；仍失败则通知用户 |
| `not_logged_in` | 未登录 zhipin.com | 通知用户在 CDP Chrome 窗口里手动登录 |
| `no_crawl_url` | 没有可用的列表页 URL | 首次需要用户提供 BOSS 筛选页 URL |
| `job_not_found` | 职位 ID 不存在 | 用 `jobs` 重新查询正确 ID |
| `crawl_failed` | 采集失败 | 看 `data` 里的详情；可能是反爬拦截，建议隔几小时再试 |
| `ai_failed` / `timeout` | AI 分析失败 | 可重试 1 次；连续失败换 `--provider` 或通知用户 |
| `index_error` / `internal` | 内部错误 | 通知用户，附 message |

## 2. 前置条件

1. Chrome 以 CDP 调试模式运行：`python3 -m gaj setup-chrome`（端口 9222）。
2. 该 Chrome 里已登录 **zhipin.com**（采集需要）和 **chat.deepseek.com** 等
   要用的网页版大模型（AI 分析需要）。
3. `data/profile.md`（个人画像）与 `data/resumes/master.md`（主简历）已就绪，
   AI 打分会融入这两份材料。

## 3. 命令参考

### 3.1 `status` — 健康检查与数据概况

```bash
python3 -m gaj agent status
```

返回：`chrome_cdp_ready` / `boss_logged_in` / `providers` / 职位统计
（total、rule_scored、ai_scored、**ai_pending** 待 AI 分析数、favorites）/
`last_crawl`（上次采集 URL、时间、覆盖率、是否提前结束）。

**任何自动化流程的第一步都应该是它**，根据返回决定后续动作。

### 3.2 `jobs` — 查询职位列表

```bash
python3 -m gaj agent jobs [--search 关键词] [--city 苏州,无锡] \
    [--status PASS,REVIEW] [--scored all|none|rule_only|ai] \
    [--min-salary 20] [--online] [--favorite] [--include-ignored] \
    [--new-within-hours 24] [--sort best_total] [--asc] \
    [--limit 20] [--offset 0]
```

返回 `total`（筛选后总数）+ `jobs` 数组。每项含 `job_id`、`title`、
`company_name`、`salary_raw`、`rule_status`、`rule_total`、`latest_ai_total`、
`best_total`（人工调分 > AI > 规则的最优分）、`score`（=best_total）、`url` 等。

### 3.3 `job <ID>` — 单个职位全量详情

```bash
python3 -m gaj agent job <encryptJobId>
```

返回：`job`（归一化职位字段）、`jd_markdown`（清洗后的 JD 全文，
**分析/改写简历时用它**）、`rule_score`（规则打分及触发的规则）、
`ai_scores`（历史 AI 打分，倒序）、`company`（公司资料）。

### 3.4 `analyze` — AI 分析打分

```bash
# 单个职位
python3 -m gaj agent analyze --job <ID> [--provider deepseek] [--deep]

# 自动挑选规则引擎标记"需要 AI 介入"的职位批量分析
python3 -m gaj agent analyze --auto [--limit 5] [--provider deepseek]
```

调用网页版大模型（默认 deepseek，可选 doubao/tongyi/kimi）。
返回每个职位的 `total_score`（0-10）、`status`、`recommendation`、
`dimension_scores`、`deep_analysis_report`。

> 注意：每次分析耗时约 1-5 分钟（网页版生成），批量时自动加提问间隔。
> 分析期间 DeepSeek 标签页会被切到前台，完成后自动切回原来的标签页。

### 3.5 `crawl` — 增量采集

```bash
python3 -m gaj agent crawl [--url <BOSS列表页URL>] [--max-pages N] \
    [--no-company] [--no-score]
```

- `--url` 缺省时复用上次采集记住的 URL。
- 已抓过的职位自动跳过（不打开详情页）。
- **自适应降速**：连续整页都是重复职位时，翻页延迟逐次翻倍（上限 60s）；
  连续 3 页全重复即判定"该搜索条件已覆盖"，提前结束并记录覆盖率。
- 上次采集覆盖率 ≥ 90% 时，本次基础延迟自动 ×2。
- 返回 `crawl_stats`（pages/jobs_found/jobs_scraped/jobs_skipped_dup/
  coverage/early_stop_reason/elapsed_seconds）+ `migrated` + `scored`。

### 3.6 `daily` — 每日编排（定时任务首选）

```bash
python3 -m gaj agent daily [--url <URL>] [--max-pages 10] \
    [--analyze-limit 3] [--provider deepseek] [--deep] [--no-crawl]
```

一条命令完成完整日常流程：

1. 增量采集（自动降速/提前结束；未登录时跳过采集并给出 warning）；
2. 从本次新抓职位里挑候选（优先规则引擎标记需 AI 介入的，其次高分），
   不足则补历史待办，共 `--analyze-limit` 个；
3. 用同一个浏览器驱动逐个 AI 分析；
4. 生成 `digest_markdown` —— 可直接转发给用户的每日摘要。

返回：`crawl`（采集结果+`new_job_ids`）、`analyzed`（分析结果数组）、
`analyzed_success`、`warnings`、`digest_markdown`。

## 4. 典型智能体工作流

### 每日定时任务（推荐模板）

```text
1. 运行 `python3 -m gaj agent status`：
   - chrome_cdp_ready=false → 运行 setup-chrome，等 5 秒再查一次；
     仍 false 则通知用户"请启动 Chrome CDP"并结束。
   - boss_logged_in=false → 通知用户"请在 CDP Chrome 里登录 BOSS直聘"
     （仍可继续，daily 会跳过采集只分析存量）。
2. 运行 `python3 -m gaj agent daily --analyze-limit 3`。
3. 检查返回：
   - ok=true → 把 data.digest_markdown 发给用户；
     warnings 非空时一并说明。
   - ok=false → 按错误码表处理；crawl_failed 建议下午再重试一次。
4. 如果用户想看某个职位详情：用 jobs 搜索 → job <ID> 取 jd_markdown 和打分。
```

### 首次使用

需要用户先提供一次 BOSS直聘筛选页 URL（在浏览器里筛好城市/职位/薪资后
复制地址栏 URL），执行 `python3 -m gaj agent crawl --url "<URL>"`。
之后系统会记住该 URL，daily/crawl 均可省略 `--url`。

## 5. 注意事项

- AI 分析依赖**可见的** Chrome（网页版大模型需要登录态，无头模式不行）。
- 分析期间会短暂抢占浏览器焦点（把大模型标签页切到前台），完成后自动切回
  你原来在看的标签页。若想完全不抢焦点，可把 `gaj/config.py` 里
  `AIConfig.tab_mode` 改为 `"background"`（代价是后台节流时靠看门狗救援，
  响应可能更慢）。
- 采集限速是刻意保守的（防封号优先级 > 速度），不要为提速绕过降速逻辑。
- 数据都在 `data/` 下（已被 gitignore），`data/crawl_state.json` 记录
  采集覆盖率状态，删除无害。
- 选择器可能随大模型网站改版失效；`analyze` 连续失败且报"输入框注入失败"
  之类错误时，提示用户检查 `gaj/browser/llm_driver_deepseek.py` 的选择器。
