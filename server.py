"""MCP Toolkit Server — 通用 MCP 工具集，供 AI agent 直接调用。

模块 -> 工具：
  temporal    -> time_resolve / time_since      （相对时间->ISO 锚点，ISO->人话）
  proactive   -> proactive_band                 （主动搭话时机分级 + 策略提示）
  trust       -> relation_judge                 （两句话关系：confirm/reject/different）
  arr         -> dedup_record / dedup_score     （防重复/车轱辘话，进程内语料）
  directives  -> directive_store / directive_render（"别再提X"持久化，TTL 3 天）
  tscn_analyzer -> tscn_parse / project_scan / transform3d_decompose
    （不依赖 Godot 的 .tscn/.tres 静态解析：值全类型 + 场景对象树 + 全项目索引）

用法：
    python server.py          # 启动 MCP stdio server
    python test_client.py     # 端到端自测
"""

from __future__ import annotations

import json
import os
import sys
from typing import List, Optional

# 全部依赖模块与本文件同目录（arr/directives/proactive/temporal/trust/tscn_analyzer）
import temporal as _temporal
import proactive as _proactive
import trust as _trust
from arr import AntiRepeatCorpus
from directives import DirectiveStore
try:
    from tscn_analyzer import parse_tscn, parse_value, scan_project, decompose_transform3d
    TSCN_AVAILABLE = True
except ImportError as _e:  # pragma: no cover - tscn_analyzer 缺失时降级
    TSCN_AVAILABLE = False
    print(f"[warn] tscn_analyzer 不可用: {_e}", file=sys.stderr)

# 数据落地目录：arr 语料 + directives 指令存到 server 同级 data/ 下，进程重启不丢
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(_DATA_DIR, exist_ok=True)

# 进程内单例：arr 语料 / directives 存储（跨会话共享，靠 user/scope 名隔离）
_corpus = AntiRepeatCorpus(os.path.join(_DATA_DIR, "anti_repeat.json"))
_directives = DirectiveStore(os.path.join(_DATA_DIR, "directives"))

from mcp.server.fastmcp import FastMCP
_MCP = FastMCP("mcp-toolkit")


# ── temporal ──────────────────────────────────────────────
@_MCP.tool()
def time_resolve(expr: str) -> str:
    """把相对时间描述（'昨天下午' / '上周通宵加班' / '3 天前' / '明天'）解析
    成绝对时间锚点，返回 JSON：{start, end, label}。start/end 为 ISO naive 时间。
    用于把记忆/对话里的相对时间换算成可计算的时间戳。解析不出返回 "null"。"""
    r = _temporal.normalize_event_when(expr)
    if not r:
        return "null"
    return str(r)


@_MCP.tool()
def time_since(iso: str) -> str:
    """把 ISO 绝对时间转成人话（'刚刚 / N 分钟前 / 昨天 / N 天前 / N 个月前'）。
    iso 为 ISO 字符串（如 '2026-08-06T09:30'）。非法输入返回 'null'。返回 JSON：
    {label, days_ago}。"""
    label = _temporal.time_since_label(iso)
    days = _temporal.days_since(iso)
    if label is None:
        return "null"
    return f'{{"label": "{label}", "days_ago": {days}}}'


# ── proactive ─────────────────────────────────────────────
@_MCP.tool()
def proactive_band(gap_seconds: float,
                   hour: Optional[int] = None,
                   recent_topics: Optional[List[str]] = None) -> str:
    """判断现在该不该主动搭话、用什么强度。给 gap_seconds（距上次交互的秒数，
    如 5 小时 = 18000）；可选 hour（当前小时 0-23，缺省用本地时间）；recent_topics
    近期话题列表用于生策略提示。返回 JSON：
    {band, guidance, suggested_mode, gap_label, time_note, recent_topics}。
    band ∈ silent(别打扰)/nudge(轻推)/checkin(认真问候)/greet(长问候)/goodnight(晚安)。"""
    band = _proactive.proactive_band(
        gap_seconds, hour,
        extra_recent_topic=bool(recent_topics),
    )
    g = _proactive.build_proactive_guidance(band, gap_seconds, hour, recent_topics)
    return str(g)


# ── trust ──────────────────────────────────────────────
@_MCP.tool()
def relation_judge(statement: str, earlier: str) -> str:
    """判定新陈述 statement（用户刚说的话）与旧陈述 earlier（之前说过的话）的关系：
    'confirm' 确认/复述   'reject' 否定/收回   'different' 话题不同   'null' 无法判定。
    基于词法否定与转折（中英），不依赖 LLM。用于发现用户前后矛盾（比如
    '我喜欢苹果' 后来 '其实我并不喜欢苹果' -> reject）。"""
    r = _trust.deterministic_relation(earlier, statement)
    return r if r is not None else "null"


# ── arr（防重复 / 反轱辘话） ──────────────────────────────
def _scope_key(scope: str) -> str:
    return (scope or "_default").strip() or "_default"


@_MCP.tool()
def dedupe_record(scope: str, text: str) -> str:
    """把 AI 已真实说出口的一段话记入该 scope 的防重复语料后返回 "ok N 条"。
    scope 建议用会话/频道名（如 'qq:xxx'）或任意 key，不同 scope 各自的重复历史
    互相隔离。之后 dedupe_score 对同一 scope 的新草稿打分，越高越像车轱辘话。"""
    _corpus.record_output(_scope_key(scope), text)
    return "ok"


@_MCP.tool()
def dedupe_score(scope: str, draft: str) -> str:
    """给一段即将发出去的草稿打分（0~ 越高，越像该 scope 之前重复说过的
    车轱辘话）。草稿过短（<4 词）或该 scope 无语料时返回 0.0。用法：
    先 dedupe_score 打分，过高就改写或跳过；确认发出后调 dedupe_record 记入语料。"""
    score, _ = _corpus.score_draft(_scope_key(scope), draft)
    return f"{score:.3f}"


# ── directives（"别再提"持久化） ─────────────────────────
@_MCP.tool()
def directive_store(user: str, text: str) -> str:
    """从用户一句话里提取"别再提 X / 不要聊 Y / 我不喜欢 Z"类指令并持久化，
    TTL 3 天自动续期；同一指令再出现会续期 + 加命中。返回新增/刷新的指令列表
    JSON（空 [] = 没命中指令模式）。"""
    hits = _directives.record_from_text(user, text)
    return str(hits)


@_MCP.tool()
def directive_render(user: str) -> str:
    """返回该 user 当前活跃（未过期）指令渲染成的系统提示词片段，用于注入到
    agent 上下文，提醒"别碰这些雷"。无活跃指令返回空字符串。"""
    return _directives.render_block(user)


# ── tscn_analyzer（学自 GodotIQ parsers，不依赖 Godot） ──
def _json_default(o):
    if isinstance(o, dict):
        return {str(k): _json_default(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_default(v) for v in o]
    if hasattr(o, "__dataclass_fields__"):
        return {k: _json_default(v) for k, v in vars(o).items()}
    return str(o)


@_MCP.tool()
def transform3d_decompose(values: str) -> str:
    """把 Godot Transform3D 的 12 个列主序浮点数解析成
    {position, rotation(YXZ 弧度), scale} JSON。values 是逗号分隔的 12 个数
    字符串（如 "1,0,0,0,1,0,0,0,1,0,0,0"）。用于读懂 .tscn 里的 transform 属性。"""
    if not TSCN_AVAILABLE:
        return '{"error": "tscn_analyzer not available"}'
    try:
        vals = tuple(float(x.strip()) for x in values.split(","))
        if len(vals) != 12:
            return f'{{"error": "expected 12 floats, got {len(vals)}"}}'
        return json.dumps(decompose_transform3d(vals), default=_json_default)
    except ValueError as e:
        return f'{{"error": "invalid float list: {e}"}}'


@_MCP.tool()
def tscn_parse(path: str) -> str:
    """解析一个 .tscn 场景文件（纯文本，不需要启动 Godot），返回 JSON 摘要：
    header/ext_resources/sub_resources/root 节点树（含每个节点的 name/type/parent/
    instance/script/groups/position/scale/children）/connections。可加
    full_properties=true 输出全部属性。用于写 GDScript 前先读懂场景结构。"""
    if not TSCN_AVAILABLE:
        return '{"error": "tscn_analyzer not available"}'
    try:
        scene = parse_tscn(path)
    except FileNotFoundError as e:
        return f'{{"error": "{e}"}}'
    out = {
        "header": _json_default(scene.header),
        "ext_resources": _json_default(scene.ext_resources),
        "sub_resources": {k: {"type": v.type, "props": {kk: _json_default(vv) for kk, vv in list(v.properties.items())[:20]}}
                          for k, v in scene.sub_resources.items()},
        "nodes_count": len(scene.nodes),
        "root": _json_default(scene.root),
        "connections": _json_default(scene.connections),
    }
    return json.dumps(out, default=_json_default, ensure_ascii=False)


@_MCP.tool()
def project_scan(project_root: str) -> str:
    """扫描一个 Godot 项目目录（含 project.godot），返回全项目交叉引用 JSON：
    项目名/autoloads/场景列表（含各场景 root）/脚本列表/资产数、scene_instances
    （谁实例谁）、script_to_scenes（脚本用在哪）、resource_usage、unused_assets
    （未引用资源）、依赖链。用于不启动 Godot 就了解整个项目。"""
    if not TSCN_AVAILABLE:
        return '{"error": "tscn_analyzer not available"}'
    try:
        idx = scan_project(os.path.abspath(project_root))
    except FileNotFoundError as e:
        return f'{{"error": "{e}"}}'
    out = {
        "project_name": idx.project_name,
        "autoloads": idx.autoloads,
        "total_scenes": idx.total_scenes,
        "total_scripts": idx.total_scripts,
        "total_assets": idx.total_assets,
        "scenes": {k: {"root": v.root.name if v.root else None,
                       "nodes": len(v.nodes),
                       "instances": idx.scene_instances.get(k, [])}
                   for k, v in idx.scenes.items()},
        "scripts": idx.scripts,
        "script_to_scenes": idx.script_to_scenes,
        "resource_usage": {k: v for k, v in list(idx.resource_usage.items())[:100]},
        "unused_assets": [p for p in idx.assets if not idx.resource_usage.get(p, [])][:100],
    }
    return json.dumps(out, default=_json_default, ensure_ascii=False)


if __name__ == "__main__":
    _MCP.run()