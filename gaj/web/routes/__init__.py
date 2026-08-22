"""Web API 路由聚合: 每个功能域一个模块, app.py 统一挂载。

模块地图 (与前端 views/ 一一对应):
  jobs        职位: 列表/详情/收藏/忽略/调分/AI 打分触发/删除
  companies   公司: 图鉴三榜/详情/想去/公司级 AI 评价
  observatory 市场观察台: 薪资/热力/雷达/技能 四视角
  scoring     规则打分: 规则目录/参数覆盖/预设/批量打分
  config      AI 规则矫正: 建议 dry-run/应用/回写
  profile     画像: 读取/保存/权重预设
  resume      主简历: 读取/保存
  system      系统: 统计/任务/Provider/索引重建/SSE 日志流
"""
from . import companies, config, jobs, observatory, profile, resume, scoring, system

#: app.py 按此列表 include_router
ALL_ROUTERS = [
    jobs.router,
    companies.router,
    observatory.router,
    scoring.router,
    config.router,
    profile.router,
    resume.router,
    system.router,
]
