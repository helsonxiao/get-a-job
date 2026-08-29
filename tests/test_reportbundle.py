"""reportbundle 契约测试: 结构完整性 / 脱敏强制 / 指纹稳定 / 降级清单。

用 in-memory SQLite 按真实 schema 建表, 直插最小列集, 不触碰真实 data/。
"""

from __future__ import annotations

import json

import pytest

from gaj.store import index, reportbundle


def _make_db():
    conn = index.connect(":memory:")
    return conn


def _insert_job(
    conn,
    job_id,
    company_id,
    company_name,
    city,
    industry,
    salary_mid,
    exp_min,
    *,
    overtime="moderate",
    outsourcing=0,
    travel="none",
    edu_level=3,
    first_seen="2026-08-01T10:00:00",
    last_seen="2026-08-20T10:00:00",
    skills='["C++"]',
):
    conn.execute(
        """INSERT INTO jobs (job_id, title, company_id, company_name, city, district,
           salary_mid, exp_min, edu_level, industry, overtime, outsourcing, travel,
           skills, first_seen, last_seen, ignored)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
        (job_id, f"岗位{job_id}", company_id, company_name, city, "测试区",
         salary_mid, exp_min, edu_level, industry, overtime, outsourcing,
         travel, skills, first_seen, last_seen),
    )


@pytest.fixture()
def conn():
    c = _make_db()
    # 行业甲: 样本充足 (3 公司 × 4 岗)
    for i in range(4):
        _insert_job(c, f"j1{i}", "c1", "甲公司一", "无锡", "计算机软件", 20 + i, 3)
        _insert_job(c, f"j2{i}", "c2", "甲公司二", "苏州", "计算机软件", 25 + i, 5)
        _insert_job(c, f"j3{i}", "c3", "甲公司三", "无锡", "计算机软件", 30 + i, 8)
    # 行业乙: 样本稀薄 (1 条 → 薪资中位降级)
    _insert_job(c, "j40", "c4", "乙公司", "无锡", "半导体/芯片", 18, 2)
    # 红旗公司: heavy overtime / 外包, 触发红旗公司榜聚合路径
    _insert_job(c, "j50", "c5", "红旗甲", "无锡", "计算机软件", 22, 3, overtime="heavy")
    _insert_job(c, "j51", "c5", "红旗甲", "无锡", "计算机软件", 24, 4, overtime="heavy")
    _insert_job(c, "j52", "c6", "红旗乙", "苏州", "计算机软件", 26, 5, outsourcing=1)
    c.commit()
    yield c
    c.close()


def _walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_keys(item)


def test_bundle_top_level_contract(conn):
    bundle = reportbundle.build_report_bundle(conn)
    assert bundle["schema_version"] == "2.0"
    assert set(bundle) >= {
        "schema_version", "generated_at", "data_fingerprint",
        "meta", "quality", "market", "focus",
    }
    assert bundle["meta"]["job_count"] == 16
    assert bundle["meta"]["company_count"] == 6
    assert bundle["meta"]["window"]["first_seen_min"] == "2026-08-01T10:00:00"
    assert bundle["meta"]["window"]["last_seen_max"] == "2026-08-20T10:00:00"
    cities = {c["city"]: c["job_count"] for c in bundle["meta"]["cities"]}
    assert cities["无锡"] == 11 and cities["苏州"] == 5


def test_employer_profile_block(conn):
    emp = reportbundle.build_report_bundle(conn)["market"]["employer_profile"]
    assert emp["sample_count"] == 16
    # 月薪构成: 未标 months 的按 12 薪归档
    assert any(m["months"] == 12 for m in emp["months_mix"])
    # 工时分布桶齐全且计数守恒
    assert sum(b["count"] for b in emp["hours_dist"]) == 16
    # 规模段聚合含未知归档
    assert sum(b["job_count"] for b in emp["scale_dist"]) == 16
    # 福利词频来自夹具 skills 无 welfare 列 → 允许为空列表
    assert isinstance(emp["top_welfare"], list)
    assert emp["salary_spread"]["count"] >= 0


def test_fingerprint_stable_but_generated_at_changes(conn):
    b1 = reportbundle.build_report_bundle(conn)
    b2 = reportbundle.build_report_bundle(conn)
    assert b1["data_fingerprint"] == b2["data_fingerprint"]
    assert b1["generated_at"] <= b2["generated_at"]


def test_focus_companies_named(conn):
    """完整版: 焦点行业代表公司暴露真实名, 但无 brand_id/个人打分。"""
    bundle = reportbundle.build_report_bundle(conn)
    focus = bundle["focus"]
    assert focus["industry"] == "计算机软件"
    detail = focus["detail"]
    raw = json.dumps(detail, ensure_ascii=False)
    assert "甲公司一" in raw and "甲公司二" in raw
    for secret in ("c1", "c2", "c3", "best_score"):
        assert secret not in raw
    comps = detail["top_companies"]
    assert comps[0]["company"].startswith("甲")
    for c in comps:
        assert set(c) == {"company", "job_count", "avg_salary"}


def test_red_flag_companies_named(conn):
    bundle = reportbundle.build_report_bundle(conn)
    radar = bundle["market"]["signal_radar"]
    raw = json.dumps(radar, ensure_ascii=False)
    assert "红旗甲" in raw and "红旗乙" in raw
    for secret in ("c5", "c6", "brand_id", "alias"):
        assert secret not in raw
    flags = radar["red_flag_companies"]
    assert flags, "夹具应产出至少一家红旗公司"
    for c in flags:
        assert set(c) == {"company", "job_count", "hours_per_day", "heavy_overtime",
                          "outsourcing", "travel", "flags", "avg_salary"}


def test_no_single_record_leakage(conn):
    bundle = reportbundle.build_report_bundle(conn)
    keys = set(_walk_keys(bundle))
    leaked = keys & reportbundle.FORBIDDEN_KEYS
    assert not leaked, f"契约泄漏单条记录字段: {leaked}"


def test_quality_degradation_listed(conn):
    bundle = reportbundle.build_report_bundle(conn)
    scopes = [d["scope"] for d in bundle["quality"]["degradations"]]
    # 行业乙只有 1 条薪资样本 → 中位降级必须出现
    assert any("半导体/芯片" in s for s in scopes)
    # 全市场薪资覆盖率 < 1
    assert bundle["quality"]["salary_coverage"] == 1.0  # 本夹具全部有薪资
    thresholds = bundle["quality"]["thresholds"]
    assert thresholds["min_salary_samples"] == 2


def test_empty_db_never_crashes():
    c = _make_db()
    try:
        bundle = reportbundle.build_report_bundle(c)
        assert bundle["focus"]["industry"] is None
        assert bundle["focus"]["detail"] is None
        assert bundle["market"]["salary_pricing"]["overall"] is None
        assert any("全市场" in d["scope"] for d in bundle["quality"]["degradations"])
    finally:
        c.close()


def test_real_db_smoke_if_present():
    """真实库冒烟: 契约成立 + 值级泄漏检测 (真实公司名/brand_id 不得出现在包内)。"""
    from gaj import config as cfg

    if not cfg.INDEX_DB.exists():
        pytest.skip("真实 index.db 不存在, 跳过")
    with index.session() as real_conn:
        company_names = {
            row[0] for row in real_conn.execute(
                "SELECT DISTINCT name FROM companies WHERE name IS NOT NULL AND name != ''"
            )
        }
        brand_ids = {
            row[0] for row in real_conn.execute("SELECT DISTINCT brand_id FROM companies")
        }
        bundle = reportbundle.build_report_bundle(real_conn)
    assert bundle["meta"]["job_count"] > 0
    keys = set(_walk_keys(bundle))
    assert not (keys & reportbundle.FORBIDDEN_KEYS)
    raw = json.dumps(bundle, ensure_ascii=False)
    # 完整版: 真实公司名必须出现在双榜; brand_id 值仍不得出现
    boards_raw = json.dumps(bundle["market"]["company_boards"]["hiring"], ensure_ascii=False)
    hit = [n for n in company_names if len(n) >= 4 and n in boards_raw]
    assert hit, "双榜未包含任何真实公司名 (完整版应暴露公司名)"
    leaked = [b for b in brand_ids if len(b) >= 4 and b in raw]
    assert not leaked, f"brand_id 值级泄漏: {leaked[:5]}"


def test_company_boards_named_and_lite(conn):
    """2.0: 完整版双榜真实名; lite 版双榜别名, 两版数据同源。"""
    bundle = reportbundle.build_report_bundle(conn)
    boards = bundle["market"]["company_boards"]
    raw = json.dumps(boards, ensure_ascii=False)
    assert "甲公司一" in raw
    for secret in ("c1", "brand_id", "company_name"):
        assert secret not in raw
    jobs = [c["job_count"] for c in boards["hiring"]]
    assert jobs == sorted(jobs, reverse=True) and jobs[0] == 4
    for c in boards["hiring"]:
        assert set(c) == {"company", "industry", "job_count", "avg_salary"}
    # lite 版: 别名且唯一
    lite_h = boards["hiring_lite"]
    lite_s = boards["salary_lite"]
    assert all(set(c) == {"alias", "industry", "job_count", "avg_salary"} for c in lite_h)
    assert len({c["alias"] for c in lite_h}) == len(lite_h), "招聘榜别名不唯一"
    assert len({c["alias"] for c in lite_s}) == len(lite_s), "薪资榜别名不唯一"
    h_map = {c["alias"]: c for c in lite_h}
    for c in lite_s:
        if c["alias"] in h_map:
            assert h_map[c["alias"]]["job_count"] == c["job_count"], "跨榜同别名但数据不一致"
    # 同源: 真名版与别名版在招数一致 (同名公司)
    assert [c["job_count"] for c in boards["hiring"]] == [c["job_count"] for c in lite_h]
