"""scope_members 成员表: connect() 迁移回填 / reindex 重建不丢成员 / 重迁移首归合并。

文件仓储 + index.db 全部重定向到函数级临时目录, 不触碰真实 data/。
"""

from __future__ import annotations

import pytest

from gaj import config as cfg
from gaj.core.models import Job
from gaj.store import index, observatory_snapshot as obsnap, repo


@pytest.fixture()
def isolated_repo(tmp_path, monkeypatch):
    """repo 文件侧 + index.db 重定向到本测试专属临时目录 (每测试全新, 互不干扰)。"""
    (tmp_path / "jobs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "companies").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cfg, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(cfg, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(cfg, "COMPANIES_DIR", tmp_path / "companies")
    monkeypatch.setattr(cfg, "INDEX_DB", tmp_path / "index.db")
    return tmp_path


def _make_job(job_id: str, source_link: str = "") -> Job:
    job = Job.build(
        job_id=job_id,
        list_item={"jobName": f"岗位{job_id}", "salaryDesc": "20k", "cityName": "无锡"},
        jd_dom={"jd_full": "x"},
        company=None,
        blacklist=set(),
    )
    job.source_link = source_link
    return job


def _member_rows(job_id=None):
    conn = index.connect()
    try:
        sql = "SELECT source_link, job_id, first_seen_at, last_epoch_id FROM scope_members"
        params: tuple = ()
        if job_id:
            sql += " WHERE job_id = ?"
            params = (job_id,)
        return sorted(tuple(r) for r in conn.execute(sql, params))
    finally:
        conn.close()


def test_connect_backfills_scope_members(isolated_repo):
    """_migrate 回填: 有 source_link 的存量岗位各展开一条成员行 (last_epoch_id 取
    collection_epoch), 无 source_link 岗位不回填; 重复 connect 幂等不翻倍。"""
    conn = index.connect()  # 先建 schema (空库, 回填空跑)
    conn.executemany(
        """INSERT INTO jobs (job_id, title, company_id, company_name, city, salary_mid,
           first_seen, last_seen, source_link, collection_epoch)
           VALUES (?, ?, 'c1', '公司一', '无锡', 20,
                   '2026-09-01T10:00:00', '2026-09-02T10:00:00', ?, ?)""",
        [
            ("b1", "岗位b1", "https://s/a", "ep-1"),
            ("b2", "岗位b2", "https://s/b", "ep-2"),
            ("b3", "岗位b3", "", ""),
        ],
    )
    conn.commit()
    conn.close()

    conn = index.connect()  # 再次 connect 触发 _migrate 存量回填
    try:
        rows = {
            r["job_id"]: (r["source_link"], r["last_epoch_id"])
            for r in conn.execute(
                "SELECT source_link, job_id, last_epoch_id FROM scope_members")
        }
    finally:
        conn.close()
    assert rows == {
        "b1": ("https://s/a", "ep-1"),
        "b2": ("https://s/b", "ep-2"),
    }, "有 source_link 的岗位各回填一条成员行, 无 source_link 岗位跳过"

    # 重复 connect 幂等: 行数不翻倍, sync_scope_members 兜底同样不翻倍
    conn = index.connect()
    try:
        n = conn.execute("SELECT COUNT(*) FROM scope_members").fetchone()[0]
        obsnap.sync_scope_members(conn)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM scope_members").fetchone()[0] == n
    finally:
        conn.close()
    assert n == 2


def test_reindex_restores_scope_members(isolated_repo):
    """重建不丢成员: provenance.scope_links=[A,B] + scope_member_epochs 有值 →
    reindex 后 A、B 成员行都在且 last_epoch_id 恢复正确; 重复 reindex 幂等。"""
    link_a, link_b = "https://r/a", "https://r/b"
    repo.save_job(_make_job("rj1"))
    repo.update_source_link("rj1", link_a)  # 文件侧首归
    epoch_a = obsnap.active_epoch_id(link_a)
    epoch_b = obsnap.active_epoch_id(link_b)
    repo.add_scope_link("rj1", link_a, epoch_a)
    repo.add_scope_link("rj1", link_b, epoch_b)  # append-only → [A, B]
    assert repo.load_job("rj1").provenance["scope_links"] == [link_a, link_b]

    assert index.reindex()["jobs"] == 1
    rows = {r[0]: r[3] for r in _member_rows("rj1")}
    assert set(rows) == {link_a, link_b}, "成员 A、B 两行都从文件恢复"
    assert rows[link_a] == epoch_a and rows[link_b] == epoch_b, "last_epoch_id 恢复正确"

    before = _member_rows("rj1")
    index.reindex()  # 重复重建: 不翻倍、纪元不丢
    assert _member_rows("rj1") == before


def test_remigrate_preserves_first_scope_and_links(isolated_repo):
    """重迁移合并: 以 existing provenance 为基底 —— 首归 source_link 与
    scope_links 不被新口径冲掉 (回归「重迁移冲掉 scope_links」的修复)。"""
    from gaj.store.migrate import LegacyRecord, _migrate_record

    link_a, link_b = "https://m/a", "https://m/b"

    def _rec():
        return LegacyRecord(
            dirname="001",
            meta={
                "source_url": "https://www.zhipin.com/job_detail/mj1.html",
                "crawled_at": "2026-09-01T10:00:00",
                "company_name": "测试公司",
                "brand_id": "brand-1",
                "company_url": "https://www.zhipin.com/gongsi/brand-1.html",
            },
            jd_dom={
                "job_name": "岗位mj1",
                "jd_full": "岗位职责: 测试",
                "company_address": "无锡市测试区",
                "url": "https://www.zhipin.com/job_detail/mj1.html",
            },
        )

    kwargs = dict(blacklist=[], conflicts={}, company_cache={})
    assert _migrate_record(_rec(), src=isolated_repo, source_link=link_a, **kwargs)
    assert _migrate_record(_rec(), src=isolated_repo, source_link=link_b, **kwargs)

    j = repo.load_job("mj1")
    assert j.source_link == link_a, "首归冻结: 重迁移不改写 source_link"
    assert j.provenance["source_link"] == link_a, "provenance.source_link 保持首归"
    assert j.provenance["scope_links"] == [link_a, link_b], "scope_links 以 existing 为基底合并"
