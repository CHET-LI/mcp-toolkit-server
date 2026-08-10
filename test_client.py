"""mcp-toolkit-server 端到端自测。
跑法：.venv\\Scripts\\python.exe test_client.py   （用 venv 里的 mcp）
  A) 直接校验每个模块底层函数（I/O 正确性）
  B) 用 mcp.stdio 客户端走真实 MCP 会话（握手 + list_tools + 逐个 call）
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import server as S

FAIL = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail or ''}")
    if not cond:
        FAIL.append(name)


def _make_tiny_project(tmp: str) -> Path:
    """临时小 Godot 项目，供 tscn_analyzer 相关用例使用。"""
    root = Path(tmp) / "proj"
    root.mkdir(exist_ok=True)
    (root / "project.godot").write_text(
        '[application]\n\nconfig/name="Tiny"\n\n[autoload]\n\nGame="*res://autoload.gd"\n',
        encoding="utf-8",
    )
    (root / "autoload.gd").write_text("extends Node\n", encoding="utf-8")
    (root / "main.gd").write_text("extends Node2D\n", encoding="utf-8")
    (root / "main.tscn").write_text(
        '[gd_scene load_steps=3 format=3 uid="uid://abc"]\n'
        '[ext_resource type="Script" path="res://main.gd" id="1"]\n'
        "[node name=\"Main\" type=\"Node2D\"]\nscript = ExtResource(\"1\")\n"
        "position = Vector2(10, 20)\n",
        encoding="utf-8",
    )
    (root / "world.tscn").write_text(
        '[gd_scene load_steps=3 format=3]\n'
        '[ext_resource type="PackedScene" path="res://main.tscn" id="1"]\n'
        '[ext_resource type="Texture2D" path="res://icon.png" id="2"]\n'
        "[node name=\"World\" type=\"Node2D\"]\n"
        "[node name=\"M\" parent=\".\" instance=ExtResource(\"1\")]\n"
        "[node name=\"Sprite\" parent=\".\" type=\"Sprite2D\"]\ntexture = ExtResource(\"2\")\n",
        encoding="utf-8",
    )
    (root / "icon.png").write_bytes(b"png")
    (root / "unused.png").write_bytes(b"png")
    return root


# ── A. 进程级模块校验 ─────────────────────────────────────
def test_functions():
    # temporal
    r = S._temporal.normalize_event_when("前天下午")
    check("time_resolve 前天下午", bool(r and r.get("start")), str(r)[:60])
    check("time_since 非法", S.time_since("not-a-date") == "null")
    check("time_since 合法", "昨天" not in S.time_since("9999-01-01T00:00"))

    # proactive
    from proactive import proactive_band as pb
    check("proactive 20h=greet", pb(3600 * 20) == "greet", pb(3600 * 20))
    g = S.proactive_band(3600 * 5, None, ["CS 饰品"])
    check("proactive_band 返含checkin", "checkin" in g, g[:50])

    # trust
    check("relation reject", S.relation_judge("其实我并不喜欢苹果", "我喜欢苹果") == "reject")
    check("relation different", S.relation_judge("我想喝奶茶", "今天下雨") == "different")

    # dedupe: 记录后重复分应明显>异题分
    S._corpus.record_output("T", "今天聊了 CS 饰品行情")
    sc, _ = S._corpus.score_draft("T", "今天聊了 CS 饰品行情呢")
    lo, _ = S._corpus.score_draft("T", "今天天气不错去散步")
    check("dedupe_score 重复>异题", sc > 0 and sc > lo, f"{sc:.3f} vs {lo:.3f}")

    # directives
    S._directives.clear("t_user")
    hits = S._directives.record_from_text("t_user", "以后别再提那个项目了")
    check("directive 提取", len(hits) >= 1, str(hits)[:60])
    block = S._directives.render_block("t_user")
    check("directive 渲染", "那个项目" in block, block[:40].replace("\n", " "))

    # tscn_analyzer
    with tempfile.TemporaryDirectory() as tmp:
        proj = _make_tiny_project(tmp)
        scene_json = S.tscn_parse(str(proj / "main.tscn"))
        check("tscn_parse 含 root", '"root"' in scene_json and "Main" in scene_json, scene_json[:80])
        check("tscn_parse script 关联", '"res://main.gd"' in scene_json)
        idx_json = S.project_scan(str(proj))
        check("project_scan 项目名", '"Tiny"' in idx_json, idx_json[:80])
        check("project_scan 场景数", '"total_scenes": 2' in idx_json)
        check("project_scan 未用资产", "unused.png" in idx_json)
        dec = S.transform3d_decompose("1,0,0,0,1,0,0,0,1,5,6,7")
        check("transform3d_decompose", '"position": [5' in dec, dec[:60])


# ── B. 真实 MCP stdio 会话 ──────────────────────────────
async def test_stdio():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,  # .venv python
        args=[os.path.join(HERE, "server.py")],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            check("MCP 工具数>=11", len(names) >= 11, str(names))

            def text_of(resp):
                return resp.content[0].text if resp.content else ""

            r = await session.call_tool("time_resolve", {"expr": "昨天"})
            check("MCP time_resolve", "start" in text_of(r), text_of(r)[:50])

            r = await session.call_tool("relation_judge",
                                        {"statement": "其实我并不喜欢苹果", "earlier": "我喜欢苹果"})
            check("MCP relation_judge=reject", text_of(r) == "reject", text_of(r))

            r = await session.call_tool("directive_store", {"user": "mq", "text": "以后别再提那个项目了"})
            check("MCP directive_store", "ban_topic" in text_of(r), text_of(r)[:60])

            r = await session.call_tool("dedupe_record", {"scope": "mq", "text": "今天聊了 CS 饰品行情"})
            check("MCP dedupe_record", "ok" in text_of(r), text_of(r))

            r = await session.call_tool("dedupe_score", {"scope": "mq", "draft": "今天聊了 CS 饰品行情呢"})
            check("MCP dedupe_score 返回数值", True, text_of(r))

            with tempfile.TemporaryDirectory() as tmp:
                proj = _make_tiny_project(tmp)
                r = await session.call_tool("tscn_parse", {"path": str(proj / "main.tscn")})
                check("MCP tscn_parse", "Main" in text_of(r), text_of(r)[:60])
                r = await session.call_tool("project_scan", {"project_root": str(proj)})
                check("MCP project_scan", '"Tiny"' in text_of(r), text_of(r)[:60])
                r = await session.call_tool("transform3d_decompose", {"values": "1,0,0,0,1,0,0,0,1,5,6,7"})
                check("MCP transform3d_decompose", '"position": [5' in text_of(r), text_of(r)[:60])


def main():
    test_functions()
    asyncio.run(test_stdio())
    print("\n===== " + ("ALL PASS ✅" if not FAIL else f"{len(FAIL)} FAIL ❌: {FAIL}"))
    sys.exit(0 if not FAIL else 1)


if __name__ == "__main__":
    main()