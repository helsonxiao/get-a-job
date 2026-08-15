"""全局配置与路径约定。

设计原则:
  - 文件是真相源, SQLite 只是派生索引, 删掉 index.db 随时可以从文件重建。
  - 所有路径集中在这里定义, 其它模块不硬编码目录名。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 根路径

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_ROOT = Path(os.environ.get("GAJ_DATA_ROOT", PROJECT_ROOT / "data"))

JOBS_DIR = DATA_ROOT / "jobs"
COMPANIES_DIR = DATA_ROOT / "companies"
SESSIONS_DIR = DATA_ROOT / "sessions"
RESUMES_DIR = DATA_ROOT / "resumes"
TAILORED_DIR = RESUMES_DIR / "tailored"
RAW_DIR = DATA_ROOT / "_raw"
INDEX_DB = DATA_ROOT / "index.db"

REFERENCES_DIR = PROJECT_ROOT / "references"
# 个人资料放在 data/ 下, 整个 data/ 已被 .gitignore 忽略, 避免隐私外泄
PROFILE_PATH = DATA_ROOT / "profile.md"
LOGS_DIR = PROJECT_ROOT / "logs"

LEGACY_JOBS_DIR = PROJECT_ROOT / "jobs"

# 每个职位目录下的固定文件名
JOB_FILE = "job.json"
JOB_RAW_LIST_FILE = "raw_list_item.json"
JOB_RAW_JD_FILE = "raw_jd_dom.json"
JOB_JD_TEXT = "jd.md"
JOB_SCORES_DIR = "scores"
RULE_SCORE_FILE = "rule.json"

COMPANY_FILE = "company.json"
COMPANY_INTRO_FILE = "intro.md"
COMPANY_RAW_FILE = "raw_company_dom.json"
# 公司级 AI 评价落盘目录 (与岗位侧 scores/ 对称, append-only 历史)
COMPANY_SCORES_DIR = "scores"

MASTER_RESUME = "master.md"


def ensure_dirs() -> None:
    """确保所有数据目录存在。"""
    for p in (
        DATA_ROOT,
        JOBS_DIR,
        COMPANIES_DIR,
        SESSIONS_DIR,
        RESUMES_DIR,
        TAILORED_DIR,
        RAW_DIR,
        LOGS_DIR,
    ):
        p.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 采集配置


@dataclass
class CrawlConfig:
    """采集行为配置。

    默认值偏保守 —— 这个系统是给一个人长期用的, 账号被封的代价远大于慢一点。
    """

    cdp_port: int = 9222

    # 单次会话上限
    max_pages: int | None = None
    max_jobs_per_session: int = 60

    # 翻页间隔 (秒)
    page_delay_min: float = 4.0
    page_delay_max: float = 11.0

    # 职位详情页之间的间隔 (秒)
    job_delay_min: float = 5.0
    job_delay_max: float = 14.0

    # 每抓 N 个职位后强制长休息
    long_rest_every: int = 12
    long_rest_min: float = 45.0
    long_rest_max: float = 120.0

    # 公司页缓存有效期 (天) —— 同一家公司在此期间内不重复抓
    company_cache_days: int = 30

    fetch_company: bool = True
    # 已存在的职位是否重新抓取详情页 (False 时只更新 last_seen)
    refresh_existing: bool = False
    # 已采集职位在多少小时内跳过不重抓 (单个岗位 JD 短期内不会频繁改, 设长一点减少重复抓取)
    # 60 天 = 1440 小时
    skip_recent_hours: float = 1440.0

    # ---- 覆盖率自适应限速 (防止职位都抓过后高频调列表 API 被反爬拦截) ----
    # 整页全是已抓取职位时, 渐进加大翻页延迟 (2x/4x/8x...)
    dup_slowdown: bool = True
    # 连续多少页全重复即判定搜索结果已覆盖, 提前结束本次采集 (0=禁用)
    dup_stop_pages: int = 3
    # 降速后翻页延迟上限 (秒)
    slowdown_cap: float = 60.0
    # 上次采集覆盖率 >= 0.9 时, 本次基础翻页延迟放大倍数
    coverage_slowdown_factor: float = 2.0

    debug: bool = False


# ---------------------------------------------------------------- 打分配置


@dataclass
class ScoringConfig:
    """打分权重配置。

    注意: references/scoring_rules.md 写的是 40/30/20/10, 而 profile.md 里
    用户自己填的价值观权重是 30/30/30/10。两者冲突。
    这里默认以 profile.md 为准 (用户的真实意愿), 但允许显式覆盖。
    """

    use_profile_weights: bool = True

    # 仅在 use_profile_weights=False 时生效, 对应 scoring_rules.md v3
    finance: float = 0.40
    growth: float = 0.30
    resource: float = 0.20
    wlb: float = 0.10

    def as_dict(self) -> dict[str, float]:
        return {
            "finance": self.finance,
            "growth": self.growth,
            "resource": self.resource,
            "wlb": self.wlb,
        }


# ---------------------------------------------------------------- AI 配置


@dataclass
class AIConfig:
    """网页版大模型驱动配置。"""

    provider: str = "deepseek"

    # 等待模型生成完成的判定参数
    generation_timeout: float = 300.0
    poll_interval: float = 1.5
    # 文本连续多少次轮询无变化视为生成结束
    stable_rounds: int = 4

    # 输入框注入后、点击发送前的停顿
    pre_send_pause_min: float = 0.8
    pre_send_pause_max: float = 2.0

    # 两次提问之间的间隔
    between_calls_min: float = 6.0
    between_calls_max: float = 15.0

    # 每次打分是否新开一个对话 (避免上下文串味)
    fresh_conversation: bool = True

    # 标签页焦点策略:
    #   foreground —— 提问期间把聊天标签页切到前台, 完成后自动恢复原来聚焦的
    #                 标签页。后台标签页可能被浏览器节流导致页面无响应, 这是默认值。
    #   background —— 全程保持后台, 不打扰用户; 但若 no_response_watchdog 秒内
    #                 收不到任何回复, 会自动切前台救援。
    tab_mode: str = "foreground"

    # 无响应看门狗 (秒): 发送后这么久仍没有任何回复文本时, 触发一次救援
    # (切前台 + 若输入框未清空则重新点发送)。前台模式下同样生效, 兜底发送失败。
    no_response_watchdog: float = 30.0

    # ---- 打分 backlog (补分/重打分调度, 见 gaj/ai/backlog.py) ----
    # AI 分数保鲜期 (天): 超过即视为过期, 进入重打分队列。
    # 打分是低频行为, 定太短会制造还不完的过期债, 所以默认给长。
    stale_ttl_days: float = 90.0
    # 重打冷却 (小时): 同一岗位冷却期内不重复进重打队列
    rescore_cooldown_hours: float = 72.0

    # 公司级评价保鲜期 (天): 公司信息变化慢, 大半年不重新评价也没问题
    company_score_ttl_days: float = 180.0

    headless_note: str = "必须使用已登录的可见 Chrome, 网页版大模型依赖登录态"


@dataclass
class GuideConfig:
    """公司图鉴 (公司聚合分) 配置。

    company_score = 头部加权均值 + 活跃度修正 + 信息完整度加成,
    公式结构锁定在 design/图鉴进化方案.md, 这里只放可调系数。
    """

    # 等级徽章阈值 (S/A/B/C, 降序), company_score >= 阈值 即该档
    rank_tiers: tuple[float, float, float] = (8.5, 7.0, 5.5)

    # 头部加权: 取公司名下岗位 best_total 前 3 个, 权重 0.5/0.3/0.2
    # (不足 3 个时按实际个数归一化)
    head_weights: tuple[float, float, float] = (0.5, 0.3, 0.2)

    # ---- 活跃度修正 (整体夹在 ±activity_cap 内) ----
    # 在线岗位占比偏离 50% 的部分 × 该系数
    online_ratio_factor: float = 0.4
    # 最近 last_seen 新鲜度: ≤14 天 +fresh_bonus_high, ≤45 天 +fresh_bonus_low,
    # 更久 -fresh_penalty
    fresh_window_high_days: float = 14.0
    fresh_window_low_days: float = 45.0
    fresh_bonus_high: float = 0.3
    fresh_bonus_low: float = 0.1
    fresh_penalty: float = 0.2
    activity_cap: float = 0.5

    # ---- 信息完整度加成 ----
    # 有公司简介 / 经营范围 各加一份, 激励图鉴"解锁"
    info_bonus_each: float = 0.15
    info_bonus_cap: float = 0.3


@dataclass
class Settings:
    crawl: CrawlConfig = field(default_factory=CrawlConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    guide: GuideConfig = field(default_factory=GuideConfig)


SETTINGS = Settings()
