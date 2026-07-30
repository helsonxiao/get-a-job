"""文本清洗 —— 对抗 BOSS 直聘的反爬文本污染。

实测发现两类污染, 都会让下游的关键词匹配和大模型理解出错:

1. 随机插入的水印碎片
   BOSS 在 DOM 里塞隐藏元素, textContent 会把它们连着正文一起取出来:
       "后端开发来自BOSS直聘经验"  ->  "后端开发经验"
       "计直聘算机相关专业"        ->  "计算机相关专业"
       "岗位职直聘责：1、"          ->  "岗位职责：1、"
   插入的永远是 "来自BOSS直聘" 这个串的前缀/子串, 所以按长度倒序剥离即可。

2. 康熙部首替身字 (U+2F00 区段)
   "⼯程" 里的 ⼯ 是 U+2F27 KANGXI RADICAL WORK, 不是 U+5DE5 的 工。
   肉眼几乎看不出差别, 但 "工程" 关键词匹配会直接失效。
   同类的还有 ⽤/用、⾼/高、⽬/目、⼈/人。
   NFKC 归一化可以把它们映射回标准汉字。

注意: 这里刻意 **不** 对全角标点做 NFKC, 否则中文的 ，；：（） 会被压成
半角, 正文读起来会很难看。只针对汉字替身字做定向修复。
"""

from __future__ import annotations

import html
import re
import unicodedata

from ..logging_setup import get_logger

log = get_logger("denoise")

# 按长度倒序 —— 必须先剥离长串, 否则 "来自BOSS直聘" 会被拆成残渣
WATERMARK_TOKENS: tuple[str, ...] = (
    "来自BOSS直聘",
    "来自boss直聘",
    "BOSS直聘",
    "boss直聘",
    "Boss直聘",
    "直聘",
)

_WATERMARK_RE = re.compile("|".join(re.escape(t) for t in WATERMARK_TOKENS))

# 零宽字符 / 软连字符 —— 另一种常见的分词干扰手段
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\u00ad]")

# 需要做 NFKC 修复的 Unicode 区段: 汉字的"替身"写法
_COMPAT_RANGES: tuple[tuple[int, int], ...] = (
    (0x2E80, 0x2EFF),  # CJK Radicals Supplement
    (0x2F00, 0x2FDF),  # Kangxi Radicals
    (0x3130, 0x318F),  # Hangul Compatibility (偶见)
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0xFE30, 0xFE4F),  # CJK Compatibility Forms
)


def _is_compat_char(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _COMPAT_RANGES)


def fix_lookalike_chars(text: str) -> str:
    """把康熙部首等"替身汉字"还原成标准汉字, 保留全角标点原样。"""
    if not text:
        return text
    out = []
    for ch in text:
        if _is_compat_char(ch):
            folded = unicodedata.normalize("NFKC", ch)
            out.append(folded if folded else ch)
        else:
            out.append(ch)
    return "".join(out)


def strip_watermark(text: str) -> tuple[str, int]:
    """剥离插入的水印碎片, 返回 (清洗后文本, 剥离次数)。"""
    if not text:
        return text, 0
    count = 0

    def _sub(_m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return ""

    return _WATERMARK_RE.sub(_sub, text), count


def normalize_whitespace(text: str) -> str:
    """收敛空白: 制表符/不间断空格转普通空格, 压缩连续空行。"""
    if not text:
        return text
    text = text.replace("\u3000", " ").replace("\xa0", " ").replace("\t", " ")
    text = re.sub(r"[ ]{2,}", " ", text)
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def clean_text(text: str, *, context: str = "") -> str:
    """完整清洗管线: HTML 实体 -> 隐形字符 -> 水印 -> 替身字 -> 空白。

    Args:
        text: 原始文本
        context: 仅用于日志, 说明这段文本来自哪里
    """
    if not text:
        return ""
    text = html.unescape(text)
    text = _INVISIBLE_RE.sub("", text)
    text, removed = strip_watermark(text)
    text = fix_lookalike_chars(text)
    text = normalize_whitespace(text)
    if removed:
        log.debug(f"清除水印碎片 {removed} 处 {f'({context})' if context else ''}")
    return text


def clean_list(items, *, context: str = "") -> list[str]:
    """清洗字符串列表并去重, 保持原有顺序。"""
    if not items:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in items:
        if not isinstance(raw, str):
            continue
        val = clean_text(raw, context=context)
        val = val.strip(" ·,，、")
        if not val or val in seen:
            continue
        seen.add(val)
        out.append(val)
    return out


def looks_polluted(text: str) -> bool:
    """判断一段文本是否仍带有污染痕迹, 用于采集后的质量校验。"""
    if not text:
        return False
    if _WATERMARK_RE.search(text):
        return True
    return any(_is_compat_char(ch) for ch in text)


def merge_skill_lists(api_skills, dom_skills) -> list[str]:
    """合并技能标签, 以列表 API 的干净数据为准。

    列表接口返回的 skills 数组没有被注水, 详情页 DOM 抓到的常常是脏的。
    策略: API 有就用 API 的, 再把 DOM 里 API 没覆盖到的补进来。
    """
    api_clean = clean_list(api_skills, context="skills/api")
    dom_clean = clean_list(dom_skills, context="skills/dom")
    if not api_clean:
        return dom_clean

    result = list(api_clean)
    lowered = {s.lower() for s in result}
    for cand in dom_clean:
        low = cand.lower()
        if low in lowered:
            continue
        # DOM 版本常常是 API 版本被注水后的残缺形态, 用包含关系过滤掉
        if any(low in known or known in low for known in lowered):
            continue
        result.append(cand)
        lowered.add(low)
    return result
