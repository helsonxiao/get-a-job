"""面向 AI 智能体的操作接口 (gaj agent ...)。

所有命令只在 stdout 输出一个 JSON 信封, 日志走 stderr 与 logs/,
退出码: 0=成功, 1=执行失败, 2=参数错误。

详见项目根目录 AGENT.md。
"""

from .cli import main

__all__ = ["main"]
