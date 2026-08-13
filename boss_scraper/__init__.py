"""
BOSS直聘自动化岗位采集工具

模块化架构:
  - cdp_session:   CDP 协议会话管理
  - har_parser:    HAR 文件解析与验证
  - network:       网络请求处理（分页 API 调用）
  - page_parser:   页面解析（DOM 提取、薪资解析、JD 拆分）
  - storage:       数据持久化存储
  - chrome_manager: Chrome 环境管理
  - crawler:       主控制模块（采集流程编排）
  - logger:        日志系统
"""

__version__ = "0.1.0"
