"""规则打分的可编辑覆盖项。

scoring.py 里的 max_points / 淘汰置信度下限等参数原本是硬编码常量,
改一次就得动代码。这里把它们抽到 data/scoring_config.json, Web 上直接编辑,
打分引擎读取覆盖值, 没填的字段回退到代码默认值。

约定: 只覆盖"数值阈值", 不改规则的结构和判定逻辑。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .. import config as cfg
from ..logging_setup import get_logger

log = get_logger("scoring_config")

SCORING_CONFIG_PATH: Path = cfg.DATA_ROOT / "scoring_config.json"

#: 评分项配比预设方案。每套覆盖全部评分项, 保证各维度满分之和 = 10。
#: 规则概览页一键套用, 免去逐项调整 23 个配置项的麻烦。
SCORING_ITEM_PRESETS: dict[str, dict[str, float]] = {
    "推荐均衡": {
        # finance = 10
        "f01_max": 4.0, "f02_max": 3.5, "f03_max": 1.0,
        "f04_max": 0.5, "f05_max": 0.5, "f06_max": 0.5,
        # growth = 10
        "g01_max": 3.0, "g02_max": 2.0, "g03_max": 2.0,
        "g04_max": 1.0, "g05_max": 0.5, "g06_max": 1.0, "g07_max": 0.5,
        # resource = 10
        "r01_max": 4.0, "r02_max": 2.0, "r03_max": 1.0,
        "r04_max": 1.0, "r05_max": 2.0,
        # wlb = 10
        "w01_max": 3.0, "w02_max": 2.0, "w03_max": 1.0,
        "w04_max": 3.0, "w05_max": 1.0,
    },
    "成长优先(3年价值)": {
        # finance = 10: 略降中位数, 升构成质量
        "f01_max": 4.0, "f02_max": 3.0, "f03_max": 1.0,
        "f04_max": 0.5, "f05_max": 0.5, "f06_max": 1.0,
        # growth = 10: G-01 技术栈降, G-07 前瞻性升
        "g01_max": 2.0, "g02_max": 2.0, "g03_max": 2.0,
        "g04_max": 1.0, "g05_max": 0.5, "g06_max": 1.0, "g07_max": 1.5,
        # resource = 10
        "r01_max": 4.0, "r02_max": 2.0, "r03_max": 1.0,
        "r04_max": 1.0, "r05_max": 2.0,
        # wlb = 10
        "w01_max": 3.0, "w02_max": 2.0, "w03_max": 1.0,
        "w04_max": 3.0, "w05_max": 1.0,
    },
    "WLB优先(真实时薪)": {
        # finance = 10
        "f01_max": 4.0, "f02_max": 3.5, "f03_max": 1.0,
        "f04_max": 0.5, "f05_max": 0.5, "f06_max": 0.5,
        # growth = 10
        "g01_max": 3.0, "g02_max": 2.0, "g03_max": 2.0,
        "g04_max": 1.0, "g05_max": 0.5, "g06_max": 1.0, "g07_max": 0.5,
        # resource = 10
        "r01_max": 4.0, "r02_max": 2.0, "r03_max": 1.0,
        "r04_max": 1.0, "r05_max": 2.0,
        # wlb = 10: W-01 弹性降, W-04 加班/W-05 通勤升
        "w01_max": 2.0, "w02_max": 2.0, "w03_max": 1.0,
        "w04_max": 4.0, "w05_max": 1.0,
    },
}


@dataclass
class ScoringOverrides:
    """打分参数覆盖。所有字段为 None 表示用代码默认值。"""

    # 全局
    reject_confidence_floor: float | None = None

    # 财务回报 (finance)
    f01_max: float | None = None  # 薪资下限达标
    f02_max: float | None = None  # 薪资中位数达标
    f03_max: float | None = None  # 薪资上限惊喜
    f04_max: float | None = None  # 股票/期权激励
    f05_max: float | None = None  # 高价值福利
    f06_max: float | None = None  # 薪资构成质量

    # 职业发展 (growth)
    g01_max: float | None = None  # 技术栈重合度
    g02_max: float | None = None  # 公司阶段价值
    g03_max: float | None = None  # 业务方向匹配
    g04_max: float | None = None  # 团队规模契合
    g05_max: float | None = None  # 经验学历兼容
    g06_max: float | None = None  # 技术深度信号
    g07_max: float | None = None  # 技术前瞻性

    # 资源匹配 (resource)
    r01_max: float | None = None  # 城市匹配
    r02_max: float | None = None  # 行业经验重叠
    r03_max: float | None = None  # 公司体量偏好
    r04_max: float | None = None  # 特殊资源(手动)
    r05_max: float | None = None  # 文化适配信号

    # 工作生活平衡 (wlb)
    w01_max: float | None = None  # 工作模式弹性
    w02_max: float | None = None  # 弹性福利信号
    w03_max: float | None = None  # 企业规范性
    w04_max: float | None = None  # 加班强度
    w05_max: float | None = None  # 通勤便利

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def get(self, code: str, default: float) -> float:
        """取某评分项的 max_points, None 回退到 default。

        code 形如 "F-01", 对应字段 f01_max (去掉连字符)。
        """
        key = code.lower().replace("-", "") + "_max"
        v = getattr(self, key, None)
        return float(v) if v is not None else float(default)

    def get_floor(self, default: float) -> float:
        return float(self.reject_confidence_floor) if self.reject_confidence_floor is not None else float(default)


def load_overrides(path: Path | None = None) -> ScoringOverrides:
    path = path or SCORING_CONFIG_PATH
    if not path.exists():
        return ScoringOverrides()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # 过滤掉未知键, 防止旧字段污染
        known = {f for f in ScoringOverrides.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in known}
        return ScoringOverrides(**clean)
    except Exception as e:
        log.warning(f"读取 scoring_config.json 失败, 用默认值: {e}")
        return ScoringOverrides()


def save_overrides(overrides: ScoringOverrides, path: Path | None = None) -> Path:
    path = path or SCORING_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(overrides.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"打分覆盖已保存到 {path}")
    return path
