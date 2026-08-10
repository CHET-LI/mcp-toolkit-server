"""arr.py — 防重复说话（Anti-Repeat）

借鉴业界防重复语料（BM25 背景/新鲜窗口/TTL 老化）思路，自研精简版：
AI 每句要输出的草稿，先跟"最近说过的内容"做 BM25 相似度打分，
分越高越像重复，调用方据此把相近的重复内容压下去。

核心设计（为什么这样做）：
- **BG 全量语料算 DF/IDF**（词频背景）：独特词汇 → 高 IDF，不重复。
  背景窗口永不裁剪，保留完整词频上下文。
- **FG 只算 TTL 内的新鲜窗口**（词频/重复度信号）：时间太久的内容不再算
  "车轱辘话"，语义上"很久前说过 ≠ 现在重复"。
- 太短草稿 / 空语料 → 返回 0（不压），避免误伤。
- 全程纯函数 + 简单 JSON 持久化，可嵌入任意 agent 回复前过滤。

用法：
    corpus = AntiRepeatCorpus("~/.anti_repeat.json")
    corpus.record_output(name, "昨天聊了 CS 饰品")
    score, per = corpus.score_draft(name, "我们聊了 CS 饰品行情")
    if score > 阈值:  # 太像重复，改写或跳过
        ...
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# ── 可调参数 ─────────────────────────────────────────────
FG_WINDOW = 6              # 只跟最近 N 条比对
FG_TTL_SECONDS = 3600 * 6  # 6 小时内才算"新鲜"
TAIL_DROP_SECONDS = 3600 * 24  # 超过 24h 的旧记录丢弃（省空间）
MIN_DRAFT_TOKENS = 4        # 太短不判
NGRAM_N = 2                # 2-gram

_DEFAULT_KEY = "_default"


def _ngrams(text: str, n: int = NGRAM_N) -> List[str]:
    """把文本拆成连续 n-gram：中文按单字、英文按词（正则 \\w+ 会把整句中文当一个 token）。"""
    text = text.lower()
    toks: List[str]
    if re.search(r"[\u4e00-\u9fff]", text):
        # 中英混合：中文字符逐个、英文数字按词
        toks = re.findall(r"[\u4e00-\u9fff]|[a-z0-9]+", text)
    else:
        toks = re.findall(r"[a-z0-9]+", text)
    if not toks:
        return []
    return [" ".join(toks[i:i + n]) for i in range(max(0, len(toks) - n + 1))]


def _now() -> float:
    return time.time()


def bm25_score(query_ngrams: List[str], fg_docs: List[List[str]],
               bg_docs: List[List[str]], k1: float = 1.5, b: float = 0.75) -> float:
    """BM25：query 对 fg 文档的平均相关分。bg 提供 DF/IDF 背景。"""
    if not fg_docs or not query_ngrams:
        return 0.0
    # 词频背景统计（全量 bg，含 fg 语义：fg 是 df 子集，合并更稳）
    all_docs = [*bg_docs, *fg_docs]
    N = len(all_docs)
    doc_freq: Counter = Counter()
    for doc in all_docs:
        for t in set(doc):
            doc_freq[t] += 1
    idf = {t: math.log(1 + (N - df + 0.5) / (df + 0.5)) for t, df in doc_freq.items()}
    avg_len = sum(len(d) for d in all_docs) / max(1, N)

    total = 0.0
    for doc in fg_docs:
        tf = Counter(doc)
        dl = len(doc)
        for q in query_ngrams:
            f = tf.get(q, 0)
            if f == 0:
                continue
            numen = f * (k1 + 1)
            denom = f + k1 * (1 - b + b * dl / max(1, avg_len))
            total += idf.get(q, 0.0) * numen / denom
    return total / max(1, len(fg_docs))


class AntiRepeatCorpus:
    """线程安全的防重复语料。按 name 隔离（不同会话/频道各自的重复历史）。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: Dict[str, List[Dict]] = self._load()

    def _load(self) -> Dict[str, List[Dict]]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def record_output(self, name: Optional[str], text: str) -> None:
        """记录一条 AI 已输出的内容（成功送达 / 真实回复后调用）。"""
        name = name or _DEFAULT_KEY
        toks = _ngrams(text)
        if not toks:
            return
        with self._lock:
            window = self._data.setdefault(name, [])
            window.append({"ts": _now(), "ngrams": toks})
            cutoff = _now() - TAIL_DROP_SECONDS
            self._data[name] = [e for e in window if e["ts"] > cutoff][-200:]
            self._save()

    def _split_fg_bg(self, name: str, now: float,
                     fg_window: int) -> Tuple[List[List[str]], List[List[str]]]:
        window = self._data.get(name, [])
        bg_docs = [e["ngrams"] for e in window]
        fresh = [e for e in window if now - e.get("ts", 0) <= FG_TTL_SECONDS]
        fg_docs = [e["ngrams"] for e in fresh[-fg_window:]] if fresh else []
        return fg_docs, bg_docs

    def score_draft(self, name: Optional[str], draft: str,
                    fg_window: int = FG_WINDOW,
                    now: Optional[float] = None) -> Tuple[float, Dict[str, float]]:
        """给草稿打分：与最近 FG 条内容的重复度。越大越重复。"""
        draft = (draft or "").strip()
        dng = _ngrams(draft)
        if len(dng) < MIN_DRAFT_TOKENS:
            return 0.0, {}
        now = now or _now()
        with self._lock:
            fg, bg = self._split_fg_bg(name or _DEFAULT_KEY, now, fg_window)
        if not fg:
            return 0.0, {}
        return bm25_score(dng, fg, bg), {}

    def record_outputs(self, name: Optional[str], texts: Iterable[str]) -> None:
        for t in texts:
            self.record_output(name, t)


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        c = AntiRepeatCorpus(Path(d) / "c.json")
        c.record_output("s", "我喜欢吃苹果")
        print("重复度(很重复，应高):", round(c.score_draft("s", "我喜欢吃苹果呀")[0], 3))
        print("重复度(话题不同，应低):", round(c.score_draft("s", "今天天气不错去散步")[0], 3))