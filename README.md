# 坑位图鉴（GAJ）

本机的就业市场分析工具，做两件事：**分析市场**、**规划自己**。数据全本地存储，无云端依赖。

[在线介绍页](https://helsonxiao.github.io/get-a-job/) · 版本与升级说明见 [CHANGELOG.md](CHANGELOG.md)。

已知限制：

- AI 打分目前只对 DeepSeek 网页版支持较好，doubao / tongyi / kimi driver 待测试改进。
- 简历优化为实验性功能，尚未充分测试。

两种用法：开发者用 CLI + Web 图鉴；不想碰命令的，把 `gaj-agent/` 装成 Skill 用自然语言驱动。

## 核心能力

### 就业市场分析

- **薪资定价** — 按分位 / 经验 / 学历 / 行业 / 融资阶段切分，统一取中位数（均值会被少数极高薪岗位拉偏）
- **技能需求与溢价** — 需求量、薪资中位、相对溢价，标注样本量；低样本显式提示
- **行业与区域结构** — 行业卡片墙（薪资结构 / 技能 Top / 区域分布 / 代表公司）+ 区域热力图，图表可下钻到具体岗位
- **公司图鉴** — 分数榜 / 招聘力度榜 / 薪资榜三榜，象限图看分布，四维雷达做对比
- **快照与历史对比** — 取数按纪元隔离，导出即固化为不可变快照，带数据指纹与 run_id，同口径可跨月 / 跨季度 diff
- **红旗信号** — 市场里的可疑信号集中摊开，可下钻查看具体岗位

### 个人提升规划

- **双层打分引擎** — 规则层：4 维度（财务 / 成长 / 资源 / WLB）× 10 项评分，权重取自你的画像，10 条硬性淘汰按证据置信度判定（< 60% 只标 REVIEW 不误杀）；AI 层：只对规则标记的岗位调用网页版大模型（DeepSeek / 豆包 / 通义 / Kimi），不消耗 API 额度
- **个人策略生成** — 读画像输出技能缺口 / 谈薪锚定 / 投递优先级 / 避雷 / 行动清单；纯规则计算，同输入必得同输出
- **预期校准与反馈** — 打分数据反向指导策略调整，画像权重可切换 5 套预设，AI 规则矫正需人工复核后才应用
- **针对性简历优化**（实验性）— JD 全文 + 主简历，optimize / rewrite 两种风格，不造假不夸大

### 数据来源（取数层）

- **本地增量取数** — CDP 驱动你自己的 Chrome，复用已登录会话，模拟自然翻页节奏，连续 3 页全重复即提前结束，中断可原地续跑
- **只取公开信息** — 抓岗位本身（标题 / 薪资 / JD / 公司公开信息），不抓求职者个人信息与联系方式
- **Web 图鉴 + Agent 接口** — FastAPI 应用承载上述全部视图（职位 / 公司 / 市场 / 行业，图表下钻 + 快照切换，支持热重载）；`python3 -m gaj agent` 输出统一 JSON，智能体可直接消费

## 明确不做的事

| 不做 | 为什么 |
|---|---|
| 自动投递 / 批量投递 | 投递用你的信用背书，批量投会摊薄它，也会丢掉「哪类岗位真有回应」这个信号 |
| 预测收益 | 技能溢价是相关关系不是因果关系，用溢价算收益会系统性高估。只反推回本门槛：要回本需要多大的兑现概率 |
| 推荐具体课程 | 数据支撑不了「哪门课更好」，只算你打算花多少钱、多少小时 |
| 采集个人隐私 | 只取公开招聘信息；画像与简历只在本机参与计算，不出网、不落远端库 |

## 快速开始（开发者 / CLI）

命令都在仓库根目录执行。前置条件：本机有 Chrome 和 Python 3，有 BOSS 直聘账号（采集要复用登录态）。

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 启动 Chrome CDP 调试模式

```bash
python3 -m gaj setup-chrome
# 在弹出的 Chrome 里登录 zhipin.com，以及要用的网页版大模型（如 chat.deepseek.com）
python3 -m gaj check          # 校验环境 → ✓ Chrome CDP 就绪
```

### 3. 填个人画像与主简历

```bash
cp templates/profile.md data/profile.md      # 画像
cp 你的简历.md data/resumes/master.md         # 主简历
```

`templates/profile.md` 里的 `{...}` 全是占位符，必须连同花括号一起替换成真实值：

```diff
- 期望最低年薪（万元）: {数字}
+ 期望最低年薪（万元）: 35
- 当前城市: {城市名}
+ 当前城市: 苏州
- 接受出差: {是/否}
+ 接受出差: 否
```

花括号留着不报错，但会被当成值解析：`当前城市: {城市名}` 会让 H-01 城市硬性规则误杀岗位，`接受出差: {是/否}` 会退化成默认开关，这是「规则打分结果不对」最常见的原因。也可以在 `python3 -m gaj web` 的「配置」Tab 里填，由界面写回文件。

### 4. 取数

在 BOSS 直聘网页版筛好城市 / 关键词 / 薪资，复制地址栏完整 URL 发起采集：

```bash
python3 -m gaj crawl "https://www.zhipin.com/web/geek/job?query=前端&city=苏州"
```

采集默认自动打规则分。单口径完整采集常见数小时，前台会占住终端，推荐后台分块跑：

```bash
python3 -m gaj agent crawl --url "<你的URL>" --max-pages 10 --background
python3 -m gaj agent crawl-status --wait 480   # 查进度；中断可原地续跑
```

### 5. 打分 + 启动 Web 图鉴

```bash
python3 -m gaj score --all   # 规则打分，纯本地，不调用大模型
python3 -m gaj web           # → http://127.0.0.1:8765
```

AI 深度分析（走网页版大模型，不消耗 API 额度）：`python3 -m gaj ai-score --job <job_id> --deep`。命令细节见 [AGENT.md](AGENT.md)。

## Roadmap

个人规划这条线目前只到「对照出差距」，下一步是「排优先级」和「算账」：

- **学习路径排序** — 技能缺口现在按需求量取 Top，结果常是你已掌握或最卷的那几个。改为按「需求量 × 溢价 × 邻近度」排序，邻近度衡量技能离你现有技术栈多远，它才是学习成本的主因
- **投入算账** — 不预测收益，只反推回本门槛与所需兑现概率：学费 + 时间成本共多少，要多大的把握才不亏

方案见 [docs/career-path-plan.md](docs/career-path-plan.md)，排期见 [BOARD.md](BOARD.md)。两项均未开工。

## 项目结构

```
boss_scraper/   # CDP 取数核心（复用的成熟爬虫）
gaj/
  ├── core/      # 规则引擎、归一化、画像、打分配置
  ├── ai/        # AI 打分（prompts / parser / runner）
  ├── browser/   # 网页版大模型驱动（CDP 注入 + 轮询）
  ├── scraper/   # 取数适配层 + 覆盖率状态
  ├── store/     # 文件型存储 + SQLite 索引（可重建）
  ├── web/       # FastAPI Web 图鉴
  ├── resume/    # 针对性简历生成（实验性）
  └── agent/     # 面向智能体的 JSON CLI
gaj-agent/       # Skill 包（可安装到各智能体）
landing/         # 公网站点源码（index.html + assets，CI 自动发布到 gh-pages）
docs/            # 工程/设计文档唯一家（adr/ proposals/ archive/YYYY-MM）
references/      # 打分规则 / JD 字段 / AI 触发条件
templates/       # profile.md 模板
```

## 技术栈

Python · Chrome CDP · FastAPI · SQLite（仅作派生索引，文件是真相源）

## 隐私

个人数据（简历、画像、职位、索引）都在 `data/` 下，已被 `.gitignore` 排除，永不离开本机。

## 免责声明

> 本工具仅限个人学习与研究使用，禁止用于任何违反相关网站用户准则的商业用途。使用者需自行承担因不当使用带来的全部风险与责任，与本项目作者无关。

## 贡献

欢迎 Issue 和 PR。目前最缺人的方向是**简历优化模块**（`gaj/resume/`）：提示词、生成质量与对比展示都未经充分打磨，欢迎帮忙测试和改进。

## 联系作者

交流求职 / 技术经验，或打赏支持项目维护：

![微信二维码](landing/assets/wechat-qr.jpg)

## License

MIT
