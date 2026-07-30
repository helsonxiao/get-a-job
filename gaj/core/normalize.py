"""字段归一化 —— 把 BOSS 的展示文本转成可计算的结构。

这一层的存在意义: 打分规则里写的是 "薪资下限 >= 35万"、"公司规模 >= 2000人"
这类数值比较, 但抓下来的全是 "25-40K·14薪"、"1000-9999人" 这种字符串。
所有解析逻辑集中在这里, 打分引擎只跟数字打交道。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .denoise import clean_text

# ---------------------------------------------------------------- 薪资


@dataclass
class Salary:
    raw: str = ""
    min_10k: float | None = None  # 年薪下限, 单位万元
    max_10k: float | None = None
    months: int = 12  # 一年发几个月
    composition: str = ""  # "14薪" / "期权" 等
    negotiable: bool = False  # 面议

    @property
    def mid_10k(self) -> float | None:
        if self.min_10k is None or self.max_10k is None:
            return None
        return round((self.min_10k + self.max_10k) / 2, 2)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["mid_10k"] = self.mid_10k
        return d


_MONTHS_RE = re.compile(r"(\d{2})\s*薪")
_EQUITY_RE = re.compile(r"(期权|股票|股权)")


def parse_salary(raw: str) -> Salary:
    """解析薪资文本为年薪区间 (万元)。

    支持:
        "25-40K"          -> 30.0 ~ 48.0
        "25-40K·14薪"     -> 35.0 ~ 56.0   (关键: 按 14 个月算, 不是 12)
        "1-2万/月"        -> 12.0 ~ 24.0
        "8000-12000元/月" -> 9.6  ~ 14.4
        "30-50万/年"      -> 30.0 ~ 50.0
        "500-800元/天"    -> 按 21.75 天/月 折算
        "面议"            -> negotiable=True
    """
    text = clean_text(raw or "", context="salary")
    sal = Salary(raw=raw or "")
    if not text:
        return sal

    if any(k in text for k in ("面议", "薪资面议", "详聊")):
        sal.negotiable = True
        return sal

    parts: list[str] = []
    m_months = _MONTHS_RE.search(text)
    if m_months:
        sal.months = int(m_months.group(1))
        parts.append(m_months.group(0))
    m_equity = _EQUITY_RE.search(text)
    if m_equity:
        parts.append(m_equity.group(1))
    sal.composition = "·".join(parts)

    # 把薪资构成部分切掉, 只留数字区间
    body = text.split("·")[0].strip()

    def _finish(low: float, high: float, per_month: bool) -> Salary:
        if per_month:
            sal.min_10k = round(low * sal.months, 2)
            sal.max_10k = round(high * sal.months, 2)
        else:
            sal.min_10k = round(low, 2)
            sal.max_10k = round(high, 2)
        return sal

    # X-YK (千元/月)
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-~至]\s*(\d+(?:\.\d+)?)\s*[Kk千]", body)
    if m:
        return _finish(float(m.group(1)) / 10, float(m.group(2)) / 10, True)

    # X-Y万/年
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-~至]\s*(\d+(?:\.\d+)?)\s*万\s*/?\s*年", body)
    if m:
        return _finish(float(m.group(1)), float(m.group(2)), False)

    # X-Y万 (默认按月)
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-~至]\s*(\d+(?:\.\d+)?)\s*万", body)
    if m:
        return _finish(float(m.group(1)), float(m.group(2)), True)

    # X-Y元/天
    m = re.search(r"(\d+)\s*[-~至]\s*(\d+)\s*元?\s*/\s*天", body)
    if m:
        return _finish(
            float(m.group(1)) * 21.75 / 10000, float(m.group(2)) * 21.75 / 10000, True
        )

    # X-Y元/月
    m = re.search(r"(\d+)\s*[-~至]\s*(\d+)\s*元", body)
    if m:
        return _finish(float(m.group(1)) / 10000, float(m.group(2)) / 10000, True)

    # 单值: "30K以上"
    m = re.search(r"(\d+(?:\.\d+)?)\s*[Kk]", body)
    if m:
        v = float(m.group(1)) / 10
        return _finish(v, v, True)

    return sal


# ---------------------------------------------------------------- 经验


@dataclass
class Experience:
    raw: str = ""
    min_years: float | None = None
    max_years: float | None = None
    unlimited: bool = False
    fresh_graduate: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def parse_experience(raw: str) -> Experience:
    text = clean_text(raw or "", context="experience")
    exp = Experience(raw=raw or "")
    if not text:
        return exp
    if "不限" in text:
        exp.unlimited = True
        exp.min_years = 0
        return exp
    if "应届" in text or "在校" in text:
        exp.fresh_graduate = True
        exp.min_years = 0
        exp.max_years = 1
        return exp
    m = re.search(r"(\d+)\s*[-~]\s*(\d+)\s*年", text)
    if m:
        exp.min_years = float(m.group(1))
        exp.max_years = float(m.group(2))
        return exp
    m = re.search(r"(\d+)\s*年以[上内]", text)
    if m:
        val = float(m.group(1))
        if "以上" in text:
            exp.min_years = val
        else:
            exp.min_years = 0
            exp.max_years = val
        return exp
    m = re.search(r"(\d+)\s*年", text)
    if m:
        exp.min_years = float(m.group(1))
    return exp


# ---------------------------------------------------------------- 学历

EDUCATION_LEVELS: dict[str, int] = {
    "学历不限": 0,
    "不限": 0,
    "初中及以下": 1,
    "中专/中技": 1,
    "高中": 1,
    "大专": 2,
    "本科": 3,
    "硕士": 4,
    "博士": 5,
}


@dataclass
class Education:
    raw: str = ""
    level: int = 0
    unlimited: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def parse_education(raw: str) -> Education:
    text = clean_text(raw or "", context="education")
    edu = Education(raw=raw or "")
    if not text or "不限" in text:
        return edu
    edu.unlimited = False
    for key, lvl in sorted(EDUCATION_LEVELS.items(), key=lambda kv: -len(kv[0])):
        if key in text:
            edu.level = lvl
            return edu
    return edu


# ---------------------------------------------------------------- 公司规模


@dataclass
class Scale:
    raw: str = ""
    min_people: int | None = None
    max_people: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def representative(self) -> int | None:
        """取一个代表值用于比较大小。"""
        if self.min_people is None:
            return self.max_people
        if self.max_people is None:
            return self.min_people
        return (self.min_people + self.max_people) // 2


def parse_scale(raw: str) -> Scale:
    text = clean_text(raw or "", context="scale")
    sc = Scale(raw=raw or "")
    if not text:
        return sc
    text = text.replace("公司规模", "").strip()
    m = re.search(r"(\d+)\s*[-~]\s*(\d+)\s*人", text)
    if m:
        sc.min_people = int(m.group(1))
        sc.max_people = int(m.group(2))
        return sc
    m = re.search(r"(\d+)\s*人以上", text)
    if m:
        sc.min_people = int(m.group(1))
        return sc
    m = re.search(r"(\d+)\s*人以下", text)
    if m:
        sc.min_people = 0
        sc.max_people = int(m.group(1))
        return sc
    m = re.search(r"(\d+)\s*人", text)
    if m:
        sc.min_people = sc.max_people = int(m.group(1))
    return sc


# ---------------------------------------------------------------- 公司性质

_STATE_OWNED_HINTS = (
    "国有",
    "国资",
    "央企",
    "国企",
    "中央企业",
    "全民所有制",
    "国有control",
)
_STATE_OWNED_NAME_HINTS = ("中国", "中铁", "中建", "中交", "中电", "中核", "国家", "省属")
_FOREIGN_HINTS = ("外商投资", "外资", "合资", "港澳台")


def infer_company_nature(
    *,
    name: str = "",
    stage: str = "",
    intro: str = "",
    business_scope: str = "",
) -> str:
    """粗判公司性质。规则只做高置信度判断, 拿不准就返回"未知"交给 AI。

    返回值: 国企 / 上市公司 / 外资 / 民营 / 未知
    """
    blob = " ".join(filter(None, (name, intro, business_scope)))
    if any(h in blob for h in _STATE_OWNED_HINTS):
        return "国企"
    if "已上市" in (stage or ""):
        return "上市公司"
    if any(h in blob for h in _FOREIGN_HINTS):
        return "外资"
    if name and any(name.startswith(h) for h in _STATE_OWNED_NAME_HINTS):
        return "国企"
    if stage in ("不需要融资", "未融资") and name:
        return "民营"
    if stage:
        return "民营"
    return "未知"


# ---------------------------------------------------------------- 工作时间


@dataclass
class WorkingHours:
    raw: str = ""
    start: str = ""
    end: str = ""
    hours_per_day: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


_TIME_RE = re.compile(r"(上午|下午|凌晨|中午|晚上)?\s*(\d{1,2})[::](\d{2})")


def parse_working_hours(raw: str) -> WorkingHours:
    """解析 "上午08:00 - 下午05:00" 这类工时描述并算出日工时。"""
    text = clean_text(raw or "", context="working_hours")
    wh = WorkingHours(raw=raw or "")
    if not text:
        return wh
    matches = _TIME_RE.findall(text)
    if len(matches) < 2:
        return wh

    def _to_minutes(period: str, hh: str, mm: str) -> int:
        h = int(hh)
        if period in ("下午", "晚上") and h < 12:
            h += 12
        if period == "凌晨" and h == 12:
            h = 0
        return h * 60 + int(mm)

    start_min = _to_minutes(*matches[0])
    end_min = _to_minutes(*matches[1])
    if end_min <= start_min:
        end_min += 12 * 60
    wh.start = f"{start_min // 60:02d}:{start_min % 60:02d}"
    wh.end = f"{end_min // 60:02d}:{end_min % 60:02d}"
    wh.hours_per_day = round((end_min - start_min) / 60, 1)
    return wh


# ---------------------------------------------------------------- JD 正文拆分

_SECTION_PATTERNS = [
    (
        "requirement",
        re.compile(
            r"(任职资格|任职要求|岗位要求|职位要求|人员要求|应聘条件|任职条件|我们希望你|"
            r"你需要具备|技能要求|Requirements?)\s*[:：]?",
            re.IGNORECASE,
        ),
    ),
    (
        "responsibility",
        re.compile(
            r"(岗位职责|工作职责|职位描述|工作内容|主要职责|岗位描述|你将负责|"
            r"Responsibilit(?:y|ies))\s*[:：]?",
            re.IGNORECASE,
        ),
    ),
    ("bonus", re.compile(r"(加分项|优先条件|优先考虑|Bonus|Nice to have)\s*[:：]?", re.IGNORECASE)),
    ("benefit", re.compile(r"(我们提供|福利待遇|公司福利|薪资福利|We offer)\s*[:：]?", re.IGNORECASE)),
]


@dataclass
class JDSections:
    full: str = ""
    responsibility: str = ""
    requirement: str = ""
    bonus: str = ""
    benefit: str = ""
    extras: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def split_jd(full_text: str) -> JDSections:
    """把 JD 正文按小标题拆成职责 / 要求 / 加分项 / 福利。

    比原来只按 "任职资格" 一刀切要健壮 —— 实际 JD 的标题写法有十几种。
    """
    text = clean_text(full_text or "", context="jd")
    sections = JDSections(full=text)
    if not text:
        return sections

    hits: list[tuple[int, int, str]] = []
    for key, pattern in _SECTION_PATTERNS:
        for m in pattern.finditer(text):
            hits.append((m.start(), m.end(), key))
    if not hits:
        sections.responsibility = text
        return sections

    hits.sort(key=lambda x: x[0])
    # 同一位置只保留第一个匹配
    deduped: list[tuple[int, int, str]] = []
    for start, end, key in hits:
        if deduped and start < deduped[-1][1]:
            continue
        deduped.append((start, end, key))

    # 第一个标题之前的内容算作职责前言
    preamble = text[: deduped[0][0]].strip()

    buckets: dict[str, list[str]] = {}
    for idx, (start, end, key) in enumerate(deduped):
        stop = deduped[idx + 1][0] if idx + 1 < len(deduped) else len(text)
        body = text[end:stop].strip()
        if body:
            buckets.setdefault(key, []).append(body)

    sections.responsibility = "\n".join(buckets.get("responsibility", [])).strip()
    sections.requirement = "\n".join(buckets.get("requirement", [])).strip()
    sections.bonus = "\n".join(buckets.get("bonus", [])).strip()
    sections.benefit = "\n".join(buckets.get("benefit", [])).strip()

    if preamble and not sections.responsibility:
        sections.responsibility = preamble
    elif preamble:
        sections.extras["preamble"] = preamble

    return sections


# ---------------------------------------------------------------- 城市

_CITY_SUFFIX_RE = re.compile(r"(市|地区)$")


def normalize_city(raw: str) -> str:
    """去掉 "市" 后缀, 让 "无锡市" 和 "无锡" 能匹配上。"""
    text = clean_text(raw or "", context="city")
    if not text:
        return ""
    return _CITY_SUFFIX_RE.sub("", text).strip()


#: 常见直辖市 / 省份前缀, 地址里出现时要先剥掉再取城市
_PROVINCE_PREFIXES = (
    "北京市", "上海市", "天津市", "重庆市",
    "江苏省", "浙江省", "安徽省", "广东省", "山东省", "福建省", "河南省",
    "河北省", "湖北省", "湖南省", "四川省", "陕西省", "辽宁省", "吉林省",
    "黑龙江省", "山西省", "江西省", "云南省", "贵州省", "甘肃省", "青海省",
    "海南省", "台湾省",
    "广西壮族自治区", "内蒙古自治区", "宁夏回族自治区", "新疆维吾尔自治区",
    "西藏自治区", "广西", "内蒙古", "宁夏", "新疆", "西藏",
)

_MUNICIPALITIES = ("北京", "上海", "天津", "重庆")

#: 常见地级市白名单。放在正则之前做前缀匹配, 避免 "哈尔滨南岗区" 被切成 "哈尔"。
#: 只需覆盖用户实际会投递的范围 + 主流城市, 命中不了会自动退化到正则。
_KNOWN_CITIES = (
    # 长三角 (用户主战场)
    "无锡", "苏州", "南京", "常州", "南通", "扬州", "镇江", "泰州", "徐州",
    "盐城", "淮安", "连云港", "宿迁",
    "杭州", "宁波", "温州", "嘉兴", "湖州", "绍兴", "金华", "台州", "衢州",
    "丽水", "舟山", "合肥", "芜湖", "马鞍山", "滁州",
    # 其他主流
    "深圳", "广州", "东莞", "佛山", "珠海", "中山", "惠州", "汕头",
    "成都", "武汉", "西安", "长沙", "郑州", "青岛", "济南", "大连", "沈阳",
    "哈尔滨", "长春", "石家庄", "太原", "福州", "厦门", "南昌", "昆明",
    "贵阳", "南宁", "海口", "兰州", "银川", "西宁", "呼和浩特", "乌鲁木齐",
    "拉萨", "烟台", "潍坊", "洛阳", "唐山", "保定", "廊坊", "秦皇岛",
    "泉州", "莆田", "赣州", "株洲", "襄阳", "宜昌", "绵阳", "德阳",
)

#: 兜底: "城市(可带市) + 区/县/市/旗"
_ADDRESS_RE = re.compile(
    r"^(?P<city>[\u4e00-\u9fa5]{2,4}?)市?"
    r"(?P<district>[\u4e00-\u9fa5]{1,6}?(?:区|县|市|旗))"
)

_DISTRICT_RE = re.compile(r"^(?P<district>[\u4e00-\u9fa5]{1,8}?(?:区|县|市|旗))")


def split_address(raw: str) -> tuple[str, str, str]:
    """把 "无锡新吴区天安智慧城7栋" 拆成 (城市, 区县, 详细地址)。

    BOSS 的 JD 页地址前缀通常是 "城市+区县", 不带省, 也不带 "市" 后缀。
    这是老数据回填 city 字段的主要来源, 所以要尽量鲁棒:

    1. 剥省级前缀 (直辖市前缀直接当城市)
    2. 城市白名单前缀匹配 (最可靠)
    3. 正则兜底 "2-4字城市 + 区县"

    解析不出来时返回空串, 交给上层降级处理, 绝不瞎猜。
    """
    text = clean_text(raw or "", context="address")
    if not text:
        return "", "", ""

    city = ""

    # 直辖市: "上海市浦东新区" / "北京朝阳区"
    for muni in _MUNICIPALITIES:
        if text.startswith(muni):
            city = muni
            text = text[len(muni):].lstrip("市")
            break

    if not city:
        for prefix in _PROVINCE_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):]
                break
        for known in _KNOWN_CITIES:
            if text.startswith(known):
                city = known
                text = text[len(known):].lstrip("市")
                break

    if city:
        m = _DISTRICT_RE.match(text)
        if m:
            district = m.group("district")
            return city, district, text[m.end():].strip() or text
        return city, "", text

    m = _ADDRESS_RE.match(text)
    if m:
        return (
            normalize_city(m.group("city")),
            m.group("district"),
            text[m.end():].strip() or text,
        )
    return "", "", text


def parse_gps(value) -> dict | None:
    """统一 GPS 表示。接受 dict 或 "lng,lat" 字符串。"""
    if isinstance(value, dict):
        lng = value.get("longitude", value.get("lng"))
        lat = value.get("latitude", value.get("lat"))
        if lng is not None and lat is not None:
            return {"lng": float(lng), "lat": float(lat)}
        return None
    if isinstance(value, str) and "," in value:
        try:
            lng_s, lat_s = value.split(",", 1)
            return {"lng": float(lng_s), "lat": float(lat_s)}
        except (TypeError, ValueError):
            return None
    return None
