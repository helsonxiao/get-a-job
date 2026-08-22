"""市场观察台路由: 从 app.py 抽出的 APIRouter。

后续 G1/G2/S1 的路由也加在此模块。
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from ...store import index, observatory

router = APIRouter(prefix="/api/observatory", tags=["observatory"])


@router.get("/salary")
async def api_obs_salary() -> dict:
    """薪资定价曲线: overall 分位 + by_exp/by_edu/by_industry/by_stage 中位数。"""
    with index.session() as conn:
        return observatory.observatory_salary_pricing(conn)


@router.get("/geo")
async def api_obs_geo(cell_size: float = Query(0.01, ge=0.001, le=0.5)) -> dict:
    """区域机会热力图: 按 lat/lng 网格分桶, 返回中心点 + 边界。"""
    with index.session() as conn:
        return observatory.observatory_geo_heatmap(conn, cell_size=cell_size)


@router.get("/radar")
async def api_obs_radar() -> dict:
    """加班/红旗信号雷达: overtime 分布 + 外包/出差率 + 红旗公司榜。"""
    with index.session() as conn:
        return observatory.observatory_signal_radar(conn)


@router.get("/skills")
async def api_obs_skills(top_n: int = Query(40, le=200)) -> dict:
    """技能热度榜: 需求岗位数 + 招聘公司数 + 均薪 + 薪资溢价。"""
    with index.session() as conn:
        return observatory.observatory_skill_leaderboard(conn, top_n=top_n)
