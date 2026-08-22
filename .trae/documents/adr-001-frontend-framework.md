# ADR-001 前端框架选型：Alpine.js 组件岛路线

> **状态**：已采纳（2026-08-18）
> **决策者**：项目维护者
> **相关**：[市场观察台迭代计划](../.trae/documents/market-insights-iteration-plan.md) §0

---

## 一、决策背景

### 1.1 触发问题

项目前端已触"单体文件天花板"：
- [app.js](../gaj/web/static/app.js) **889 行 / ~220 方法**，全是 `function app()` 返回的**单个 Alpine 根组件**，4 个视图（职位/图鉴/配置/简历+详情）的所有状态与方法挤在一个扁平对象里。
- [index.html](../gaj/web/static/index.html) **1319 行**模板内联；[style.css](../gaj/web/static/style.css) **1980 行**单文件。
- 再往里塞「市场观察台」4 子模块（热力图+雷达+技能+薪资）会让 app.js 冲到 ~1100 行 / 280+ 方法，每改一视图都要在塞满无关逻辑的文件里穿行。

### 1.2 硬约束（项目刻意取向，不可破）

| 约束 | 来源 |
|------|------|
| 零构建（无 bundler/无 npm 运行时） | **本项目是 skill 衍生的 web 工作台，刻意保持轻量、不引入 node 工具链**；过度工程化与项目定位相悖 |
| 外部资源必须本地化（无 CDN 依赖） | 系统代理会断 CDN；Alpine 已本地化为 `alpine.min.js` |
| JSON API 客户端架构 | 后端 FastAPI 返回 JSON，前端拉取渲染 |
| 个人工具，单用户 | 无需 SSR/SEO/大规模并发 |

> **注**：模板内联 HTML、纯 JS（无 TS）**不是硬约束**，而是零构建取向的副产物——无构建则无 SFC 编译/Volar 模板类型检查。维护者已确认接受此副产物。若未来零构建取向改变（见 §6 revisit 信号），二者随之解除。

---

## 二、决策结论

**锁定 Alpine.js 3 + 组件岛（`Alpine.data()`）+ `Alpine.store()` 共享层，渐进式重构。**

- 不引入构建工具（vite/webpack）。
- 不迁移 Vue/Svelte/React。
- 旧 `app()` 根组件暂不动，新视图用新架构写，逐视图迁出留后续迭代。

---

## 三、核心概念（对比维度释义）

### 3.1 内联 HTML 模板

模板代码的**物理位置**。三种放法：

| 放法 | 代表 | 模板在哪 | 是否需构建 |
|------|------|---------|-----------|
| 内联 HTML | **Alpine** | 直接写在 index.html 当 DOM | 否 |
| JS 字符串模板 | Vue3 CDN / Lit | JS 里 `template: '<...>'` 字符串 | 否（但长模板难读） |
| 构建编译 SFC | Vue/Svelte/React | 独立 `.vue/.svelte/.jsx` 文件 | **是**（需 vite） |

本项目零构建取向直接排除 SFC；不构建的 Vue3 退回 JS 串模板可读性差。Alpine 的内联 HTML 最贴合。

### 3.2 组件岛（Component Islands）

把一个巨型根组件 `app()` 拆成**多个各自独立的 `x-data` 岛**，每岛有自己的状态/方法，互不干扰，可单独交付。

```
单根 SPA（现状）              组件岛（目标）
┌─────────────────┐          职位岛 ─┐
│ x-data=app()     │          图鉴岛 ─┼─→ Alpine.store('app')
│ 889行/220方法    │          配置岛 ─┤    (api/toast/stats)
│ 职位 图鉴 配置 详情│          观察台岛─┘
│  全纠缠在一起     │
└─────────────────┘          每岛独立 + 共享态走 store
改一处碰一片                可单独交付
```

### 3.3 Store（共享单一真相源）

跨岛共享的东西（API 客户端、toast、统计、SSE）只放一份在 `Alpine.store('app')`，谁需要谁 `$store.app` 取，一处更新所有岛自动响应——避免每岛各拉各的导致数值漂移。

---

## 四、候选方案对比矩阵

维度 → 映射目标：`简单` `易维护` `快`

| 对比维度 | Alpine 3 ✓推荐 | Petite-Vue | Vue3 CDN | 需构建类(Lit/Svelte/React) |
|---------|---------------|-----------|----------|---------------------------|
| 内联 HTML 模板 `简单` | ✓ 模板即 HTML | ✓ | △ 退回 JS 串模板 | ✗ SFC 独立文件 |
| 组件岛 `易维护` | ✓ Alpine.data() 原生 | ✗ 无岛模型 | ✓ createApp 组件 | ✓ 标准组件 |
| 共享 Store `易维护` | ✓ Alpine.store() | ✗ 无 store | ✓ provide/Pinia | ✓ signals/Redux |
| 零构建 `简单` | ✓ 一个 min.js | ✓ | △ 全局 build 偏重 | ✗ 必须 vite/webpack |
| 运行体积 `快` | ✓ ~15KB | ✓ ~7KB | △ ~50KB+ | △ 编译后中等 |
| 学习成本 `简单` | ✓ HTML+指令，低 | ✓ 低 | △ 中 | ✗ 高（JSX/编译） |
| 代码组织约束力 `易维护` | △ 软约束靠纪律 | ✗ 无约束 | ✓ SFC 强约束 | ✓ 强约束 |

**Alpine 7 项拿 6 项最优**，唯一弱项"代码组织约束力"由本次重构的目录约定补上。

---

## 五、Alpine 的缺点与风险（诚实评估）

> 按对本项目**实际影响**排序，每条标注：影响等级 + 缓解措施 + 接受判断。

### 5.1 影响中等 — 值得权衡的点

#### ① 软约束，结构靠纪律维持
- **缺点**：Alpine 只提供能力不强制结构，框架不会阻止你再造一个 889 行 god 组件。
- **本项目影响**：中。重构后约束力来自目录约定（`views/<name>.js` 一岛一文件 + `core/store.js` 共享层）+ 自我 review，而非框架强制。
- **缓解**：约定焊死——新视图必须建独立 `Alpine.data()` 文件，共享态必须走 store，禁止往旧 `app()` 继续塞。
- **你能否接受**：✅ 能，前提是**愿意守纪律**。如果你自觉容易偷懒往大文件塞，这条要三思。

#### ② 过渡期双轨共存
- **缺点**：渐进式迁移期间，旧 `app()` 根 + 新岛 + store **三套模式并存**，心智负担上升。
- **本项目影响**：中。本轮抽 store + 建观察台岛后，旧 4 视图仍是旧根组件模式，直到逐个迁出。
- **缓解**：限定过渡期窗口，迁完一个删一个，别让"过渡"变"永久"。corral 项目 ADR 明确接受此风险。
- **你能否接受**：✅ 能，前提是**有节奏推进迁移**，不无限期搁置。

#### ③ 无官方路由，视图多了就一堆布尔 flag
- **缺点**：Alpine 不带路由。现有 `showGuide/showConfig/showJobs` flag 模式，加观察台是第 4 个 flag，**未来再加市场观察子模块/新顶级视图会持续膨胀**。
- **本项目影响**：中→大（随视图数增长）。这是观察台之后**真正会撞的墙**。
- **缓解**：本轮先把 view-switch 改成单一 `currentView` 状态 + 路由表（而非 N 个布尔）；或后续引入社区 `alpinejs/router` 插件（本地化）。
- **你能否接受**：✅ 能，前提是**视图总数长期 <8 个**。若预期会到 10+ 个顶级视图，应提前评估切 Vue3+vue-router。

#### ④ 模板与逻辑物理分离
- **缺点**：内联 HTML（你选的）意味着 index.html 模板段会很长，逻辑在 `views/*.js`，**改一个组件要在两文件间跳**，不像 SFC 同文件 co-locate。
- **本项目影响**：小-中。4 子模块的观察台模板段预计 ~250 行在 index.html，逻辑在 observatory.js。
- **缓解**：模板段严格按注释分区（`<!-- OBSERVATORY: GEO -->` 等），逻辑文件按视图对齐。
- **你能否接受**：✅ 能，这是你选"模板内联"时已接受的取舍。

### 5.2 影响小 — 可忽略或远期再说

#### ⑤ 无 computed 语法糖
- **缺点**：Alpine 用方法代替计算属性，派生状态不如 Vue `computed` 顺（可写 `get foo() {}` 但略别扭）。
- **影响**：小。本项目派生状态不多。

#### ⑥ 生态/第三方组件薄
- **缺点**：Alpine 第三方组件远少于 Vue/React，复杂交互组件（虚拟列表/下拉树/富日历）要手写。
- **影响**：小。你已决定图表纯 SVG 手绘，不依赖组件库。未来要复杂组件时再评估。

#### ⑦ TS 支持弱
- **缺点**：`x-data` 是 JS 对象字面量，TS 无法类型检查模板指令（`x-for`/`x-show`）和 `@click="methodNmae"` 拼写。
- **影响**：**小（你纯 JS）**。若未来想上 TS，Alpine 模板无法类型检查，那时应切 Vue3 SFC + Volar。

#### ⑧ 调试体验弱
- **缺点**：Alpine DevTools 够看 `$data` 树，但比 Vue/React DevTools 简陋，复杂响应纠葛难调，无 time-travel/profiling。
- **影响**：小。个人工具规模可控，加 `console.log` + DevTools 够用。

#### ⑨ 扩展性天花板
- **缺点**：Proxy 反应在 >10k DOM 节点 / 数百组件时会慢于 Solid/Svelte 编译式反性。
- **影响**：小（远期）。你离这量级很远，真到了再迁。

#### ⑩ 无 tree-shaking / 打包优化
- **缺点**：全量 `alpine.min.js` ~15KB，你自己的 JS 不压缩、不 code-split。
- **影响**：小。本地工具无所谓首屏体积。

#### ⑪ 升级 breaking 风险
- **缺点**：Alpine 2→3 曾大改。v3 稳定多年，但小社区补丁周期慢。
- **影响**：小。锁版本即可。

#### ⑫ 测试支持弱
- **缺点**：组件单测不如 Vue/React Testing Library 顺。
- **影响**：小。Playwright E2E 够用。

---

## 六、综合接受判断

**Alpine 完全 hold 得住的条件**（本项目均满足）：
- 视图总数长期 <8 个
- 愿意守目录约定（一岛一文件 + 共享走 store）
- 图表坚持手绘不依赖组件库
- 纯 JS，无 TS 强需求
- 个人工具，无 SSR/SEO/大规模并发

**应重新评估、考虑切 Vue3+Vite 的信号**（任一触发）：
- 顶级视图预期 >10 个，或需要正式路由（带参数/嵌套/历史）
- 想上 TypeScript 并要模板类型检查
- 需要复杂交互组件库（数据网格/虚拟列表/富文本）
- Proxy 反应出现明显卡顿（>10k DOM 节点）
- 团队扩张，多人协作需强结构约束

---

## 七、落地约束（本轮重构必须遵守）

1. **新视图必须独立文件**：`views/<name>.js`，用 `Alpine.data('<name>Panel', () => ({...}))` 注册。
2. **共享态必须走 store**：api/toast/stats/SSE 进 `core/store.js` 的 `Alpine.store('app')`，禁止新视图自带副本。
3. **脚本顺序坑**：所有新 `<script>` 必须在 `<script defer src="alpine.min.js">` **之前**（Alpine defer 自动 start 会错过晚注册的 `alpine:init`）。
4. **模板内联但分区**：index.html 模板段按视图用注释严格分区，不拆 fetch 加载。
5. **CSS 按视图拆**：`styles/<view>.css` 多 `<link>`，旧 style.css 逐视图抽离。
6. **旧 `app()` 不动**：本轮不重写旧 4 视图，迁出留独立迭代。

---

## 八、参考资料

- Alpine.js 官方文档：https://alpinejs.dev
- `Alpine.data()` 组件注册：https://alpinejs.dev/globals/alpine-data
- `Alpine.store()` 全局状态：https://alpinejs.dev/globals/alpine-store
- 同处境参考：[tuna-os/corral 前端 ADR (2026-07)](https://github.com/tuna-os/corral/issues/52) — 单 1732 行 vanilla app.js、零构建、JSON API 客户端，结论同为 Alpine 岛式迁移
