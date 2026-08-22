# 就业市场观察台 — 功能迭代计划（v3）

> 主题转向：从「找坑位」转向「看市场」。用户当前无迫切求职需求，更想以观察者视角把就业市场当样本研究，因此**调高市场观察类功能优先级、压低个人匹配类功能**。
>
> 本版新增旗舰：① 用户提出的「区域机会热力图」（经纬度数据已确认存在，详见 §1.1）；② 挖掘更多「有意思」的观察角度（加班雷达、红旗信号、技术共生、匿名雇主等）。
>
> **v3 关键调整：前端先做渐进式架构重构再上功能。** 经尽调（§0），Alpine.js 组件岛 + `Alpine.store` 是零构建约束下的最优解，路线锁定为：抽共享层 + 新视图用新架构落地，**旧视图暂不动**（渐进式，呼应 corral 项目 ADR 的"过渡期可接受"原则）。
>
> 决策依据（用户确认）：宏观市场情报 + 微观自我匹配两者并重 → 本版修正为**以市场观察为主、个人匹配延后**；时序本轮轻量、完整快照留后续；全景规划分优先级、本轮落地 P0。

---

## 〇、前端架构重构（前置，渐进式）

### 0.0 为何重构 — 量化触顶信号

实测当前前端：[app.js](file:///Users/helsonxiao/Codes/get-a-job/gaj/web/static/app.js) **889 行 / ~220 个方法**（单个 `function app()` 返回的扁平 Alpine 根组件，4 个视图的逻辑全挤一起）；[index.html](file:///Users/helsonxiao/Codes/get-a-job/gaj/web/static/index.html) **1319 行**模板内联；[style.css](file:///Users/helsonxiao/Codes/get-a-job/gaj/web/static/style.css) **1980 行**。无构建工具、Alpine 本地化（`alpine.min.js`）是项目刻意取向。再加市场观察台 4 子模块会让 app.js 冲到 ~1100 行 / 280+ 方法。**单体文件触顶，需先重构再加功能。**

### 0.1 路线尽调结论 — 锁定 Alpine 组件岛

| 方案 | 零构建 | 本地化 | 内联HTML模板 | 岛+Store | 判定 |
|------|--------|--------|-------------|---------|------|
| **Alpine.js 3** | ✅ | ✅ | ✅原生 | ✅ `Alpine.data()`+`Alpine.store()` | **最优** |
| Petite-Vue | ✅ | ✅ | ✅ | ❌ 无岛模型/无store | 不如Alpine |
| htmx | ✅ | ✅ | - | ❌ 服务端渲染会fork渲染层 | 与JSON API架构冲突 |
| Vue3 CDN | ✅ | ✅ | ⚠️需JS串模板 | ✅ | 内联体验不如Alpine |
| Lit/Svelte/React | ❌需编译 | - | ❌ | ✅ | 违背零构建/内联取向 |

**佐证**：2026-07 [tuna-os/corral](https://github.com/tuna-os/corral/issues/52) 同处境（单 1732 行 vanilla app.js、零构建、JSON API 客户端）做 ADR 采用 Alpine 岛式迁移，理由一致：JSON API 是多端共享面、Alpine 保留之；项目刻意无 node 工具链，bundler/SPA 会破坏取向；原话"过渡期部分Alpine部分命令式可接受，岛式迁移让每步可交付"。

### 0.2 重构范围（渐进式，本轮只做这些）

> 原则：**只抽共享层 + 新视图用新架构写**。旧视图（职位列表/公司图鉴/配置/简历）**暂不动**，继续用现有 `app()` 根组件跑，避免大爆炸式重写风险。后续逐视图迁移留作独立迭代。

**新增文件结构**：

```
gaj/web/static/
├── alpine.min.js              (不动)
├── app.js                    (旧视图逻辑, 暂不动, 后续逐岛迁出)
├── core/
│   ├── store.js              (新) Alpine.store('app'): 共享 api客户端/toast/stats/SSE
│   └── icons.js              (新) 迁出现 refreshIconsDebounced, 供新旧视图共用
├── views/
│   └── observatory.js        (新) Alpine.data('observatoryPanel', () => ({...}))
└── styles/
    ├── base.css              (新, 拆出全局变量/重置/通用组件)
    └── observatory.css       (新, 观察台专属样式)
```

**共享层 `core/store.js` 内容**（`Alpine.store('app', {...})`）：

```js
document.addEventListener('alpine:init', () => {
  Alpine.store('app', {
    // 跨视图共享的响应式状态
    stats: {}, sseConnected: false, toasts: [], _toastSeq: 0,
    tasks: {},                 // 后台任务状态(打分/AI评价等)
    async api(path, method='GET', body=null) { /* fetch 封装, 现有 app.api 逻辑迁入 */ },
    toast(msg, type='info') { /* 现有 toast 逻辑迁入 */ },
    removeToast(id) { ... },
    async connectSSE() { /* 现有 SSE 连接迁入 */ },
    async pollTasks() { ... },
    async loadStats() { ... },
  });
});
```

**新视图 `views/observatory.js`**（`Alpine.data()` 工厂）：

```js
document.addEventListener('alpine:init', () => {
  Alpine.data('observatoryPanel', () => ({
    tab: 'geo', geo: [], radar: null, skills: [], salary: null, loading: false,
    init() { this.load('geo'); },
    setTab(t) { this.tab = t; this.load(t); },
    async load(tab) { /* 调 $store.app.api 拉对应端点 */ },
    // ... 渲染辅助方法(热力点坐标/技能溢价着色等)
  }));
});
```

### 0.3 index.html 接线（模板内联，呼应用户选择）

- 顶部 view-switch 加第三按钮 `市场观察台`（`@click="$store.app.openView('observatory')"` 或简单 `showObservatory=true`）。
- 新增 `<div x-data="observatoryPanel()" x-show="showObservatory">` 岛，内含 4 子 Tab 模板（**仍内联在 index.html，仅用注释严格分区**，不拆 fetch 加载，避免异步卡顿）。
- 旧 `<div class="app-shell" x-data="app()">` 根壳**不动**，但需让 store 可达：新视图岛用 `$store.app.*` 访问共享态，旧视图仍用 `this.*`（过渡期共存，corral 模式）。
- **关键接线坑（来自 corral ADR）**：Alpine CDN 版会在其 `defer` 脚本执行时自动 `start`，若新视图脚本在 Alpine 之后加载会错过 `alpine:init`。**所有新 `<script>` 必须放在 Alpine 的 `<script defer src="alpine.min.js">` 之前**（或在 `alpine:init` 监听器里注册，监听器本身要在 Alpine 脚本之前注册）。
- 现有 [index.html#L9-L10](file:///Users/helsonxiao/Codes/get-a-job/gaj/web/static/index.html#L9-L10) 是 `<script src="/static/app.js">` 在 `<script defer src="/static/alpine.min.js">` 之前，符合此约束；新增 `core/store.js`、`views/observatory.js` 同样插在 Alpine 之前。

### 0.4 CSS 拆分

- `style.css`（1980 行）本轮**不强行拆完**，仅：
  - 抽出全局 CSS 变量/重置/通用组件到 `styles/base.css`，`index.html` 加 `<link>`；
  - 新增 `styles/observatory.css`（观察台专属），旧 style.css 其余暂留。
- 后续逐视图迁移时同步抽对应 CSS（jobs.css/guide.css/...）。

### 0.5 重构验收（必须先过，再进 §三）

1. store.js 接线后，旧 `app()` 视图功能完全不变（职位列表/图鉴/配置/简历/详情全可正常用），stats/toast/SSE 由 store 提供、旧 `app()` 通过 `$store.app` 或保留自身实现过渡。
2. 一个最小 `Alpine.data()` 岛 demo（如 hello 计数器）能挂载并响应。
3. 脚本顺序坑验证：新视图岛在首次刷新后能正确初始化（不报 `observatoryPanel is not defined`）。
4. `alpine:init` 监听器在 Alpine 加载前注册成功。

> 通过 §0.5 后再进 §三，把市场观察台 4 子模块用新架构落地。这一步把"重构"和"新功能"绑在一个可交付单元里，避免重构空转。

---

## 一、现状与数据资产核对（本轮新增核实）

### 1.1 经纬度数据 — 用户直觉正确，但未入索引

- **原始层**：[data/jobs/<id>/job.json](file:///Users/helsonxiao/Codes/get-a-job/data/jobs) 里 `gps: {"lng": float, "lat": float}`。实测 325 个岗位中 **240 个有非空 gps（74%）**，坐标精确到区级（无锡新吴区 120.36/31.52、锡山区 120.49/31.58、宜兴市 119.87/31.40 等）。`city/district` 同步填充；`business_district` 多为空（BOSS 该字段稀疏）。
- **索引层**：[gaj/store/index.py#L30-L85](file:///Users/helsonxiao/Codes/get-a-job/gaj/store/index.py#L30-L85) 的 `jobs` 表**没有 lat/lng 列**，gps 目前只存在原始文件。→ 地理类功能需要**一次轻量迁移**：给 jobs 表加 `lat/lng/district/business_district` 列 + 从现有 job.json 重索引（一次性，不重爬）。
- 爬虫侧已具备抓取能力：[boss_scraper/page_parser.py#L110-L112](file:///Users/helsonxiao/Codes/get-a-job/boss_scraper/page_parser.py#L110-L112) 从 `.job-location-map` 的 `data-lat/data-lng` 取坐标，[gaj/core/normalize.py#L535](file:///Users/helsonxiao/Codes/get-a-job/gaj/core/normalize.py#L535) 的 `parse_gps()` 归一。

### 1.2 信号层 — 加班/红旗分析的金矿

`jobs` 表已落盘 [gaj/store/index.py#L47-L52](file:///Users/helsonxiao/Codes/get-a-job/gaj/store/index.py#L47-L52)：`overtime/overtime_conf/work_mode/outsourcing/travel/team_size`。原始 `signals` 还含 `tech_depth/blacklist_hits`。实测加班分 heavy/moderate/light/none 四档、work_mode 有 onsite/remote、outsourcing 布尔、travel 有 occasional/frequent。**这是普通招聘平台不会告诉你的「市场真相面」**，极具观察价值。

### 1.3 其它已具备但未利用的数据

- `boss` 字段：实测多数岗位 `name/title` 为空（BOSS 列表 API 在详情页爬取时拿不到），`gold_hunter` 多为 false → **招聘官画像价值低，降级**。
- `welfare`：丰富（生日福利/住房补贴/宿舍空调/补充公积金…），可做福利组合分析。
- `company.founded/registered_capital/business_scope/intro/working_hours`：公司年龄、注册资本、经营范围、工时 → 公司体量画像。
- `salary_months`：12/13/14/15 薪分布。
- `anonymous/data_conflict`：匿名雇主「某中型储能公司」、串号公司 → 匿名雇主现象分析。
- `first_seen/last_seen`：240+ 岗位跨 7/31~8/17 多批次 → 轻量时序可行。

### 1.4 架构约定（落地需遵守，不变）

后端聚合在 [gaj/store/index.py](file:///Users/helsonxiao/Codes/get-a-job/gaj/store/index.py) 复用 `facets()` 的 `GROUP BY` 与 `visible = "(ignored=0 OR ignored IS NULL)"` 约定；路由在 [gaj/web/app.py](file:///Users/helsonxiao/Codes/get-a-job/gaj/web/app.py)；前端 Alpine.js + 纯 SVG 图表（**不引入 CDN**，遵循项目硬约束）；新顶级视图走 `showObservatory` 开关，对称 `showGuide`。

---

## 二、候选观察视角全景（按「有意思程度 × 数据可行性」排序）

> 标记：📊=宏观市场 / 🎯=微观匹配 / 🗺=地理 / 🕐=时序 / 🚩=信号 / 🧪=好奇研究
> 优先级：**P0 本轮 / P1 下一轮 / P2 远期**

| # | 视角 | 类型 | 数据源（已具备） | 为什么有意思 | 优先级 |
|---|------|------|-----------------|-------------|--------|
| **G1** | **区域机会热力图** | 🗺📊 | `gps`(需迁移入索引) + `city/district` | 一眼看出机会往哪个区/板块扎堆，比城市榜更细。**用户点子，旗舰** | **P0** |
| **G2** | **加班/红旗信号雷达** | 🚩📊 | `overtime/work_mode/outsourcing/travel/blacklist_hits` × `industry/company/district` | 哪些行业/公司/区域最 996、最外包、最出差——平台不会说的真相 | **P0** |
| **S1** | **技能热度榜** | 📊 | `jobs.skills` × `salary_mid` | 哪些技能最抢手、哪些带薪资溢价，指导学习方向 | **P0** |
| **S2** | **薪资定价曲线** | 📊 | `salary_mid` × `exp_min/edu_level/industry/stage` | 「市场给每一年经验定多少价」、学历溢价、行业薪资水位 | **P0** |
| **I1** | **行业景气度榜** | 📊 | `industry` × `job_count/salary_mid/online` | 哪些行业在招、活跃度、薪资水位（已有 facets 基座） | **P1** |
| **T1** | **轻量新增岗位趋势** | 🕐 | `first_seen` 日/周分桶 | 零成本感知市场冷热曲线（轻量方案） | **P1** |
| **T2** | **技术栈共生网络** | 🧪 | `skills` 共现矩阵 | 哪些技能总一起出现（React+TS、Java+Spring），看技术栈「套餐」 | **P1** |
| **W1** | **福利组合图谱** | 🧪 | `welfare` 共现 + 稀有福利 | 哪些福利是标配、哪些是稀缺福利、福利组合聚类 | **P1** |
| **C1** | **公司融资阶段-薪资象限** | 📊 | `stage` × `salary_mid/company_score` | 早期高期权 vs 成熟期稳薪的分布（公司象限图扩展到全局） | **P2** |
| **A1** | **匿名雇主现象分析** | 🧪 | `anonymous/data_conflict` × `industry/salary` | 「某中型储能公司」式匿名岗占多少、集中在哪些行业、薪资是否偏低 | **P2** |
| **M1** | **工作模式分布** | 🚩 | `work_mode` × `industry/district` | 远程/混合/现场比例，哪些行业/区域更开放远程 | **P2** |
| **M2** | **公司年龄/注册资本画像** | 🧪 | `company.founded/registered_capital` | 招聘公司的「年龄/体量」分布，老牌 vs 新锐 | **P2** |
| **M3** | **薪资月份溢价** | 🧪 | `salary_months` × `industry` | 13/14/15 薪在哪些行业更常见 | **P2** |
| ~~P-技能缺口~~ | ~~技能缺口分析~~ | ~~🎯~~ | ~~profile × skills~~ | ~~个人匹配类，用户暂无迫切需求，**延后**~~ | ~~P2~~ |
| ~~P-门槛分布~~ | ~~学历经验门槛分布~~ | ~~🎯~~ | ~~edu/exp × 个人~~ | ~~同上，延后~~ | ~~P2~~ |
| ~~P-通勤~~ | ~~通勤友好度~~ | ~~🎯~~ | ~~gps × 居住地~~ | ~~同上，且 profile 经纬度为空，延后~~ | ~~P2~~ |
| ~~R-招聘官~~ | ~~招聘官画像~~ | ~~🎯~~ | ~~boss~~ | ~~数据稀疏（多数 name/title 空），**降级/搁置**~~ | ~~P3~~ |

**P0 选型理由**：G1 是用户点子且数据已具备（仅需轻量迁移）；G2 用独有的信号层数据，是平台不公开的「真相面」，观察价值最高且无可替代；S1/S2 是市场情报的基础设施。四者构成「市场观察台」首发阵容，全部复用现有数据。个人匹配类（P-*）因用户无迫切需求统一延后到 P2。

---

## 三、P0 本轮实施（市场观察台）

新增顶级视图「市场观察台」（图标 `lucide:radar` 或 `lucide:globe`），与 `职位列表 / 公司图鉴` 并列。内含 4 个子模块（Tab 切换）。

### 3.0 前置：经纬度入索引（地理功能基座，必做）

**文件**：[gaj/store/index.py](file:///Users/helsonxiao/Codes/get-a-job/gaj/store/index.py)

1. `SCHEMA` 的 `jobs` 表追加列：`lat REAL, lng REAL, district TEXT, business_district TEXT`；`_migrate()` 里加 `ALTER TABLE jobs ADD COLUMN lat` 等幂等迁移。
2. `upsert_job()` 写入时落 `lat=job.gps.get('lat')` 等。
3. 新增 `def backfill_geo(conn, repo)`：遍历 `data/jobs/*/job.json`，对每条读 `gps/city/district/business_district` 回填 SQLite（一次性，~325 条，秒级）。
4. 跑一次 `backfill_geo` 完成存量回填；新增 `lat/lng` 索引 `idx_jobs_geo`。

> 这一步是 G1 的前置，也顺便让 city/district 在索引层完整（当前 SQLite city 仅 171/230 有值，回填后应≈240）。

### 3.1 后端：聚合查询层（[gaj/store/index.py](file:///Users/helsonxiao/Codes/get-a-job/gaj/store/index.py)，紧邻 `facets()` 之后）

```python
def observatory_geo_heatmap(conn, cell_size=0.01) -> list[dict]:
    """区域机会热力图。按 lat/lng 网格分桶(cell_size度≈1km),
    每格: count、avg_salary_mid、top_industry、top_district。
    过滤 lat IS NOT NULL。返回网格中心点列表供前端画热力点。"""

def observatory_signal_radar(conn) -> dict:
    """加班/红旗信号雷达。返回:
      overtime_dist(heavy/moderate/light/none 计数) 按 industry/district 分组,
      outsourcing_rate, travel_rate(occasional+frequent), blacklist_hit_count,
      以及『红旗公司榜』(overtime=heavy 或 outsourcing=1 或 blacklist_hits>0 的公司, 按岗位数排)。"""

def observatory_skill_leaderboard(conn, top_n=40) -> list[dict]:
    """技能热度榜。展开 jobs.skills JSON → 每技能:
      demand_count、company_count、avg_salary_mid、salary_premium%、top_industries。
    skills 用 Python 侧展开(SQLite json_each 兼容性差), 小写归一, 显示用最高频原形。"""

def observatory_salary_pricing(conn) -> dict:
    """薪资定价曲线。返回:
      overall 分位(p10/p25/p50/p75/p90/mean),
      by_exp(按 exp_min 分桶 0-3/3-5/5-8/8+ 的 median/count),
      by_edu(按 edu_level 的 median),
      by_industry(median/count), by_stage(median/count)。
    纯 Python statistics.quantiles 计算。"""
```

实现要点：复用 `visible` 过滤；薪资单位统一「万元/年」（`salary_mid` 已是该口径）；空样本返回 `[]`/`null` 由前端降级显示。

### 3.2 后端：HTTP 路由（[gaj/web/app.py](file:///Users/helsonxiao/Codes/get-a-job/gaj/web/app.py)，紧邻「公司图鉴」段之后）

```python
# ---------------------------------------------------------------- 市场观察台

@app.get("/api/observatory/geo")
async def api_obs_geo() -> dict:
    with index.session() as conn:
        return {"cells": index.observatory_geo_heatmap(conn)}

@app.get("/api/observatory/radar")
async def api_obs_radar() -> dict:
    with index.session() as conn:
        return index.observatory_signal_radar(conn)

@app.get("/api/observatory/skills")
async def api_obs_skills(top_n: int = Query(40, le=200)) -> dict:
    with index.session() as conn:
        return {"items": index.observatory_skill_leaderboard(conn, top_n)}

@app.get("/api/observatory/salary")
async def api_obs_salary() -> dict:
    with index.session() as conn:
        return index.observatory_salary_pricing(conn)
```

### 3.3 前端：用新架构落地（依赖 §0 重构已通过）

> 关键变化（vs v2）：观察台**不再往旧 `app.js` 塞状态**，而是作为独立 `Alpine.data()` 岛 + `$store.app` 共享层落地。旧 `app.js` 一行不动。

**新建 [gaj/web/static/views/observatory.js](file:///Users/helsonxiao/Codes/get-a-job/gaj/web/static/views/observatory.js)**（§0.2 已给骨架）：`Alpine.data('observatoryPanel', () => ({ tab, geo, radar, skills, salary, loading, init(), setTab(t), async load(tab) { 调 $store.app.api('/api/observatory/'+tab) } }))`，外加渲染辅助方法（热力点坐标投影、技能溢价着色等）。

**[gaj/web/static/index.html](file:///Users/helsonxiao/Codes/get-a-job/gaj/web/static/index.html)**
- `<head>` 在 Alpine 脚本**之前**插入 `<script src="/static/core/store.js">`、`<script src="/static/core/icons.js">`、`<script src="/static/views/observatory.js">`（§0.3 顺序坑）。
- view-switch(L60-68) 加第三个按钮 `<i data-lucide="radar"></i><span>市场观察台</span>`，`@click` 切 `showObservatory`（用根壳 `app()` 上的一个布尔标志驱动显隐，新视图岛 `x-data="observatoryPanel()"` 内部状态自治）。
- 新增 `<div class="observatory-panel" x-data="observatoryPanel()" x-show="showObservatory">` 岛，内含 4 子 Tab 模板（**内联，注释严格分区**，不 fetch 加载）：
  1. **区域热力图**：纯 SVG 散点云（经纬度自适应缩放，无锡范围 120.3-120.5/31.4-31.7）+ `<circle>` 热力点（半径/色深 ∝ 岗位数），hover 显示格内 count/均薪/Top 行业/区域。无地图瓦片（遵循无 CDN 约束）。
  2. **信号雷达**：加班分档柱图 + 红旗公司榜表格（公司/岗位数/红旗类型）+ 外包率/出差率/黑名单命中数概览卡片。
  3. **技能热度榜**：表格列 = 技能/需求岗位数/招聘公司数/均薪/薪资溢价%/Top 行业；溢价正数绿、负数红。
  4. **薪资定价**：经验-薪资折线、学历-薪资柱图、行业/阶段中位数对比柱图（纯 SVG）。

**新建 [gaj/web/static/styles/observatory.css](file:///Users/helsonxiao/Codes/get-a-job/gaj/web/static/styles/observatory.css)**：`.observatory-panel/.obs-tabs/.heatmap/.radar-card/.skill-bar/.salary-curve` 样式，沿用 `--gaj-*` 变量与紧凑布局。`index.html` 加 `<link rel="stylesheet" href="/static/styles/observatory.css">`。

### 3.4 验证步骤（P0）

> 前置：先过 §0.5 重构验收（旧视图不回归 + 岛 demo 可挂载 + 脚本顺序坑已解）。

1. `backfill_geo` 后 `SELECT COUNT(*) FROM jobs WHERE lat IS NOT NULL` ≈ 240（与原始 gps 非空数一致）。
2. `/api/observatory/geo` 返回的格点 count 总和 ≈ 240；均薪在无锡合理区间（15-30 万）。
3. `/api/observatory/radar` 的 `overtime_dist` 各档计数和 ≈ 可见岗位数；红旗公司榜非空。
4. `/api/observatory/skills` 的 `demand_count` 总和 ≥ 岗位数（一岗多技能）；溢价 = (skill_avg - market_avg)/market_avg。
5. `/api/observatory/salary` 的 `by_exp` 各桶 count 和 ≈ 有薪资岗位数；`overall.p50` 与全市场 `salary_mid` 中位数一致。
6. 前端 4 Tab 均有数据，热力点落在无锡各区，溢价颜色方向正确，空样本显示「样本不足」不报错（遵循 null 安全硬约束）。
7. **回归**：旧视图（职位列表/公司图鉴/配置/简历/详情）全部功能正常，未因 store 抽取/脚本顺序变更而回归。

---

## 四、P1 下一轮（市场观察扩展 + 轻量时序）

- **I1-行业景气度榜**：复用 P0 的 `facets()` 基座扩展，行业 × 岗位数/公司数/均薪/在线率/阶段分布。
- **T1-轻量新增趋势**：`first_seen` 日/周柱图，零爬虫改动（与原 v1 一致）。
- **T2-技术栈共生网络**：`skills` 两两共现矩阵 → 力导向图（纯 SVG），看技术栈「套餐」。
- **W1-福利组合图谱**：`welfare` 共现 + 稀有福利榜（补充公积金/企业年金等稀缺项排行）。
- **T-完整时序(独立排期)**：改 [boss_scraper/crawler.py](file:///Users/helsonxiao/Codes/get-a-job/boss_scraper/crawler.py) + index 新增 `job_daily_snapshot` 表，每日爬取后落盘岗位计数快照，支撑真正的「招聘活跃度随时间变化」。需独立任务。

---

## 五、P2 远期候选（仅登记）

- C1 公司融资阶段-薪资象限（全局散点）
- A1 匿名雇主现象分析（`anonymous/data_conflict` 占比、行业集中度、薪资差异）
- M1 工作模式分布（远程/现场比例 × 行业/区域）
- M2 公司年龄/注册资本画像（`founded/registered_capital`）
- M3 薪资月份溢价（13/14/15 薪行业分布）
- P-技能缺口/门槛分布/通勤（个人匹配类，待用户有求职需求时再启）
- R-招聘官画像（数据稀疏，待爬虫补全 boss 字段后再议）

---

## 六、假设与决策

1. **地理迁移**：给 jobs 表加 lat/lng/district/business_district 列 + 一次性 `backfill_geo` 从现有 job.json 回填，不重爬。新爬的岗位在 `upsert_job` 自动落 geo。
2. **热力图实现**：纯 SVG 散点云（经纬度自适应缩放），不引入地图瓦片/Leaflet（遵循无 CDN 硬约束）；如后续要底图，本地化一份简化 GeoJSON。
3. **技能归一**：P0 先 `skill.lower()` 聚合 + 显示最高频原形；完整别名映射（Go/Golang、React/react）留 P1。
4. **薪资口径**：统一「万元/年」，用 `salary_mid`，不另乘 `salary_months`（沿用 `salary_mid_avg` 约定）。
5. **信号口径**：加班 `overtime` 取 `value`(heavy/moderate/light/none) + `overtime_conf` 置信度；红旗公司 = `overtime=heavy` 或 `outsourcing=1` 或 `blacklist_hits 非空`，UI 标注红旗类型。
6. **时序口径**：P0 不做时序（已挪到 P1）；`first_seen` 是「首次发现」非「发布时间」，UI 文案明确「新增发现数」。
7. **空值降级**：所有图表 `data==null||length==0` 时显示「样本不足」，不渲染空 SVG（遵循 UI null 安全硬约束）。
8. **个人匹配延后**：用户暂无求职迫切性，技能缺口/门槛/通勤统一 P2，profile 经纬度为空也支撑该决策。

---

## 七、本轮交付物清单

**前置重构（§0）**：
- 新建 [gaj/web/static/core/store.js](file:///Users/helsonxiao/Codes/get-a-job/gaj/web/static/core/store.js)：`Alpine.store('app')` 共享层（api/toast/stats/SSE/pollTasks）
- 新建 [gaj/web/static/core/icons.js](file:///Users/helsonxiao/Codes/get-a-job/gaj/web/static/core/icons.js)：迁出 `refreshIconsDebounced` 供新旧视图共用
- 新建 [gaj/web/static/styles/base.css](file:///Users/helsonxiao/Codes/get-a-job/gaj/web/static/styles/base.css)：抽全局变量/重置/通用组件
- [index.html](file:///Users/helsonxiao/Codes/get-a-job/gaj/web/static/index.html)：head 接线新脚本（顺序在 Alpine 之前）+ `<link>` base.css；旧 `app()` 根壳不动，过渡期共存

**后端**：
- [gaj/store/index.py](file:///Users/helsonxiao/Codes/get-a-job/gaj/store/index.py)：jobs 表 +geo 列与幂等迁移、`backfill_geo()`、4 个 observatory 聚合函数
- [gaj/web/app.py](file:///Users/helsonxiao/Codes/get-a-job/gaj/web/app.py)：+4 个 `/api/observatory/*` 路由

**前端新视图（§3.3，新架构）**：
- 新建 [gaj/web/static/views/observatory.js](file:///Users/helsonxiao/Codes/get-a-job/gaj/web/static/views/observatory.js)：`Alpine.data('observatoryPanel')` 工厂
- 新建 [gaj/web/static/styles/observatory.css](file:///Users/helsonxiao/Codes/get-a-job/gaj/web/static/styles/observatory.css)：观察台样式
- [index.html](file:///Users/helsonxiao/Codes/get-a-job/gaj/web/static/index.html)：view-switch 第三按钮 + observatory-panel 岛模板（4 子 Tab 内联）

**运维**：跑一次 `backfill_geo` 回填存量 geo 数据。

**不动**：旧 `app.js`（889 行）、`style.css`（1980 行）本轮不改——后续逐视图迁出留独立迭代。

无新增依赖、无爬虫改动（本轮）、无破坏性 schema 变更（只加列）。
