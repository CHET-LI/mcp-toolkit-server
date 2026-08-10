"""trust.py — 说话人信任仲裁（Speaker Trust）

借鉴业界说话人信任仲裁思路，自研精简版：
当同一个人前后说话矛盾（"我喜欢 A" 后来 "其实我讨厌 A"），不能傻傻两条都记住，
而要判定哪条是"真话"，并给说话人累积信任分——恶意造谣/频繁改口的说话人，信任分下降。

本模块聚焦**确定性关系判定**（纯词法，不依赖 LLM）：
检测一条陈述是否在"否定/收回"另一条陈述。

覆盖的词法否定信号（中英文）：
- 英文: not / never / don't / isn't / but / however / except / actually / yet
- 否定词 + 短语: "don't like X" vs "like X"
- 中文: 不/不是/并不/从没/但/不过/其实/然而/别/没/毫不/只好 not

用法：
    rel = deterministic_relation("我喜欢苹果", "其实我讨厌苹果")
    # rel -> "reject"   （新句否定了旧句）
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

DEFAULT_TRUST = 0.5          # 新说话人默认信任
TRUST_RECORD_WINDOW = 50


# ── 词法否定 / 转折检测 ─────────────────────────────────────

# 明确的否定词（独立 token 优先）
_EN_NEG_TOKENS = {
    "not", "never", "don't", "dont", "isn't", "arent", "wasn't", "weren't",
    "doesn't", "didn't", "won't", "wouldn't", "can't", "cannot", "couldn't",
    "no", "nor", "hardly", "scarcely", "neither", "without", "refuse", "unlike",
}
_EN_TURN_TOKENS = {"but", "however", "yet", "although", "though", "except",
                   "actually", "in fact", "on the contrary", "instead", "rather"}
_CJK_NEG = ["不", "没", "别", "非", "无须", "无需", "未有", "毫无", "绝无", "毫不"]
_CJK_TURN = ["但", "不过", "然而", "可是", "但是", "其实", "反而", "倒是", "好在", "只是"]

_TOKEN_RE = re.compile(r"[\w']+\-?[\w']*")


def _tokens(text: str) -> set:
    """中英分诊 token：中文按单字 bigram、英文按词。避免正则 \\w+ 把整句当一个 token。"""
    if re.search(r"[\u4e00-\u9fff]", text):
        return set(_CJK_fragments(text))
    return {t.lower() for t in _TOKEN_RE.findall(text)}


def _en_tokens(text: str) -> set:
    return {t.lower() for t in _TOKEN_RE.findall(text)}


def _has_en_negation(text: str) -> bool:
    toks = _en_tokens(text)
    return bool(toks & _EN_NEG_TOKENS)


def _has_cjk_negation(text: str) -> bool:
    return any(n in text for n in _CJK_NEG)


def _has_turn(text: str) -> bool:
    low = text.lower()
    if any(t in low for t in _EN_TURN_TOKENS):
        return True
    return any(t in text for t in _CJK_TURN)


def negated(text: str) -> bool:
    """文本是否含任何否定/收回信号。"""
    return _has_en_negation(text) or _has_cjk_negation(text)


def is_negated_lexical_pair(old: str, new: str) -> bool:
    """new 是否在词面上否定 old 的核心话题。

    启发式（够用不追求完美）：
    - 如果 new 带转折词且二者词面相似度较高 → 很可能是"收回"
    - 若 new 带否定词且和 old 的实词重叠 → 也可能是否定追问
    这里简化：出现转折词 + 覆盖 old 的部分关键词 → reject。
    """
    old_toks = _tokens(old) or set(_CJK_fragments(old))
    new_toks = _tokens(new) or set(_CJK_fragments(new))
    if not old_toks or not new_toks:
        return False
    overlap = old_toks & new_toks
    if not overlap:
        return False
    # 有转折（"但是/其实/然而"）+ 有话题重叠 + new 有否定词 → reject
    return _has_turn(new) and negated(new)


def _CJK_fragments(text: str) -> List[str]:
    """中文按 2-gram 变体提取（simplified：拆成整句单词表不给力，用字干）。"""
    # 用中文字符 bigram 近似"词干"，避免"喜欢"也被当独立无意义。
    chars = [c for c in text if "\u4e00" <= c <= "\u9fff"]
    return ["".join(chars[i:i + 2]) for i in range(max(0, len(chars) - 1))]


def deterministic_relation(old_text: str, new_text: str) -> Optional[str]:
    """判定 new 相对 old 的关系。

    返回:
      - "reject"   new 收回/否定 old
      - "confirm"  new 确认/复述 old（词面高度重叠且无否定）
      - "different" 话题不同
      - None       无法判定
    """
    if not old_text or not new_text:
        return None
    old_toks = _tokens(old_text) or set(_CJK_fragments(old_text))
    new_toks = _tokens(new_text) or set(_CJK_fragments(new_text))
    overlap = old_toks & new_toks
    if not overlap:
        return "different"

    # 新句带否定 + 转折 → 收回旧句
    if _has_turn(new_text) and negated(new_text):
        return "reject"
    # 新句本身否定（如 "我从没说过喜欢 X"）→ 收回
    if negated(new_text) and not _has_turn(new_text):
        return "reject"
    # 高度重叠且无否定 → 确认
    if not negated(new_text):
        return "confirm"
    return None


# ── 信任分管理 ─────────────────────────────────────────────

def finite_trust_score(value: float) -> float:
    """把信任分夹进 [0,1]，NaN/不合法回默认。"""
    if isinstance(value, (int, float)) and value == value:  # NaN check
        return max(0.0, min(1.0, float(value)))
    return DEFAULT_TRUST


class SpeakerTrust:
    """按说话人维护信任分；reject 事件降低信任，confirm/可信来源上升。"""

    def __init__(self, default: float = DEFAULT_TRUST):
        self._trust: Dict[str, float] = {}
        self._default = default

    def get(self, speaker: str) -> float:
        return self._trust.get(speaker, self._default)

    def observe(self, speaker: str, relation: str, delta: float = 0.1) -> float:
        """根据关系更新信任：reject 降、confirm 升、different 持平。"""
        cur = self.get(speaker)
        if relation == "reject":
            cur -= delta
            reason = "contradiction"
        elif relation == "confirm":
            cur += delta * 0.3
            reason = "consistency"
        else:
            return cur
        cur = max(0.0, min(1.0, cur))
        self._trust[speaker] = cur
        return cur

    def trust_belt(self, speaker: str) -> str:
        s = self.get(speaker)
        if s >= 0.8:
            return "high"
        if s >= 0.5:
            return "medium"
        return "low"


if __name__ == "__main__":
    print("reject sample:", deterministic_relation("我喜欢苹果", "其实我并不喜欢苹果"))
    print("confirm sample:", deterministic_relation("明天开会", "对，明天要开会"))
    print("different sample:", deterministic_relation("今天下雨", "我想喝奶茶"))
    t = SpeakerTrust()
    print("init:", t.get("小明"), t.trust_belt("小明"))
    t.observe("小明", "reject")
    t.observe("小明", "reject")
    print("after 2 rejects:", round(t.get("小明"), 2), t.trust_belt("小明"))