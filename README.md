# 坑位图鉴（GAJ）

> 自动浏览 BOSS直聘，基于简历和偏好打分，持续优化求职策略。

<p align="center">
  <a href="https://helsonxiao.github.io/get-a-job/">在线介绍页</a>
</p>

一个跑在本机的个人猎头系统：CDP 驱动真实 Chrome 采集 BOSS直聘职位，规则引擎 + 网页版大模型打分，数据全本地存储，零云端依赖。

> **当前状态：v0.1 早期原型，尚未发布正式版。**
> AI 打分目前仅对 **DeepSeek 网页版**有较好的支持，其它网页版大模型
> driver（doubao / tongyi / kimi）有待测试与改进。
> **简历优化为实验性功能**，尚未充分测试与优化，欢迎贡献改进。

- **开发者** 用 CLI + Web 工作台
- **小白** 用 Skill（自然语言驱动，Agent Loop 自我进化）

## 核心能力

- **增量采集** — CDP 驱动 Chrome 复用已登录会话，模拟自然浏览节奏，连续重复页自动提前结束
- **规则打分** — 基于个人画像（薪资/城市/技术栈/价值观权重）的本地规则引擎，四维评分 + 硬性淘汰
- **AI 打分** — 驱动网页版大模型（DeepSeek / 豆包 / 通义 / Kimi）做深度分析，不消耗 API 额度
- **Web 工作台** — FastAPI 看板，职位列表 / 详情 / 调分 / 忽略，支持热重载，代码开放可用任意 Agent 自定义
- **Agent 接口** — `python3 -m gaj agent` 统一 JSON CLI，智能体可直接 `json.loads` 决策

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动 Chrome CDP 调试模式（端口 9222）
python3 -m gaj setup-chrome

# 3. 在弹出的 Chrome 里登录 zhipin.com 和你要用的网页版大模型

# 4. 填写个人画像与主简历
#    data/profile.md        ← 个人画像（参考 templates/profile.md）
#    data/resumes/master.md ← 主简历

# 5. 采集 + 打分
python3 -m gaj crawl "<BOSS直聘筛选页URL>"
python3 -m gaj score --all

# 6. 启动 Web 看板
python3 -m gaj web
# → http://127.0.0.1:8765
```

> 完整产品介绍与截图见 [docs/index.html](docs/index.html)，命令详解见 [AGENT.md](AGENT.md)。

## 项目结构

```
boss_scraper/   # CDP 采集核心（复用的成熟爬虫）
gaj/
  ├── core/      # 规则引擎、归一化、画像、打分配置
  ├── ai/        # AI 打分（prompts / parser / runner）
  ├── browser/   # 网页版大模型驱动（CDP 注入 + 轮询）
  ├── scraper/   # 采集适配层 + 覆盖率状态
  ├── store/     # 文件型存储 + SQLite 索引（可重建）
  ├── web/       # FastAPI 工作台
  ├── resume/    # 针对性简历生成（实验性）
  └── agent/     # 面向智能体的 JSON CLI
gaj-agent/       # Skill 包（可安装到各智能体）
references/      # 打分规则 / JD 字段 / AI 触发条件
templates/       # profile.md 模板
```

## 技术栈

Python · Chrome CDP · FastAPI · SQLite（仅作派生索引，文件是真相源）

## 隐私

所有个人数据（简历、画像、职位、索引）都在 `data/` 目录下，已被 `.gitignore` 排除，永不离开本机。

## 免责声明

> 本工具仅限个人学习与研究使用，禁止用于任何违反相关网站用户准则的商业用途。使用者需自行承担因不当使用带来的全部风险与责任，与本项目作者无关。

## 贡献

欢迎 Issue 和 PR。如果你正在找工作，希望这个工具能帮到你。

目前最缺人的方向：**简历优化模块**（`gaj/resume/`）——功能尚为实验性，
提示词、生成质量与对比展示都未经充分打磨，欢迎帮忙测试、提改进。

## 联系作者

觉得好用？想交流求职 / 技术经验？欢迎加我微信，当然也可以打赏一杯咖啡，支持项目持续维护：

![微信二维码](docs/assets/wechat-qr.jpg)

## License

MIT
