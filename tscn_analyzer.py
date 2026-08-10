"""tscn_analyzer — 不依赖 Godot 的 .tscn / 项目静态解析（精简自 GodotIQ parsers）

设计思想（学自 salvo10f/godotiq，MIT）：
- Godot 引擎读起来又慢又重，但 .tscn/.tres 是文本格式——纯 Python 就能解析成对象树。
- 有了对象树，AI 在写 GDScript 前先"读懂"场景：节点层级、属性、信号连接、脚本挂载、实例引用。
- 再进一步建全项目索引：哪个场景实例了谁、哪个脚本用在哪些场景、哪个资源没人用。

本模块只提炼核心：
1. parse_value          — TSCN 属性值全类型解析（标量/向量/颜色/Transform3D 分解/引用/数组/字典）
2. parse_tscn           — .tscn 状态机解析 → TscnScene 对象树（节点/资源/连接/组/脚本）
3. scan_project         — 全项目扫描 + 交叉引用（场景实例链/脚本→场景/未用资产）
"""

from __future__ import annotations

import math
import os
import re
import enum
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# ============================================================================
# 1. parse_value — TSCN 值解析
# ============================================================================

_EXT_RESOURCE_RE = re.compile(r'^ExtResource\("([^"]+)"\)$')
_SUB_RESOURCE_RE = re.compile(r'^SubResource\("([^"]+)"\)$')
_NODE_PATH_RE = re.compile(r'^NodePath\("([^"]*)"\)$')
_STRING_NAME_RE = re.compile(r'^StringName\(&"([^"]*)"\)$')

_FLOAT_TUPLE_TYPES: dict[str, int] = {
    "Vector2": 2, "Vector3": 3, "Vector4": 4, "Color": 4, "Rect2": 4,
    "Rect2i": 4, "AABB": 6, "Quaternion": 4, "Plane": 4, "Basis": 9,
    "Transform2D": 6, "Projection": 16,
}
_INT_TUPLE_TYPES: dict[str, int] = {"Vector2i": 2, "Vector3i": 3, "Vector4i": 4}
_PACKED_PREFIXES = (
    "PackedByteArray", "PackedInt32Array", "PackedInt64Array",
    "PackedFloat32Array", "PackedFloat64Array", "PackedStringArray",
    "PackedVector2Array", "PackedVector3Array", "PackedColorArray",
    "PackedVector4Array",
)
_NOISE_EPSILON = 1e-7
_GIMBAL_EPSILON = 0.00000025
_ANGLE_EPSILON = 1e-10


def parse_value(raw: str) -> object:
    """Parse a TSCN property value string into a Python object.

    Returns bool/int/float/str/tuple/list/dict, {"type":"ext_resource","id":...}
    for references, or the raw string when unrecognized (fail-safe).
    """
    s = raw.strip()
    if not s:
        return ""

    # Packed arrays — return raw (avoid regex on megabyte lines)
    if s.startswith("Packed"):
        return s

    m = _EXT_RESOURCE_RE.match(s)
    if m:
        return {"type": "ext_resource", "id": m.group(1)}
    m = _SUB_RESOURCE_RE.match(s)
    if m:
        return {"type": "sub_resource", "id": m.group(1)}
    m = _NODE_PATH_RE.match(s)
    if m:
        return m.group(1)
    m = _STRING_NAME_RE.match(s)
    if m:
        return m.group(1)

    if s.startswith("Transform3D(") and s.endswith(")"):
        inner = s[len("Transform3D("):-1]
        try:
            values = tuple(float(x.strip()) for x in inner.split(","))
            if len(values) == 12:
                return decompose_transform3d(values)
        except (ValueError, IndexError):
            logger.warning("Malformed Transform3D: %s", s[:80])
        return s

    for prefix, count in _FLOAT_TUPLE_TYPES.items():
        if s.startswith(prefix + "(") and s.endswith(")"):
            inner = s[len(prefix) + 1:-1]
            try:
                parts = [float(x.strip()) for x in inner.split(",")]
                if len(parts) == count:
                    return tuple(parts)
            except ValueError:
                logger.warning("Malformed %s: %s", prefix, s[:80])
            return s

    for prefix, count in _INT_TUPLE_TYPES.items():
        if s.startswith(prefix + "(") and s.endswith(")"):
            inner = s[len(prefix) + 1:-1]
            try:
                parts = [int(x.strip()) for x in inner.split(",")]
                if len(parts) == count:
                    return tuple(parts)
            except ValueError:
                logger.warning("Malformed %s: %s", prefix, s[:80])
            return s

    if s.startswith("{"):
        return _parse_dict(s)
    if s.startswith("["):
        return _parse_array(s)
    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
        return _unescape_string(s[1:-1])
    if s == "true":
        return True
    if s == "false":
        return False
    if s == "null":
        return None
    try:
        if "." in s or "e" in s or "E" in s:
            return float(s)
        return int(s)
    except ValueError:
        pass
    return s


def decompose_transform3d(values: tuple[float, ...]) -> dict:
    """Decompose Godot's 12 column-major Transform3D floats → p/rot(YXZ)/scale.

    Mirrors Godot basis.cpp Euler decomposition incl. gimbal-lock branch.
    """
    position = (values[9], values[10], values[11])
    rows = [
        [values[0], values[3], values[6]],
        [values[1], values[4], values[7]],
        [values[2], values[5], values[8]],
    ]
    for i in range(3):
        for j in range(3):
            if abs(rows[i][j]) < _NOISE_EPSILON:
                rows[i][j] = 0.0

    sx = math.sqrt(rows[0][0] ** 2 + rows[1][0] ** 2 + rows[2][0] ** 2)
    sy = math.sqrt(rows[0][1] ** 2 + rows[1][1] ** 2 + rows[2][1] ** 2)
    sz = math.sqrt(rows[0][2] ** 2 + rows[1][2] ** 2 + rows[2][2] ** 2)
    det = (
        rows[0][0] * (rows[1][1] * rows[2][2] - rows[1][2] * rows[2][1])
        - rows[0][1] * (rows[1][0] * rows[2][2] - rows[1][2] * rows[2][0])
        + rows[0][2] * (rows[1][0] * rows[2][1] - rows[1][1] * rows[2][0])
    )
    if det < 0:
        sx = -sx
    scale = (sx, sy, sz)

    m = [[0.0] * 3 for _ in range(3)]
    for i in range(3):
        s_val = [sx, sy, sz][i]
        if abs(s_val) > _NOISE_EPSILON:
            for r in range(3):
                m[r][i] = rows[r][i] / s_val
        else:
            m[i][i] = 1.0

    m12 = m[1][2]
    if abs(m12) < (1.0 - _GIMBAL_EPSILON):
        x = math.asin(-m12)
        y = math.atan2(m[0][2], m[2][2])
        z = math.atan2(m[1][0], m[1][1])
    else:
        z = 0.0
        if m12 < 0:
            x = math.pi / 2.0
            y = math.atan2(m[0][1], m[0][0])
        else:
            x = -math.pi / 2.0
            y = math.atan2(-m[0][1], m[0][0])

    if abs(x) < _ANGLE_EPSILON:
        x = 0.0
    if abs(y) < _ANGLE_EPSILON:
        y = 0.0
    if abs(z) < _ANGLE_EPSILON:
        z = 0.0
    return {"position": position, "rotation": (x, y, z), "scale": scale, "raw": values}


def _parse_array(s: str) -> list:
    s = s.strip()
    if not (s.startswith("[") and s.endswith("]")):
        return [s]
    inner = s[1:-1].strip()
    if not inner:
        return []
    return [parse_value(p.strip()) for p in _split_at_depth_zero(inner, ",") if p.strip()]


def _parse_dict(s: str) -> dict:
    s = s.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return {}
    inner = s[1:-1].strip()
    if not inner:
        return {}
    result: dict = {}
    for pair in _split_at_depth_zero(inner, ","):
        pair = pair.strip()
        if not pair:
            continue
        colon_parts = _split_at_depth_zero(pair, ":")
        if len(colon_parts) < 2:
            continue
        key = parse_value(colon_parts[0].strip())
        if not isinstance(key, str):
            key = str(key)
        result[key] = parse_value(":".join(colon_parts[1:]).strip())
    return result


def _split_at_depth_zero(s: str, delimiter: str = ",") -> list[str]:
    """Split on delimiter only at bracket/brace/paren depth zero, respecting quotes."""
    parts: list[str] = []
    depth = 0
    in_string = False
    escape = False
    dlen = len(delimiter)
    slen = len(s)
    start = 0
    i = 0
    while i < slen:
        c = s[i]
        if escape:
            escape = False
            i += 1
            continue
        if c == "\\":
            escape = True
            i += 1
            continue
        if c == '"':
            in_string = not in_string
            i += 1
            continue
        if in_string:
            i += 1
            continue
        if c in "([{":
            depth += 1
            i += 1
            continue
        if c in ")]}":
            depth -= 1
            i += 1
            continue
        if depth == 0 and s[i:i + dlen] == delimiter:
            parts.append(s[start:i])
            i += dlen
            start = i
            continue
        i += 1
    parts.append(s[start:])
    return parts


def _unescape_string(s: str) -> str:
    return s.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t").replace("\\\\", "\\")


# ============================================================================
# 2. parse_tscn — 场景状态机解析
# ============================================================================

class _ParserState(enum.Enum):
    INITIAL = "initial"
    FILE_HEADER = "file_header"
    EXT_RESOURCE = "ext_resource"
    SUB_RESOURCE = "sub_resource"
    NODE = "node"
    CONNECTION = "connection"
    EDITABLE = "editable"


_SECTION_TYPES = frozenset({
    "gd_scene", "gd_resource", "ext_resource", "sub_resource",
    "node", "connection", "editable",
})
_HEADER_ATTR_RE = re.compile(
    r'(\w+)=(("[^"]*")|(\[[^\]]*\])|(\w+\([^)]*\))|([^\s\]]*))'
)
_QUOTED_TOKEN_RE = re.compile(r'&?"([^"]+)"')


@dataclass
class SceneHeader:
    format: int = 3
    load_steps: int | None = None
    uid: str | None = None


@dataclass
class ExtResource:
    id: str
    type: str = ""
    path: str = ""
    uid: str | None = None


@dataclass
class SubResource:
    id: str
    type: str = ""
    properties: dict = field(default_factory=dict)


@dataclass
class Connection:
    signal: str = ""
    from_node: str = ""
    to_node: str = ""
    method: str = ""
    flags: int = 0


@dataclass
class TscnNode:
    name: str = ""
    type: str = ""
    parent: str = ""
    instance: str | None = None  # ext_resource id of instanced scene
    unique_id: int | None = None
    groups: list[str] = field(default_factory=list)
    script: str | None = None  # ext_resource id of script
    position: tuple | None = None
    rotation: tuple | None = None
    scale: tuple | None = None
    properties: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    children: list["TscnNode"] = field(default_factory=list)
    is_instance_override: bool = False


@dataclass
class TscnScene:
    header: SceneHeader = field(default_factory=SceneHeader)
    ext_resources: dict[str, ExtResource] = field(default_factory=dict)
    sub_resources: dict[str, SubResource] = field(default_factory=dict)
    root: TscnNode | None = None
    nodes: list[TscnNode] = field(default_factory=list)
    connections: list[Connection] = field(default_factory=list)

    def find_node(self, path: str) -> TscnNode | None:
        """Find node by Godot path (e.g. 'Root/Camera' or 'Camera')."""
        if self.root is None:
            return None
        if path == self.root.name or path in ("", "."):
            return self.root
        if not path.startswith(self.root.name):
            # Try relative lookup
            target = path.split("/")[-1]
            for node in self.nodes:
                if node.name == target:
                    return node
            return None
        rel = path[len(self.root.name):].strip("/")
        if not rel:
            return self.root
        current = self.root
        for part in rel.split("/"):
            found = None
            for child in current.children:
                if child.name == part:
                    found = child
                    break
            if found is None:
                return None
            current = found
        return current

    def get_nodes_by_group(self, group: str) -> list[TscnNode]:
        return [n for n in self.nodes if group in n.groups]

    def get_nodes_by_type(self, type_name: str) -> list[TscnNode]:
        return [n for n in self.nodes if n.type == type_name]

    def get_all_scripts(self) -> list[str]:
        """Return res:// paths of all scripts referenced (ext resources with type Script)."""
        return [
            ext.path for ext in self.ext_resources.values()
            if ext.type == "Script" and ext.path.endswith(".gd")
        ]

    def get_all_instances(self) -> list[str]:
        return [ext.path for ext in self.ext_resources.values()
                if ext.type == "PackedScene" and ext.path.endswith(".tscn")]


def _is_section_header(line: str) -> str | None:
    if not line.startswith("["):
        return None
    end = len(line)
    for i, c in enumerate(line[1:], 1):
        if c in (" ", "]"):
            end = i
            break
    word = line[1:end]
    return word if word in _SECTION_TYPES else None


def _parse_header_attrs(line: str) -> dict[str, str]:
    inner = line.strip()
    if inner.startswith("["):
        inner = inner[1:]
    if inner.endswith("]"):
        inner = inner[:-1]
    attrs: dict[str, str] = {}
    for m in _HEADER_ATTR_RE.finditer(inner):
        key = m.group(1)
        val = m.group(2)
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        attrs[key] = val
    return attrs


def _bracket_depth_change(text: str) -> int:
    depth = 0
    in_string = False
    escape = False
    for c in text:
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
    return depth


def parse_tscn(path: str) -> TscnScene:
    """Parse a .tscn file into a TscnScene (node tree + resources + connections)."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Scene file not found: {path}")
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    scene = TscnScene()
    state = _ParserState.INITIAL
    current_sub: SubResource | None = None
    current_node: TscnNode | None = None
    accumulating = False
    accum_lines: list[str] = []
    accum_key = ""
    depth = 0

    for raw_line in lines:
        line = raw_line.rstrip("\n").rstrip("\r")
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            continue

        if accumulating:
            sec_type = _is_section_header(stripped)
            if sec_type is not None:
                logger.warning("Unfinished multiline value for '%s'", accum_key)
                accumulating = False
                accum_lines = []
                depth = 0
            else:
                accum_lines.append(line)
                depth += _bracket_depth_change(line)
                if depth <= 0:
                    full_value = "\n".join(accum_lines)
                    if accum_key.startswith("_") or accum_key in ("blend_shapes", "surfaces"):
                        parsed: object = full_value
                    else:
                        parsed = parse_value(full_value)
                    _store_property(state, accum_key, parsed, current_sub, current_node)
                    accumulating = False
                    accum_lines = []
                    depth = 0
                continue

        sec_type = _is_section_header(stripped)
        if sec_type is not None:
            attrs = _parse_header_attrs(stripped)
            if sec_type in ("gd_scene", "gd_resource"):
                state = _ParserState.FILE_HEADER
                scene.header = SceneHeader(
                    format=int(attrs.get("format", "3")),
                    load_steps=int(attrs["load_steps"]) if "load_steps" in attrs else None,
                    uid=attrs.get("uid"),
                )
            elif sec_type == "ext_resource":
                state = _ParserState.EXT_RESOURCE
                res_id = attrs.get("id", "")
                scene.ext_resources[res_id] = ExtResource(
                    id=res_id, type=attrs.get("type", ""),
                    path=attrs.get("path", ""), uid=attrs.get("uid"),
                )
            elif sec_type == "sub_resource":
                state = _ParserState.SUB_RESOURCE
                sub_id = attrs.get("id", "")
                current_sub = SubResource(id=sub_id, type=attrs.get("type", ""))
                scene.sub_resources[sub_id] = current_sub
            elif sec_type == "node":
                state = _ParserState.NODE
                groups: list[str] = []
                if "groups" in attrs:
                    parsed_groups = parse_value(attrs["groups"])
                    if isinstance(parsed_groups, list):
                        groups = [str(g) for g in parsed_groups]
                instance_id: str | None = None
                if "instance" in attrs:
                    inst_val = parse_value(attrs["instance"])
                    if isinstance(inst_val, dict) and inst_val.get("type") == "ext_resource":
                        instance_id = inst_val["id"]
                    elif isinstance(inst_val, str):
                        instance_id = inst_val
                unique_id: int | None = None
                if "unique_id" in attrs:
                    try:
                        unique_id = int(attrs["unique_id"])
                    except ValueError:
                        pass
                current_node = TscnNode(
                    name=attrs.get("name", ""), type=attrs.get("type", ""),
                    parent=attrs.get("parent", ""), instance=instance_id,
                    unique_id=unique_id, groups=groups,
                )
                scene.nodes.append(current_node)
            elif sec_type == "connection":
                state = _ParserState.CONNECTION
                flags = 0
                if "flags" in attrs:
                    try:
                        flags = int(attrs["flags"])
                    except ValueError:
                        pass
                scene.connections.append(Connection(
                    signal=attrs.get("signal", ""), from_node=attrs.get("from", ""),
                    to_node=attrs.get("to", ""), method=attrs.get("method", ""),
                    flags=flags,
                ))
            elif sec_type == "editable":
                state = _ParserState.EDITABLE
            continue

        if " = " not in stripped:
            continue
        key, _, value_str = stripped.partition(" = ")
        key = key.strip()
        value_str = value_str.strip()
        line_depth = _bracket_depth_change(value_str)
        if line_depth > 0:
            accumulating = True
            accum_lines = [value_str]
            accum_key = key
            depth = line_depth
            continue
        _store_property(state, key, parse_value(value_str), current_sub, current_node)

    _build_tree(scene)
    return scene


def _store_property(state, key, value, current_sub, current_node) -> None:
    if state == _ParserState.SUB_RESOURCE and current_sub is not None:
        current_sub.properties[key] = value
    elif state == _ParserState.NODE and current_node is not None:
        if key == "script":
            if isinstance(value, dict) and value.get("type") == "ext_resource":
                current_node.script = value["id"]
            elif isinstance(value, str):
                current_node.script = value
        elif key == "transform":
            if isinstance(value, dict) and "position" in value:
                current_node.position = value["position"]
                current_node.rotation = value["rotation"]
                current_node.scale = value["scale"]
            current_node.properties[key] = value
        elif key.startswith("metadata/"):
            meta_key = key[len("metadata/"):]
            current_node.metadata[meta_key] = value
            if meta_key == "_groups":
                _merge_metadata_groups(current_node, value)
        else:
            current_node.properties[key] = value


def _merge_metadata_groups(node: TscnNode, value: object) -> None:
    for group in _normalize_group_values(value):
        if group not in node.groups:
            node.groups.append(group)


def _normalize_group_values(value: object) -> list[str]:
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str):
        raw_items = _QUOTED_TOKEN_RE.findall(value)
        if not raw_items and value and not any(c in value for c in "()[]{}"):
            raw_items = [value]
    else:
        raw_items = []
    groups: list[str] = []
    for item in raw_items:
        if not isinstance(item, str):
            continue
        group = item
        if group.startswith('&"') and group.endswith('"'):
            group = group[2:-1]
        elif group.startswith('"') and group.endswith('"'):
            group = group[1:-1]
        if group and group not in groups:
            groups.append(group)
    return groups


def _build_tree(scene: TscnScene) -> None:
    if not scene.nodes:
        return
    root = next((n for n in scene.nodes if n.parent == ""), None)
    if root is None:
        logger.warning("No root node found")
        return
    scene.root = root

    path_map: dict[str, TscnNode] = {root.name: root}
    for node in scene.nodes:
        if node is root:
            continue
        full_path = f"{root.name}/{node.name}" if node.parent == "." else f"{root.name}/{node.parent}/{node.name}"
        path_map[full_path] = node

    instance_names = {n.name for n in scene.nodes if n.instance is not None}

    for node in scene.nodes:
        if node is root:
            continue
        parent_path = root.name if node.parent == "." else f"{root.name}/{node.parent}"
        parent_node = path_map.get(parent_path)
        if parent_node is not None:
            parent_node.children.append(node)
        else:
            first_segment = node.parent.split("/")[0] if node.parent != "." else ""
            instance_parent = path_map.get(f"{root.name}/{first_segment}")
            if first_segment in instance_names and instance_parent is not None:
                node.is_instance_override = True
                instance_parent.children.append(node)
            else:
                logger.warning("Parent not found for node '%s'", node.name)


# ============================================================================
# 3. scan_project — 全项目索引 + 交叉引用
# ============================================================================

_ASSET_EXTENSIONS: dict[str, str] = {
    ".glb": "model", ".gltf": "model", ".obj": "model", ".fbx": "model",
    ".png": "texture", ".jpg": "texture", ".jpeg": "texture", ".svg": "texture",
    ".webp": "texture", ".ttf": "font", ".otf": "font", ".woff": "font",
    ".tscn": "scene", ".scn": "scene", ".tres": "resource", ".res": "resource",
    ".gdshader": "shader", ".shader": "shader", ".wav": "audio", ".ogg": "audio",
    ".mp3": "audio",
}


@dataclass
class ProjectIndex:
    project_root: Path
    project_name: str
    scenes: dict[str, TscnScene]  # res:// path -> scene
    scripts: list[str]
    assets: dict[str, dict]
    scene_instances: dict[str, list[str]]  # scene -> [scenes it instances]
    scene_instance_of: dict[str, list[str]]  # scene -> [scenes that instance it]
    script_to_scenes: dict[str, list[str]]
    resource_usage: dict[str, list[str]]
    autoloads: dict[str, str]
    total_scenes: int
    total_scripts: int
    total_assets: int


def walk_project_files(project_root: Path):
    """Yield every project file, skipping dot-dirs and addons/godotiq."""
    for dirpath, dirnames, filenames in os.walk(project_root):
        current_dir = Path(dirpath)
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".")
            and (current_dir / d).relative_to(project_root).as_posix() != "addons/godotiq"
        ]
        for fname in filenames:
            yield Path(dirpath) / fname


def scan_project(project_root: Path) -> ProjectIndex:
    """Scan a Godot project: parse all .tscn, build cross-reference index."""
    project_root = Path(project_root)
    if not project_root.exists():
        raise FileNotFoundError(f"Project root does not exist: {project_root}")
    godot_file = project_root / "project.godot"
    if not godot_file.exists():
        raise FileNotFoundError(f"No project.godot found in {project_root}")

    tscn_files: list[Path] = []
    script_paths: list[str] = []
    assets: dict[str, dict] = {}

    for fpath in walk_project_files(project_root):
        ext = fpath.suffix.lower()
        if ext == ".tscn":
            tscn_files.append(fpath)
            assets[_to_res_path(fpath, project_root)] = {"type": "scene", "used_by": []}
        elif ext == ".gd":
            script_paths.append(_to_res_path(fpath, project_root))
        elif ext in _ASSET_EXTENSIONS:
            assets[_to_res_path(fpath, project_root)] = {"type": _ASSET_EXTENSIONS[ext], "used_by": []}

    scenes: dict[str, TscnScene] = {}
    for fpath in tscn_files:
        res_path = _to_res_path(fpath, project_root)
        try:
            scenes[res_path] = parse_tscn(str(fpath))
        except Exception:
            logger.warning("Failed to parse %s, skipping", fpath)

    scene_instances: dict[str, list[str]] = defaultdict(list)
    scene_instance_of: dict[str, list[str]] = defaultdict(list)
    script_to_scenes: dict[str, list[str]] = defaultdict(list)
    resource_usage: dict[str, list[str]] = defaultdict(list)

    for scene_res, scene_obj in scenes.items():
        for ext in scene_obj.ext_resources.values():
            resource_usage[ext.path].append(scene_res)
            if ext.type == "PackedScene" and ext.path.endswith(".tscn"):
                scene_instances[scene_res].append(ext.path)
                scene_instance_of[ext.path].append(scene_res)
            if ext.type == "Script":
                script_to_scenes[ext.path].append(scene_res)

    for asset_path in assets:
        assets[asset_path]["used_by"] = resource_usage.get(asset_path, [])

    project_name, autoloads = _parse_project_godot(godot_file)

    return ProjectIndex(
        project_root=project_root,
        project_name=project_name,
        scenes=scenes,
        scripts=script_paths,
        assets=assets,
        scene_instances=dict(scene_instances),
        scene_instance_of=dict(scene_instance_of),
        script_to_scenes=dict(script_to_scenes),
        resource_usage=dict(resource_usage),
        autoloads=autoloads,
        total_scenes=len(scenes),
        total_scripts=len(script_paths),
        total_assets=len(assets),
    )


def find_asset_usage(index: ProjectIndex, asset_path: str) -> list[str]:
    return index.resource_usage.get(asset_path, [])


def find_unused_assets(index: ProjectIndex) -> list[str]:
    return [p for p in index.assets if not index.resource_usage.get(p, [])]


def get_scene_dependency_chain(index: ProjectIndex, scene_path: str) -> list[str]:
    """All scenes transitively instanced by the given scene (BFS)."""
    visited: set[str] = set()
    queue = list(index.scene_instances.get(scene_path, []))
    result: list[str] = []
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        result.append(current)
        for dep in index.scene_instances.get(current, []):
            if dep not in visited:
                queue.append(dep)
    return result


def _to_res_path(abs_path: Path, project_root: Path) -> str:
    return "res://" + abs_path.relative_to(project_root).as_posix()


def _parse_project_godot(path: Path) -> tuple[str, dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"project.godot not found: {path}")
    project_name = ""
    autoloads: dict[str, str] = {}
    current_section = ""
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith(";"):
                continue
            if line.startswith("[") and line.endswith("]"):
                current_section = line[1:-1]
                continue
            if current_section == "application" and line.startswith("config/name="):
                value = line.split("=", 1)[1].strip()
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                project_name = value
            elif current_section == "autoload" and "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                if value.startswith("*"):
                    value = value[1:]
                autoloads[key] = value
    return project_name, autoloads
