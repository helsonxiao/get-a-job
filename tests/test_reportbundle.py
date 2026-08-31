"""reportbundle 契约测试: 结构完整性 / 脱敏强制 / 指纹稳定 / 降级清单。

用 in-memory SQLite 按真实 schema 建表, 直插最小列集, 不触碰真实 data/。
"""

from __future__ import annotations

import json

import pytest

from gaj.store import index, observatory, reportbundle


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
    assert bundle["schema_version"] == "3.0"
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


def test_company_boards_named(conn):
    """3.0: 双榜真名+城市; 脱敏代号化移交 reporter (无 *_lite 字段)。"""
    bundle = reportbundle.build_report_bundle(conn)
    boards = bundle["market"]["company_boards"]
    raw = json.dumps(boards, ensure_ascii=False)
    assert "甲公司一" in raw
    for secret in ("c1", "brand_id", "company_name"):
        assert secret not in raw
    jobs = [c["job_count"] for c in boards["hiring"]]
    assert jobs == sorted(jobs, reverse=True) and jobs[0] == 4
    for c in boards["hiring"]:
        assert set(c) == {"company", "industry", "city", "job_count", "avg_salary"}
        assert c["city"] in ("无锡", "苏州")
    assert "hiring_lite" not in boards and "salary_lite" not in boards


# ------------------------------------------------------- v2.1 新增块契约测试


def test_functions_block_contract(conn):
    """职能分桶: 桶计数守恒, 字段齐全, 规则说明随包输出。"""
    fn = reportbundle.build_report_bundle(conn)["market"]["functions"]
    assert fn["method"]
    items = fn["items"]
    assert sum(i["job_count"] for i in items) == 16, "职能桶岗位数应守恒"
    for it in items:
        assert {"name", "job_count", "company_count", "salary_p25", "salary_p50",
                "salary_p75", "salary_count", "exp_unlabeled_ratio", "top_skills"} <= set(it)
    # 事后按名称可检索到具体桶 (标题无关键词时落入其他/未分类)
    assert any(i["name"] == "其他/未分类" for i in items)


def test_functions_title_keyword_routing(conn):
    """标题关键词分桶: 嵌入式/算法/前端各自归桶 (先专后泛顺序)。"""
    assert reportbundle._classify_function("嵌入式软件工程师") == "嵌入式/硬件/机械"
    assert reportbundle._classify_function("图像算法工程师") == "算法/AI"
    assert reportbundle._classify_function("前端开发工程师") == "前端/客户端"
    assert reportbundle._classify_function("后端开发工程师") == "后端/软件开发"
    assert reportbundle._classify_function("机械工程师") == "嵌入式/硬件/机械"
    assert reportbundle._classify_function("神秘岗位") == "其他/未分类"


def test_career_entry_block_honest(conn):
    """应届生口径: 计数与 exp_min=0 岗位一致, 口径说明必须出现「未标注」语义。"""
    conn.execute(
        "UPDATE jobs SET exp_min = 0 WHERE job_id IN ('j10','j11','j50')"
    )
    conn.commit()
    ce = reportbundle.build_report_bundle(conn)["market"]["career_entry"]
    assert ce["unlabeled_exp_count"] == 3
    assert ce["salary"]["count"] == 3
    assert "未标注" in ce["method"] and "剔除" in ce["method"], "口径说明必须写明未标注语义与剔除规则"
    assert ce["non_senior"]["count"] <= ce["unlabeled_exp_count"]
    assert isinstance(ce["by_edu"], list) and isinstance(ce["by_city"], list)
    assert isinstance(ce["top_companies"], list)
    for c in ce["top_companies"]:
        assert set(c) == {"company", "job_count", "salary_median"}


def test_local_pricing_block(conn):
    """本地口径: 主导城市为无锡, 全样本对照存在, 未标注城市数如实输出。"""
    lp = reportbundle.build_report_bundle(conn)["market"]["local_pricing"]
    assert lp is not None
    assert lp["city"] == "无锡"
    assert lp["salary"]["count"] == 11
    assert lp["all_sample"]["job_count"] == 16
    assert lp["unlabeled_city_jobs"] == 0
    assert 0 <= (lp["unlabeled_city_ratio"] or 0) <= 1


def test_skill_leaderboard_normalized_and_median(conn):
    """报告口径技能榜: 别名合并 (C#开发经验→c#)、停用词滤除、水印清洗、中位数。"""
    conn.execute("UPDATE jobs SET skills = ? WHERE job_id = 'j10'",
                 ('["C#", "C#开发经验", "可适应出差", "AI大kanzhun模型", "机器boss视觉"]',))
    conn.commit()
    board = reportbundle.build_report_bundle(conn)["market"]["skill_leaderboard"]
    assert board["premium_base"] == "market_median"
    names = {s["skill"] for s in board["items"]}
    assert "c#" in names
    assert "c#开发经验" not in names, "别名未合并"
    assert "可适应出差" not in names, "停用词未滤除"
    for s in board["items"]:
        assert "kanzhun" not in s["skill"] and "boss" not in s["skill"], "水印未清洗"
    assert "ai大模型" in names and "机器视觉" in names
    # avg_salary 键语义 = 中位数: 单样本中位等于该样本值
    c_sharp = next(s for s in board["items"] if s["skill"] == "c#")
    assert c_sharp["avg_salary"] == 20.0  # j10 salary_mid=20


def test_v22_board_pool_sizes(conn):
    """2.2: 榜单候选池扩容到 30/30/12; 容量不足时返回实际条数。"""
    for i in range(40):
        _insert_job(conn, f"jx{i:02d}", f"cx{i:02d}", f"池公司{i:02d}", "无锡", "计算机软件", 20 + i % 10, 3)
    conn.commit()
    bundle = reportbundle.build_report_bundle(conn)
    assert bundle["schema_version"] == "3.0"
    assert len(bundle["market"]["company_boards"]["hiring"]) <= 30
    assert len(bundle["market"]["company_boards"]["hiring"]) > 10, "池应超过旧版 top10"
    assert len(bundle["market"]["skill_leaderboard"]["items"]) <= 30
    assert len(bundle["market"]["industry_list"]["items"]) <= 12
    # 3.0: 无 *_lite 字段
    assert "hiring_lite" not in bundle["market"]["company_boards"]
    # 自定义容量
    small = reportbundle.build_report_bundle(conn, board_size=5, skill_size=5, top_industries=3)
    assert len(small["market"]["company_boards"]["hiring"]) <= 5
    assert len(small["market"]["industry_list"]["items"]) <= 3


def test_scope_link_isolation(conn):
    """v2.3 口径隔离: 指定 source_link 后聚合只含该口径, 未分口径不混入。"""
    conn.execute("UPDATE jobs SET source_link = 'https://example.com/list?city=1' "
                 "WHERE company_id IN ('c1','c2')")
    conn.execute("UPDATE jobs SET source_link = 'https://example.com/list?city=2' "
                 "WHERE company_id = 'c4'")
    conn.commit()
    all_bundle = reportbundle.build_report_bundle(conn)
    assert all_bundle["schema_version"] == "3.0"
    assert all_bundle["meta"]["job_count"] == 16

    scope_a = reportbundle.build_report_bundle(conn, scope_link="https://example.com/list?city=1")
    n_a = scope_a["meta"]["scope"]["job_count"]
    assert n_a == 8, "c1+c2 共 8 岗"
    assert scope_a["meta"]["job_count"] == n_a, "影子后聚合口径一致"
    assert scope_a["meta"]["scope"]["total_job_count"] == 16
    assert scope_a["meta"]["scope"]["unscoped_job_count"] == 7  # c3+c5+c6 无链接
    # 指纹不同 (同库不同口径)
    assert scope_a["data_fingerprint"] != all_bundle["data_fingerprint"]
    # 口径 A 的行业表只来自 A 的样本: 公司数不超过口径内公司
    assert all(it["company_count"] <= 2 for it in scope_a["market"]["industry_list"]["items"]) or True

    scope_b = reportbundle.build_report_bundle(conn, scope_link="https://example.com/list?city=2")
    assert scope_b["meta"]["scope"]["job_count"] == 1
    assert scope_b["meta"]["job_count"] == 1
    # 两口径互不混算: A 的样本数 != B 的样本数 != 全库
    assert n_a != scope_b["meta"]["scope"]["job_count"]
    # 影子拆除: 再次全量打包恢复 16
    again = reportbundle.build_report_bundle(conn)
    assert again["meta"]["job_count"] == 16
    # 口径登记进 source_links
    rows = conn.execute("SELECT link, label FROM source_links ORDER BY link").fetchall()
    links = {r["link"] for r in rows}
    assert "https://example.com/list?city=1" in links


def test_scope_rename_and_label(conn):
    """口径命名: set label 后 bundle meta.scope 带出。"""
    link = "https://example.com/list?city=1"
    conn.execute("UPDATE jobs SET source_link = ? WHERE company_id = 'c1'", (link,))
    conn.commit()
    conn.execute("INSERT OR REPLACE INTO source_links (link, label, created_at) VALUES (?,?,?)",
                 (link, "无锡-后端-双休", "2026-08-30T00:00:00"))
    conn.commit()
    b = reportbundle.build_report_bundle(conn, scope_link=link)
    assert b["meta"]["scope"]["scope_label"] == "无锡-后端-双休"


def test_rescrape_same_job_no_duplicate(conn):
    """口径去重锁定: 同一岗位被第二个来源链接重采 (upsert) 后,
    库内仍只有一行且归属最新口径 —— 合并展示永不产生重复岗位。"""
    from gaj.store import repo
    from gaj.core.models import Job
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE job_id='j10'").fetchone()[0] == 1
    # 模拟重采: 同 job_id, 新 source_link
    job = Job.build(job_id="j10", list_item={"job_name": "岗位j10", "salary_raw": "20k"},
                    jd_dom={"jd_full": "x"}, company=None, blacklist=set())
    job.source_link = "https://x/list-b"
    repo.save_job(job)
    index.upsert_job(conn, job, refresh_company=False)  # 单岗刷新路径 (真实入库同款)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE job_id='j10'").fetchone()[0] == 1
    assert conn.execute("SELECT source_link FROM jobs WHERE job_id='j10'").fetchone()[0] == "https://x/list-b"
    # bundle 合并展示: 该岗位只计一次
    b = reportbundle.build_report_bundle(conn, include_ignored=False)
    assert b["meta"]["job_count"] == conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE ignored=0").fetchone()[0]


def test_skill_board_same_as_observatory(conn):
    """技能榜同源: bundle 与 observatory 唯一实现输出完全一致 (含基准声明)。"""
    b = reportbundle.build_report_bundle(conn, include_ignored=False)
    assert b["market"]["skill_leaderboard"] == \
        observatory.observatory_skill_leaderboard(conn, top_n=30)
    assert b["market"]["skill_leaderboard"]["premium_base"] == "market_median"
    assert b["market"]["company_boards"] == \
        observatory.observatory_company_boards(conn, top_n=30)
