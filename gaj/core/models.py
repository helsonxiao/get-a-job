"""领域模型。

Job / Company 是落盘的真相源, 全部用普通 dict 序列化, 保证 JSON 文件
人眼可读、可手工编辑、git diff 友好。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from . import normalize as nz
from .denoise import clean_list, clean_text, merge_skill_lists
from .signals import JobSignals, extract_signals

SCHEMA_VERSION = 2


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------- 公司


@dataclass
class Company:
    brand_id: str = ""
    name: str = ""
    short_name: str = ""
    industry: str = ""
    stage: str = ""
    scale_raw: str = ""
    scale_min: int | None = None
    scale_max: int | None = None
    nature: str = "未知"
    founded: str = ""
    registered_capital_10k: float | None = None
    business_scope: str = ""
    intro: str = ""
    working_hours_raw: str = ""
    working_hours_start: str = ""
    working_hours_end: str = ""
    hours_per_day: float | None = None
    url: str = ""
    logo: str = ""
    #: BOSS 上的匿名雇主("某中型储能公司")。这类岗位详情页给的公司链接
    #: 往往指向一家毫不相干的公司, 必须隔离, 不能拿去打分。
    anonymous: bool = False
    #: 同一 brand_id 关联到多个不同公司名 —— 说明抓取时页面串号了
    data_conflict: bool = False
    notes: list[str] = field(default_factory=list)
    first_seen: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Company":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    @property
    def scale_representative(self) -> int | None:
        if self.scale_min is None:
            return self.scale_max
        if self.scale_max is None:
            return self.scale_min
        return (self.scale_min + self.scale_max) // 2

    @classmethod
    def build(
        cls,
        *,
        brand_id: str,
        list_item: dict | None = None,
        jd_dom: dict | None = None,
        company_dom: dict | None = None,
        existing: "Company | None" = None,
    ) -> "Company":
        """把三个来源的公司信息合并成一条记录。

        优先级: 公司页 > JD 页侧边栏 > 列表 API。
        已有记录的非空字段不会被新来源的空值覆盖。
        """
        li = list_item or {}
        jd = jd_dom or {}
        cd = company_dom or {}

        def pick(*values: Any, default: Any = "") -> Any:
            for v in values:
                if v not in (None, "", [], {}):
                    return v
            return default

        name = clean_text(
            pick(jd.get("company_name"), cd.get("company_name"), li.get("brandName")),
            context="company_name",
        )
        short_name = clean_text(
            pick(li.get("brandName"), jd.get("company_short_name")), context="brand"
        )
        industry = clean_text(
            pick(jd.get("company_industry"), li.get("brandIndustry")), context="industry"
        )
        stage = clean_text(
            pick(jd.get("company_financing_stage"), li.get("brandStageName")),
            context="stage",
        )
        scale_raw = clean_text(
            pick(jd.get("company_scale"), li.get("brandScaleName")), context="scale"
        )
        scale = nz.parse_scale(scale_raw)
        intro = clean_text(cd.get("company_intro", ""), context="company_intro")
        business_scope = clean_text(cd.get("business_scope", ""), context="scope")
        wh = nz.parse_working_hours(cd.get("working_hours", ""))

        obj = cls(
            brand_id=brand_id,
            name=name,
            short_name=short_name,
            industry=industry,
            stage=stage,
            scale_raw=scale_raw,
            scale_min=scale.min_people,
            scale_max=scale.max_people,
            founded=clean_text(jd.get("company_founding_date", ""), context="founded"),
            registered_capital_10k=jd.get("company_registered_capital") or None,
            business_scope=business_scope,
            intro=intro,
            working_hours_raw=wh.raw,
            working_hours_start=wh.start,
            working_hours_end=wh.end,
            hours_per_day=wh.hours_per_day,
            url=pick(cd.get("url"), jd.get("company_url")),
            logo=pick(li.get("brandLogo")),
        )
        obj.nature = nz.infer_company_nature(
            name=obj.name, stage=obj.stage, intro=obj.intro, business_scope=obj.business_scope
        )

        if existing:
            obj.first_seen = existing.first_seen
            # 新数据为空时保留旧值
            for f_name in cls.__dataclass_fields__:
                if f_name in ("updated_at", "schema_version", "first_seen"):
                    continue
                if getattr(obj, f_name) in (None, "", "未知") and getattr(existing, f_name):
                    setattr(obj, f_name, getattr(existing, f_name))
        obj.updated_at = _now()
        return obj


# ---------------------------------------------------------------- 职位


@dataclass
class Job:
    job_id: str = ""  # encryptJobId, 天然主键
    source: str = "boss"
    url: str = ""
    title: str = ""

    salary: dict = field(default_factory=dict)
    city: str = ""
    district: str = ""
    business_district: str = ""
    address: str = ""
    gps: dict | None = None

    experience: dict = field(default_factory=dict)
    education: dict = field(default_factory=dict)
    skills: list[str] = field(default_factory=list)
    welfare: list[str] = field(default_factory=list)

    jd: dict = field(default_factory=dict)
    signals: dict = field(default_factory=dict)

    boss: dict = field(default_factory=dict)
    company_id: str = ""
    company_name: str = ""

    online: bool = True
    first_seen: str = field(default_factory=_now)
    last_seen: str = field(default_factory=_now)
    crawled_at: str = field(default_factory=_now)
    provenance: dict = field(default_factory=dict)
    quality: dict = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    favorite: bool = False
    favorited_at: str = ""
    #: 用户手动忽略, 忽略后在列表默认不展示
    ignored: bool = False
    #: 人工调分覆盖。{"total": float, "note": str, "at": str}
    #: 设置后 best_total 优先取这里的 total, note 在详情页显示并可用于简历生成。
    manual_override: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Job":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    # ---- 便捷访问 ----
    @property
    def salary_min(self) -> float | None:
        return self.salary.get("min_10k")

    @property
    def salary_max(self) -> float | None:
        return self.salary.get("max_10k")

    @property
    def salary_mid(self) -> float | None:
        return self.salary.get("mid_10k")

    @property
    def jd_text(self) -> str:
        return self.jd.get("full", "")

    @classmethod
    def build(
        cls,
        *,
        job_id: str,
        list_item: dict | None = None,
        jd_dom: dict | None = None,
        company: Company | None = None,
        blacklist: list[str] | None = None,
        existing: "Job | None" = None,
    ) -> "Job":
        """合并列表 API 与详情页数据, 生成归一化职位记录。

        重点: 列表 API 的 skills / cityName / gps 是干净的结构化数据,
        详情页 DOM 抓到的同名字段常常被注水, 所以优先用 API 的。
        """
        li = list_item or {}
        jd = jd_dom or {}
        company = company or Company()

        title = clean_text(li.get("jobName") or jd.get("job_name", ""), context="title")
        salary_raw = li.get("salaryDesc") or jd.get("job_salary", "")
        salary = nz.parse_salary(salary_raw)

        exp_raw = li.get("jobExperience") or jd.get("experience_required", "")
        edu_raw = li.get("jobDegree") or jd.get("education_required", "")

        skills = merge_skill_lists(li.get("skills"), jd.get("skill_tags"))
        welfare = clean_list(
            (li.get("welfareList") or []) + (jd.get("benefits") or []), context="welfare"
        )

        sections = nz.split_jd(jd.get("jd_full", ""))

        signals: JobSignals = extract_signals(
            jd_text=sections.full,
            welfare=welfare,
            hours_per_day=company.hours_per_day,
            company_industry=company.industry,
            company_name=company.name,
            company_intro=company.intro,
            blacklist=blacklist or [],
        )

        obj = cls(
            job_id=job_id,
            url=jd.get("url") or f"https://www.zhipin.com/job_detail/{job_id}.html",
            title=title,
            salary=salary.to_dict(),
            city=nz.normalize_city(li.get("cityName", "")),
            district=clean_text(li.get("areaDistrict", ""), context="district"),
            business_district=clean_text(li.get("businessDistrict", ""), context="bd"),
            address=clean_text(jd.get("company_address", ""), context="address"),
            gps=nz.parse_gps(li.get("gps") or jd.get("company_lat_lng")),
            experience=nz.parse_experience(exp_raw).to_dict(),
            education=nz.parse_education(edu_raw).to_dict(),
            skills=skills,
            welfare=welfare,
            jd=sections.to_dict(),
            signals=signals.to_dict(),
            boss={
                "name": clean_text(li.get("bossName", ""), context="boss"),
                "title": clean_text(li.get("bossTitle", ""), context="boss"),
                "cert": li.get("bossCert"),
                "online": li.get("bossOnline"),
                "gold_hunter": bool(li.get("goldHunter")),
            },
            company_id=company.brand_id or li.get("encryptBrandId", ""),
            company_name=company.name or clean_text(li.get("brandName", "")),
            online=li.get("jobValidStatus", 1) == 1,
            provenance={
                "list_api": bool(list_item),
                "jd_page": bool(jd_dom),
                "company_page": bool(company and company.intro),
            },
        )

        obj.quality = obj._assess_quality()

        if existing:
            obj.first_seen = existing.first_seen
            # 保留人工填写的字段
            if existing.jd.get("manual_note"):
                obj.jd["manual_note"] = existing.jd["manual_note"]
        obj.last_seen = _now()
        obj.crawled_at = _now()
        return obj

    def _assess_quality(self) -> dict:
        """给这条记录打一个数据完整度评级, UI 上用来提示哪些数据不可信。"""
        from .denoise import looks_polluted

        missing: list[str] = []
        if not self.title:
            missing.append("title")
        if self.salary.get("min_10k") is None and not self.salary.get("negotiable"):
            missing.append("salary")
        if not self.city:
            missing.append("city")
        if not self.jd.get("full"):
            missing.append("jd")
        if not self.company_id:
            missing.append("company")

        polluted = looks_polluted(self.jd.get("full", ""))
        total_fields = 5
        score = round((total_fields - len(missing)) / total_fields, 2)
        return {
            "score": score,
            "missing": missing,
            "polluted": polluted,
            "jd_length": len(self.jd.get("full", "")),
        }
