"""规则配置 AI 矫正器。

功能: 根据当前画像预期 + 打分覆盖 + 市场观察台数据, 生成提示词调用网页版
大模型, 产出"配置调整建议"; 建议仅落盘展示, 必须人工在前端复核勾选后
才会通过既有的 PUT /api/profile 与 PUT /api/scoring-config 应用。

设计约定 (与 company_runner 对称):
  - 纯手动触发, 后台线程执行, 进度走 SSE 日志
  - append-only 落盘到 data/calibration/calibrate_<provider>_<时间戳>.json
  - 解析失败也保存原始回复, 前端可查看/人工重试
  - 建议字段必须命中白名单 (profile 字段名 ∪ scoring override 字段名),
    未知字段直接丢弃, 防止大模型幻觉污染配置
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .. import config as cfg
from ..logging_setup import get_logger
from .parser import extract_json

log = get_logger("ai.calibrator")

#: 网页版大模型引用标记
_CITATION_RE = re.compile(r"\[\s*citation\s*:\s*\d+\s*\]", re.IGNORECASE)


def _strip_citations(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s{2,}", " ", _CITATION_RE.sub("", text)).strip()


# ---------------------------------------------------------------- 落盘


def calibration_dir() -> Path:
    d = cfg.CALIBRATION_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_calibrations() -> list[dict]:
    """列出全部矫正记录 (按时间倒序), 含解析失败的历史。"""
    out: list[dict] = []
    for p in sorted(calibration_dir().glob("calibrate_*.json"), reverse=True):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def latest_calibration() -> dict | None:
    items = list_calibrations()
    return items[0] if items else None


# ---------------------------------------------------------------- 上下文收集


def _collect_market_data() -> dict:
    """拉取市场观察台四类聚合, 作为矫正的市场事实输入。"""
    from ..store import index, observatory

    with index.session() as conn:
        salary = observatory.observatory_salary_pricing(conn)
        skills = observatory.observatory_skill_leaderboard(conn, top_n=15)
        radar = observatory.observatory_signal_radar(conn)
    return {
        "salary": salary,
        "skills": skills,
        "radar_summary": radar.get("summary"),
        "radar_top_industries": (radar.get("by_industry") or [])[:8],
    }


def _collect_current_config() -> dict:
    """当前画像 (可调字段) + 打分覆盖 + 规则目录 (标签与当前生效值)。"""
    from ..core.profile import load_profile
    from ..core.scoring import build_rules_catalog
    from ..core.scoring_config import load_overrides

    profile = load_profile()
    pfields = profile.to_dict()
    pfields.pop("raw", None)
    catalog = build_rules_catalog(profile=profile)
    # 评分项: {override_key: {label, max, default_max}}
    items: dict[str, dict] = {}
    for dim in catalog.get("dimensions", []):
        for it in dim.get("items", []):
            items[it["override_key"]] = {
                "label": f"{dim.get('name', '')}·{it.get('label', '')}",
                "current_max": it.get("max"),
                "default_max": it.get("default_max"),
            }
    return {
        "profile_fields": pfields,
        "scoring_items": items,
        "reject_confidence_floor": catalog.get("reject_confidence_floor"),
        "weights": profile.normalized_weights,
        "overrides_raw": load_overrides().to_dict(),
    }


# ---------------------------------------------------------------- 提示词


def build_calibration_prompt() -> str:
    """构建矫正提示词 (不调用大模型), 供 dry-run / 调试。"""
    conf = _collect_current_config()
    market = _collect_market_data()

    # 只给可安全调整的画像字段 (排除坐标/性别等硬信息)
    tunable = {
        k: v for k, v in conf["profile_fields"].items()
        if k in _TUNABLE_PROFILE_FIELDS
    }
    payload = {
        "当前画像预期(可调整字段)": tunable,
        "四维权重": conf["weights"],
        "评分项配置(可调整字段 scoring)": conf["scoring_items"],
        "淘汰置信度下限": conf["reject_confidence_floor"],
        "市场数据": {
            "薪资定价": market["salary"],
            "技能热度Top15": market["skills"],
            "红旗信号汇总": market["radar_summary"],
            "红旗行业Top8": market["radar_top_industries"],
        },
    }
    body = json.dumps(payload, ensure_ascii=False, indent=1)
    return f"""你是一个求职策略顾问。我有一套基于规则的岗位打分系统, 配置分为两部分:
1. 画像预期 (profile): 薪资预期/城市/排除项/偏好等个人参数
2. 评分配置 (scoring): 23 个评分项的满分配比 (四个维度 finance/growth/resource/wlb 各 10 分)

下面是我的当前配置和市场数据分析结果。请对比我的预期与市场现实, 给出配置矫正建议。

{body}

要求:
- 只建议调整数值型字段, 不要建议增删字段或改动规则结构
- scoring 各维度的建议值之和应保持 10 分 (每维度)
- 每条建议必须有明确的市场数据依据, 不确定就不建议
- 建议数量控制在 3~10 条, 只提真正值得改的

请严格按照以下 JSON 格式输出, 不要在 JSON 外面加任何文字:

```json
{{
  "market_reading": "150字以内, 对市场数据的核心解读 (薪资水位/技能需求/红旗信号)",
  "suggestions": [
    {{
      "target": "profile 或 scoring",
      "field": "字段名, 如 expect_min_salary_10k 或 f01_max",
      "current": 35,
      "suggested": 38,
      "reason": "80字以内, 引用市场数据说明为什么改"
    }}
  ],
  "summary": "100字以内, 总体策略建议"
}}
```"""


#: 画像中允许 AI 建议调整的字段白名单 (数值/列表型偏好, 不含身份硬信息)
_TUNABLE_PROFILE_FIELDS = {
    "hard_min_salary_10k", "expect_min_salary_10k", "expect_max_salary_10k",
    "max_commute_minutes", "reject_scale_below",
    "weight_growth", "weight_finance", "weight_wlb", "weight_resource",
    "team_size_min", "team_size_max",
}


def _valid_scoring_fields() -> set[str]:
    from ..core.scoring_config import ScoringOverrides

    return set(ScoringOverrides.__dataclass_fields__)


# ---------------------------------------------------------------- 解析


def parse_calibration(raw_text: str, provider: str) -> dict | None:
    """解析大模型回复为矫正建议, 未知字段/非法值丢弃。失败返回 None。"""
    obj = extract_json(raw_text)
    if not obj or not isinstance(obj.get("suggestions"), list):
        return None

    valid_scoring = _valid_scoring_fields()
    suggestions: list[dict] = []
    for s in obj["suggestions"]:
        if not isinstance(s, dict):
            continue
        target = str(s.get("target", "")).strip()
        field = str(s.get("field", "")).strip()
        if target == "profile" and field not in _TUNABLE_PROFILE_FIELDS:
            continue
        if target == "scoring" and field not in valid_scoring:
            continue
        try:
            current = s.get("current")
            suggested = s.get("suggested")
            if suggested is None:
                continue
            suggested = float(suggested)
        except (TypeError, ValueError):
            continue
        suggestions.append({
            "target": target,
            "field": field,
            "current": current,
            "suggested": round(suggested, 2),
            "reason": _strip_citations(str(s.get("reason", "") or "")),
        })

    if not suggestions:
        return None
    return {
        "kind": "config_calibration",
        "status": "OK",
        "provider": provider,
        "market_reading": _strip_citations(str(obj.get("market_reading", "") or "")),
        "summary": _strip_citations(str(obj.get("summary", "") or "")),
        "suggestions": suggestions,
        "applied": [],  # 前端人工复核应用后回写: [{field, at}]
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


# ---------------------------------------------------------------- 执行


def run_calibration(provider: str = "deepseek") -> dict:
    """执行一次矫正: 生成 prompt → 网页大模型 → 解析 → 落盘。

    Returns: {success, provider, result, raw_response, error, elapsed, file}
    """
    started = time.time()
    prompt = build_calibration_prompt()
    log.info(f"[calibrate] AI 矫正开始 | provider={provider} | prompt={len(prompt)} 字")

    own_driver = False
    raw_response = ""
    error = None
    result = None
    path: Path | None = None

    try:
        from ..browser import get_driver

        driver = get_driver(provider)
        own_driver = True
        raw_response = driver.ask(prompt)
        log.info(f"[calibrate] 大模型回复 {len(raw_response)} 字")

        result = parse_calibration(raw_response, provider=provider)
        if result:
            result["raw_response"] = raw_response
            path = _save(provider, result)
            log.info(
                f"[calibrate] 矫正完成: {len(result['suggestions'])} 条建议 -> {path.name if path else ''}"
            )
        else:
            error = "无法从回复中解析出矫正建议 JSON"
            log.warning(f"[calibrate] {error}, 原始回复前 300 字: {raw_response[:300]}")
            path = _save_raw_fallback(provider, raw_response, prompt)
    except Exception as exc:
        error = str(exc)
        log.error(f"[calibrate] AI 矫正失败: {error}")
        if raw_response:
            path = _save_raw_fallback(provider, raw_response, prompt)
    finally:
        if own_driver:
            try:
                from ..browser import close_driver

                close_driver(driver)
            except Exception:
                pass

    return {
        "success": result is not None,
        "provider": provider,
        "result": result,
        "raw_response": raw_response,
        "error": error,
        "elapsed": round(time.time() - started, 1),
        "file": path.name if path else None,
    }


def _save(provider: str, result: dict) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    path = calibration_dir() / f"calibrate_{provider}_{stamp}.json"
    result["file"] = path.name
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def _save_raw_fallback(provider: str, raw: str, prompt: str) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    path = calibration_dir() / f"calibrate_{provider}_raw_{stamp}.json"
    payload = {
        "kind": "config_calibration",
        "status": "PARSE_FAILED",
        "provider": provider,
        "raw_response": raw,
        "prompt": prompt[:8000],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"原始回复已保存: {path.name}")
    return path


def mark_applied(file_name: str, applied_fields: list[str]) -> dict | None:
    """前端人工复核应用后, 把已应用字段回写到矫正记录 (幂等追加)。"""
    if not applied_fields:
        return None
    # 防路径穿越: 只取文件名部分
    path = calibration_dir() / Path(file_name).name
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    existed = {a.get("field") for a in data.get("applied", [])}
    for f in applied_fields:
        if f not in existed:
            data.setdefault("applied", []).append(
                {"field": f, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            )
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data
