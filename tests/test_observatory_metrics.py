"""观察台聚合函数黄金测试 (口径锁定)。

在内存库插入固定 fixture, 断言各聚合函数的输出——重点锁定 "薪资口径注册表"
(见 gaj/store/observatory.py 顶部) 的语义:
  - avg_salary 全引擎 = **中位数** (均值明显不同于中位时, 断言等于中位而非均值)
  - avg_salary_mean = **均值** (仅在保留处出现)
  - industry_detail 代表公司: 在招数优先 + 有公司分按分排前 (回归修复双 sort 覆盖)

任何对均值/中位口径的改动, 必须先改本测试——否则红灯。
"""

from __future__ import annotations

import pytest

from gaj.store import index, observatory


def _insert(conn, job_id, *, company_id="cA", salary_mid=20, industry="计算机软件",
            best_total=None, lat=None, lng=None, district="新区", skills='["python"]',
            overtime="moderate", outsourcing=0, travel="none"):
    conn.execute(
        """INSERT INTO jobs (job_id, title, company_id, company_name, city, district,
           salary_mid, exp_min, edu_level, industry, overtime, outsourcing, travel,
           skills, first_seen, last_seen, ignored, source_link, best_total, lat, lng)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (job_id, f"岗位{job_id}", company_id, f"公司{company_id}", "无锡", district,
         salary_mid, 3, 3, industry, overtime, outsourcing, travel,
         skills, "2026-09-01T10:00:00", "2026-09-08T10:00:00", 0, "",
         best_total, lat, lng),
    )
    conn.commit()


@pytest.fixture()
def conn():
    return index.connect(":memory:")


# ------------------------------------------------ avg_salary 全引擎 = 中位数

def test_industry_top_companies_avg_salary_is_median(conn):
    # [10,10,100]: 均值40 vs 中位10 → 须取中位, 均值仅存 avg_salary_mean
    _insert(conn, "j1", company_id="cA", salary_mid=10, best_total=9)
    _insert(conn, "j2", company_id="cA", salary_mid=10, best_total=8)
    _insert(conn, "j3", company_id="cA", salary_mid=100, best_total=7)
    d = observatory.observatory_industry_detail(conn, "计算机软件")
    top = next(c for c in d["top_companies"] if c["brand_id"] == "cA")
    assert top["avg_salary"] == 10.0        # 中位, 非均值40
    assert top["avg_salary_mean"] == 40.0   # 均值改名保留


def test_industry_top_skills_avg_salary_is_median(conn):
    # 三家不同公司、同一技能, 薪资 [10,10,100]
    _insert(conn, "s1", company_id="c1", salary_mid=10)
    _insert(conn, "s2", company_id="c2", salary_mid=10)
    _insert(conn, "s3", company_id="c3", salary_mid=100)
    d = observatory.observatory_industry_detail(conn, "计算机软件")
    skill = next(s for s in d["top_skills"] if s["name"] == "python")
    assert skill["avg_salary"] == 10.0
    assert skill["avg_salary_mean"] == 40.0


def test_geo_heatmap_avg_salary_is_median(conn):
    _insert(conn, "g1", salary_mid=10, lat=31.004, lng=120.003)
    _insert(conn, "g2", salary_mid=10, lat=31.004, lng=120.003)
    _insert(conn, "g3", salary_mid=100, lat=31.004, lng=120.003)
    h = observatory.observatory_geo_heatmap(conn)
    cell = h["cells"][0]
    assert cell["avg_salary"] == 10.0
    assert cell["avg_salary_mean"] == 40.0


def test_district_top_avg_salary_is_median(conn):
    _insert(conn, "d1", company_id="c1", salary_mid=10)
    _insert(conn, "d2", company_id="c2", salary_mid=10)
    _insert(conn, "d3", company_id="c3", salary_mid=100)
    tops = observatory.observatory_district_top(conn, top_n=10)
    item = next(i for i in tops if i["district"] == "新区")
    assert item["avg_salary"] == 10.0


def test_red_flag_companies_avg_salary_is_median(conn):
    _insert(conn, "r1", company_id="cR", salary_mid=10, outsourcing=1)
    _insert(conn, "r2", company_id="cR", salary_mid=10, outsourcing=1)
    _insert(conn, "r3", company_id="cR", salary_mid=100, outsourcing=1)
    radar = observatory.observatory_signal_radar(conn)
    comp = next(c for c in radar["red_flag_companies"] if c["brand_id"] == "cR")
    assert comp["avg_salary"] == 10.0
    assert comp["avg_salary_mean"] == 40.0


# ------------------------------------------------ 代表公司排序 (修双 sort 覆盖)

def test_industry_top_companies_sort_prefers_best_score(conn):
    # 两家公司各2岗、在招数相同: 高分公司 cX(best=9) 应排在 cY(best=8) 前。
    # 先插入 cY 再插入 cX; 旧 bug 的第二次 sort(按岗位数, 稳定)会保持插入序,
    # 使 cY 在前、忽略分数——本断言回归"有公司分按分排前"。
    _insert(conn, "t3", company_id="cY", best_total=8)
    _insert(conn, "t4", company_id="cY", best_total=4)
    _insert(conn, "t1", company_id="cX", best_total=9)
    _insert(conn, "t2", company_id="cX", best_total=5)
    d = observatory.observatory_industry_detail(conn, "计算机软件")
    order = [c["brand_id"] for c in d["top_companies"]]
    assert order[:2] == ["cX", "cY"]