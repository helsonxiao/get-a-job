---
name: gaj-agent
description: 通过 `python3 -m gaj agent` JSON CLI 操作坑位图鉴（GAJ）个人猎头系统（BOSS直聘职位采集 + 规则/AI 打分），执行每日采集分析、职位查询、AI 打分（岗位级与公司级评价）并生成摘要。当用户要求跑每日职位报告、查询/分析职位、评价某家公司、采集 BOSS直聘职位，或定时任务需要调用坑位图鉴系统时使用。
version: 0.1.0
---

# 坑位图鉴智能体操作技能

坑位图鉴（GAJ）是用户本机上的个人猎头系统：CDP 驱动 Chrome 采集 BOSS直聘职位，规则引擎 +
网页版大模型打分。所有操作统一走 `python3 -m gaj agent` JSON CLI。

## 首次使用配置（第一次调用必做）

本技能不含任何写死的本机路径，仓库位置从用户级配置
`~/.gaj-agent/config.json` 读取：

```json
{ "repo_path": "/绝对路径/get-a-job", "python": "python3" }
```

每次执行命令前：

1. 读取 `~/.gaj-agent/config.json`。
2. **文件不存在时**：向用户索取 get-a-job 仓库在本机的绝对路径
   （根目录下应有 `gaj/` 包和 `AGENT.md`），校验
   `<repo_path>/gaj/__main__.py` 存在后写入配置文件：

   ```bash
   mkdir -p ~/.gaj-agent && cat > ~/.gaj-agent/config.json <<'EOF'
   { "repo_path": "<用户提供的绝对路径>", "python": "python3" }
   EOF
   ```

   若用户使用虚拟环境或特定解释器，一并询问并填入 `python`
   （默认 `python3`）。路径校验不通过时继续向用户确认，不要猜测。
3. 后续所有命令以配置里的 `repo_path` 为工作目录、`python` 为解释器执行。

配置错误（如仓库被移动）的表现是命令报模块找不到或路径不存在：
重新向用户索取路径并更新配置文件即可。

## 调用方式

下文中 `<repo_path>` / `<python>` 均取自 `~/.gaj-agent/config.json`：

```bash
cd <repo_path> && <python> -m gaj agent <command> [options]
```

stdout 只输出一个 JSON 信封：`ok=true` 时数据在 `data`，`ok=false` 时看
`error.code` / `error.message`。退出码：0 成功 / 1 失败 / 2 参数错误。

**可用命令及参数请运行 `<python> -m gaj agent -h` 查看，以 CLI 实际输出为准。**
本技能不重复罗列参数，只定义操作策略与注意事项。

## AI 打分与去重

系统内置 **backlog 打分队列**，自带去重与冷却保护，无需手动筛选未打分职位：

- `analyze --auto`：走 backlog 队列，默认 `--pool all`（补历史未打分 +
  重打已过时的分），不会重复打已打过分且未过时的岗位。
  - `--pool backfill`：只补从未 AI 打分的岗位
  - `--pool rescore`：只重打已过时的分（画像/规则变了或超保鲜期）
  - `--dry-run`：只看候选名单不调用大模型，**先 dry-run 再决定要不要真打**
- `daily` 内部已集成 backlog 调度，新岗位不足时预算自动流向补历史欠分。

手动查未打分职位仍可用 `jobs --scored no_ai`，但 `--auto` 本身已覆盖此逻辑。

## 公司尽调（图鉴词条）

`analyze --company <brand_id>` 对公司整体做 AI 尽调评价，输出图鉴词条：
业务分析、技术栈画像、招聘紧迫度、值不值得去、亮点/风险、面试策略、
AI 独立评分（`company_score_ai` 0-10）。结果 append-only 落盘，可反复跑
覆盖更新。

**保鲜期缓存**：默认 180 天内的评价且上下文未变会跳过大模型调用
（返回 `cached=true`），公司信息变化慢，大半年不重评也没问题。
`--force` 强制重评，`--max-age-days` 覆盖保鲜期。

**获取 brand_id**：`jobs --search <公司名>` 结果的 `company_id` 字段，
或 `job <ID>` 详情里的 `job.company_id`。

需要 Chrome CDP 就绪（同样走网页版大模型）。公司名下至少要有一个岗位，
否则报 `usage`。

返回 `data.mode = "company"`，含全部词条字段；把摘要讲给用户听即可，
完整词条用户可在 Web 图鉴公司抽屉里查看。

**自动化场景**：在 `daily` 或 `analyze --auto` 跑完岗位打分后，可对用户
关注的公司（如高分岗位所在公司、收藏的公司）追加 `analyze --company`
生成尽调词条，一并在摘要中呈现，帮助用户从公司维度做决策。

## 每日任务标准流程

1. **健康检查** `status`：
   - `chrome_cdp_ready=false` → 运行 `<python> -m gaj setup-chrome`，等 5 秒
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
  `ai_failed` 连续失败可换 `--provider doubao/tongyi/kimi` 再试
  （注意：目前仅 deepseek 支持较成熟，其它 provider 为实验性，
  失败率可能更高，换用后仍失败就停止并通知用户）；
  `crawl_failed` 多为服务端临时拦截，隔几小时再试。
- **内部类**（`internal` / `index_error`）：通知用户，附 `error.message`。

**同一错误码连续出现两次 = 需要人工介入，通知用户而不是继续重试。**

## 调用方超时预算

系统内部已有全部重试与退出保护，命令不会静默挂起；调用方只需包进程级超时：

- `status` / `jobs` / `job`：预算 30 秒足够。
- `crawl`：通常几分钟，最坏约 30 分钟。
- `daily`：建议预算 **45 分钟**。

超时 kill 后重试是安全的（数据增量落盘，重跑自动跳过已抓职位）。

## 操作注意

- **控制采集量**：`crawl` 和 `daily` 默认 `--max-pages 10`，没必要一次性
  把所有页面爬完。连续 3 页全重复会自动停止（`early_stop_reason=covered`），
  但系统有续翻机制：记住上次停止的页码（`last_dup_page`），下次前几页
  全重复时跳到那里再试几页，有新职位就继续，全重复才真停。多次运行
  逐步覆盖全部页面，最新职位优先（前几页），旧职位也不会一直采不到。
- 分析期间大模型标签页会短暂切到前台、完成后自动切回，属正常行为；
  若用户正在高频使用浏览器，避免在高峰时段排 `daily`。
- 首次采集需要用户提供 BOSS 筛选页 URL（报 `no_crawl_url` 时索取），
  之后系统记住，可省略 `--url`。
- `daily` 始终产出 `digest_markdown`，`warnings` 不阻断流程，局部失败
  不要整体重跑。
- `ai_failed` 反复出现且 message 含"注入失败/选择器" → 大模型网站改版，
  通知用户检查 `<repo_path>/gaj/browser/llm_driver_deepseek.py`，不要无限重试。
- 采集节奏模拟人工浏览，不要为提速改动节奏逻辑或并发多开 crawl。

## 验证

每次调用确认：进程在预算内退出、stdout 可 `json.loads`、信封含 `ok` 字段：

```bash
cd <repo_path> && <python> -m gaj agent status \
  | <python> -c "import json,sys; d=json.load(sys.stdin); print(d['ok'], d['data']['chrome_cdp_ready'])"
```
