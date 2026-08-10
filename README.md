# MCP Toolkit Server

给 AI agent（QwenPaw 的 QA / default / local01 等）提供的一批通用 **MCP 工具**：
时间语义解析、主动搭话分级、陈述关系判断、防重复语料、用户指令持久化，
以及不依赖 Godot 的 `.tscn` 场景静态解析。

全部通过 stdio 暴露为 MCP 工具，agent 直接调用，无需拼命令行。

## 工具清单（11 个）

| 模块 | 工具 | 说明 |
|------|------|------|
| temporal | `time_resolve(expr)` | "昨天/上周/3天前" → ISO 起止锚点 {start,end,label} |
| temporal | `time_since(iso)` | ISO → 人话 "N分钟前/昨天/N周前" |
| proactive | `proactive_band(gap_seconds, [hour], [recent_topics])` | 主动搭话分级 silent/nudge/checkin/greet/goodnight + 策略 |
| trust | `relation_judge(statement, earlier)` | 两句话关系 confirm/reject/different（词法否定+转折） |
| arr | `dedupe_record(scope, text)` | 记录已出口的话进防重复语料 |
| arr | `dedupe_score(scope, draft)` | 草稿重复打分（越高越像车轱辘话） |
| directives | `directive_store(user, text)` | 提取并持久化"别再提X"，TTL 3 天自动续期 |
| directives | `directive_render(user)` | 活跃指令渲染成系统提示片段，注入上下文提醒 |
| tscn_analyzer | `tscn_parse(path)` | 解析 Godot `.tscn` 场景文件（纯文本，无需启动 Godot）→ 节点树/资源/连接/脚本 JSON |
| tscn_analyzer | `project_scan(project_root)` | Godot 项目全量扫描 → 场景/脚本/资产/实例链/未用资产 JSON |
| tscn_analyzer | `transform3d_decompose(values)` | 12 浮点 Transform3D → {position, rotation(YXZ), scale} |

有状态工具（dedupe / directives）用进程内单例 + `data/` 目录持久化，进程重启不丢，按 scope/user 隔离。

## 设计要点

- **纯函数 + 轻状态**：只暴露"判别 / 换算 / 轻状态"类能力；重逻辑（记忆提炼、归档、证据衰减等）留在宿主库侧
- **失败安全**：非法输入返回明确错误值，不抛异常打崩 MCP 会话
- **中英双语**：时间解析、否定/转折检测均支持中英文
- **静态解析**：tscn 工具不依赖 Godot 运行时，纯文本状态机解析

## 安装 / 运行

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install mcp==1.29.0
.venv\Scripts\python.exe server.py          # 启动 stdio server
.venv\Scripts\python.exe test_client.py     # 端到端自测（A 函数级 + B 真实 MCP 会话）
```

## 注册进 QwenPaw（本机 agent）

1. `drivers/mcp/<name>.yaml` — stdio 指向 `.venv` python + server.py，`default_effect: allow`
2. `credentials.yaml` 加 `mcp/<name>`（static 空 secrets）——缺了会 CredentialNotFoundError
3. reload-config；若需触发 watcher 重试：touch 卡片文件

⚠️ 注意：MCP 工具按 **workspace 隔离**，当前 agent 的 `drivers/mcp/` 与 `credentials.yaml`。
给其他 agent 用需在对方 workspace 下各放一份卡片 + credentials 条目。

## 测试

`test_client.py` 24 项全绿：函数级 I/O（含临时小项目真实 tscn_parse/project_scan/transform3d_decompose）
+ mcp.stdio 真实会话握手 / list_tools / 逐个 call。
