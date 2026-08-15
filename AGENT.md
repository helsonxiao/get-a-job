# 坑位图鉴（GAJ）智能体操作接口 (AGENT.md)

## 文档分工

| 文档 | 定位 | 读者 |
|---|---|---|
| **AGENT.md**（本文） | 应用场景与容错机制：8 个使用案例、重试/降级/超时策略、注意事项 | 开发者、想了解系统行为细节的智能体 |
| **gaj-agent/SKILL.md** | 操作策略：首次配置、每日流程编排、错误处理决策树、超时预算 | 可安装到各智能体的技能包 |

命令的参数、返回字段、错误码、退出码等信息已内置于 CLI help text，
运行 `python3 -m gaj agent -h` 即可查看完整说明，本文不再重复。

简单说：**`-h` 管"有哪些参数和返回值"，AGENT.md 管"怎么用、出错怎么办"，
SKILL.md 管"智能体的操作策略和决策树"**。

## 前置条件

1. Chrome 以 CDP 调试模式运行：`python3 -m gaj setup-chrome`（端口 9222）。
2. 该 Chrome 里已登录 **zhipin.com**（采集需要）和 **chat.deepseek.com** 等
   要用的网页版大模型（AI 分析需要）。
3. `data/profile.md`（个人画像）与 `data/resumes/master.md`（主简历）已就绪，
   AI 打分会融入这两份材料。

## 应用场景与案例

### 场景一：每日职位报告（定时任务）

```bash
# 1. 健康检查
python3 -m gaj agent status

# 2. 执行每日编排（采集 + AI 分析 + 摘要）
python3 -m gaj agent daily --analyze-limit 3

# 3. 对当日高分岗位所在公司追加尽调
python3 -m gaj agent analyze --company <brand_id>
```

`daily` 始终产出 `digest_markdown`，`warnings` 不阻断流程，局部失败不需要
整体重跑。完整操作策略（健康检查、错误处理、超时预算）见
`gaj-agent/SKILL.md`「每日任务标准流程」。

### 场景二：查特定职位并做 AI 分析

```bash
# 1. 搜索职位
python3 -m gaj agent jobs --search "前端" --city 苏州

# 2. 看某个职位全量详情
python3 -m gaj agent job <encryptJobId>

# 3. 对该职位做 AI 深度分析
python3 -m gaj agent analyze --job <encryptJobId> --deep
```

### 场景三：评价某家公司

```bash
# 1. 搜公司名拿 brand_id
python3 -m gaj agent jobs --search "某公司"

# 2. 生成公司尽调词条（业务分析、技术栈、值不值得去、面试策略）
python3 -m gaj agent analyze --company <brand_id>
```

### 场景四：批量补打分（backlog 队列）

```bash
# 1. 先看候选名单（不调用大模型）
python3 -m gaj agent analyze --auto --dry-run

# 2. 确认后真打
python3 -m gaj agent analyze --auto --limit 5
```

### 场景五：首次采集

第一次使用系统，需要先完成环境准备并采集第一批职位：

```bash
# 1. 启动 Chrome CDP 调试模式
python3 -m gaj setup-chrome

# 2. 在弹出的 Chrome 窗口里登录 zhipin.com
#    （登录后在地址栏能看到登录态即可，不需要手动操作）

# 3. 去 BOSS 直聘筛选页（选好城市/关键词/薪资范围），
#    复制浏览器地址栏的 URL，用它来发起首次采集
python3 -m gaj agent crawl --url "https://www.zhipin.com/web/geek/job?query=前端&city=苏州"

# 4. 采集完成后批量打规则分
python3 -m gaj agent status   # 确认数据已入库
python3 -m gaj agent analyze --auto --limit 5  # 先打 5 个试试

# 5. 之后想看 Web 图鉴
python3 -m gaj web   # 浏览器访问 http://127.0.0.1:8765
```

首次采集的 URL 会被系统记住，后续 `crawl` / `daily` 不再需要 `--url`。
换城市或换关键词时，重新带 `--url` 即可覆盖。

### 场景六：过期重评（画像 / 规则变了）

用户修改了画像权重或硬性规则后，之前的打分会标记为"上下文已变"，
可以通过 rescore 模式批量重打：

```bash
# 1. 先看有多少过期待重评的
python3 -m gaj agent analyze --auto --pool rescore --dry-run

# 2. 确认后批量重打（只打过期的，不碰未过期的）
python3 -m gaj agent analyze --auto --pool rescore --limit 10
```

`--pool all`（默认）会同时补历史未打分 + 重打过期的，一步到位。
岗位级打分有 90 天保鲜期，超过 90 天也会自动进入过期队列。

### 场景七：Backlog 巡检

不确定有多少职位待打分、有多少已过期时，用 `status` + `dry-run` 摸底：

```bash
# 1. 看系统概况：backlog 里有几个未打分 / 几个已过期
python3 -m gaj agent status
# → data.backlog: {unscored: 12, stale_total: 3, ...}

# 2. 看具体是哪些职位
python3 -m gaj agent analyze --auto --dry-run
# → data.candidates[] 列出全部候选 job_id + 原因(backfill/stale)

# 3. 决定打多少，一次打完
python3 -m gaj agent analyze --auto --limit 20
```

### 场景八：Web 图鉴浏览

命令行适合自动化，想可视化浏览 / 对比 / 配置时用 Web 图鉴：

```bash
# 启动 Web 图鉴（前后端热重载，改代码不用重启）
python3 -m gaj web --port 8765

# 浏览器打开 http://127.0.0.1:8765
# - 职位列表 Tab：筛选 / 排序 / 忽略 / 查看详情
# - 公司图鉴 Tab：卡片墙 / 象限气泡图 / 并排雷达对比 / 公司详情抽屉
# - 配置 Tab：编辑画像 / 权重预设 / 规则阈值 / 硬性底线
```

Web 图鉴纯前端 + 本地后端，不消耗任何 AI 词元。
在公司图鉴 Tab 里可以手动触发公司级 AI 尽调（走网页版大模型）。

## 容错与超时保障

所有命令都有最外层异常兜底：**任何情况下都会输出 JSON 信封并以退出码结束，
不会静默挂起**。调用方（智能体）可以放心按信封决策。

### 超时上界（最坏情况）

| 命令 | 上界来源 | 量级 |
|---|---|---|
| `status` / `jobs` / `job` | 纯本地读索引/文件 | 秒级 |
| `crawl` | 会话上限 60 个新职位 + `--max-pages`（默认 10）+ 连续重复页提前结束；单次 CDP 通信超时 30s，列表 API 每次重试 3 次 | 通常几分钟，最坏约 30 分钟 |
| `analyze` / `daily` 的 AI 部分 | 每个职位生成超时 300s（超时即返回，不阻塞）；daily 默认只分析 3 个 | 每职位 ≤ 5-6 分钟 |

进程被外部强制终止是安全的：数据逐个职位增量落盘，重跑不会重复抓取
（已抓的自动跳过）。调用方的超时预算策略见 `gaj-agent/SKILL.md`。

### 内置重试

- 列表页 API：失败自动重试 3 次（退避递增），仍失败则终止本次翻页并给出
  `crawl_failed`。
- 大模型发送：按钮点击失败自动改 Enter 键，最多 3 轮；30 秒仍无任何回复时
  看门狗自动切前台并补发一次。
- AI 解析失败：原始回复仍会落盘（`ai_<provider>_raw_*.json`），可事后排查，
  该职位记为失败，不影响其他职位。

### 退出机制

- 采集：三种提前结束——`covered`（连续 3 页全重复，职位已覆盖）、
  `session_limit`（新职位数达到会话上限 60）、API 连续失败；
  原因在 `crawl_stats.early_stop_reason` 里可见。
- AI：生成超时即止损返回；单个职位失败不中断批量流程。
- `daily`：任何阶段失败都降级为 `warnings` 继续往下走，
  **始终产出 `digest_markdown`**，不让一次局部失败浪费整个编排。

### 采集覆盖策略

连续 3 页全重复时触发 `covered` 提前结束，这能保护列表 API 不被过度调用。
但存在一个已知局限：如果前几页都是已抓过的旧职位、后面的页面才有新职位，
爬虫会在第 3 页就停住，后面的新职位当天拿不到。

**续翻机制**（已实现）：系统记住上次因 `covered` 停止时的页码
（`last_dup_page`），下次采集时如果前几页又全重复，会跳到该页码续翻几页。
如果续翻的页面有新职位，恢复正常翻页；如果续翻也全重复，才真正停止并
更新 `last_dup_page`（下次从更后面续翻）。这样既优先采最新职位（前几页），
又不会让后面的旧职位一直采不到，多次运行逐步覆盖全部页面。

你也可以调大 `--max-pages` 或换筛选条件更窄的 URL 减少重复率。

## 注意事项

- AI 分析依赖**可见的** Chrome（网页版大模型需要登录态，无头模式不行）。
- 分析期间会短暂抢占浏览器焦点（把大模型标签页切到前台），完成后自动切回
  你原来在看的标签页。若想完全不抢焦点，可把 `gaj/config.py` 里
  `AIConfig.tab_mode` 改为 `"background"`（代价是后台节流时靠看门狗救援，
  响应可能更慢）。
- 采集节奏模拟人工浏览，不要为提速改动节奏逻辑或并发多开 crawl。
- 数据都在 `data/` 下（已被 gitignore），`data/crawl_state.json` 记录
  采集覆盖率状态，删除无害。
- 选择器可能随大模型网站改版失效；`analyze` 连续失败且报"输入框注入失败"
  之类错误时，提示用户检查 `gaj/browser/llm_driver_deepseek.py` 的选择器。
