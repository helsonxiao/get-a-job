# GAJ 全栈实现计划：从爬虫到个人猎头系统

## Summary

把现有的 `boss_scraper/`（已能跑通的 CDP 爬虫）与 `gaj/`（数据层骨架）整合成一个完整的"个人猎头"系统：**爬取 → 规则打分 → AI 打分（CDP 驱动网页版大模型）→ Web 可视化 → 针对性简历生成**。

五大模块全做，但分主次：
- **核心三块（完整可用）**：规则打分引擎、AI 网页版大模型驱动、Web 可视化
- **最小可用**：简历生成、老爬虫迁移到 gaj 数据层

所有实现严格复用已有规格文档（`references/scoring_rules.md` v3、`references/ai_fallback.md`）和数据层（`gaj/core/*`、`gaj/store/*`、`gaj/config.py`），不改动已落地的领域模型与存储约定。

---

## Current State Analysis

### 已就绪的地基（不动）
| 模块 | 路径 | 状态 |
|---|---|---|
| 领域模型 | `gaj/core/models.py` | `Job`/`Company` dataclass + `build()` 三方合并 + `_assess_quality()` |
| 归一化 | `gaj/core/normalize.py` | 薪资/经验/学历/规模/工时/城市/GPS/公司性质 全套解析 |
| 信号推断 | `gaj/core/signals.py` | 加班/工作模式/外包/出差/团队规模/技术深度/黑名单 6 路 |
| 文本去噪 | `gaj/core/denoise.py` | BOSS 水印 + 康熙部首替身字清洗 |
| 画像解析 | `gaj/core/profile.py` | `Profile` + `summary_for_ai()` + `normalized_weights` |
| 文件仓储 | `gaj/store/repo.py` | 原子写入 + 职位/公司/打分/简历/会话 全套 CRUD |
| SQLite 索引 | `gaj/store/index.py` | `query_jobs`/`facets`/`reindex` + FTS5 全文搜索，已为 Web 准备 |
| 配置 | `gaj/config.py` | 路径/采集/打分/AI 配置集中定义，`AIConfig` 已有 driver 参数 |
| 日志 | `gaj/logging_setup.py` | `SINK` 已预埋，Web SSE 可直接挂回调 |
| CDP 爬虫 | `boss_scraper/*` | 完整可用：CDPSession + XHR 翻页 + JD/公司页解析 + 人类行为模拟 |
| 规格文档 | `references/*.md` | scoring_rules.md v3 + ai_fallback.md + jd_fields.md |

### 五个空壳（本次填充）
| 空壳 | 路径 | 本次目标 |
|---|---|---|
| `gaj/scoring/` | 不存在 | 规则打分引擎（核心，完整） |
| `gaj/ai/` | 仅空 `__init__.py` | AI provider + runner（核心，完整） |
| `gaj/browser/` | 空 `__init__.py` | 网页版大模型 CDP 驱动（核心，完整） |
| `gaj/web/` | 空 `__init__.py` | FastAPI + 单页前端（核心，完整） |
| `gaj/resume/` | 空 `__init__.py` | 简历改写（最小可用） |
| `gaj/scraper/` | 空 `__init__.py` | 老爬虫适配层（最小可用） |

### 关键约束
- `gaj/` 当前是 **untracked**（`git status` 显示 `?? gaj/`），worktree 创建前需先提交。
- `references/profile.md` 在 `.gitignore` 中（个人隐私），不会被 worktree 带走，需用户在新工作区重新填写或从主目录拷贝。
- `data/` 目录在 `.gitignore` 中（运行时数据），同理。
- `requirements.txt` 当前只有 `requests` + `websocket-client`，需新增 web 依赖。

---

## Worktree 策略（执行第一步）

由于其他 agent 也在修改主目录，所有实现必须在独立 worktree 进行。

```bash
cd /Users/helsonxiao/Codes/get-a-job
# 1. 先把 gaj/ 骨架提交到 main（wip commit，仅 stage gaj/）
git add gaj/
git commit -m "wip: scaffold gaj data layer before full implementation"

# 2. 创建 worktree（新分支基于上面的 commit）
git worktree add ../get-a-job-gaj -b feat/gaj-full-stack

# 3. 后续所有操作在 /Users/helsonxiao/Codes/get-a-job-gaj 下进行
cd /Users/helsonxiao/Codes/get-a-job-gaj

# 4. 如果主目录已有 data/ 或 references/profile.md，拷贝过来
cp -r ../get-a-job/data . 2>/dev/null || true
cp ../get-a-job/references/profile.md references/ 2>/dev/null || true
```

**工作目录**：`/Users/helsonxiao/Codes/get-a-job-gaj`
**分支**：`feat/gaj-full-stack`

---

## Proposed Changes

### 依赖更新

`requirements.txt` 追加：
```
fastapi>=0.110.0
uvicorn[standard]>=0.27.0
```
不引入 jinja2（前端用纯静态 HTML + Alpine.js CDN，零构建）。不引入 pyyaml（prompt 用 Python 字符串模板）。

---

### 模块 1：规则打分引擎 `gaj/scoring/`（核心，完整）

**职责**：把 `references/scoring_rules.md` v3 翻译成 Python 函数，输入 `Job + Profile`，输出符合规格的 JSON，写入 `data/jobs/<id>/scores/rule.json`。

**文件**：
- `gaj/scoring/__init__.py` — 导出 `score_job`、`score_all`
- `gaj/scoring/engine.py` — 打分核心
- `gaj/scoring/cli.py` — 命令行 `python -m gaj.scoring.cli`

**`engine.py` 关键函数签名**：
```python
def hard_filter(job: Job, profile: Profile) -> tuple[str | None, str | None]:
    """返回 (reject_rule_id, reject_reason)，通过则 (None, None)。H-01~H-07。"""

def score_finance(job: Job, profile: Profile) -> tuple[float, list[str]]:
    """0-10 分 + 评分依据列表。"""

def score_growth(job: Job, profile: Profile) -> tuple[float, list[str]]:
def score_resource(job: Job, profile: Profile) -> tuple[float, list[str]]:
def score_wlb(job: Job, profile: Profile) -> tuple[float, list[str]]:

def check_ai_triggers(
    dims: dict[str, float], total: float, job: Job, profile: Profile
) -> list[str]:
    """A-01~A-06，返回触发的规则编号列表。"""

def score_job(job: Job, profile: Profile, weights: dict | None = None) -> dict:
    """主入口。返回符合 scoring_rules.md 第四节的 JSON。
    包含 status/reject_reason/dimension_scores/total_score/
    ai_intervention_needed/triggered_ai_rules/raw_data/evidence。"""

def score_all(profile: Profile | None = None, force: bool = False) -> dict:
    """批量给所有未打分（或 force=True）的职位打分，写 rule.json，返回统计。"""
```

**实现要点**：
- 权重：默认走 `profile.normalized_weights`（30/30/30/10），`ScoringConfig.use_profile_weights=False` 时走 40/30/20/10。
- H-01 城市：用 `profile.all_acceptable_cities()`。
- H-04 薪资：`job.salary_min`（万/年）< `profile.hard_min_salary_10k`。
- H-06 加班：读 `job.signals.overtime.value`（996/大小周 → 淘汰）。
- H-07 黑名单：`job.signals.blacklist_hits`。
- 维度评分严格按表格的"分值规则"逐条 `if/else`，每条都记 evidence。
- A-03 语义关联：规则阶段无法做语义，先用 `profile.industry_tags` 与 `company.industry` 的子串/同义词粗判，命中则触发 A-03 让 AI 精修。
- A-05 信息缺失：`job.quality.missing` 含 `jd` 或 `job.jd.get('responsibility')` 为空。
- 输出 JSON 额外加 `evidence`（每条得分依据）和 `created_at`、`rules_version: "v3"`，方便 UI 展示。
- 调用 `repo.save_rule_score(job_id, payload)` 落盘。

**CLI**：
```bash
python -m gaj.scoring.cli --all              # 批量打分
python -m gaj.scoring.cli --job <job_id>     # 单个打分
python -m gaj.scoring.cli --all --force      # 重新打分
```

---

### 模块 2：网页版大模型 CDP 驱动 `gaj/browser/`（核心，完整）

**职责**：通过 CDP 驱动已登录的网页版大模型（DeepSeek/豆包/通义/Kimi），注入 prompt → 点发送 → 轮询 DOM 直到文本稳定 → 提取结果。

**文件**：
- `gaj/browser/__init__.py` — 导出 `get_driver`
- `gaj/browser/cdp.py` — 复用 `boss_scraper.cdp_session.CDPSession` 的薄封装
- `gaj/browser/llm_driver.py` — 通用网页版大模型驱动基类
- `gaj/browser/llm_driver_deepseek.py` — DeepSeek 适配
- `gaj/browser/llm_driver_doubao.py` — 豆包适配
- `gaj/browser/llm_driver_tongyi.py` — 通义千问适配
- `gaj/browser/llm_driver_kimi.py` — Kimi 适配

**`cdp.py`**：薄封装，直接 `from boss_scraper.cdp_session import CDPSession`，加一个 `ensure_chrome_running(port)` 工具函数（检查 `/json/version`，未启动则提示用户跑 `python jd_cdp_parser.py --setup-chrome`）。不重写 CDP 通信层。

**`llm_driver.py` 基类**：
```python
class LLMDriver:
    name: str = "base"
    chat_url: str = ""
    # 各 provider 子类覆盖这些选择器
    input_selector: str = ""        # 输入框
    send_selector: str = ""         # 发送按钮
    response_selector: str = ""     # 最新一条助手回复容器
    clear_selector: str = ""        # 新建对话按钮

    def __init__(self, session: CDPSession, config: AIConfig): ...

    def ensure_logged_in(self) -> bool:
        """导航到 chat_url，检查登录态（DOM 有输入框即视为已登录）。"""

    def new_conversation(self) -> None:
        """点击新建对话按钮（fresh_conversation=True 时调用）。"""

    def send_prompt(self, prompt: str) -> None:
        """注入 prompt 到输入框 → 等 pre_send_pause → 点发送。
        用 Runtime.evaluate 执行 JS:
          - 定位输入框，设值（React 受控组件需触发 input 事件）
          - 点击发送按钮"""

    def wait_for_completion(self) -> str:
        """轮询 response_selector 的 textContent:
          - 每 poll_interval 秒读一次
          - 连续 stable_rounds 次文本无变化视为完成
          - 超时 generation_timeout 抛错
        返回最终文本。"""

    def ask(self, prompt: str) -> str:
        """ensure_logged_in → (可选)new_conversation → send_prompt → wait_for_completion。"""
```

**Provider 适配**（每个子类只覆盖选择器和登录检测逻辑）：
- `deepseek`: `https://chat.deepseek.com/`，输入框 `textarea#chat-input`，发送 `div[role="button"].ds-icon-btn`（或按 Enter），回复 `.ds-message--bot .ds-markdown`
- `doubao`: `https://www.doubao.com/chat/`，输入 `textarea[data-testid="chat_input"]`，回复 `[data-testid="message_text_content"]`
- `tongyi`: `https://tongyi.aliyun.com/qianwen/`，输入 `textarea#baiduOrXunFeiInput`，回复 `.message-content`
- `kimi`: `https://kimi.moonshot.cn/chat/`，输入 `textarea.chat-input`，回复 `.markdown`

> **注意**：选择器会随网站更新失效，每个 driver 加 `__post_init__` 做选择器自检，失败时 log 警告并提示用户手动检查。选择器集中在类属性上，方便后续维护。

**注入 prompt 的 JS 模式**（处理 React 受控输入框）：
```javascript
const ta = document.querySelector('{input_selector}');
const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
  window.HTMLTextAreaElement.prototype, 'value').set;
nativeInputValueSetter.call(ta, {json_string});
ta.dispatchEvent(new Event('input', {{bubbles: true}}));
```

---

### 模块 3：AI 编排与 Prompt `gaj/ai/`（核心，完整）

**职责**：根据规则打分结果触发 AI 介入（A-01~A-06），组装 prompt，调用 driver，解析返回 JSON，落盘 `scores/ai_<provider>_<时间戳>.json`；以及简历改写。

**文件**：
- `gaj/ai/__init__.py` — 导出 `run_ai_score`、`run_resume_tailoring`
- `gaj/ai/providers/__init__.py` — provider 注册表
- `gaj/ai/providers/base.py` — `AIProvider` 抽象基类
- `gaj/ai/prompts.py` — prompt 模板
- `gaj/ai/runner.py` — 编排逻辑
- `gaj/ai/parser.py` — 从大模型返回文本解析 JSON
- `gaj/ai/cli.py` — 命令行

**`providers/base.py`**：
```python
class AIProvider:
    name: str
    driver_class: type[LLMDriver]

    def __init__(self, session: CDPSession, config: AIConfig): ...

    def score(self, job: Job, profile: Profile, rule_result: dict) -> dict:
        """根据 triggered_ai_rules 组装 prompt → driver.ask → 解析 JSON → 补全字段。"""

    def tailor_resume(self, job: Job, master: str, profile: Profile) -> str:
        """组装简历改写 prompt → driver.ask → 返回 markdown。"""
```

**`providers/__init__.py` 注册表**：
```python
PROVIDERS = {
    "deepseek": DeepSeekProvider,
    "doubao": DoubaoProvider,
    "tongyi": TongyiProvider,
    "kimi": KimiProvider,
}
def get_provider(name, session, config) -> AIProvider: ...
```

**`prompts.py`**（核心，认真设计）：
- `build_score_prompt(job, profile, rule_result) -> str`：
  - 系统提示：你是资深猎头，按 schema 返回 JSON
  - 注入：`profile.summary_for_ai()` + JD 全文 + 规则打分快照 + 触发的 A-XX 规则
  - 要求：只输出 JSON，不要 markdown 代码块
  - JSON schema 严格对齐 `ai_fallback.md` 的输出格式
- `build_resume_prompt(job, profile, master_resume) -> str`：
  - 系统提示：你是简历优化专家，针对目标岗位调整简历
  - 注入：JD 关键词 + 母版简历 + 画像技能
  - 要求：保留事实（公司/时间/数据）不变，调整摘要、技能顺序、项目描述措辞以对齐 JD；输出 markdown

**`parser.py`**：
- `extract_json(text) -> dict`：从大模型返回里抠 JSON
  - 先尝试整体 `json.loads`
  - 失败则找 ```json ... ``` 代码块
  - 再失败则找第一个 `{` 到最后一个 `}` 的子串
  - 仍失败返回 `{"_parse_error": "...", "_raw": text}`，不崩

**`runner.py`**：
```python
def run_ai_score(job_id: str, provider: str, *, force: bool = False) -> dict:
    """加载 job + profile + rule_score → 无 rule_score 则先跑规则打分
    → 检查 ai_intervention_needed（force=True 跳过检查）
    → get_provider → provider.score()
    → 补全 provider/model/created_at 等元数据
    → repo.save_ai_score() → 返回结果"""

def run_resume_tailoring(job_id: str, provider: str) -> Path:
    """加载 job + master_resume + profile → provider.tailor_resume()
    → repo.save_tailored_resume() → 返回路径"""
```

**CLI**：
```bash
python -m gaj.ai.cli --score --job <id> --provider deepseek
python -m gaj.ai.cli --score --job <id> --provider deepseek,doubao  # 多 provider 对比
python -m gaj.ai.cli --score --all --provider deepseek              # 批量（仅 ai_needed 的）
python -m gaj.ai.cli --resume --job <id> --provider deepseek
```

---

### 模块 4：Web 可视化 `gaj/web/`（核心，完整）

**职责**：FastAPI 后端 + 单页前端，复用 `index.py` 的查询接口，支持列表/筛选/排序/详情/触发打分/SSE 日志。

**文件**：
- `gaj/web/__init__.py`
- `gaj/web/app.py` — FastAPI 应用与路由
- `gaj/web/server.py` — uvicorn 启动入口
- `gaj/web/static/index.html` — 单页应用（Alpine.js CDN + 原生 CSS）
- `gaj/web/static/app.js` — 前端逻辑（可选，也可内联到 index.html）

**`app.py` 路由**：
```
GET  /                              → index.html
GET  /api/jobs                      → query_jobs 包装（支持 search/cities/statuses/scored/providers/salary_min/online_only/outsourcing/sort/desc/limit/offset）
GET  /api/facets                    → facets()
GET  /api/jobs/{job_id}             → {job, company, scores: {rule, ai_list}}
GET  /api/jobs/{job_id}/scores      → 全部打分记录
POST /api/jobs/{job_id}/score/rule  → 触发规则打分（后台线程）
POST /api/jobs/{job_id}/score/ai    → 触发 AI 打分（provider 必填，后台线程）
POST /api/jobs/{job_id}/resume      → 触发简历生成（provider 必填）
GET  /api/resumes?job_id=           → 简历列表
GET  /api/profile                   → 当前画像
POST /api/reindex                   → 重建索引
GET  /api/sse/logs                  → SSE 日志流（挂 logging_setup.SINK）
GET  /api/stats                     → 概览统计（总数/已打分/AI 数/各 provider 数）
```

**后台任务**：打分/简历生成是长任务（AI 调用可能 1-5 分钟），用 `asyncio.create_task` + 线程池跑，立即返回 `{task_id, status: "running"}`，前端通过 SSE 看进度。不引入 Celery/RQ（过度工程）。

**SSE 实现**：
```python
@app.get("/api/sse/logs")
async def sse_logs():
    import queue, asyncio
    q = queue.Queue()
    loop = asyncio.get_event_loop()
    def sink(payload): loop.call_soon_threadsafe(q.put_nowait, payload)
    SINK.add(sink)
    async def event_stream():
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(loop.run_in_executor(None, q.get, True), timeout=1.0)
                    yield f"data: {json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            SINK.remove(sink)
    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

**前端 `index.html`**（单页，Alpine.js）：
- **顶部**：统计栏（总数/已规则打分/已 AI 打分）+ 重建索引按钮 + 日志抽屉开关
- **左侧筛选面板**：搜索框 + 城市/行业/阶段/打分状态/provider 复选 + 薪资下限 + 在线/外包开关
- **主列表**：表格（职位/公司/城市/薪资/规则分/AI 分/provider/状态），点击行展开详情抽屉
- **详情抽屉**：JD 全文（markdown 渲染）+ 公司信息 + 规则打分明细（各维度 + evidence）+ AI 打分对比（多 provider 横向）+ 触发按钮（规则打分/AI 打分多选 provider/生成简历）
- **日志抽屉**：SSE 实时日志流，按 level 着色
- 样式：深色主题，无外部 CSS 框架（原生 CSS 200 行内联）
- Alpine.js 从 CDN 加载，零构建

**`server.py`**：
```python
import uvicorn
from .app import app
def main(): uvicorn.run(app, host="127.0.0.1", port=7788)
if __name__ == "__main__": main()
```

**CLI**：`python -m gaj.web.server` 或统一入口 `gaj web`

---

### 模块 5：简历生成 `gaj/resume/`（最小可用）

**职责**：调 AI 把 `master.md` 针对特定岗位改写，存到 `data/resumes/tailored/`。

**文件**：
- `gaj/resume/__init__.py` — 导出 `tailor_resume`
- `gaj/resume/tailor.py` — 薄封装，调 `gaj.ai.runner.run_resume_tailoring`
- `gaj/resume/cli.py` — `python -m gaj.resume.cli --job <id> --provider deepseek`

**实现**：简历生成的核心逻辑（prompt 组装、AI 调用、解析、存储）都在 `gaj/ai/` 里，`gaj/resume/` 只是一个面向用户的薄入口。这样避免逻辑分散。

---

### 模块 6：老爬虫适配层 `gaj/scraper/`（最小可用）

**职责**：复用 `boss_scraper/` 的爬取能力，但输出落到 `gaj` 的数据层（`Job.build` + `repo.save_job` + `Company.build` + `repo.save_company`），而不是老的 `jobs/001/` 格式。

**文件**：
- `gaj/scraper/__init__.py` — 导出 `crawl`
- `gaj/scraper/adapter.py` — 适配层
- `gaj/scraper/cli.py` — `python -m gaj.scraper.cli`

**`adapter.py` 策略**（不重写爬虫，最小改动）：
```python
def crawl(url: str, config: CrawlConfig | None = None) -> dict:
    """复用 boss_scraper.crawler.JobCrawler，但替换它的存储钩子。
    方案: 直接调用 boss_scraper 抓 raw（list_item + jd_dom + company_dom），
    然后用 gaj 的 models.build + repo.save 落盘。
    不改动 boss_scraper 源码，只在 gaj 这边包一层。"""
```

具体做法：`boss_scraper/crawler.py` 的 `crawl_from_url` 内部在抓到每个职位的 raw 数据后会调 `build_structured` + `save_to_jobs_dir`。我们不 monkey-patch 它，而是：
1. 调用 `boss_scraper.page_parser.scrape_jd_page` / `scrape_company_page` 拿 raw dom
2. 调用 `boss_scraper.network.fetch_job_list_with_retry` 拿 list API
3. 把 raw 喂给 `Job.build` + `Company.build` + `repo.save_job` + `repo.save_company`

翻页循环复用 `boss_scraper.network` 的 `fetch_job_list_with_retry`，但外层循环自己写（约 80 行），避免改 boss_scraper。

**CLI**：`python -m gaj.scraper.cli --crawl <URL>`（行为等同老 `jd_cdp_parser.py --crawl`，但存到 `data/jobs/<job_id>/`）

---

### 模块 7：统一 CLI `gaj/cli.py`

```bash
gaj crawl <URL>                    # 爬取
gaj score [--all | --job <id>]     # 规则打分
gaj ai-score --job <id> -p deepseek[,doubao]
gaj resume --job <id> -p deepseek
gaj web [--port 7788]              # 启动 Web
gaj reindex                        # 重建索引
gaj setup-chrome                   # 启动 CDP Chrome
```

用 `argparse` 子命令实现，不引入 click/typer（避免新依赖）。

---

## 实现顺序（按依赖关系）

1. **Worktree 创建 + 依赖更新**（10 分钟）
2. **规则打分引擎 `gaj/scoring/`**（无外部依赖，可独立验证）
   - 先写 `engine.py`，用 `examples/` 下的样本验证
   - 写 `cli.py`，跑 `--all` 给已有数据打分
3. **网页版大模型驱动 `gaj/browser/`**
   - 先写 `cdp.py` + `llm_driver.py` 基类
   - 先适配 DeepSeek（用户默认），跑通端到端
   - 再适配豆包/通义/Kimi（选择器可能需要调试）
4. **AI 编排 `gaj/ai/`**
   - `prompts.py` + `parser.py` + `runner.py`
   - 用 DeepSeek 跑通单个 AI 打分
   - 简历改写
5. **Web 可视化 `gaj/web/`**
   - FastAPI 路由 + 静态 HTML
   - 列表/筛选/详情先跑通
   - SSE 日志 + 触发打分按钮
6. **简历 `gaj/resume/` + 爬虫适配 `gaj/scraper/`**（最小可用）
7. **统一 CLI `gaj/cli.py`**
8. **端到端验证**

---

## Assumptions & Decisions

1. **权重冲突**：`scoring_rules.md` 写 40/30/20/10，`profile.md` 用户填 30/30/30/10。按 `config.py` 现有设计，默认 `use_profile_weights=True` 走 profile，不强行统一。
2. **AI 选择器可能失效**：网页版大模型的 DOM 选择器会随网站更新失效。每个 driver 加自检 + log 警告，不崩溃。选择器集中在类属性，方便维护。这是网页版驱动的固有风险，已接受。
3. **不引入前端构建链**：Alpine.js CDN + 原生 CSS，零构建，契合项目简洁风格。
4. **不引入任务队列**：打分/简历用线程池 + SSE 推进度，不引入 Celery/RQ。
5. **不改动 `boss_scraper/` 源码**：爬虫适配层只在外面包一层，老爬虫保持可用。
6. **worktree 创建需先 commit gaj/**：因为 `gaj/` 是 untracked。会在 main 上做一个 wip commit，仅 stage `gaj/`。这是必要操作，用户批准后执行。
7. **profile.md / data/ 不跨 worktree**：`.gitignore` 忽略，需手动从主目录拷贝到 worktree。
8. **JSON 解析容错**：大模型返回的 JSON 可能不合法，`parser.py` 三级降级（整体→代码块→子串），失败不崩，存 `_parse_error`。
9. **AI 打分不覆盖**：每次新增 `ai_<provider>_<时间戳>.json`，已有 `repo.save_ai_score` 实现此约定，不改动。
10. **A-03 语义匹配**：规则阶段无法做真正的语义，先用子串/同义词粗判触发 A-03，让 AI 精修。规则引擎只负责"是否触发"，语义判断交给 AI。

---

## Verification Steps

1. **规则打分**：
   - `python -m gaj.scoring.cli --all` 对已有职位打分
   - 抽查 `data/jobs/<id>/scores/rule.json` 格式是否符合 `scoring_rules.md` 第四节
   - 验证 H-04 薪资不达标被淘汰、A-04 模糊区间触发 AI
2. **网页版大模型驱动**：
   - `python jd_cdp_parser.py --setup-chrome` 启动 Chrome，登录 DeepSeek
   - `python -c "from gaj.browser import get_driver; d=get_driver('deepseek'); print(d.ask('1+1='))"` 验证端到端
3. **AI 打分**：
   - `python -m gaj.ai.cli --score --job <id> --provider deepseek`
   - 检查 `scores/ai_deepseek_<时间戳>.json` 是否含 `ai_corrections` 和 `final_recommendation`
   - 多 provider 对比：`--provider deepseek,doubao`，检查两个文件都生成
4. **Web 可视化**：
   - `python -m gaj.web.server` 启动，浏览器访问 `http://127.0.0.1:7788`
   - 验证列表/筛选/排序/详情/SSE 日志
   - 在 UI 上点"规则打分"和"AI 打分"按钮，验证后台执行 + 日志推送
5. **简历生成**：
   - 准备 `data/resumes/master.md`
   - `python -m gaj.resume.cli --job <id> --provider deepseek`
   - 检查 `data/resumes/tailored/<id>_<时间戳>.md` 生成
6. **爬虫适配**：
   - `python -m gaj.scraper.cli --crawl <URL>`
   - 检查数据落到 `data/jobs/<job_id>/` 而非老 `jobs/001/`
7. **端到端**：
   - 爬取 → 规则打分 → AI 打分 → Web 查看 → 生成简历，全链路跑通
