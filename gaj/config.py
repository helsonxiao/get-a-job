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

    headless_note: str = "必须使用已登录的可见 Chrome, 网页版大模型依赖登录态"


@dataclass
class Settings:
    crawl: CrawlConfig = field(default_factory=CrawlConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    ai: AIConfig = field(default_factory=AIConfig)


SETTINGS = Settings()
