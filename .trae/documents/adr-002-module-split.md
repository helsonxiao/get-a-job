# ADR-002 前后端功能域分治重构（组件岛迁移完成）

> **状态**：已采纳（2026-08-22）
> **决策者**：项目维护者
> **前置**：[ADR-001 前端框架选型](adr-001-frontend-framework.md)
> **结果**：模块地图见 [gaj/web/README.md](../../gaj/web/README.md)

---

## 一、背景

ADR-001 落地了「Alpine 组件岛」路线，但当时的过渡策略是 §7.6
「旧 `app()` 不动，迁出留独立迭代」。过渡期风险（ADR-001 §5.1②：
双轨共存变永久）已经成为现实负担：

- `app.js` 涨到 **1023 行**：职位/图鉴/配置/简历 4 个视图 + 详情 +
  公司抽屉全部挤在单个 `app()` 根组件里，~200 个方法扁平混排
- `index.html` **1926 行**模板依赖根作用域，改任何视图都要通读全局
- `app.py` **725 行**：除已迁出的 3 个 router 外，其余 20+ 端点
  （职位/规则/画像/简历/SSE）仍内联在组装层
- `style.css` **1980 行**单文件，与 `base.css` 存在整段重复的令牌定义

结论：执行 ADR-001 预留的「逐视图迁出」迭代，一次到位，消灭双轨。

## 二、决策

**前后端各按功能域分治，一个功能域 = 一个文件，行为零变化。**

### 2.1 后端：routes/ 全量拆分

`app.py` 从 725 行瘦身为 ~90 行纯组装层（app 工厂 / 生命周期 /
静态资源 / 启动入口），全部端点按域拆入 `routes/`：

| 模块 | 前缀 | 域 |
|---|---|---|
| `jobs.py` | `/api/jobs` | 职位列表/详情/收藏/忽略/调分/AI 打分/删除 |
| `scoring.py` | `/api` | 规则目录、打分参数覆盖、配比预设、批量打分 |
| `profile.py` | `/api/profile` | 画像读写、权重预设 |
| `resume.py` | `/api/resume` | 主简历读写 |
| `system.py` | `/api` | 统计/筛选项/任务/Provider/重建索引/SSE 日志流 |

- `routes/__init__.py` 提供 `ALL_ROUTERS` 聚合表，`app.py` 循环挂载
- SSE 日志桥（订阅队列 + SINK 回调）随 `/api/logs/stream` 一起进
  `system.py`，`app.py` 启动/关闭时调 `init_sse()/shutdown_sse()`
- 环境约束（Python 3.9）：路由签名注解用 `Optional[X]`，禁 `X | None`

### 2.2 前端：旧 `app()` 根组件删除，五岛齐备

| 岛 (`views/`) | 模板区段 | 职责 |
|---|---|---|
| `jobs.js` | `<!-- VIEW: JOBS -->` | 职位列表 + 详情（含人工调分/AI 重解析/分数轨迹） |
| `guide.js` | `<!-- VIEW: GUIDE -->` | 公司图鉴三榜/象限/对比 + **公司详情抽屉**（含内嵌岗位详情） |
| `config.js` | `<!-- VIEW: CONFIG -->` | 规则概览 + 画像编辑（含隐藏的 AI 矫正） |
| `resume.js` | `<!-- VIEW: RESUME -->` | 主简历编辑器 |
| `observatory.js` | `<!-- VIEW: OBSERVATORY -->` | 市场观察台（v0.3 已是岛，本次接入 store） |

配套决策（落实 ADR-001 §5.1③ 的缓解项）：

1. **视图路由单一真相源**：`$store.core.view` 取代 4 个布尔 flag
   （showGuide/showConfig/showResume/showObservatory）
2. **store 扩容为共享层**：api/toast/stats/tasks/SSE 之外，新增
   `taskRunning()` / `scoreAll()` / `openJob()` / `openCompany()` /
   `bootstrap()`（body `x-init` 引导，替代旧根组件 init）
3. **跨视图通信走 window 事件**：`gaj:open-job` / `gaj:open-company` /
   `gaj:refresh`（任务状态变化时 store 广播，各岛自刷新）——取代旧
   `$dispatch('obs-open-*')` + 根组件转发的模式
4. **公司抽屉移入 guide 岛**：fixed 定位不占布局；观察台跳公司现在
   会切到 guide 视图再开抽屉（旧行为是浮在观察台上，语义上
   「在主视图打开」本就该切视图）
5. **图鉴惰性加载保留**：`x-effect` 监听 `$store.core.view` 首次进入
   才拉 facets/items，其余视图启动即载（与旧根组件行为一致）

### 2.3 CSS 按视图抽离 + 去重

- `style.css` 1980 → ~300 行纯共享外壳（按钮/Header/徽章/日志/Toast/
  视图切换器 + 外壳响应式），头部与 `base.css` 整段重复的令牌/重置删除
- 新增 `styles/jobs.css` / `guide.css` / `config.css` / `resume.css`，
  与 `views/*.js` 一一对应
- 视图专属的响应式规则随视图文件走（避免共享层 media 规则被后加载的
  视图基础规则覆盖的级联顺序问题）
- 选择器全集校验：拆分前后 448 个选择器无丢失（仅去掉 base.css 重复项）

### 2.4 模板片段化（修订 ADR-001 §7.4）

> 2026-08-22 追加：CSS 拆分完成后 index.html 仍有 ~1950 行（五视图模板
> 全部内联），骨架与内容耦合的问题依旧。**修订 ADR-001 §7.4 的
> 「不拆 fetch 加载」约束**，改为骨架 + 片段方案。

- **index.html 1946 → ~200 行**，只保留：head 资源引用、Toast/Header/
  Log Panel 共享 UI、每视图一个**挂载点** div（含岛声明 x-data/x-show/
  事件 + `data-tpl="<name>"`）、模板 loader
- **模板拆为 `views/<name>.html` 独立片段**（纯内容，不含岛声明）——
  至此一视图三件套配对：`<name>.js`（逻辑）+ `<name>.html`（模板）+
  `styles/<name>.css`（样式），改某视图不再需要碰 index.html
- **加载时序**：本地 alpine.min.js（3.15.12 CDN 构建）已无
  `deferLoadingAlpine` 钩子，故不用该钩子，改用更通用的
  「**先注入后加载**」：loader（body 末尾同步脚本）fetch 全部片段
  innerHTML 注入挂载点 → 全部完成后才动态插入 alpine.min.js 的
  `<script>` 标签 → Alpine 扫描时全部视图 DOM 已就位
- **降级兜底**：任一片段 fetch 失败 → `#tpl-error` 显示错误且不启动
  Alpine，避免半初始化状态
- **代价**：alpine.min.js 的下载从「并行于 HTML 解析」退化为「片段
  fetch 完成后串行」（localhost 多一个 RTT，实测无感）；`?v=` 版本号
  需同时维护 head 引用与 loader 内 `VER` 常量两处

## 三、验证

- 后端：16 个 GET 端点拆分前后响应逐字节一致；404/SSE/详情正常
- 前端：浏览器冒烟 8 项 + 交互回归 5 项全部通过（五视图切换、公司抽屉、
  下钻抽屉、批量模式、跨视图「在主视图打开」双向链路），console 零报错
- 模板片段化（§2.4 追加）：主文件 + 5 片段 HTML 配平校验通过、关键
  锚点齐全；冒烟 8 项 + 交互回归 5 项复测全部通过，干净加载零报错
- 过程中修复一个迁移引入的回归：抽屉内 `closeCompany()` 先置空
  `companyJobDetail` 导致后续表达式读 null，已改为先取 id 再关闭

## 四、后果

- **正向**：一视图三件套后，改某视图只需读 `views/<name>.js` +
  `views/<name>.html` + `styles/<name>.css`，不碰 index.html；agent
  上下文占用从「通读 1023 行 app.js + 1946 行 index.html」降为
  「读单视图 ~300-500 行三件套」；后端同理
- **负向/成本**：window 事件协议是软契约，事件名拼错不会报错，只能靠
  运行时验证；store 仍可能被塞进本应私有的状态，靠 README 契约约束；
  模板 loader 的「先注入后加载」时序是隐式约定，若有人把 alpine.min.js
  改回 defer 标签引用会导致岛扫描不到片段内容（README 已注明）
- **后续**：新增视图严格按 `gaj/web/README.md` 的步骤走；若顶级视图
  超过 8 个（ADR-001 的上限信号），重新评估路由方案
