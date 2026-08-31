"""报告 ↔ 观察台 数据一致性测试 (防两套代码数字漂移)。

复用矩阵 (2026-08-31 审计):
  完全复用 observatory: salary_pricing / industry_list / industry_detail /
                        signal_radar / geo_heatmap / district_top
  报告口径差异 (有意): skill 榜 (中位+归一化 vs 均值) /
                        公司双榜 (中位+脱敏) / employer·functions·career_entry (报告专有)
本文件锁定「共享维度」的数字一致性 + 报告独立统计的守恒关系。
"""

from __future__ import annotations

import json

import pytest

from gaj.store import index, observatory, reportbundle


def _make_db():
    return index.connect(":memory:")


def _insert_job(conn, job_id, company_id, company_name, city, industry,
                salary_mid, exp_min, *, overtime="moderate", outsourcing=0,
                travel="none", edu_level=3, skills='["C++"]',
                first_seen="2026-08-01T10:00:00", last_seen="2026-08-20T10:00:00",
                ignored=0, source_link=""):
    conn.execute(
        """INSERT INTO jobs (job_id, title, company_id, company_name, city, district,
           salary_mid, exp_min, edu_level, industry, overtime, outsourcing, travel,
           skills, first_seen, last_seen, ignored, source_link)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (job_id, f"岗位{job_id}", company_id, company_name, city, "测试区",
         salary_mid, exp_min, edu_level, industry, overtime, outsourcing,
         travel, skills, first_seen, last_seen, ignored, source_link),
    )


@pytest.fixture()
def conn():
    c = _make_db()
    for i in range(4):
        _insert_job(c, f"j1{i}", "c1", "甲公司一", "无锡", "计算机软件", 20 + i, 3)
        _insert_job(c, f"j2{i}", "c2", "甲公司二", "苏州", "计算机软件", 25 + i, 5)
        _insert_job(c, f"j3{i}", "c3", "甲公司三", "无锡", "人工智能", 30 + i, 8)
    _insert_job(c, "j40", "c4", "乙公司", "无锡", "半导体/芯片", 18, 2)
    _insert_job(c, "j50", "c5", "红旗甲", "无锡", "计算机软件", 22, 3,
                overtime="heavy", ignored=1, source_link="https://x/a")
    c.commit()
    yield c
    c.close()


def test_shared_dimensions_match_observatory(conn):
    """共享维度: bundle 数字必须与观察台聚合完全一致 (同库同口径)。"""
    bundle = reportbundle.build_report_bundle(conn, include_ignored=False)
    market = bundle["market"]

    # 1) 行业表: 岗位/公司/中位/红旗
    obs_ind = {i["name"]: i for i in observatory.observatory_industry_list(conn)["items"]}
    for it in market["industry_list"]["items"]:
        o = obs_ind[it["name"]]
        assert it["job_count"] == o["job_count"]
        assert it["company_count"] == o["company_count"]
        assert it["salary_median"] == o["salary_median"]
        assert it["red_flag_count"] == o["red_flag_count"]

    # 2) 薪资分位: overall 五点 + 均值 + 样本
    obs_pricing = observatory.observatory_salary_pricing(conn)
    assert market["salary_pricing"]["overall"] == obs_pricing["overall"]
    assert market["salary_pricing"]["by_exp"] == obs_pricing["by_exp"]

    # 3) 信号雷达: 分布与占比
    assert market["signal_radar"]["summary"] == observatory.observatory_signal_radar(conn)["summary"]

    # 4) 区域热力: 网格总数
    # bundle geo 块将 total 重命名为 gps_total
    assert market["geo"]["gps_total"] == observatory.observatory_geo_heatmap(conn)["total"]

    # 5) 焦点行业详情: 分位/技能需求
    detail = bundle["focus"]["detail"]
    obs_detail = observatory.observatory_industry_detail(conn, bundle["focus"]["industry"])
    assert detail["salary"] == obs_detail["salary"]
    assert [s["name"] for s in detail["top_skills"]] == \
        [s["name"] for s in obs_detail["top_skills"]]


def test_skill_board_demand_matches_raw_expansion(conn):
    """报告技能榜需求量: 与独立展开 (raw skills JSON) 重算结果一致。"""
    _insert_job(conn, "j60", "c6", "丙公司", "无锡", "计算机软件", 28, 3,
                skills='["Python", "C#开发经验", "可适应出差", "AI大kanzhun模型"]')
    conn.commit()
    board = reportbundle.build_report_bundle(conn, include_ignored=False)["market"]["skill_leaderboard"]
    demand = {s["skill"]: s["demand_count"] for s in board["items"]}
    # 归一化语义: C#开发经验→c#, 水印清洗, 停用词滤除, 岗位内去重
    assert demand.get("python") == 1 and demand.get("c#") == 1
    assert demand.get("ai大模型") == 1
    assert "可适应出差" not in demand and "ai大kanzhun模型" not in demand


def test_include_ignored_conservation(conn):
    """含忽略守恒: 含忽略 job_count = 仅可见 job_count + 忽略数; 且口径声明报数。"""
    b_all = reportbundle.build_report_bundle(conn, include_ignored=True)
    b_vis = reportbundle.build_report_bundle(conn, include_ignored=False)
    ignored_n = conn.execute("SELECT COUNT(*) FROM jobs WHERE ignored = 1").fetchone()[0]
    assert b_all["meta"]["job_count"] == b_vis["meta"]["job_count"] + ignored_n
    assert b_all["meta"]["scope"]["ignored_included"] == ignored_n
    assert b_all["meta"]["scope"]["include_ignored"] is True
    assert b_vis["meta"]["scope"]["include_ignored"] is False
    assert b_vis["meta"]["scope"]["ignored_included"] == 0


def test_scope_and_ignored_combined(conn):
    """口径 × 忽略 组合: 指定链接且含忽略时, 数字 = 该链接全部行 (含被忽略的)。"""
    conn.execute("UPDATE jobs SET source_link = 'https://x/a' WHERE company_id IN ('c1','c5')")
    conn.commit()
    b = reportbundle.build_report_bundle(conn, scope_link="https://x/a", include_ignored=True)
    # c1=4 + c5=1(ignored) = 5; 影子放行 ignored
    assert b["meta"]["job_count"] == 5
    assert b["meta"]["scope"]["ignored_included"] == 1
    b2 = reportbundle.build_report_bundle(conn, scope_link="https://x/a", include_ignored=False)
    assert b2["meta"]["job_count"] == 4
