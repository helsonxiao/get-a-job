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
    assert bundle["schema_version"] == "1.0"
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


def test_fingerprint_stable_but_generated_at_changes(conn):
    b1 = reportbundle.build_report_bundle(conn)
    b2 = reportbundle.build_report_bundle(conn)
    assert b1["data_fingerprint"] == b2["data_fingerprint"]
    assert b1["generated_at"] <= b2["generated_at"]


def test_focus_companies_deidentified(conn):
    bundle = reportbundle.build_report_bundle(conn)
    focus = bundle["focus"]
    assert focus["industry"] == "计算机软件"
    detail = focus["detail"]
    raw = json.dumps(detail, ensure_ascii=False)
    # 明文公司名 / brand_id / 个人打分 不得出现
    for secret in ("甲公司一", "甲公司二", "甲公司三", "c1", "c2", "c3"):
        assert secret not in raw
    aliases = [c["alias"] for c in detail["top_companies"]]
    assert aliases[0] == "公司A"
    for c in detail["top_companies"]:
        assert set(c) == {"alias", "job_count", "avg_salary"}


def test_red_flag_companies_deidentified(conn):
    bundle = reportbundle.build_report_bundle(conn)
    radar = bundle["market"]["signal_radar"]
    raw = json.dumps(radar, ensure_ascii=False)
    for secret in ("红旗甲", "红旗乙", "c5", "c6"):
        assert secret not in raw
    flags = radar["red_flag_companies"]
    assert flags, "夹具应产出至少一家红旗公司"
    for c in flags:
        assert "brand_id" not in c and "name" not in c
        assert c["alias"].startswith("公司")


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
        secrets = {
            row[0] for row in real_conn.execute(
                "SELECT DISTINCT name FROM companies WHERE name IS NOT NULL AND name != ''"
            )
        }
        secrets |= {
            row[0] for row in real_conn.execute(
                "SELECT DISTINCT brand_id FROM companies"
            )
        }
        bundle = reportbundle.build_report_bundle(real_conn)
    assert bundle["meta"]["job_count"] > 0
    keys = set(_walk_keys(bundle))
    assert not (keys & reportbundle.FORBIDDEN_KEYS)
    raw = json.dumps(bundle, ensure_ascii=False)
    leaked = [s for s in secrets if len(s) >= 4 and s in raw]
    assert not leaked, f"真实公司名/brand_id 值级泄漏: {leaked[:5]}"
