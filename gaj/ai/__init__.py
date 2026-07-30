"""AI 编排模块 —— 通过网页版大模型进行打分复核和简历优化。

核心入口:
  - score_with_ai():  单个职位 AI 打分
  - batch_score():    批量 AI 打分
  - build_prompt():   构建 (不调用) 提示词, 用于 dry-run
"""

from .parser import extract_json, normalize_ai_score, parse_ai_response
from .prompts import build_resume_prompt, build_scoring_prompt
from .runner import batch_score, build_prompt, score_with_ai

__all__ = [
    "score_with_ai",
    "batch_score",
    "build_prompt",
    "build_scoring_prompt",
    "build_resume_prompt",
    "parse_ai_response",
    "normalize_ai_score",
    "extract_json",
]
