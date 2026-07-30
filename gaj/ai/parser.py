"""大模型回复解析器。

网页版大模型的输出不是干净的 JSON —— 经常被包在 ```json``` 代码块里,
或者前后带解释性文字, 偶尔还有尾随逗号、单引号、注释。这个解析器
用多策略提取 + 容错清洗, 尽可能把结构化数据捞出来。

提取策略 (按优先级):
  1. ```json ... ``` 代码块
  2. ``` ... ``` 代码块
  3. 从第一个 { 到最后一个 } 的子串
  4. 整段当纯文本返回 (兜底)
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ..logging_setup import get_logger

log = get_logger("ai.parser")

# ---------------------------------------------------------------- 提取 JSON 文本

_CODE_BLOCK_RE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.DOTALL)


def _extract_code_block(text: str) -> str | None:
    """从 ```json ... ``` 代码块中提取内容。"""
    matches = _CODE_BLOCK_RE.findall(text)
    for m in reversed(matches):  # 取最后一个代码块 (LLM 常先解释再给 JSON)
        candidate = m.strip()
        if candidate.startswith("{") or candidate.startswith("["):
            return candidate
    return None


def _extract_brace_range(text: str) -> str | None:
    """从第一个 { 到最后一个 } 的子串 (贪心匹配最外层花括号)。"""
    start = text.find("{")
    if start == -1:
        return None
    # 从后往前找 }
    end = text.rfind("}")
    if end == -1 or end <= start:
        return None
    return text[start : end + 1]


# ---------------------------------------------------------------- 清洗

_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_SINGLE_QUOTE_KEY_RE = re.compile(r"'(\w+)'\s*:")
_JS_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)


def _clean_json_text(text: str) -> str:
    """清洗常见的 JSON 格式问题。"""
    # 去掉 JS 风格注释
    text = _JS_COMMENT_RE.sub("", text)
    # 去掉尾随逗号 (}, ] 前的逗号)
    text = _TRAILING_COMMA_RE.sub(r"\1", text)
    # 单引号键名转双引号 (部分 LLM 会用 JS 对象语法)
    text = _SINGLE_QUOTE_KEY_RE.sub(r'"\1":', text)
    return text


def _try_parse(text: str) -> dict | None:
    """尝试解析 JSON, 失败返回 None。"""
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # 清洗后重试
    cleaned = _clean_json_text(text)
    if cleaned != text:
        try:
            obj = json.loads(cleaned)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    return None


def extract_json(text: str) -> dict | None:
    """从 LLM 回复文本中提取 JSON 对象。

    返回解析后的 dict, 失败返回 None。
    """
    if not text or not text.strip():
        return None

    # 策略 1: 代码块
    block = _extract_code_block(text)
    if block:
        obj = _try_parse(block)
        if obj:
            return obj

    # 策略 2: 花括号范围
    brace = _extract_brace_range(text)
    if brace:
        obj = _try_parse(brace)
        if obj:
            return obj

    # 策略 3: 整段直接试
    obj = _try_parse(text.strip())
    if obj:
        return obj

    log.warning(f"无法从 LLM 回复中提取 JSON (回复前 200 字: {text[:200]})")
    return None


# ---------------------------------------------------------------- 规范化打分结果

_VALID_STATUSES = {"PASS", "REVIEW", "REJECTED"}
_VALID_RECS = {"强烈推荐", "可以考虑", "不建议"}
_DIM_KEYS = ("finance", "growth", "resource", "wlb")


def _clamp(value: Any, lo: float, hi: float, default: float = 0.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return round(max(lo, min(hi, v)), 2)


def _coerce_str_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return []


def normalize_ai_score(
    raw: dict,
    provider: str,
    job_id: str = "",
    model: str = "",
) -> dict:
    """把 LLM 返回的 JSON 规范化成可落盘的打分记录。

    保证输出始终包含 repo/index 期望的字段, 缺失的用默认值补齐。
    """
    dims_raw = raw.get("dimension_scores") or {}
    dims = {k: _clamp(dims_raw.get(k), 0, 10) for k in _DIM_KEYS}

    status = raw.get("status", "").upper().strip()
    if status not in _VALID_STATUSES:
        # 用推荐结论反推
        rec = raw.get("recommendation", "")
        if rec == "不建议":
            status = "REJECTED"
        elif rec == "强烈推荐":
            status = "PASS"
        else:
            status = "REVIEW"

    total = raw.get("total_score")
    if total is None:
        # 没给总分就取四维平均
        total = round(sum(dims.values()) / len(dims), 2) if any(dims.values()) else 0.0
    total = _clamp(total, 0, 10)

    rec = raw.get("recommendation", "").strip()
    if rec not in _VALID_RECS:
        rec = {"PASS": "强烈推荐", "REVIEW": "可以考虑", "REJECTED": "不建议"}.get(status, "可以考虑")

    corrections = raw.get("ai_corrections") or {}
    # 如果 AI 修正了 wlb, 用修正值覆盖
    wlb_corr = corrections.get("wlb_corrected")
    if wlb_corr is not None:
        dims["wlb"] = _clamp(wlb_corr, 0, 10, dims["wlb"])

    return {
        "job_id": job_id,
        "provider": provider,
        "model": model or raw.get("model", ""),
        "status": status,
        "total_score": total,
        "dimension_scores": dims,
        "recommendation": rec,
        "recommendation_reason": raw.get("recommendation_reason", ""),
        "rule_answers": raw.get("rule_answers") or {},
        "ai_corrections": corrections,
        "highlights": _coerce_str_list(raw.get("highlights")),
        "risks": _coerce_str_list(raw.get("risks")),
        "interview_tips": _coerce_str_list(raw.get("interview_tips")),
        "deep_analysis_report": raw.get("deep_analysis_report", ""),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def parse_ai_response(raw_text: str, provider: str, job_id: str = "") -> dict | None:
    """完整解析流程: 提取 JSON → 规范化。"""
    obj = extract_json(raw_text)
    if not obj:
        return None
    return normalize_ai_score(obj, provider=provider, job_id=job_id)
