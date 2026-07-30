"""个人职业画像解析 —— 读 data/profile.md 转成结构化对象。

profile.md 用 "- 键: 值" 的行式格式, 方便你随手改。这里做宽松解析:
  - 全角/半角冒号都认
  - 键名里的括号单位 (万元)/(%) 会被剥掉再匹配
  - 多选值支持中英文逗号、顿号分隔
  - 布尔值认 是/否/true/false/y/n
缺失的键一律走默认值, 不会因为你少填一行就崩掉。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import PROFILE_PATH
from ..logging_setup import get_logger

log = get_logger("profile")

_LINE_RE = re.compile(r"^\s*[-*]\s*(?P<key>[^:：]+)[:：]\s*(?P<value>.*)$")
_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")
_SPLIT_RE = re.compile(r"[,，、;；]+")

_TRUE = {"是", "true", "yes", "y", "1", "接受"}
_FALSE = {"否", "false", "no", "n", "0", "不接受"}


def _norm_key(key: str) -> str:
    return _PAREN_RE.sub("", key).strip()


def _to_float(value: str) -> float | None:
    if not value:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
    return float(m.group(0)) if m else None


def _to_int(value: str) -> int | None:
    f = _to_float(value)
    return int(f) if f is not None else None


def _to_bool(value: str, default: bool = False) -> bool:
    v = (value or "").strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return default


def _to_list(value: str) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in _SPLIT_RE.split(value) if p.strip()]


@dataclass
class Profile:
    # 基础
    age: int | None = None
    gender: str = ""
    total_years: float | None = None
    current_salary_10k: float | None = None

    # 薪资预期
    hard_min_salary_10k: float = 30.0
    expect_min_salary_10k: float = 35.0
    expect_max_salary_10k: float = 50.0

    # 地理
    current_city: str = ""
    acceptable_cities: list[str] = field(default_factory=list)
    max_commute_minutes: int | None = None
    #: 居住地坐标 (后续用于计算通勤时间)
    home_lng: float | None = None
    home_lat: float | None = None

    # 学历与技能
    education: str = ""
    skills: list[str] = field(default_factory=list)
    industry_tags: list[str] = field(default_factory=list)

    # 排除项
    reject_industries: list[str] = field(default_factory=list)
    reject_work_modes: list[str] = field(default_factory=list)
    blacklist_keywords: list[str] = field(default_factory=list)
    reject_scale_below: int = 20

    # 价值观权重 (原始百分数)
    weight_growth: float = 30.0
    weight_finance: float = 30.0
    weight_wlb: float = 30.0
    weight_resource: float = 10.0

    # 软性偏好
    preferred_stages: list[str] = field(default_factory=list)
    preferred_directions: list[str] = field(default_factory=list)
    team_size_min: int | None = None
    team_size_max: int | None = None
    tech_culture: list[str] = field(default_factory=list)
    max_weekly_hours: float | None = None
    latest_offwork_hour: int | None = None
    valued_benefits: list[str] = field(default_factory=list)

    # 硬性开关
    accept_travel: bool = False
    accept_weekend_shift: bool = True
    accept_stack_change: bool = True
    accept_outsourcing: bool = False
    accept_relocation: bool = False

    raw: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    # ---- 派生属性 ----

    @property
    def expect_mid_salary_10k(self) -> float:
        return round((self.expect_min_salary_10k + self.expect_max_salary_10k) / 2, 2)

    @property
    def salary_surprise_threshold_10k(self) -> float:
        """"惊喜"上限阈值: 期望上限的 1.2 倍。"""
        return round(self.expect_max_salary_10k * 1.2, 2)

    @property
    def normalized_weights(self) -> dict[str, float]:
        """把价值观权重归一化成和为 1 的小数。"""
        raw = {
            "finance": self.weight_finance,
            "growth": self.weight_growth,
            "resource": self.weight_resource,
            "wlb": self.weight_wlb,
        }
        total = sum(v for v in raw.values() if v)
        if not total:
            return {"finance": 0.4, "growth": 0.3, "resource": 0.2, "wlb": 0.1}
        return {k: round(v / total, 4) for k, v in raw.items()}

    def all_acceptable_cities(self) -> list[str]:
        """当前城市一定是可接受的, 合并去重。"""
        cities = list(self.acceptable_cities)
        if self.current_city and self.current_city not in cities:
            cities.insert(0, self.current_city)
        return cities

    def summary_for_ai(self) -> str:
        """给大模型看的画像摘要, 控制在几百字以内。"""
        lines = [
            f"- 工作年限: {self.total_years or '未填'} 年, 学历: {self.education or '未填'}",
            f"- 当前年薪: {self.current_salary_10k or '未填'} 万; "
            f"期望区间: {self.expect_min_salary_10k}~{self.expect_max_salary_10k} 万, "
            f"低于 {self.hard_min_salary_10k} 万直接不考虑",
            f"- 现居 {self.current_city}, 可接受城市: {', '.join(self.all_acceptable_cities()) or '未填'}; "
            f"不接受异地搬迁: {'是' if not self.accept_relocation else '否'}",
            f"- 技术栈: {', '.join(self.skills) or '未填'}",
            f"- 行业经验: {', '.join(self.industry_tags) or '未填'}",
            f"- 偏好业务方向: {', '.join(self.preferred_directions) or '未填'}",
            f"- 偏好公司阶段: {', '.join(self.preferred_stages) or '未填'}",
            f"- 看重的福利: {', '.join(self.valued_benefits) or '未填'}",
            f"- 价值观权重(归一化): 财务 {self.normalized_weights['finance']}, "
            f"成长 {self.normalized_weights['growth']}, "
            f"资源 {self.normalized_weights['resource']}, "
            f"生活平衡 {self.normalized_weights['wlb']}",
            f"- 硬性红线: 不接受出差={not self.accept_travel}, "
            f"不接受外包={not self.accept_outsourcing}, "
            f"排除关键词={', '.join(self.blacklist_keywords) or '无'}",
            f"- 可接受每周工时上限: {self.max_weekly_hours or '未填'} 小时, "
            f"可接受最晚下班: {self.latest_offwork_hour or '未填'} 点",
        ]
        return "\n".join(lines)


# 文件里的键名 -> Profile 字段名
_KEY_MAP: dict[str, str] = {
    "年龄": "age",
    "性别": "gender",
    "工作总年限": "total_years",
    "当前年薪": "current_salary_10k",
    "硬性最低可接受年薪": "hard_min_salary_10k",
    "期望最低年薪": "expect_min_salary_10k",
    "期望最高年薪": "expect_max_salary_10k",
    "单程通勤上限": "max_commute_minutes",
    "居住地经度": "home_lng",
    "居住地纬度": "home_lat",
    "最高学历": "education",
    "当前城市": "current_city",
    "可接受城市": "acceptable_cities",
    "拒绝行业": "reject_industries",
    "拒绝工作模式": "reject_work_modes",
    "拒绝公司规模下限": "reject_scale_below",
    "技能关键词列表": "skills",
    "行业经验标签": "industry_tags",
    "职业成长权重": "weight_growth",
    "财务回报权重": "weight_finance",
    "工作生活平衡权重": "weight_wlb",
    "平台资源权重": "weight_resource",
    "偏好公司阶段": "preferred_stages",
    "偏好业务方向": "preferred_directions",
    "偏好团队规模最小": "team_size_min",
    "偏好团队规模最大": "team_size_max",
    "偏好技术氛围": "tech_culture",
    "可接受每周工作时长": "max_weekly_hours",
    "可接受最晚下班时间": "latest_offwork_hour",
    "看重的福利": "valued_benefits",
    "接受出差": "accept_travel",
    "接受周末调休": "accept_weekend_shift",
    "接受更换技术栈": "accept_stack_change",
    "接受外包岗位": "accept_outsourcing",
    "接受异地搬迁": "accept_relocation",
    "排除关键词": "blacklist_keywords",
}

_INT_FIELDS = {"age", "max_commute_minutes", "team_size_min", "team_size_max",
               "latest_offwork_hour", "reject_scale_below"}
_FLOAT_FIELDS = {"total_years", "current_salary_10k", "hard_min_salary_10k",
                 "expect_min_salary_10k", "expect_max_salary_10k",
                 "weight_growth", "weight_finance", "weight_wlb", "weight_resource",
                 "max_weekly_hours", "home_lng", "home_lat"}
_LIST_FIELDS = {"acceptable_cities", "reject_industries", "reject_work_modes",
                "skills", "industry_tags", "preferred_stages", "preferred_directions",
                "tech_culture", "valued_benefits", "blacklist_keywords"}
_BOOL_FIELDS = {"accept_travel", "accept_weekend_shift", "accept_stack_change",
                "accept_outsourcing", "accept_relocation"}


def parse_profile_text(text: str) -> Profile:
    profile = Profile()
    raw: dict[str, str] = {}

    # "偏好团队规模（最小）" 这类键, 括号内容其实是键的一部分, 单独处理
    special = {
        "偏好团队规模（最小）": "team_size_min",
        "偏好团队规模(最小)": "team_size_min",
        "偏好团队规模（最大）": "team_size_max",
        "偏好团队规模(最大)": "team_size_max",
    }

    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        key_raw = m.group("key").strip()
        value = m.group("value").strip()
        raw[key_raw] = value

        field_name = special.get(key_raw) or _KEY_MAP.get(_norm_key(key_raw))
        if not field_name:
            continue

        if field_name in _LIST_FIELDS:
            setattr(profile, field_name, _to_list(value))
        elif field_name in _BOOL_FIELDS:
            setattr(profile, field_name, _to_bool(value, getattr(profile, field_name)))
        elif field_name in _INT_FIELDS:
            v = _to_int(value)
            if v is not None:
                setattr(profile, field_name, v)
        elif field_name in _FLOAT_FIELDS:
            v = _to_float(value)
            if v is not None:
                setattr(profile, field_name, v)
        else:
            if value:
                setattr(profile, field_name, value)

    profile.raw = raw

    # 没显式填硬性下限时, 用期望下限的 85% 兜底
    if "硬性最低可接受年薪" not in {_norm_key(k) for k in raw}:
        profile.hard_min_salary_10k = round(profile.expect_min_salary_10k * 0.85, 1)

    return profile


def load_profile(path: Path | None = None) -> Profile:
    path = Path(path or PROFILE_PATH)
    if not path.exists():
        log.warning(f"未找到画像文件 {path}, 使用默认画像。请先填写 data/profile.md")
        return Profile()
    profile = parse_profile_text(path.read_text(encoding="utf-8"))
    log.debug(
        f"画像已加载: {profile.current_city} / "
        f"{profile.expect_min_salary_10k}-{profile.expect_max_salary_10k}万 / "
        f"{len(profile.skills)} 项技能"
    )
    return profile


# ---------------------------------------------------------------- 序列化回文件

#: 字段名 -> 文件里的键名 (按 profile.md 分组顺序排列)
_SERIALIZE_GROUPS: list[tuple[str, list[tuple[str, str, str]]]] = [
    ("基础数值", [
        ("age", "年龄", "int"),
        ("gender", "性别", "str"),
        ("total_years", "工作总年限", "float"),
        ("current_salary_10k", "当前年薪（万元）", "float"),
        ("hard_min_salary_10k", "硬性最低可接受年薪（万元）", "float"),
        ("expect_min_salary_10k", "期望最低年薪（万元）", "float"),
        ("max_commute_minutes", "单程通勤上限（分钟）", "int"),
    ]),
    ("居住地坐标（用于通勤计算，经纬度小数）", [
        ("home_lng", "居住地经度", "float"),
        ("home_lat", "居住地纬度", "float"),
    ]),
    ("枚举与分类（多选用英文逗号分隔）", [
        ("education", "最高学历", "str"),
        ("current_city", "当前城市", "str"),
        ("acceptable_cities", "可接受城市", "list"),
        ("reject_industries", "拒绝行业", "list"),
        ("reject_work_modes", "拒绝工作模式", "list"),
        ("reject_scale_below", "拒绝公司规模下限", "int"),
        ("skills", "技能关键词列表", "list"),
        ("industry_tags", "行业经验标签", "list"),
    ]),
    ("价值观权重（数字，总和100）", [
        ("weight_growth", "职业成长权重", "float"),
        ("weight_finance", "财务回报权重", "float"),
        ("weight_wlb", "工作生活平衡权重", "float"),
        ("weight_resource", "平台资源权重", "float"),
    ]),
    ("软性偏好（多选用英文逗号分隔）", [
        ("preferred_stages", "偏好公司阶段", "list"),
        ("preferred_directions", "偏好业务方向", "list"),
        ("team_size_min", "偏好团队规模（最小）", "int"),
        ("team_size_max", "偏好团队规模（最大）", "int"),
        ("tech_culture", "偏好技术氛围", "list"),
        ("max_weekly_hours", "可接受每周工作时长（小时）", "float"),
        ("latest_offwork_hour", "可接受最晚下班时间（点）", "int"),
        ("valued_benefits", "看重的福利", "list"),
    ]),
    ("硬性开关（是/否）", [
        ("accept_travel", "接受出差", "bool"),
        ("accept_weekend_shift", "接受周末调休", "bool"),
        ("accept_stack_change", "接受更换技术栈", "bool"),
        ("accept_outsourcing", "接受外包岗位", "bool"),
        ("accept_relocation", "接受异地搬迁", "bool"),
    ]),
    ("硬性负面排除关键词（脚本黑名单，匹配即淘汰）", [
        ("blacklist_keywords", "排除关键词", "list"),
    ]),
]


def _fmt_value(value: Any, vtype: str) -> str:
    if value is None:
        return ""
    if vtype == "list":
        return ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
    if vtype == "bool":
        return "是" if value else "否"
    if vtype == "float":
        try:
            f = float(value)
            return str(int(f)) if f == int(f) else str(f)
        except (TypeError, ValueError):
            return str(value) if value else ""
    if vtype == "int":
        try:
            return str(int(value))
        except (TypeError, ValueError):
            return str(value) if value else ""
    return str(value) if value else ""


def serialize_profile(fields: dict[str, Any]) -> str:
    """把字段字典序列化回 profile.md 格式文本。

    保留 profile.md 的分组结构和注释, 方便人眼阅读。
    未提供的字段写空值, 不会丢行。
    """
    lines = [
        "# 个人职业画像（结构化数据）",
        "",
        '## 说明：本文件键值对供脚本自动解析，多选用英文逗号分隔，布尔值用"是/否"。',
        "",
    ]
    for group_title, field_list in _SERIALIZE_GROUPS:
        lines.append(f"### {group_title}")
        for field_name, key_label, vtype in field_list:
            value = fields.get(field_name)
            lines.append(f"- {key_label}: {_fmt_value(value, vtype)}")
        lines.append("")
    return "\n".join(lines)


def save_profile(fields: dict[str, Any], path: Path | None = None) -> Path:
    """把字段字典写回 profile.md。

    Args:
        fields: 字段名 -> 值 (与 Profile 的字段名一致)
        path: 目标路径, 默认 data/profile.md

    Returns:
        写入的文件路径
    """
    from ..store.repo import write_text

    path = Path(path or PROFILE_PATH)
    text = serialize_profile(fields)
    write_text(path, text)
    log.info(f"画像已保存到 {path}")
    return path
