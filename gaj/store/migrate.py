"""老数据迁移: jobs/<自增ID>/  ->  data/jobs/<encryptJobId>/ + data/companies/<brandId>/

老 demo 的存储有四个问题, 迁移时一并修掉:

1. **主键是自增序号** —— 换机器/重跑就错位, 无法跨会话去重。改用 BOSS 的
   encryptJobId (从 source_url 里提取) 作为天然主键。
2. **公司数据冗余在每个职位目录里** —— 同一家公司抓 N 次, 且无法单独更新。
   抽离到 data/companies/<brandId>/。
3. **没有城市字段** —— 而 H-01 城市淘汰是打分第一条硬规则。
   从 JD 页 company_address 反解 (split_address)。
4. **匿名雇主的公司数据串号** —— BOSS 上 "某中型储能公司" 这类岗位没有真实
   公司主页, 老爬虫顺着页面上的推荐链接抓到了完全不相干的公司 (实测 6 条
   职位全被写入了"叮咚买菜"的简介)。这类数据必须隔离, 否则打分会基于错误
   的公司信息, 属于最危险的一类脏数据。

迁移是**只读老目录 + 只写 data/**, 不删不改 jobs/, 可以反复重跑。

用法::

    python -m gaj.store.migrate                 # 迁移 + 重建索引
    python -m gaj.store.migrate --dry-run       # 只看报告, 不落盘
    python -m gaj.store.migrate --src jobs      # 指定源目录
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config as cfg
from ..core import normalize as nz
from ..core.denoise import clean_text, looks_polluted
from ..core.models import Company, Job
from ..core.profile import load_profile
from ..logging_setup import get_logger
from . import index, repo

log = get_logger("migrate")

#: 从 https://www.zhipin.com/job_detail/<id>.html 里取 encryptJobId
_JOB_ID_RE = re.compile(r"/job_detail/([A-Za-z0-9~_\-]+)\.html")
_BRAND_ID_RE = re.compile(r"/gongsi/([A-Za-z0-9~_\-]+)\.html")

#: BOSS 匿名雇主的展示名模式: "某半导体公司" / "无锡某大型医疗器械公司"
_ANON_RE = re.compile(r"某[\u4e00-\u9fa5A-Za-z0-9]{0,12}(公司|集团|企业|厂|上市)")


def is_anonymous_employer(name: str) -> bool:
    """判断是不是 BOSS 的匿名雇主展示名。

    真实公司名里也可能出现"某"字吗? 极罕见, 且工商注册名不允许。
    这里宁可误判为匿名 (代价是丢掉公司页数据), 也不能把别家公司的
    简介安到这个岗位头上。
    """
    text = (name or "").strip()
    if not text:
        return False
    return bool(_ANON_RE.search(text))


# ---------------------------------------------------------------- 报告


@dataclass
class MigrationReport:
    scanned: int = 0
    migrated: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    companies: int = 0
    anonymous_jobs: list[str] = field(default_factory=list)
    quarantined_companies: list[str] = field(default_factory=list)
    conflict_brands: dict[str, list[str]] = field(default_factory=dict)
    city_filled: int = 0
    city_missing: list[str] = field(default_factory=list)
    salary_fixed: list[tuple[str, str, str]] = field(default_factory=list)
    denoised: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            "",
            "=" * 62,
            "  迁移报告",
            "=" * 62,
            f"  扫描目录      : {self.scanned}",
            f"  成功迁移职位  : {self.migrated}",
            f"  抽离公司      : {self.companies}",
            f"  回填城市      : {self.city_filled}",
        ]
        if self.salary_fixed:
            lines.append(f"  修正年薪(多薪): {len(self.salary_fixed)}")
            for jid, raw, fixed in self.salary_fixed[:10]:
                lines.append(f"      {jid[:12]}… {raw} -> {fixed}")
        if self.denoised:
            lines.append(f"  清洗反爬污染  : {len(self.denoised)}")
        if self.anonymous_jobs:
            lines.append(f"  匿名雇主职位  : {len(self.anonymous_jobs)} (公司数据已隔离)")
        if self.quarantined_companies:
            lines.append(f"  隔离脏公司页  : {len(self.quarantined_companies)}")
            for b in self.quarantined_companies[:10]:
                lines.append(f"      {b}")
        if self.conflict_brands:
            lines.append(f"  brand_id 串号 : {len(self.conflict_brands)}")
            for brand, names in list(self.conflict_brands.items())[:5]:
                lines.append(f"      {brand[:16]}… -> {' / '.join(names[:4])}")
        if self.city_missing:
            lines.append(f"  城市未知      : {len(self.city_missing)}")
            lines.append(f"      {', '.join(x[:10] for x in self.city_missing[:8])}")
        if self.skipped:
            lines.append(f"  跳过          : {len(self.skipped)}")
            for name, why in self.skipped[:10]:
                lines.append(f"      {name}: {why}")
        lines.append("=" * 62)
        return "\n".join(lines)


# ---------------------------------------------------------------- 读取老目录


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("读取失败 %s: %s", path, exc)
        return {}


@dataclass
class LegacyRecord:
    """一个老 jobs/NNN 目录的原始内容。"""

    dirname: str
    meta: dict = field(default_factory=dict)
    jd_dom: dict = field(default_factory=dict)
    company_dom: dict = field(default_factory=dict)
    structured: dict = field(default_factory=dict)
    jd_full_txt: str = ""

    @property
    def job_id(self) -> str:
        url = self.meta.get("source_url") or self.jd_dom.get("url") or ""
        m = _JOB_ID_RE.search(url)
        return m.group(1) if m else ""

    @property
    def brand_id(self) -> str:
        bid = self.meta.get("brand_id") or self.company_dom.get("brand_id") or ""
        if bid:
            return bid
        m = _BRAND_ID_RE.search(self.meta.get("company_url", ""))
        return m.group(1) if m else ""

    @property
    def company_name(self) -> str:
        raw = (
            self.jd_dom.get("company_name")
            or self.meta.get("company_name")
            or self.structured.get("company_name")
            or ""
        )
        # 老 meta 里的公司名带 HTML 实体 (&amp;)
        return clean_text(raw, context="company_name")


def load_legacy(src: Path) -> list[LegacyRecord]:
    records: list[LegacyRecord] = []
    for d in sorted(src.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        rec = LegacyRecord(
            dirname=d.name,
            meta=_read_json(d / "meta.json"),
            jd_dom=_read_json(d / "jd_dom_data.json"),
            company_dom=_read_json(d / "company_dom_data.json"),
            structured=_read_json(d / "structured.json"),
        )
        txt = d / "jd_full.txt"
        if txt.exists():
            rec.jd_full_txt = txt.read_text(encoding="utf-8", errors="ignore")
        records.append(rec)
    return records


# ---------------------------------------------------------------- 串号检测


def detect_brand_conflicts(records: list[LegacyRecord]) -> dict[str, list[str]]:
    """同一个 brand_id 被关联到多个不同公司名 = 抓取时页面串号了。

    返回 {brand_id: [公司名, ...]}, 只包含有冲突的。
    """
    by_brand: dict[str, set[str]] = defaultdict(set)
    for rec in records:
        bid = rec.brand_id
        name = rec.meta.get("company_name") or rec.company_name
        if bid and name:
            by_brand[bid].add(clean_text(name, context="company_name"))
    return {b: sorted(names) for b, names in by_brand.items() if len(names) > 1}


# ---------------------------------------------------------------- 主流程


def _legacy_list_item(rec: LegacyRecord, city: str, district: str) -> dict:
    """老数据没有列表 API 响应, 这里合成一个最小 list_item, 让 Job.build
    能走和新采集完全一样的代码路径 —— 避免出现两套字段合并逻辑。"""
    item: dict[str, Any] = {}
    if city:
        item["cityName"] = city
    if district:
        item["areaDistrict"] = district
    return item


def _migrate_record(
    rec: LegacyRecord,
    *,
    src: Path,
    blacklist: list[str],
    conflicts: dict[str, list[str]],
    company_cache: dict[str, Company],
    assume_city: str = "",
    dry_run: bool = False,
    report: MigrationReport | None = None,
) -> bool:
    """迁移单条老格式记录到 data/。

    成功迁移返回 True, 跳过/失败返回 False。
    这是 migrate() 和 migrate_one() 共用的核心逻辑。
    """
    if report is None:
        report = MigrationReport()
        report.scanned = 1

    job_id = rec.job_id
    if not job_id:
        report.skipped.append((rec.dirname, "无法从 URL 提取 encryptJobId"))
        return False
    if not rec.jd_dom and not rec.structured:
        report.skipped.append((rec.dirname, "无 JD 数据"))
        return False

    raw_name = rec.meta.get("company_name") or rec.company_name
    anonymous = is_anonymous_employer(raw_name)
    brand_id = rec.brand_id
    conflicted = brand_id in conflicts

    # ---- 公司: 匿名 / 串号 -> 隔离公司页数据 ----
    company_dom = rec.company_dom
    notes: list[str] = []
    if anonymous:
        report.anonymous_jobs.append(job_id)
        notes.append("BOSS 匿名雇主, 详情页公司链接不可信, 公司页数据已丢弃")
        company_dom = {}
        # 匿名雇主不共享 brand 目录, 用职位 ID 兜底, 避免互相污染
        brand_id = f"anon-{job_id}"
    elif conflicted:
        notes.append(
            "该 brand_id 关联到多个公司名: " + " / ".join(conflicts[brand_id])
        )
        if brand_id not in report.quarantined_companies:
            report.quarantined_companies.append(brand_id)
        company_dom = {}

    if not brand_id:
        brand_id = f"unknown-{job_id}"

    existing = company_cache.get(brand_id) or (
        None if dry_run else repo.load_company(brand_id)
    )
    company = Company.build(
        brand_id=brand_id,
        jd_dom=rec.jd_dom,
        company_dom=company_dom,
        existing=existing,
    )
    if not company.name:
        company.name = clean_text(raw_name, context="company_name")
    company.anonymous = anonymous
    company.data_conflict = conflicted
    for note in notes:
        if note not in company.notes:
            company.notes.append(note)
    if not company.url and rec.meta.get("company_url") and not anonymous:
        company.url = rec.meta["company_url"]

    if brand_id not in company_cache:
        report.companies += 1
    company_cache[brand_id] = company

    # ---- 城市回填 ----
    address = rec.jd_dom.get("company_address") or rec.structured.get(
        "company_address", ""
    )
    city, district, _rest = nz.split_address(address)
    city_source = "address"
    if city:
        report.city_filled += 1
    elif assume_city:
        # 老数据是在某个城市筛选条件下采集的, 允许人工指定兜底值,
        # 但要在 provenance 里留痕, 免得后面把猜测当成事实。
        city = nz.normalize_city(assume_city)
        city_source = "assumed"
        report.city_filled += 1
    else:
        city_source = "unknown"
        report.city_missing.append(job_id)

    # ---- 职位 ----
    jd_dom = dict(rec.jd_dom)
    if not jd_dom.get("jd_full") and rec.jd_full_txt:
        jd_dom["jd_full"] = rec.jd_full_txt
    if not jd_dom.get("url"):
        jd_dom["url"] = rec.meta.get("source_url", "")
    # 老 structured.json 里有些字段 DOM 没抓到, 补上
    for src_key, dst_key in (
        ("job_name", "job_name"),
        ("experience_required", "experience_required"),
        ("education_required", "education_required"),
        ("salary_composition", "salary_composition"),
    ):
        if not jd_dom.get(dst_key) and rec.structured.get(src_key):
            jd_dom[dst_key] = rec.structured[src_key]
    if not jd_dom.get("skill_tags") and rec.structured.get("skill_tags"):
        jd_dom["skill_tags"] = rec.structured["skill_tags"]

    if looks_polluted(jd_dom.get("jd_full", "")):
        report.denoised.append(job_id)

    job = Job.build(
        job_id=job_id,
        list_item=_legacy_list_item(rec, city, district),
        jd_dom=jd_dom,
        company=company,
        blacklist=blacklist,
        existing=None if dry_run else repo.load_job(job_id),
    )
    job.crawled_at = rec.meta.get("crawled_at", job.crawled_at)
    job.first_seen = rec.meta.get("crawled_at", job.first_seen)
    # list_item 是迁移时合成的, 不是真的抓到了列表 API
    job.provenance["list_api"] = False
    job.provenance["migrated_from"] = f"{src.name}/{rec.dirname}"
    job.provenance["company_page"] = bool(company_dom)
    job.provenance["city_source"] = city_source
    if anonymous:
        job.provenance["employer_anonymous"] = True

    # 老 structured.json 的年薪是按 12 个月算的, 有多薪时必然偏低
    old_high = rec.structured.get("job_salary_high_10k")
    new_high = job.salary.get("max_10k")
    if old_high and new_high and abs(float(old_high) - float(new_high)) > 0.5:
        report.salary_fixed.append(
            (
                job_id,
                f"{rec.structured.get('job_salary_low_10k')}-{old_high}万",
                f"{job.salary.get('min_10k')}-{new_high}万",
            )
        )

    if not dry_run:
        repo.save_company(company, raw_dom=rec.company_dom or None)
        repo.save_job(job, raw_jd_dom=rec.jd_dom or None)
    report.migrated += 1
    return True


def migrate_one(
    src_dir: Path,
    *,
    dry_run: bool = False,
    assume_city: str = "",
) -> MigrationReport:
    """迁移单个老格式职位目录到 data/。

    用于增量入库: 爬虫每抓完一个职位 (老格式 .../NNN/), 立刻调本函数
    把它迁移到 data/jobs/<encryptJobId>/ 并保存, 让 Web 端能实时看到。

    Args:
        src_dir: 单个老格式职位目录, 形如 .../crawl-<ts>/001/
        dry_run: 只报告不落盘
        assume_city: 地址缺失时的城市兜底值

    Returns:
        MigrationReport (scanned 恒为 1)
    """
    report = MigrationReport()
    if not src_dir.exists():
        log.warning("源目录不存在: %s", src_dir)
        return report

    cfg.ensure_dirs()
    profile = load_profile()
    blacklist = profile.blacklist_keywords

    # 直接构造单条 LegacyRecord (不遍历父目录, 避免重复加载)
    rec = LegacyRecord(
        dirname=src_dir.name,
        meta=_read_json(src_dir / "meta.json"),
        jd_dom=_read_json(src_dir / "jd_dom_data.json"),
        company_dom=_read_json(src_dir / "company_dom_data.json"),
        structured=_read_json(src_dir / "structured.json"),
    )
    txt = src_dir / "jd_full.txt"
    if txt.exists():
        rec.jd_full_txt = txt.read_text(encoding="utf-8", errors="ignore")

    report.scanned = 1
    # 增量迁移不做串号检测 (单条无法判断), conflicts 传空
    _migrate_record(
        rec,
        src=src_dir.parent,
        blacklist=blacklist,
        conflicts={},
        company_cache={},
        assume_city=assume_city,
        dry_run=dry_run,
        report=report,
    )
    return report


def migrate(
    src: Path | None = None,
    *,
    dry_run: bool = False,
    rebuild_index: bool = True,
    assume_city: str = "",
) -> MigrationReport:
    src = src or (cfg.PROJECT_ROOT / "jobs")
    report = MigrationReport()

    if not src.exists():
        log.warning("源目录不存在: %s", src)
        return report

    cfg.ensure_dirs()
    profile = load_profile()
    blacklist = profile.blacklist_keywords

    records = load_legacy(src)
    report.scanned = len(records)

    conflicts = detect_brand_conflicts(records)
    report.conflict_brands = conflicts
    if conflicts:
        log.warning("检测到 %d 个 brand_id 串号, 相关公司页数据将被隔离", len(conflicts))

    company_cache: dict[str, Company] = {}

    for rec in records:
        _migrate_record(
            rec,
            src=src,
            blacklist=blacklist,
            conflicts=conflicts,
            company_cache=company_cache,
            assume_city=assume_city,
            dry_run=dry_run,
            report=report,
        )

    if not dry_run and rebuild_index:
        index.reindex()

    return report


# ---------------------------------------------------------------- 串号修复


def _is_anonymous_like(name: str) -> bool:
    """更宽松的匿名雇主检测。

    迁移时的 ``_ANON_RE`` 限制了"某"后面的字符数 (≤12) 且不允许标点,
    导致"无锡某大型光伏、锂电、半导体智能装备上市公司"这种长名字漏判。
    修复时用更宽松的策略: 含"某"且以公司/集团/企业/厂/上市结尾。
    """
    text = (name or "").strip()
    if not text:
        return False
    if is_anonymous_employer(text):
        return True
    return "某" in text and text.endswith(("公司", "集团", "企业", "厂", "上市"))


@dataclass
class FixReport:
    """修复报告 —— 字段都带公司名/职位标题, 方便人类阅读。"""

    # (brand_id, company_name) —— 误标的 anon-* 公司, 已隔离无需冲突标记
    anon_false_positives: list[tuple[str, str]] = field(default_factory=list)
    # (job_id, job_title, old_company_name) —— 匿名职位改挂到独立 anon-{job_id}
    reassigned_jobs: list[tuple[str, str, str]] = field(default_factory=list)
    # (brand_id, company_name) —— 因串号被删除的脏公司目录
    deleted_companies: list[tuple[str, str]] = field(default_factory=list)
    # (brand_id, winning_name, votes, total) —— 多数投票定名
    resolved_by_majority: list[tuple[str, str, int, int]] = field(default_factory=list)
    # (brand_id, company_name, reason) —— 无法自动处理
    skipped: list[tuple[str, str, str]] = field(default_factory=list)

    def render(self) -> str:
        lines = [""]

        if not (
            self.anon_false_positives
            or self.reassigned_jobs
            or self.deleted_companies
            or self.resolved_by_majority
            or self.skipped
        ):
            lines += ["=" * 62, "  没有发现需要修复的串号数据", "=" * 62]
            return "\n".join(lines)

        lines += [
            "=" * 62,
            "  串号修复报告",
            "=" * 62,
            "",
            "  背景: 抓取时详情页的『公司链接』被串号, 导致同一个 brand_id",
            "  关联到了多家不相关的公司 (最典型的就是匿名雇主『某XX公司』",
            "  的详情页指向了完全无关的真实公司, 简介被张冠李戴)。",
            "",
        ]

        if self.anon_false_positives:
            lines.append(f"  ▸ 清除误标冲突标记: {len(self.anon_false_positives)} 家公司")
            lines.append("    这些公司已经是 anon-{job_id} 独立目录了 (迁移时已隔离),")
            lines.append("    但残留了 data_conflict=True 标记, 清掉即可, 无数据变动。")
            for _, name in self.anon_false_positives[:5]:
                lines.append(f"      - {name}")
            if len(self.anon_false_positives) > 5:
                lines.append(f"      ... 共 {len(self.anon_false_positives)} 家")
            lines.append("")

        if self.reassigned_jobs:
            lines.append(f"▸ 匿名职位重新归档: {len(self.reassigned_jobs)} 个职位")
            lines.append("    这些职位是 BOSS 匿名雇主 (公司名含『某』字),")
            lines.append("    但被错误归到了一个串号的 brand_id 下, 共享了别家的公司简介。")
            lines.append("    处理: 每个职位改挂到 anon-{职位ID}, 公司简介清空。")
            lines.append("    影响: 职位本身保留, 只是公司信息不再被错误数据污染。")
            for jid, title, old_name in self.reassigned_jobs[:8]:
                title_short = (title or "(无标题)")[:24]
                lines.append(f"      - [{title_short}] 原公司: {old_name[:30]}")
            if len(self.reassigned_jobs) > 8:
                lines.append(f"      ... 共 {len(self.reassigned_jobs)} 个职位")
            lines.append("")

        if self.deleted_companies:
            lines.append(f"▸ 删除脏公司目录: {len(self.deleted_companies)} 家")
            lines.append("    这些 brand_id 下的职位全部是匿名雇主, 已全部改挂走,")
            lines.append("    原公司目录里只剩脏数据 (典型: 叮咚买菜的简介被安到了")
            lines.append("    一堆匿名雇主头上), 直接删除。")
            for _, name in self.deleted_companies[:5]:
                lines.append(f"      - {name}")
            lines.append("")

        if self.resolved_by_majority:
            lines.append(f"▸ 多数投票定名: {len(self.resolved_by_majority)} 家公司")
            lines.append("    同一 brand_id 下有真实公司名 (非匿名), 按出现次数最多")
            lines.append("    的公司名作为正确名称, 清除冲突标记。")
            for _, name, votes, total in self.resolved_by_majority[:5]:
                lines.append(f"      - {name} ({votes}/{total} 个职位使用此名)")
            lines.append("")

        if self.skipped:
            lines.append(f"▸ 无法自动处理: {len(self.skipped)} 家")
            for _, name, why in self.skipped[:5]:
                lines.append(f"      - {name}: {why}")
            lines.append("")

        lines.append("=" * 62)
        return "\n".join(lines)


def _collect_conflict_companies() -> list[Company]:
    """加载所有 data_conflict=True 的公司记录。"""
    out: list[Company] = []
    for comp in repo.iter_companies():
        if comp.data_conflict:
            out.append(comp)
    return out


def _jobs_by_brand_id(brand_id: str) -> list[Job]:
    """找出所有 company_id = brand_id 的职位。"""
    return [j for j in repo.iter_jobs() if j.company_id == brand_id]


def _reassign_job_to_anon(job: Job, *, dry_run: bool) -> str:
    """把一条职位改挂到 anon-{job_id}, 并创建对应匿名公司。

    返回新的 brand_id。
    """
    new_bid = f"anon-{job.job_id}"
    old_bid = job.company_id

    # 创建匿名公司 (从 JD 侧边栏信息构建, 不用脏的 company_dom)
    company = Company.build(
        brand_id=new_bid,
        jd_dom=repo.read_json(repo.job_dir(job.job_id) / cfg.JOB_RAW_JD_FILE) or {},
        company_dom={},
        existing=None,
    )
    if not company.name:
        company.name = job.company_name
    company.anonymous = True
    company.data_conflict = False
    company.notes = ["BOSS 匿名雇主, 原 brand_id 串号已修复, 公司页数据已丢弃"]

    # 更新职位
    job.company_id = new_bid
    job.provenance["employer_anonymous"] = True
    job.provenance["conflict_reassigned_from"] = old_bid

    if not dry_run:
        repo.save_company(company)
        repo.save_job(job)
    return new_bid


def fix_conflicts(*, dry_run: bool = False, rebuild_index: bool = True) -> FixReport:
    """自动修复 brand_id 串号遗留的脏数据。

    三种情况:
    1. ``anon-{job_id}`` 公司残留 ``data_conflict=True`` → 清掉 (已隔离, 无真实冲突)
    2. 真·串号 brand_id 下全是匿名雇主 → 每个 job 改挂 ``anon-{job_id}``, 删原公司
    3. 真·串号 brand_id 下有真实公司名 → 多数投票定名, 清 conflict 标记
    """
    report = FixReport()
    conflicted = _collect_conflict_companies()
    if not conflicted:
        log.info("没有 data_conflict=True 的公司, 无需修复")
        return report

    log.info("发现 %d 个 data_conflict=True 的公司, 开始修复", len(conflicted))

    for comp in conflicted:
        bid = comp.brand_id
        comp_name = comp.name or "(未命名公司)"

        # ---- 情况 1: anon-* 的误标 ----
        if bid.startswith("anon-"):
            report.anon_false_positives.append((bid, comp_name))
            comp.data_conflict = False
            comp.notes = [
                n for n in comp.notes
                if not n.startswith("该 brand_id 关联到多个公司名")
            ]
            if not dry_run:
                repo.save_company(comp)
            continue

        # ---- 情况 2/3: 真·串号 ----
        jobs = _jobs_by_brand_id(bid)
        if not jobs:
            report.skipped.append((bid, comp_name, "无职位引用此 brand_id, 跳过"))
            continue

        anon_count = sum(1 for j in jobs if _is_anonymous_like(j.company_name))
        real_count = len(jobs) - anon_count

        if anon_count == len(jobs):
            # ---- 情况 2: 全是匿名雇主, 改挂 anon-{job_id} ----
            for job in jobs:
                _reassign_job_to_anon(job, dry_run=dry_run)
                report.reassigned_jobs.append(
                    (job.job_id, job.title, job.company_name)
                )
            # 删除脏公司目录
            report.deleted_companies.append((bid, comp_name))
            if not dry_run:
                import shutil

                d = repo.company_dir(bid)
                if d.exists():
                    shutil.rmtree(d)
                    log.info("已删除脏公司目录: %s", d)
        elif real_count >= anon_count:
            # ---- 情况 3: 多数是真实公司名, 多数投票 ----
            from collections import Counter

            real_names = [
                j.company_name for j in jobs if not _is_anonymous_like(j.company_name)
            ]
            winner, votes = Counter(real_names).most_common(1)[0]
            comp.name = winner
            comp.data_conflict = False
            comp.notes = [
                n for n in comp.notes
                if not n.startswith("该 brand_id 关联到多个公司名")
            ]
            comp.notes.append(
                f"串号已修复: 多数投票选定 '{winner}' ({votes}/{len(jobs)} 条职位)"
            )
            report.resolved_by_majority.append((bid, winner, votes, len(jobs)))
            if not dry_run:
                repo.save_company(comp)
        else:
            # 匿名占多数但非全部 —— 保守起见按匿名处理
            for job in jobs:
                if _is_anonymous_like(job.company_name):
                    _reassign_job_to_anon(job, dry_run=dry_run)
                    report.reassigned_jobs.append(
                        (job.job_id, job.title, job.company_name)
                    )
            # 剩余真实职位的公司名冲突无法自动解决
            remaining = [j for j in jobs if not _is_anonymous_like(j.company_name)]
            if remaining:
                if not dry_run:
                    comp.notes.append(
                        f"匿名职位已改挂, 剩余 {len(remaining)} 条真实职位保留"
                    )
                    repo.save_company(comp)
                else:
                    report.skipped.append(
                        (
                            bid,
                            comp_name,
                            f"匿名已改挂, {len(remaining)} 条真实职位保留冲突标记",
                        )
                    )

    if not dry_run and rebuild_index:
        log.info("重建索引...")
        index.reindex()

    return report


def main() -> None:
    from ..logging_setup import setup

    setup()
    ap = argparse.ArgumentParser(description="迁移老 jobs/ 数据到 data/")
    ap.add_argument("--src", default=None, help="老数据目录, 默认 <项目根>/jobs")
    ap.add_argument("--dry-run", action="store_true", help="只打印报告, 不写入")
    ap.add_argument("--no-index", action="store_true", help="跳过索引重建")
    ap.add_argument(
        "--assume-city",
        default="",
        help="地址缺失时的城市兜底值 (会在 provenance.city_source 标记为 assumed)",
    )
    args = ap.parse_args()

    report = migrate(
        Path(args.src).expanduser().resolve() if args.src else None,
        dry_run=args.dry_run,
        rebuild_index=not args.no_index,
        assume_city=args.assume_city,
    )
    print(report.render())
    if args.dry_run:
        print("  (dry-run, 未写入任何文件)\n")


if __name__ == "__main__":
    main()
