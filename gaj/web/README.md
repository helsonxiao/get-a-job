# gaj/web — Web 图鉴模块地图

前后端按功能域分治的结构说明。**改代码前先看本文**，可直接定位到
目标文件，避免全库扫描。架构决策背景见
[ADR-001](../../.trae/documents/adr-001-frontend-framework.md)（Alpine 组件岛选型）
与 [ADR-002](../../.trae/documents/adr-002-module-split.md)（本次分治重构）。

## 目录总览

```
gaj/web/
├── app.py            # 组装层: FastAPI 创建/路由挂载/生命周期/静态资源/启动入口 (~90 行)
├── runtime.py        # 后台任务基础设施: _run_in_thread/_running_tasks/_task_lock
└── routes/           # 后端路由, 一个功能域一个模块 (见下方地图)
    ├── __init__.py   # ALL_ROUTERS 聚合表, app.py 据此挂载
    ├── jobs.py       # /api/jobs/**          职位: 列表/详情/收藏/忽略/调分/AI打分/删除
    ├── companies.py  # /api/companies/**     公司: 三榜/详情/想去/公司级 AI 评价
    ├── observatory.py# /api/observatory/**   市场观察台: 薪资/热力/雷达/技能
    ├── scoring.py    # /api/rules|scoring-config|score-all  规则打分配置
    ├── config.py     # /api/config/**        AI 规则矫正 (人工复核)
    ├── profile.py    # /api/profile/**       画像读写/权重预设
    ├── resume.py     # /api/resume           主简历读写
    └── system.py     # /api/stats|facets|tasks|providers|reindex|logs/stream + SSE 桥

static/
├── index.html        # 应用骨架 (~200 行): 共享 UI + 各岛挂载点 [data-tpl] + loader
├── style.css         # 共享外壳: 按钮/Header/徽章/日志面板/Toast/视图切换器
├── alpine.min.js     # 本地化的 Alpine 3 (禁 CDN; 由 loader 动态加载)
├── core/             # 前端共享层
│   ├── store.js      # Alpine.store('core'): api/toast/stats/tasks/SSE/view 路由
│   └── icons.js      # Lucide 图标刷新 (debounce + 防重入)
├── views/            # 视图岛, 一视图三件套: <name>.js (逻辑) + <name>.html (模板) + styles/<name>.css
│   ├── jobs.js/.html # jobsPanel: 职位列表 + 详情 (左列表右详情)
│   ├── guide.js/.html# guidePanel: 公司图鉴 (卡片墙/象限/对比 + 公司抽屉)
│   ├── config.js/.html# configPanel: 规则概览 + 画像编辑 (+ 隐藏的 AI 矫正)
│   ├── resume.js/.html# resumePanel: 主简历编辑器
│   └── observatory.js/.html# observatoryPanel: 市场观察台 4 视角 + 下钻抽屉
└── styles/           # CSS 按视图拆分 (与 views/ 一一对应)
    ├── base.css      # 设计令牌 + 重置 (唯一来源)
    ├── jobs.css / guide.css / config.css / resume.css
    ├── observatory.css / calibration.css
```

### 视图三件套配对 (改某视图只读这 3 个文件)

| 视图 | 逻辑 | 模板 | 样式 |
|---|---|---|---|
| 职位 | `views/jobs.js` | `views/jobs.html` | `styles/jobs.css` |
| 公司图鉴 | `views/guide.js` | `views/guide.html` | `styles/guide.css` |
| 市场观察台 | `views/observatory.js` | `views/observatory.html` | `styles/observatory.css` |
| 规则配置 | `views/config.js` | `views/config.html` | `styles/config.css` + `calibration.css` |
| 主简历 | `views/resume.js` | `views/resume.html` | `styles/resume.css` |

index.html 里每个视图只留一个**挂载点**（含岛声明 x-data/x-show/事件，
标 `data-tpl="<name>"`），模板内容由 loader 注入。

## 前端架构契约 (Alpine 组件岛)

### 视图路由
- 视图切换唯一状态: `$store.core.view` ∈ `jobs | guide | observatory | config | resume`
- 面板显隐一律 `x-show="$store.core.view === '<name>'"`，**禁止新增布尔 flag**
- 分段按钮用 `switchView(v)`；规则配置/简历等叠层按钮用 `toggleView(v)`（再点回 jobs）

### 共享态 → store，私有态 → 岛
- 跨视图共享的只有: api/toast/stats/tasks/logs/SSE/providers/aiProvider/
  view/ui.selectMode/resumeExists/logOpen，全在 `core/store.js`
- 视图私有状态 (filters/detail/companyDetail/…) 只放各自 `views/*.js`，禁止进 store
- 岛内调共享能力: `this.$store.core.api(...)` / `.toast(...)` / `.taskRunning(key)`

### 跨视图通信 (window 事件协议)
| 事件 | 方向 | 说明 |
|---|---|---|
| `gaj:open-job` (detail.jobId) | store.openJob → jobsPanel | 切到 jobs 视图并加载职位详情 |
| `gaj:open-company` (detail.brandId) | store.openCompany → guidePanel | 切到 guide 视图并打开公司抽屉 |
| `gaj:refresh` | store.poll → 各岛 | 后台任务状态变化, 各岛自行刷新 |
| `gaj:toggle-select` | header → jobsPanel | 批量模式开关 (状态在 store.ui) |

### 模板片段加载 (硬约束)

模板与 Alpine 的加载时序由 index.html 末尾的 **loader** 保证：

1. head 里同步 `<script>`（core/*.js + views/*.js）只注册 `alpine:init`
   监听，不依赖 DOM
2. loader（body 末尾）fetch 所有 `[data-tpl]` 对应的 `views/*.html`
   并 innerHTML 注入挂载点
3. **全部注入完成后才动态插入 alpine.min.js**——保证 Alpine 扫描时
   全部视图 DOM 已就位（本地 alpine 3.15.12 CDN 构建无
   `deferLoadingAlpine` 钩子，故用"先注入后加载"）
4. fetch 失败 → `#tpl-error` 兜底块显示错误，不启动 Alpine

注意：**alpine.min.js 不再用 `<script defer>` 标签引用**，由 loader
动态加载；新增视图只需建片段文件 + 挂载点标 `data-tpl`，loader 零改动。
静态资源改版必须递增 `?v=` 参数（head 引用与 loader 里的 `VER` 常量
两处都要改）。

## 后端契约

- 新增路由: 在 `routes/` 建模块 (APIRouter + prefix + tags) → 在
  `routes/__init__.py` 的 `ALL_ROUTERS` 登记，app.py 不用改
- 耗时任务 (AI 打分/采集/矫正) 一律 `runtime._run_in_thread(fn, task_key, **kw)`
  后台执行，前端通过 `GET /api/tasks` 轮询 + SSE 日志看进度
- **Python 3.9**: 路由函数签名注解用 `Optional[X]`，不能用 `X | None`
- SSE 日志桥在 `routes/system.py`，app.py 启动时调 `system.init_sse()` 绑定

## 新增一个视图的步骤

1. `static/views/<name>.js`: `Alpine.data('<name>Panel', () => ({...}))`，共享态走 `$store.core`
2. `static/views/<name>.html`: 视图模板片段（纯内容，不含岛声明 div）
3. `static/styles/<name>.css`: 视图样式
4. `index.html`: 新增挂载点 div（`data-tpl="<name>"` + x-data/x-show/事件）
   + `<script>`/`<link>` 引用（带 `?v=`，同步 bump loader 里的 `VER`）
5. header 的 view-switch 加按钮 (`$store.core.switchView('<name>')`)
6. 后端如需新 API: `routes/<name>.py` + `ALL_ROUTERS` 登记

## 热重载

- 后端: uvicorn reload 模式, 改 Python 自动重启 (`python3 -m gaj web`)
- 前端: index.html/静态文件实时读取, 刷新浏览器即可 (记得 bump `?v=`)
