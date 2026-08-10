"""proactive.py — 主动搭话时机分级（Proactive Timing）

借鉴业界主动搭话时机分级思路，自研精简版：
不要固定时间点提醒，而是按"沉默了多久 + 当前时段 + 说话人活跃状态"
动态决定要不要主动开口，以及要开得多热烈。

输出一个"主动行为带"（behavior band）：
    silent       用户/伙伴沉默过短 → 不打扰（让它安静）
    nudge        沉默中等 → 轻推一句
    checkin      沉默较久 → 问候式 cheIn，记得带点新话题
    greet        沉默很久 → 认真打招呼/关切（长问候）
    asleep       深夜 → 仅晚安式

用法（对接 QwenPaw cron 随机提醒 / 主动消息）：
    band = proactive_band(gap_seconds=3600*5, hour=14)
    # -> "checkin"
    guidance = build_proactive_guidance(band, gap, hour, recent_topics)
"""

from __future__ import annotations

from typing import Dict, List, Optional

# ── 参数 ─────────────────────────────────────────────────
SILENT_MAX = 60 * 20            # 少于 20 分钟不说话：不打扰
NUDGE_MAX = 60 * 60 * 2         # 2 小时内：轻推
CHECKIN_MAX = 60 * 60 * 8       # 8 小时内：认真问候
GREET_MIN = 60 * 60 * 8         # 超过 8h：好好打个招呼
DEEP_NIGHT = range(23, 24) or range(0, 6)   # 23点-5点算深夜


def _band_level(gap_seconds: float, is_deep_night: bool) -> str:
    if is_deep_night:
        if gap_seconds > SILENT_MAX:
            return "goodnight"
        return "silent"
    if gap_seconds < SILENT_MAX:
        return "silent"
    if gap_seconds < NUDGE_MAX:
        return "nudge"
    if gap_seconds < CHECKIN_MAX:
        return "checkin"
    return "greet"


def hour_is_deep_night(hour: int) -> bool:
    """23-5 点视作深夜（注意跨日）。"""
    return hour >= 23 or hour < 6


def is_idle_long(since: float, now: Optional[float] = None) -> bool:
    """距上次交互是否够久（> 2h），适合触发"主动关心"。"""
    import time as _t
    gap = (now if now is not None else _t.time()) - since
    return gap >= NUDGE_MAX


def proactive_band(gap_seconds: float, hour: Optional[int] = None, *,
                   extra_recent_topic: bool = False) -> str:
    """返回主动行为带。hour 缺省用当前本地小时。"""
    from datetime import datetime
    h = hour if hour is not None else datetime.now().hour
    is_night = hour_is_deep_night(h)
    band = _band_level(gap_seconds, is_night)
    # 如果有新鲜话题，把"轻推"升级成"好好聊"——给点人味
    if extra_recent_topic and band == "nudge":
        return "checkin"
    return band


# ── 生成主动搭话的"策略提示" ────────────────────────────────

_BAND_GUIDANCE: Dict[str, str] = {
    "silent": "用户/伙伴刚活跃过或正在忙，**不要主动搭话**，保持安静陪伴。",
    "nudge": "可以轻推一句：短短的、带点新引子（一个话题、一个问题、一句问候），别长篇大论。",
    "checkin": "该认真打个招呼了：自然关心一下近况，看看要不要做点什么，可以带 1-2 个近期话题。",
    "greet": "沉默很久了：好好问候、表达在记着对方，可以拉一个具体话题（天气/近况/上次没聊完的）。",
    "goodnight": "深夜了：只发一句温柔的晚安/早点休息即可，不要抛新话题。",
}


def build_proactive_guidance(band: str, gap_seconds: float,
                             hour: Optional[int] = None,
                             recent_topics: Optional[List[str]] = None,
                             lang: str = "zh") -> Dict:
    """生成主动搭话策略条目：该做什么、多长的量、可用话题。"""
    from datetime import datetime
    h = hour if hour is not None else datetime.now().hour
    topics = recent_topics or []
    guidance = _BAND_GUIDANCE.get(band, _BAND_GUIDANCE["silent"])
    # 时间提示
    if h < 6:
        time_note = "（深夜时段）"
    elif h < 12:
        time_note = "（上午）"
    elif h < 18:
        time_note = "（下午）"
    else:
        time_note = "（晚上）"
    return {
        "band": band,
        "language": lang,
        "guidance": guidance,
        "time_note": time_note,
        "suggested_mode": "short" if band in ("silent", "nudge", "goodnight")
                          else "normal",
        "recent_topics": topics[:3],
        "gap_label": _gap_label(gap_seconds),
    }


def _gap_label(gap_seconds: float) -> str:
    if gap_seconds < 60 * 60:
        return f"{int(gap_seconds // 60)} 分钟"
    if gap_seconds < 24 * 3600:
        return f"{int(gap_seconds // 3600)} 小时"
    return f"{int(gap_seconds // 86400)} 天"


if __name__ == "__main__":
    from datetime import timedelta, datetime
    now = datetime.now()
    for gap, name in [(300, "5分钟"), (3600 * 11 / 10, "约1.1小时"),
                      (3600 * 5, "5小时"), (3600 * 20, "20小时")]:
        b = proactive_band(gap)
        print(f"gap={name:8} band={b}")
    g = build_proactive_guidance("checkin", 3600 * 5, hour=14,
                                 recent_topics=["CS 饰品行情", "Godot 游戏开发"])
    print("guidance:", g)