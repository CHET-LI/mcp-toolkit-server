"""directives.py — 用户指令持久化存储（User Directives）

借鉴业界指令持久化思路，自研精简版。

问题：用户刚说过一句"别再提 X / 别聊那事 / stop bringing up Y"。当前回合 LLM
上下文里看得到这句，不需要处理；但**一旦会话重启 / 历史被压缩清掉**，模型就忘了，
又在同一个雷上踩第二次——用户被气得再骂一次。业界做法是：把这种"禁令话题"
提取出来落盘，TTL 3 秒保留，并把活跃指令**注入系统提示词尾部**，让每次开新会话
都知道"这次别碰这些"。

关键设计：
1. **持久化**：每个名字(key)一条 JSON，版本化（__init__ 时 bump 兼容旧文件）。
2. **TTL 续期**：`expire_at = last_seen_at + TTL`。用户又提一次同一条 → 刷新
   last_seen + expire（**续期**，不是重置累计），hit_count +1；命中期内的指令
   register 时自动续，读时过滤过期，`purge` 懒清理删除。
3. **去重键** `(kind, term.casefold())`：同一指令反复注册只刷新不重复。
4. **term 规范化**：strip 后长度 ∈ [2, 40]，读写共用同一个不变量，脏 term 丢弃。
5. **容错**：一条脏记录不能毁掉整文件——单独 drop；文件损坏 → 从空开始不崩。
6. **线程安全**：per-name lock，防止并发读写同一文件。
7. **locale 不覆盖**：首次命中的 locale 是最有诊断价值的，后续不再改。
8. **注入提示块**：`render_block(name, lang)` 把活跃指令拼进系统提示词尾部，"" 为空。

用法：
    store = DirectiveStore(dir=out_dir)
    store.record_from_text("chet", "以后别再提那个项目了")
    block = store.render_block("chet", "zh")   # → "❗ 用户指令：别聊/别再提：那个项目"
    active = store.get_active("chet")           # 未过期指令列表
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1
TTL_SECONDS = 3 * 86400          # 指令保留 3 天
MAX_ACTIVE = 16                  # 单次渲染最多注入的活跃指令数
TERM_MIN_LEN = 2
TERM_MAX_LEN = 40


# ── 轻量指令提取（避免超复杂多语言正则）────────────────────────
# 返回 [(locale, kind, term)]。聚焦最常见的"禁令/偏好"两大类，
# 抓不精细也要多抓（宁误抓不放过——错的代价是用户再说一遍，漏的代价是又被冒犯）。

_BAN_ZH = re.compile(
    r"(?:别|不要|别再|不要再|千万别|请(?:别|不要|别再))"
    r"(?:再)?(?:提|聊|谈|说|讲|讨论|提起来|跟我提|和我说)\s*"
    r"([\u4e00-\u9fffA-Za-z0-9（）()《》<>「」\"'、，。!?？.!]{1,20})"
)
_BAN_ISH_ZH = None  # 预留（中英 dyadic 短语），当前未启用
_BAN_EN = re.compile(
    r"(?i)\b(?:stop|don'?t|do not|never|please (?:don'?t|stop))"
    r"[\s,]+(?:mention|talk|bring up|bring_?up|say|discuss|discussing|talking about)"
    r"[\s,'\"]+(.{2,40}?)[.!?]?\s*(?:please)?(?:\s|$)"
)
# 通用负向偏好：我不喜欢/讨厌/不想在聊 X（够用即可）
_PREF_ZH = re.compile(
    r"(?:我|我特别|我比较)(?:不?喜欢|讨厌|反感|不想聊|不爱)[\s，,、]*"
    r"([\u4e00-\u9fffA-Za-z0-9 ]{2,20})"
)


def _clean_term(term: str) -> str:
    """去掉首尾标点空白；多余字段（语气词/句末标点）一并去除。"""
    term = term.strip().strip("，,。.!！?？、（）()《》「」\"' ")
    return term[:TERM_MAX_LEN]


def extract_directives(text: str) -> List[Tuple[str, str, str]]:
    """从一句用户话里提取禁令指令，返回 [(locale, kind, term)]，缺失 = []。"""
    if not text or not text.strip():
        return []
    out: List[Tuple[str, str, str]] = []
    seen = set()
    # 中文 ban
    for m in _BAN_ZH.finditer(text):
        term = _clean_term(m.group(1))
        if TERM_MIN_LEN <= len(term) <= TERM_MAX_LEN and (m.group(0), "zh") not in seen:
            out.append(("zh", "ban_topic", term))
            seen.add((m.group(0), "zh"))
    # 中文偏好
    for m in _PREF_ZH.finditer(text):
        term = _clean_term(m.group(1))
        if TERM_MIN_LEN <= len(term) <= TERM_MAX_LEN and (m.group(0), "zh") not in seen:
            out.append(("zh", "preference", term))
            seen.add((m.group(0), "zh"))
    # 英文 ban
    for m in _BAN_EN.finditer(text):
        term = _clean_term(m.group(1))
        if TERM_MIN_LEN <= len(term) <= TERM_MAX_LEN and (m.group(0), "en") not in seen:
            out.append(("en", "ban_topic", term))
            seen.add((m.group(0), "en"))
    # 去重 (kind, term.casefold())
    dedup: Dict[Tuple[str, str], Tuple[str, str, str]] = {}
    for locale, kind, term in out:
        dedup.setdefault((kind, term.casefold()), (locale, kind, term))
    return list(dedup.values())


# ── 存取 helper ──────────────────────────────────────────
def _default_payload() -> Dict[str, Any]:
    return {"version": SCHEMA_VERSION, "directives": []}


def _normalize_term(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    term = raw.strip()
    if not (TERM_MIN_LEN <= len(term) <= TERM_MAX_LEN):
        return None
    return term


def _to_float(v, default) -> float:
    try:
        return float(v or 0) or default
    except (TypeError, ValueError):
        return default


def _normalize_entry(raw: Any) -> Optional[Dict[str, Any]]:
    """读盘一条指令；字段缺失/非法 → drop（一条脏记录不毁整文件）。"""
    if not isinstance(raw, dict):
        return None
    try:
        term = _normalize_term(raw.get("term"))
        if term is None:
            return None
        kind = raw.get("kind") or "ban_topic"
        if not isinstance(kind, str):
            kind = "ban_topic"
        locale = raw.get("locale") if isinstance(raw.get("locale"), str) else "und"
        created = _to_float(raw.get("created_at"), time.time())
        last_seen = _to_float(raw.get("last_seen_at"), created)
        expire = _to_float(raw.get("expire_at"), last_seen + TTL_SECONDS)
        hit = 1
        try:
            hit = max(1, int(raw.get("hit_count") or 1))
        except (TypeError, ValueError):
            hit = 1
        return {
            "term": term, "kind": kind, "locale": locale,
            "created_at": created, "last_seen_at": last_seen,
            "expire_at": expire, "hit_count": hit,
            "source": raw.get("source") or "regex",
        }
    except Exception:
        return None


# ── 主类 ───────────────────────────────────────────────────
class DirectiveStore:
    """上界：per-user 局部的持久化用户指令存储（线程安全）。

    ``dir`` 为根目录；每个 user 一个 ``<user>.json``。
    """

    def __init__(self, directory: Optional[str] = None) -> None:
        if directory is None:
            directory = tempfile.mkdtemp(prefix="directives_")
        self._dir = directory
        os.makedirs(self._dir, exist_ok=True)
        self._cache: Dict[str, List[Dict[str, Any]]] = {}
        self._locks: Dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def file_path(self, user: str) -> str:
        return os.path.join(self._dir, f"{user}.json")

    def _get_lock(self, user: str) -> threading.RLock:
        if user not in self._locks:
            with self._locks_guard:
                if user not in self._locks:
                    self._locks[user] = threading.RLock()
        return self._locks[user]

    # 锁由持有者持有
    def _load(self, user: str) -> List[Dict[str, Any]]:
        if user in self._cache:
            return self._cache[user]
        items: List[Dict[str, Any]] = []
        path = self.file_path(user)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
                arr = raw.get("directives") if isinstance(raw, dict) else None
                if isinstance(arr, list):
                    for r in arr:
                        n = _normalize_entry(r)
                        if n is not None:
                            items.append(n)
            except Exception:
                items = []  # 坏文件从空开始
        self._cache[user] = items
        return items

    def _save(self, user: str) -> None:
        path = self.file_path(user)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": SCHEMA_VERSION,
                           "directives": self._cache.get(user, [])},
                          f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            pass

    # ── public API ──────────────────────────────────────────
    def record(self, user: str, *, locale: str, kind: str, term: str,
               source: str = "regex", now: Optional[float] = None) -> Dict[str, Any]:
        """注册一条指令；已有 (kind, term.casefold()) → 刷新续期。

        返回落盘后的 dict；非法输入（term 非 str / 空 / 长度越界）返回空 dict。
        """
        if not user:
            return {}
        term_n = _normalize_term(term)
        if term_n is None:
            return {}
        term = term_n
        ts = float(now if now is not None else time.time())
        expire = ts + TTL_SECONDS
        key = (kind, term.casefold())
        with self._get_lock(user):
            entries = self._load(user)
            for e in entries:
                if (e["kind"], e["term"].casefold()) == key:
                    e["last_seen_at"] = ts
                    e["expire_at"] = expire
                    e["hit_count"] = int(e.get("hit_count", 1)) + 1
                    self._save(user)
                    return dict(e)
            new_entry = {
                "term": term, "kind": kind, "locale": locale,
                "created_at": ts, "last_seen_at": ts, "expire_at": expire,
                "hit_count": 1, "source": source,
            }
            entries.append(new_entry)
            self._save(user)
            return dict(new_entry)

    def record_from_text(self, user: str, text: str,
                         now: Optional[float] = None) -> List[Dict[str, Any]]:
        """提取 + 存储一整句话的指令；返回本次写入/刷新的条目（空 = 无命中）。"""
        if not user or not text:
            return []
        hits = extract_directives(text)
        if not hits:
            return []
        ts = float(now if now is not None else time.time())
        return [self.record(user, locale=lc, kind=kd, term=tm, now=ts)
                for lc, kd, tm in hits]

    def get_active(self, user: str, *, now: Optional[float] = None,
                   limit: int = MAX_ACTIVE) -> List[Dict[str, Any]]:
        """未过期指令，按 last_seen_at 降序，至多 limit 条。"""
        if not user:
            return []
        ts = float(now if now is not None else time.time())
        with self._get_lock(user):
            entries = self._load(user)
            alive = [dict(e) for e in entries if float(e.get("expire_at", 0)) > ts]
        alive.sort(key=lambda e: float(e.get("last_seen_at", 0)), reverse=True)
        if limit and limit > 0:
            alive = alive[:limit]
        return alive

    def purge(self, user: str, *, now: Optional[float] = None) -> int:
        """删除过期条目并落盘；返回删除条数。"""
        if not user:
            return 0
        ts = float(now if now is not None else time.time())
        with self._get_lock(user):
            entries = self._load(user)
            kept = [e for e in entries if float(e.get("expire_at", 0)) > ts]
            removed = len(entries) - len(kept)
            if removed:
                self._cache[user] = kept
                self._save(user)
            return removed

    def render_block(self, user: str, lang: str = "zh",
                     now: Optional[float] = None) -> str:
        """把活跃指令渲染成系统提示词片段；无内容返回 ""。"""
        active = self.get_active(user, now=now)
        if not active:
            return ""
        terms = [e["term"] for e in active]
        header = "\n# 用户指令（务必遵守）\n"
        rows = []
        for kind, t in zip([e["kind"] for e in active], terms):
            if kind == "ban_topic":
                rows.append(f"- 别再提/别再聊：{t}")
            else:
                rows.append(f"- 偏好：{t}")
        return header + "\n".join(rows) + "\n"

    def clear(self, user: str) -> None:
        if not user:
            return
        with self._get_lock(user):
            self._cache[user] = []
            self._save(user)

    def count(self, user: str) -> int:
        if not user:
            return 0
        with self._get_lock(user):
            return len(self._load(user))