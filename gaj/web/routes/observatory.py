"""市场观察台路由: 从 app.py 抽出的 APIRouter。

后续 G1/G2/S1 的路由也加在此模块。
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from ...store import index, observatory, observatory_snapshot, reportbundle
from ...store.index import CURRENT_SCOPE

router = APIRouter(prefix="/api/observatory", tags=["observatory"])

#: 各 tab → 快照 metrics 里的键 (仅这四个视图固化于快照; employer/quadrant 走实时)
_SNAP_KEY = {
    "salary": "salary_pricing",
    "geo": "geo_heatmap",
    "radar": "signal_radar",
    "skills": "skill_leaderboard",
}


def _snap_view(conn, snapshot: str, key: str, live_fn):
    """若指定了快照引用, 返回该快照固化的对应视图 (口径=当前 scope); 否则实时计算。"""
    if snapshot:
        scope = CURRENT_SCOPE.get()
        if scope:
            snap = observatory_snapshot.get_snapshot(conn, scope, snapshot)
            if snap:
                view = (snap.get("metrics") or {}).get(key)
                if view is not None:
                    return view
    return live_fn(conn)


@router.get("/snapshots")
async def api_obs_snapshots(scope: str = Query("")) -> dict:
    """某口径的历史快照列表 (供前端切换快照/纪元浏览历史市场数据)。"""
    with index.session() as conn:
        scope = (scope or "").strip()
        return {
            "scope": scope,
            "items": observatory_snapshot.list_snapshots(conn, scope) if scope else [],
        }


@router.get("/employer")
async def api_obs_employer() -> dict:
    """雇主画像 (schema 1.2+): 月薪构成/标注带宽/工时/规模/性质/福利/面议/活跃度。"""
    with index.session() as conn:
        return reportbundle._employer_block(conn)


@router.get("/salary")
async def api_obs_salary(snapshot: str = Query("")) -> dict:
    """薪资定价曲线: overall 分位 + by_exp/by_edu/by_industry/by_stage 中位数。"""
    with index.session() as conn:
        return _snap_view(conn, snapshot, "salary_pricing", observatory.observatory_salary_pricing)


@router.get("/geo")
async def api_obs_geo(cell_size: float = Query(0.01, ge=0.001, le=0.5),
                      snapshot: str = Query("")) -> dict:
    """区域机会热力图: 按 lat/lng 网格分桶, 返回中心点 + 边界。"""
    with index.session() as conn:
        return _snap_view(conn, snapshot, "geo_heatmap",
                          lambda c: observatory.observatory_geo_heatmap(c, cell_size=cell_size))


@router.get("/radar")
async def api_obs_radar(snapshot: str = Query("")) -> dict:
    """加班/红旗信号雷达: overtime 分布 + 外包/出差率 + 红旗公司榜。"""
    with index.session() as conn:
        return _snap_view(conn, snapshot, "signal_radar", observatory.observatory_signal_radar)


@router.get("/skills")
async def api_obs_skills(top_n: int = Query(40, le=200), snapshot: str = Query("")) -> dict:
    """技能热度榜: 需求岗位数 + 招聘公司数 + 均薪 + 薪资溢价。"""
    with index.session() as conn:
        return _snap_view(conn, snapshot, "skill_leaderboard",
                          lambda c: observatory.observatory_skill_leaderboard(c, top_n=top_n))


@router.get("/industry")
async def api_obs_industry_list() -> dict:
    """行业观察: 行业总览列表 (岗位/公司/薪资/红旗/技能, 排除未知)。"""
    with index.session() as conn:
        return observatory.observatory_industry_list(conn)


@router.get("/industry/{name:path}")
async def api_obs_industry_detail(name: str) -> dict:
    """行业观察: 单行业详情 (薪资分位/经验/技能/信号/代表公司)。"""
    with index.session() as conn:
        return observatory.observatory_industry_detail(conn, name)
