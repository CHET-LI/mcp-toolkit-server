"""temporal.py — 相对时间解析与时间锚点（Temporal Anchor）

借鉴业界时间语义解析思路，自研精简版：
用户说"上周通宵加班 / 两天前 / 昨天下午"，记忆系统要把这种**相对时间
解析成绝对时间戳**作为"事件锚点"，与"记忆写盘时间"分开存。
这样以后回答"你什么时候加班的？"能回答真实发生的时间，而不是"我记得那会儿"。

提供两类能力：
1. normalize_event_when(raw)  -> 把 "昨天 / 前天 / 上周 / 3天后 / 去年夏季" 等
   解析成 (event_start, event_end, label)
2. time_since_label(anchor)    -> 把存储的 ISO 时间转成"3 小时前 / 昨天" 这类人话
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple


def _now_naive() -> datetime:
    return datetime.now()


# 相对时间短语 -> 相对于"今天"的偏移天数 与 粒度
_PATTERNS = [
    (re.compile(r"刚刚|刚才|马上|现在"), 0, "D"),
    (re.compile(r"今天|今日"), 0, "D"),
    (re.compile(r"昨天|昨日|昨晚"), -1, "D"),
    (re.compile(r"前天|前天"), -2, "D"),
    (re.compile(r"大前天"), -3, "D"),
    (re.compile(r"上周|上礼拜|上星期"), -7, "W"),
    (re.compile(r"上上周|上上礼拜"), -14, "2W"),
    (re.compile(r"两周前"), -14, "2W"),
    (re.compile(r"上月|上个月"), -30, "M"),
    (re.compile(r"去年|去(?:年)"), -365, "Y"),
    (re.compile(r"明天|明日|明早"), 1, "D"),
    (re.compile(r"后天"), 2, "D"),
    (re.compile(r"三天后|3天后"), 3, "D"),
    (re.compile(r"下周|下礼拜"), 7, "W"),
    (re.compile(r"下月|下个月"), 30, "M"),
]

# 数字 + 时间单位 -> 偏移
_UNITS = {
    "秒": "seconds", "分钟": "minutes", "时": "hours", "小时": "hours",
    "天": "days", "日": "days", "周": "weeks", "星期": "weeks", "个月": "months",
    "月": "months", "年": "years",
}
_NUM_CN = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10, "半": 0.5}
_NUM_RE = re.compile(r"(\d+)")
_CNUM_RE = re.compile("(" + "|".join(list("一二两三四五六七八九十半")) + ")")


def parse_counted_offset(text: str) -> Optional[timedelta]:
    """解析 '3天前' '两小时前' '下周' 这类带数字的相对时间。返回已过去/未来的 timedelta。"""
    m = _NUM_RE.search(text)
    unit = next((u for u in _UNITS if u in text), None)
    if unit is None:
        # 中文数字
        cm = _CNUM_RE.search(text)
        if cm:
            num = _NUM_CN.get(cm.group(1), 1)
        else:
            return None
    else:
        num = int(m.group(1)) if m else 1
    kw = _UNITS[unit]
    if kw == "months":
        delta = timedelta(days=num * 30)
    elif kw == "years":
        delta = timedelta(days=num * 365)
    else:
        delta = timedelta(**{kw: num})
    if "前" in text or "过去" in text:
        delta = -delta
    return delta


def normalize_event_when(raw: Optional[str]) -> Optional[Dict]:
    """把相对时间描述解析成事件锚点。

    返回 {"start", "end", "label"} 或 None（解析不出真实锚点）。
    start/end 为 ISO naive datetime（本地时区）。
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    now = _now_naive()

    # 带数字偏移优先——但必须真的有数字（阿拉伯或中文），否则 '昨天'/'上周' 里
    # 的 '天'/'周' 会被误当"1 天后/1 周后"。
    has_arabic = _NUM_RE.search(text) is not None
    has_cn_num = _CNUM_RE.search(text) is not None
    if has_arabic or has_cn_num:
        delta = parse_counted_offset(text)
        if delta is not None:
            t0 = now + delta
            end = t0 + timedelta(hours=1)
            return {"start": t0.isoformat(timespec="minutes"),
                    "end": end.isoformat(timespec="minutes"),
                    "label": text}

    for pat, days, _g in _PATTERNS:
        if pat.search(text):
            t0 = now + timedelta(days=days)
            # 日级事件给一天宽窗
            end = t0 + timedelta(days=1)
            return {"start": t0.isoformat(timespec="minutes"),
                    "end": end.isoformat(timespec="minutes"),
                    "label": text}
    return None


def days_since(iso: Optional[str], now: Optional[datetime] = None) -> Optional[int]:
    """距 ISO 时间过去的天数。"""
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    now = now or _now_naive()
    return int((now - t).total_seconds() // 86400)


def time_since_label(iso: Optional[str], now: Optional[datetime] = None) -> Optional[str]:
    """把 ISO 时间转成 "刚刚 / N 分钟前 / N 小时前 / 昨天 / N 天前"。"""
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    now = now or _now_naive()
    sec = max(0, (now - t).total_seconds())
    if sec < 60:
        return "刚刚"
    if sec < 3600:
        return f"{int(sec // 60)} 分钟前"
    if sec < 86400:
        return f"{int(sec // 3600)} 小时前"
    d = int(sec // 86400)
    if d == 1:
        return "昨天"
    if d < 7:
        return f"{d} 天前"
    if d < 30:
        return f"{d // 7} 周前"
    return f"{d // 30} 个月前"


if __name__ == "__main__":
    for phrase in ["昨天", "上周通宵加班", "3 天前", "两小时前", "去年", "下个月"]:
        r = normalize_event_when(phrase)
        print(f"{phrase!r:14} -> {r}")
    print("since label:", time_since_label((datetime.now() - timedelta(days=3)).isoformat()))