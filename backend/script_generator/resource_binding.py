"""第二层A：统一 ResourceBinder（设计 §4，Phase B）。

设计原文（§4.1/§4.2）只允许**一个**绑定实现：

```text
ResourceRef/token → 查询当前工作区资源 → 检查版本、状态、类型、权限和文件存在
                  → 绑定 ResourceHandle/确定路径 → 记录 content hash 与来源
```

- **工具调用**：进入工具代码**之前**绑定，工具不再各自记得调用 ``resolve_image``；
- **脚本交付**：source 检查后绑定生成 bundled 文本，再做契约/类型检查。

因此本模块是**中性**的（与 ``script_artifact.py`` 同一层），同时被 V2 会话
（``session.artifacts`` 里的资源注册中心）和 V3 域（``resources`` 表）复用：
两边各实现一个 :class:`ResourceSource`，绑定规则、诊断码与事实字段**完全同一份**。

硬规则（设计 §4.2）：

- 稳定诊断码 ``RESOURCE_TOKEN_UNRESOLVED`` / ``RESOURCE_NOT_FOUND`` /
  ``RESOURCE_VERSION_EXPIRED`` / ``RESOURCE_TYPE_MISMATCH``，另有
  ``RESOURCE_SCOPE_MISMATCH`` / ``RESOURCE_PERMISSION_MISMATCH`` /
  ``RESOURCE_NOT_USABLE`` / ``RESOURCE_FILE_MISSING`` / ``RESOURCE_CONTENT_CHANGED``；
- **禁止静默回退**：不接受"没有 id 就退回焦点素材"、也不接受"名字猜一个最像的"；
  解析不出来就是结构化失败，由调用方（Runtime）报给模型。

判定顺序（固定，先身份与历史，再语义状态，再物理事实，最后授权与形状）：

```text
解析引用 → 不存在 / 版本失效（含历史版本与被替代）→ 已归档/删除
        → 文件缺失 → 内容已变化 → 其余状态不可用
        → 范围 → 权限 → 类型不符
```

顺序不是随手排的：registry 会把"文件不存在/内容已变化"同时标成 ``invalid``，
若先按状态报 ``RESOURCE_NOT_USABLE``，两套存储就给不出同一个码，可操作的原因
（换文件 / 重新登记版本）也会被吞掉。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Sequence

# ── 稳定诊断码（写进事件/结果，措辞可以变，码不能变）────────────────────

RESOURCE_TOKEN_UNRESOLVED = "RESOURCE_TOKEN_UNRESOLVED"
RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
RESOURCE_VERSION_EXPIRED = "RESOURCE_VERSION_EXPIRED"
RESOURCE_TYPE_MISMATCH = "RESOURCE_TYPE_MISMATCH"
RESOURCE_SCOPE_MISMATCH = "RESOURCE_SCOPE_MISMATCH"
RESOURCE_PERMISSION_MISMATCH = "RESOURCE_PERMISSION_MISMATCH"
RESOURCE_NOT_USABLE = "RESOURCE_NOT_USABLE"
RESOURCE_FILE_MISSING = "RESOURCE_FILE_MISSING"
RESOURCE_CONTENT_CHANGED = "RESOURCE_CONTENT_CHANGED"
RESOURCE_NAME_AMBIGUOUS = "RESOURCE_NAME_AMBIGUOUS"
#: Phase B4（设计 §4.5/§4.8）：资源已经绑定，但**工具参数里仍是资源占位表达**
#: （``[[asset_x]]`` / ``resource://`` / ``__resources__\…``）。执行边界必须在工具
#: 启动前拒绝，绝不能让旧工具拿占位表达去猜路径。
RESOURCE_ARGUMENT_NOT_MATERIALIZED = "RESOURCE_ARGUMENT_NOT_MATERIALIZED"
#: Phase B4（设计 §4.6）：参数**用途/业务语义**不匹配（不是类型不匹配、也不是"缺资源"）。
#: 它必须在工具启动前报出来，且**不得**转化成用户需求。
RESOURCE_USE_MISMATCH = "RESOURCE_USE_MISMATCH"
#: 遥测码：参数声明了业务语义标签，但资源自己没声明任何语义 —— 无法证明"不匹配"，
#: 因此不拦（宁可放过也不误伤），但留痕提醒补资源侧的语义声明。
RESOURCE_SEMANTIC_UNVERIFIED = "RESOURCE_SEMANTIC_UNVERIFIED"

#: 结果里给模型/日志看的诊断码全集（顺序即文档顺序）。
RESOURCE_BINDING_CODES: tuple[str, ...] = (
    RESOURCE_TOKEN_UNRESOLVED,
    RESOURCE_NOT_FOUND,
    RESOURCE_VERSION_EXPIRED,
    RESOURCE_TYPE_MISMATCH,
    RESOURCE_SCOPE_MISMATCH,
    RESOURCE_PERMISSION_MISMATCH,
    RESOURCE_NOT_USABLE,
    RESOURCE_FILE_MISSING,
    RESOURCE_CONTENT_CHANGED,
    RESOURCE_NAME_AMBIGUOUS,
    RESOURCE_ARGUMENT_NOT_MATERIALIZED,
    RESOURCE_USE_MISMATCH,
)

#: 可用状态（与 V2 ``USABLE_STATUSES`` / V3 ``search_resources`` 的白名单一致）。
USABLE_STATUSES: frozenset[str] = frozenset({"candidate", "active", "verified"})

#: 素材 token 的写法：``[[asset_x]]``（可带引号）。
_TOKEN_RE = re.compile(r"^\s*\[\[\s*([^\[\]\s]+?)\s*\]\]\s*$")
#: 看起来就是资源 id 的写法（其余裸字符串只能是"名字"，需显式 allow_name）。
_ID_SHAPE_RE = re.compile(r"^(?:asset|res|frame|ev|ov|claim|img|view)[_-][0-9A-Za-z._:-]+$")

# ── 角色 / 基础类型 / 允许用途（三者分开表达，不合并）──────────────────
#
# 人工要求：**不要把 probe_out 归一化成 template**。
# ``probe_out`` 是资源**真实角色**（产出图可能带框线、文字），``template`` 是一种
# **使用方式**；把两者合成一个"类型事实"会让下游无法区分
# "这是产出物，只是被显式当模板用" 与 "这是真正的模板素材"。
#
# 因此绑定事实里三者分别给出：
# - ``type``：基础类型（``image`` / ``overlay`` / …）
# - ``role``：真实角色（``scene`` / ``template`` / ``crop`` / ``overlay`` / ``probe_out`` …）
# - ``allowed_uses``：允许用途（``auto_scene`` / ``auto_template`` / ``explicit_match`` /
#   ``explicit_view`` / ``display_overlay``）

#: 允许用途词表。
USE_AUTO_SCENE = "auto_scene"
USE_AUTO_TEMPLATE = "auto_template"
USE_EXPLICIT_MATCH = "explicit_match"
USE_EXPLICIT_VIEW = "explicit_view"
USE_DISPLAY_OVERLAY = "display_overlay"

#: 用途的规范顺序（输出稳定，便于比对）。
_USE_ORDER: tuple[str, ...] = (
    USE_AUTO_SCENE,
    USE_AUTO_TEMPLATE,
    USE_EXPLICIT_MATCH,
    USE_EXPLICIT_VIEW,
    USE_DISPLAY_OVERLAY,
)

#: 覆盖层类角色：只能当叠加显示层 / 被显式查看，**不能**当模板去匹配。
_OVERLAY_ROLES: frozenset[str] = frozenset({"annotation", "overlay"})
#: 标注分组（GUI 里"标注"分组与 role=annotation 等价）。
_ANNOTATION_GROUP = "标注"
#: 探针产出分组（与 canvas.PROBE_GROUP_ALIASES 对齐）。
_PROBE_GROUPS: frozenset[str] = frozenset({"探针", "探针产出"})

#: 结构化资源引用允许的键（工具参数 schema 与 ``ResourceRequest`` 共用）。
#: ``type`` 是**基础类型**、``role`` 是**真实角色** —— 两者不可互相顶替。
RESOURCE_PARAM_KEYS: tuple[str, ...] = ("id", "version", "type", "role")

#: schema 里声明"这个参数是资源引用"的权威标记。
RESOURCE_SCHEMA_KEY = "x-resource"

#: 遥测码：旧工具没声明 ``x-resource``，但参数名命中历史命名 → 兼容绑定时记一条。
#: **只是遥测**：不是失败码，也不该成为新工具的长期事实源。
RESOURCE_SCHEMA_LEGACY_INFERRED = "RESOURCE_SCHEMA_LEGACY_INFERRED"

#: 模型界面上的**虚拟路径**根：``__resources__\<resource_id>\<可读名>``（设计 §4.4）。
#: 它不是新的资源索引，只是"可定位的 id + 可读名"：版本/hash/权限/真实路径由 Runtime 补。
VIRTUAL_PATH_ROOT = "__resources__"

#: ``resource://`` 表达（可选 ``@version``）。
_RESOURCE_URI_RE = re.compile(r"^resource://([0-9A-Za-z._:-]+?)(?:@(\d+))?$")
#: 文件名里不能出现的东西（虚拟路径最后一段只保留可读名）。
_VIRTUAL_NAME_BAD_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

#: 历史参数名（只服务旧工具兼容层与登记期诊断；**不是**新工具的权威来源）。
_RESOURCE_PARAM_SUFFIXES: tuple[str, ...] = ("_asset_id", "_asset_ids", "_resource_id")
_RESOURCE_PARAM_EXACT: frozenset[str] = frozenset(
    {"asset_id", "asset_ids", "resource_id", "template_id", "frame_id"}
)


def declared_resource_params(schema: Mapping[str, Any] | None) -> tuple[str, ...]:
    """**权威**来源：schema 里显式声明了 ``x-resource`` 的参数名。

    新工具必须走这条路（``resource_param_schema()`` 或手写 ``x-resource``）：
    否则以后出现"名字相同但根本不是资源"的普通参数时，Binder 会错拦。
    """
    properties = (schema or {}).get("properties")
    if not isinstance(properties, Mapping):
        return ()
    out: list[str] = []
    for name, spec in properties.items():
        if isinstance(spec, Mapping) and isinstance(spec.get(RESOURCE_SCHEMA_KEY), Mapping):
            out.append(str(name))
    return tuple(sorted(out))


def infer_resource_param_names(schema: Mapping[str, Any] | None) -> tuple[str, ...]:
    """**兼容层/诊断**用的历史参数名推断（不是事实源）。

    只用于：旧工具（本规则生效前写的 schema）的兼容绑定 + 登记新工具时提示补 schema。
    """
    properties = (schema or {}).get("properties")
    if not isinstance(properties, Mapping):
        return ()
    out: list[str] = []
    for name in properties:
        key = str(name)
        if key in _RESOURCE_PARAM_EXACT or key.endswith(_RESOURCE_PARAM_SUFFIXES):
            out.append(key)
    return tuple(sorted(out))


def legacy_inferred_resource_params(schema: Mapping[str, Any] | None) -> tuple[str, ...]:
    """命中历史参数名、却**没有** ``x-resource`` 声明的参数（= 兼容绑定的对象）。"""
    declared = set(declared_resource_params(schema))
    return tuple(name for name in infer_resource_param_names(schema) if name not in declared)


def resource_param_names(schema: Mapping[str, Any] | None) -> tuple[str, ...]:
    """兼容别名：老调用方要的是"权威声明"；推断请显式用 ``infer_resource_param_names``。"""
    return declared_resource_params(schema)


def resource_ids_in_arguments(
    arguments: Mapping[str, Any] | None, param_names: Iterable[str]
) -> list[tuple[str, str]]:
    """取 ``(参数名, 引用值)`` 列表：支持单个 id 与 id 列表两种写法。"""
    data = dict(arguments or {})
    out: list[tuple[str, str]] = []
    for name in param_names:
        if name not in data:
            continue
        raw = data.get(name)
        values = raw if isinstance(raw, (list, tuple)) else [raw]
        for value in values:
            text = str(value or "").strip()
            if text:
                out.append((str(name), text))
    return out


def _virtual_name(name: str, path: str | Path | None) -> str:
    """虚拟路径最后一段：可读名 + 后缀（不带目录、不带非法字符）。"""
    raw = str(name or "").strip()
    suffix = Path(str(path or "")).suffix
    if not suffix or not re.fullmatch(r"\.[0-9A-Za-z]{1,6}", suffix):
        suffix = ".png"
    if not raw:
        raw = Path(str(path or "")).stem or "resource"
    # 名字里已经带同款后缀就不重复加
    if raw.lower().endswith(suffix.lower()):
        raw = raw[: -len(suffix)]
    cleaned = _VIRTUAL_NAME_BAD_RE.sub("_", raw).strip(" .") or "resource"
    return f"{cleaned}{suffix}"


def render_resource_virtual_path(
    *,
    resource_id: str,
    name: str = "",
    path: str | Path | None = None,
) -> str:
    """资源在**模型界面**上的稳定虚拟路径：``__resources__\\<id>\\<可读名>``。

    只携带可定位的资源 id 与可读名称；**不**包含真实盘符/目录/文件名，也不含
    version/hash/权限 —— 那些仍由 Runtime 从权威资源索引补齐（设计 §4.4）。
    """
    rid = str(resource_id or "").strip()
    if not rid:
        return ""
    return f"{VIRTUAL_PATH_ROOT}\\{rid}\\{_virtual_name(name, path)}"


def parse_resource_expression(value: Any) -> tuple[str, int | None] | None:
    """把模型给的资源表达解析成 ``(resource_id, version|None)``；不是资源表达返回 ``None``。

    兼容（设计 §4.3/§4.4）：结构化 ``{"id",...,"version"}``、裸 id、历史 ``[[asset_x]]``
    token、``resource://asset_x[@3]``、以及本地渲染的 ``__resources__\\asset_x\\名字.png``。
    普通文件名/自由文本不是资源表达（返回 ``None``），继续走旧的文件名通道。
    """
    if isinstance(value, Mapping):
        rid = str(value.get("id") or value.get("resource_id") or "").strip()
        if not rid:
            return None
        raw_version = value.get("version")
        try:
            version = int(raw_version) if raw_version not in (None, "") else None
        except (TypeError, ValueError):
            version = None
        return rid, version
    candidate = _strip_quotes(str(value or ""))
    if not candidate:
        return None
    token = _TOKEN_RE.match(candidate)
    if token:
        return str(token.group(1)).strip(), None
    uri = _RESOURCE_URI_RE.match(candidate)
    if uri:
        return str(uri.group(1)), (int(uri.group(2)) if uri.group(2) else None)
    head, _, _tail = candidate.replace("/", "\\").partition("\\")
    if head == VIRTUAL_PATH_ROOT:
        parts = [part for part in candidate.replace("/", "\\").split("\\") if part]
        if len(parts) >= 2 and parts[1]:
            return parts[1], None
        return None
    if _ID_SHAPE_RE.match(candidate):
        return candidate, None
    return None


def looks_like_resource_ref(text: str) -> bool:
    """这段文本是不是**资源引用**（token 或 id 形状），而不是文件名/自由文本。

    工具侧靠它区分两条通道：资源引用必须走 Binder（绑不上就报码），
    文件名/路径才走旧的文件查找（那条路不是资源引用，不该报资源诊断码）。
    """
    candidate = _strip_quotes(text)
    if not candidate:
        return False
    if candidate.startswith("[[") or candidate.endswith("]]"):
        return True
    return bool(_ID_SHAPE_RE.match(candidate))


def looks_like_file_path(text: str) -> bool:
    """这段文本是不是"文件名/路径"（用来把绑定失败分类成 TOKEN_UNRESOLVED）。

    资源引用可以是任意**已登记**的 id（V2 历史上就有 ``corner`` 这种自由 id），
    所以"是不是 id"只能靠查表；查不到时再问"它像不像路径/文件名"：
    像 → 是写法错误（TOKEN_UNRESOLVED），不像 → 就是没这个资源（NOT_FOUND）。
    """
    candidate = _strip_quotes(text)
    if not candidate:
        return False
    if "/" in candidate or "\\" in candidate:
        return True
    suffix = Path(candidate).suffix
    return bool(suffix) and bool(re.fullmatch(r"\.[0-9A-Za-z]{1,5}", suffix))


def _strip_quotes(text: str) -> str:
    candidate = str(text or "").strip()
    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in "'\"":
        candidate = candidate[1:-1].strip()
    return candidate


# ── Phase B4：分阶段失败（设计 §4.8）与参数材料化（设计 §4.3/§4.5）────

#: 工具边界的四个阶段：绑定 / 参数材料化 / 工具执行 / 结果校验。
STAGE_RESOURCE_BINDING = "resource_binding"
STAGE_ARGUMENT_MATERIALIZATION = "argument_materialization"
STAGE_TOOL_EXECUTION = "tool_execution"
STAGE_TOOL_RESULT_VALIDATION = "tool_result_validation"


def looks_like_resource_expression(value: Any) -> bool:
    """这段值**长得像**资源占位表达（哪怕解析不出来）。

    它决定执行边界的严格程度：像占位表达但没换成真实实参 → 必须在工具启动前拒绝
    （``RESOURCE_ARGUMENT_NOT_MATERIALIZED``），不能原样丢给旧工具去猜。

    只认**整段就是**占位表达的形式（``[[asset_x]]`` 而非"这句话里有 ``[[ ]]``"）：
    工具参数里可能有普通文案/标签，不做无边界的子串识别。
    """
    if isinstance(value, Mapping):
        return parse_resource_expression(value) is not None
    candidate = _strip_quotes(str(value or ""))
    if not candidate:
        return False
    if _TOKEN_RE.match(candidate):
        return True
    if candidate.startswith("resource://"):
        return True
    head = candidate.replace("/", "\\").split("\\", 1)[0]
    return head == VIRTUAL_PATH_ROOT


def materialization_target(param: str, semantics: Mapping[str, Any] | None) -> str:
    """这个资源参数的材料化目标实参名（schema 声明 ``materialize_as``；缺省就地替换）。"""
    blob = (semantics or {}).get(str(param))
    if isinstance(blob, Mapping):
        target = str(blob.get("materialize_as") or "").strip()
        if target:
            return target
    return str(param)


def declared_materialization_target(param: str, semantics: Mapping[str, Any] | None) -> bool:
    """schema 是否给这个参数**显式**声明了材料化目标（区别于"缺省就地"）。"""
    blob = (semantics or {}).get(str(param))
    return bool(isinstance(blob, Mapping) and str(blob.get("materialize_as") or "").strip())


def _same_path(left: Any, right: Any) -> bool:
    """两个值是不是"同一个路径"（容忍分隔符/大小写/相对写法差异）。"""
    a, b = str(left or ""), str(right or "")
    if not a or not b:
        return False
    if a == b:
        return True
    try:
        import os

        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))
    except Exception:  # noqa: BLE001 - 极端路径字符：退回字符串比较
        return False


def _value_matches_materialization(existing: Any, path: str, resource_id: str) -> bool:
    """目标参数里已有的值，和"即将写入的材料化结果"是否**说的是同一件事**。

    - 空值：可以写（没人表态）；
    - 资源表达（``[[id]]`` / 虚拟路径 / 裸 id）：解出来是**同一个资源**就算一致；
    - 其它（真实路径、文件名…）：路径相同才算一致，否则是冲突。
    """
    if existing in (None, "", (), [], {}):
        return True
    expression = parse_resource_expression(existing)
    if expression is not None:
        return str(expression[0]) == str(resource_id)
    return _same_path(existing, path)


#: 参数语义里能被"绑定过滤"消费的键（其余键只是给人/模型看的说明书）。
RESOURCE_PARAM_FILTER_KEYS: tuple[str, ...] = (
    "base_types",
    "role",
    "use",
    "semantic_tags",
)


def resource_param_filters(
    param: str, semantics: Mapping[str, Any] | None
) -> dict[str, Any]:
    """参数语义 → :class:`ResourceRequest` 的过滤字段（设计 §4.6）。

    - ``base_types``：参数接受的基础类型（缺省不限）；
    - ``role``：期望的**资源角色**（``template`` / ``scene`` …）；
    - ``use``：期望的**用途**（缺了就 ``RESOURCE_USE_MISMATCH``）；
    - ``semantic_tags``：期望的**业务语义**（资源声明了才强校验）。
    """
    blob = (semantics or {}).get(str(param))
    if not isinstance(blob, Mapping):
        return {}
    out: dict[str, Any] = {}
    base_types = blob.get("base_types")
    if isinstance(base_types, str):
        out["base_types"] = (base_types,)
    elif isinstance(base_types, (list, tuple)) and base_types:
        out["base_types"] = tuple(str(x) for x in base_types if str(x))
    role = str(blob.get("role") or "").strip()
    roles = blob.get("roles")
    if role:
        out["roles"] = (role,)
    elif isinstance(roles, str) and roles:
        out["roles"] = (roles,)
    elif isinstance(roles, (list, tuple)) and roles:
        out["roles"] = tuple(str(x) for x in roles if str(x))
    use = str(blob.get("use") or "").strip()
    if use:
        out["uses"] = (use,)
    tags = blob.get("semantic_tags")
    if isinstance(tags, str):
        out["semantic_tags"] = (tags,)
    elif isinstance(tags, (list, tuple)) and tags:
        out["semantic_tags"] = tuple(str(x) for x in tags if str(x))
    return out


def placeholder_resource_arguments(
    arguments: Mapping[str, Any] | None,
    skip_params: Iterable[str] = (),
) -> list[tuple[str, str]]:
    """参数里**长得像资源占位表达**的 ``(参数名, 值)``（跳过已被声明的参数）。

    "宽进"的实现（设计 §4.3）：模型可能把 ``[[asset_x]]`` / ``__resources__\\…`` 写进
    任何参数名，Runtime 不靠参数名猜，但也**不放过**明显是占位表达的值。
    """
    skip = {str(name) for name in skip_params}
    out: list[tuple[str, str]] = []
    for param, value in dict(arguments or {}).items():
        if str(param) in skip:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            if looks_like_resource_expression(item):
                out.append((str(param), str(item)))
    return out


def materialize_resource_arguments(
    arguments: Mapping[str, Any] | None,
    *,
    handles: Iterable[Any],
    semantics: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """把参数里的**资源占位表达**换成绑定 handle 派生出的真实实参。

    返回 ``(新参数, 材料化来源, 未材料化清单)``：

    - 材料化来源：``{parameter, target_parameter, resource_id, version, content_hash, path, name}``，
      供证据落版本与事后追溯；
    - 未材料化清单：``{parameter, value, reason, resource_id?}``，非空就**不许启动工具**。

    两条边界规则（设计 §4.3/§4.4/§4.5）：

    1. **占位表达**（``[[asset_x]]`` / ``resource://…`` / ``__resources__\\…``）一律材料化 ——
       旧工具本来就用不了它们；
    2. **裸 id** 只有在 schema 显式声明了材料化目标（``materialize_as``）时才改写，
       否则原样留着：那是旧工具"按 id 自己解析"的既有契约，Runtime 已经在它之前
       校验过版本/状态/权限，不该顺手把它换成一个它不认识的东西。

    只有参数**值**被改写（设计 §4.4：即时调用在参数 schema 边界处理），不做全文替换；
    非资源参数原样保留。

    目标参数冲突（人工裁决 / 设计 §4.5）：如果模型**同时**给了资源 A 的 ``asset_id``，
    又自己往材料化目标参数（如 ``template_path``）里写了另一个值（资源 B 的真实路径、
    虚拟路径或别的文件名），一律报 ``materialization_conflict`` —— 绝不静默用 A 覆盖 B。
    """
    given = dict(arguments or {})
    original = dict(arguments or {})
    by_id: dict[str, Any] = {}
    by_param_ref: dict[tuple[str, str], Any] = {}
    for handle in handles or ():
        rid = str(getattr(handle, "resource_id", "") or "").strip()
        if rid:
            by_id[rid] = handle
        parameter = str(getattr(handle, "parameter", "") or "")
        model_ref = str(getattr(handle, "model_ref", "") or "")
        if parameter and model_ref:
            by_param_ref[(parameter, model_ref)] = handle
    writes: dict[str, Any] = {}
    materialized: list[dict[str, Any]] = []
    unmaterialized: list[dict[str, Any]] = []
    for param, value in given.items():
        values = list(value) if isinstance(value, (list, tuple)) else [value]
        updated: list[Any] = []
        for item in values:
            expression = parse_resource_expression(item)
            placeholder = looks_like_resource_expression(item)
            named_handle = by_param_ref.get((str(param), str(item)))
            if expression is None:
                # 模型层允许精确名称；Binder 已把它唯一解析成 handle。这里不再要求
                # 名称长得像内部 id，而是使用绑定事实材料化。未绑定的普通字符串仍原样。
                if named_handle is not None:
                    resource_id = str(getattr(named_handle, "resource_id", "") or "")
                    expression = (resource_id, None)
                    placeholder = True
                else:
                    if placeholder:
                        unmaterialized.append(
                            {
                                "parameter": str(param),
                                "value": str(item),
                                "reason": "unparsable_expression",
                            }
                        )
                    updated.append(item)
                    continue
            target = materialization_target(str(param), semantics)
            if not placeholder and not declared_materialization_target(str(param), semantics):
                # 裸 id 且 schema 没声明真实实参名 → 交给旧工具按自己的契约解析
                updated.append(item)
                continue
            resource_id, requested_version = expression
            handle = named_handle or by_id.get(resource_id)
            if handle is None:
                unmaterialized.append(
                    {
                        "parameter": str(param),
                        "value": str(item),
                        "reason": "no_bound_handle",
                        "resource_id": resource_id,
                    }
                )
                updated.append(item)
                continue
            if named_handle is not None and not declared_materialization_target(
                str(param), semantics
            ):
                # 旧工具的参数契约就是 asset_id：名称只在模型边界存在，进入工具前
                # 应还原成内部 id，而不是擅自改成磁盘路径。
                entry = {
                    "parameter": str(param),
                    "target_parameter": str(param),
                    "resource_id": resource_id,
                    "version": int(getattr(handle, "version", 0) or 0),
                    "content_hash": str(getattr(handle, "content_hash", "") or ""),
                    "path": str(getattr(handle, "path", "") or ""),
                    "name": str(getattr(handle, "name", "") or ""),
                    "model_ref": str(getattr(handle, "model_ref", "") or ""),
                    "resolved_by": str(getattr(handle, "resolved_by", "id") or "id"),
                    "materialized_value": resource_id,
                }
                materialized.append(entry)
                writes[str(param)] = resource_id
                updated.append(resource_id)
                continue
            path = str(getattr(handle, "path", "") or "")
            if not path:
                unmaterialized.append(
                    {
                        "parameter": str(param),
                        "value": str(item),
                        "reason": "handle_has_no_path",
                        "resource_id": resource_id,
                    }
                )
                updated.append(item)
                continue
            entry = {
                "parameter": str(param),
                "target_parameter": target,
                "resource_id": resource_id,
                "version": int(getattr(handle, "version", 0) or 0),
                "content_hash": str(getattr(handle, "content_hash", "") or ""),
                "path": path,
                "name": str(getattr(handle, "name", "") or ""),
                "model_ref": str(getattr(handle, "model_ref", "") or ""),
                "resolved_by": str(getattr(handle, "resolved_by", "id") or "id"),
            }
            if requested_version is not None:
                entry["requested_version"] = int(requested_version)
            materialized.append(entry)
            # 就地材料化，或写进 schema 声明的真实实参名（并把占位参数清空）。
            # 目标参数已经有"别的说法"（另一个 handle 写过，或模型自己写了真实/虚拟路径）
            # → 冲突：不许悄悄挑一个（人工裁决：不能静默用 A 覆盖 B）。
            existing = writes.get(target, original.get(target))
            if not _value_matches_materialization(existing, path, resource_id):
                conflict_with = next(
                    (
                        str(other.get("resource_id") or "")
                        for other in materialized
                        if other.get("target_parameter") == target
                        and str(other.get("resource_id") or "") != resource_id
                    ),
                    "",
                )
                unmaterialized.append(
                    {
                        "parameter": str(param),
                        "value": str(item),
                        "reason": "materialization_conflict",
                        "target_parameter": target,
                        "resource_id": resource_id,
                        "conflicting_value": str(existing)[:200],
                        "conflict_with": conflict_with,
                    }
                )
                updated.append(str(item))
                continue
            writes[target] = path
            updated.append(path if target == str(param) else "")
        given[param] = updated if isinstance(value, (list, tuple)) else updated[0]
    # 目标参数的写入最后统一生效：Runtime 的绑定事实优先于模型自己写的同名参数。
    given.update(writes)
    return given, materialized, unmaterialized


def resource_id_param_schema(
    *,
    description: str = "资源 id（如 asset_xxx）；不要写文件名/路径",
    version_required: bool = False,
    materialize_as: str = "",
    role: str = "",
    use: str = "",
    semantic: str = "",
) -> dict[str, Any]:
    """**字符串 id 形态**的资源参数 schema（旧风格工具的权威声明方式）。

    V2 兼容工具的参数是"一个 id 字符串"，没有版本字段；Runtime 绑定的是**当前登记
    版本**并把版本/内容 hash 回填进事实。新工具建议直接用 :func:`resource_param_schema`
    的``{id, version}`` 结构化形态。

    Phase B4（设计 §4.5/§4.6）在这里补**业务语义**，让执行边界知道怎么把绑定事实变成
    工具能消费的实参：

    - ``materialize_as``：这个资源参数最终要写进哪个**真实实参名**（例：``asset_id``
      的材料化目标是 ``template_path``；为空表示就地替换）；
    - ``role`` / ``use``：期望的资源角色与用途（用途不匹配 → ``RESOURCE_USE_MISMATCH``）；
    - ``semantic``：给人和模型的业务语义（例："待寻找目标模板"）。
    """
    blob: dict[str, Any] = {
        "kind": "resource-ref",
        "id_shaped": True,
        "version_required": bool(version_required),
    }
    if materialize_as:
        blob["materialize_as"] = str(materialize_as)
    if role:
        blob["role"] = str(role)
    if use:
        blob["use"] = str(use)
    if semantic:
        blob["semantic"] = str(semantic)
    return {
        "type": "string",
        "description": str(description),
        RESOURCE_SCHEMA_KEY: blob,
    }


def resource_param_schema(
    *,
    description: str = "资源引用（结构化）：id 必填、version 必填；不要写绝对路径",
    base_types: Sequence[str] = (),
    roles: Sequence[str] = (),
    version_required: bool = True,
) -> dict[str, Any]:
    """工具 input_schema 里"资源参数"的标准形状（设计 §4「资源参数 schema」）。

    资源**不是字符串**：至少要有 ``id`` 与 ``version``；``type``（基础类型）与
    ``role``（真实角色）分开声明，免得把"产出图只是被当模板用"写成"它就是模板"。
    """
    type_schema: dict[str, Any] = {"type": "string"}
    if base_types:
        type_schema["enum"] = [str(x) for x in base_types]
    role_schema: dict[str, Any] = {"type": "string"}
    if roles:
        role_schema["enum"] = [str(x) for x in roles]
    required = ["id"] + (["version"] if version_required else [])
    return {
        "type": "object",
        "description": description,
        # 权威声明：这个参数是资源引用（Runtime 会在工具执行前绑定）。
        RESOURCE_SCHEMA_KEY: {
            "kind": "resource-ref",
            "id_shaped": True,
            "version_required": bool(version_required),
        },
        "properties": {
            "id": {"type": "string", "description": "资源 id（如 asset_xxx / res_xxx）"},
            "version": {"type": "integer", "minimum": 1, "description": "资源版本，必填"},
            "type": type_schema,
            "role": role_schema,
        },
        "required": required,
        "additionalProperties": False,
    }


# ── 数据结构 ──


@dataclass(frozen=True)
class ResourceRequest:
    """一次绑定请求（两条入口都用它，保证判定完全一致）。"""

    ref: str
    version: Optional[int] = None
    #: 基础类型过滤（``image`` / ``overlay`` …）——**不是**角色。
    base_types: tuple[str, ...] = ()
    #: 真实角色过滤（``scene`` / ``template`` / ``probe_out`` …）。
    roles: tuple[str, ...] = ()
    #: 允许用途过滤（``explicit_match`` / ``auto_scene`` …）。给了就要求全部满足。
    uses: tuple[str, ...] = ()
    #: 业务语义标签（设计 §4.6）：参数期望的资源语义（例 ``空槽参考模板``）。
    #: **opt-in 强校验**：资源自己声明了语义集合才比对；没声明就只记遥测、不拦。
    semantic_tags: tuple[str, ...] = ()
    scope: str = "task"
    allow_name: bool = False
    origin: str = ""
    permission: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "ref", str(self.ref or "").strip())
        object.__setattr__(
            self, "base_types", tuple(str(x) for x in (self.base_types or ()) if str(x))
        )
        object.__setattr__(self, "roles", tuple(str(x) for x in (self.roles or ()) if str(x)))
        object.__setattr__(self, "uses", tuple(str(x) for x in (self.uses or ()) if str(x)))
        object.__setattr__(
            self, "semantic_tags", tuple(str(x) for x in (self.semantic_tags or ()) if str(x))
        )
        object.__setattr__(self, "scope", str(self.scope or "task"))
        object.__setattr__(self, "origin", str(self.origin or ""))
        object.__setattr__(self, "permission", str(self.permission or ""))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ref": self.ref}
        if self.version is not None:
            out["version"] = int(self.version)
        for key in (
            "base_types",
            "roles",
            "uses",
            "semantic_tags",
            "scope",
            "allow_name",
            "origin",
            "permission",
        ):
            value = getattr(self, key)
            if value not in ("", (), None, False):
                out[key] = list(value) if isinstance(value, tuple) else value
        return out


@dataclass(frozen=True)
class BoundResource:
    """绑定成功后的句柄：**两条入口必须给出同一组事实**（完成标准）。

    三个**不能合并**的维度（人工要求）：

    - ``type``：基础类型（``image`` / ``overlay``）；
    - ``role``：真实角色（``scene`` / ``template`` / ``crop`` / ``overlay`` / ``probe_out``…）；
    - ``allowed_uses``：允许用途（``auto_scene`` / ``auto_template`` / ``explicit_match`` /
      ``explicit_view`` / ``display_overlay``）。

    ``probe_out`` 的 role 原样保留，**不会**被伪装成 ``template``。
    """

    resource_id: str
    version: int
    content_hash: str
    base_type: str
    role: str
    status: str
    allowed_uses: tuple[str, ...] = ()
    path: str = ""
    name: str = ""
    scope: str = ""
    record_type: str = ""
    source: str = ""
    origin: str = ""
    # ── Phase B4（设计 §4.5）：handle 只属于 Runtime 的补充事实 ──
    #: 这个 handle 是替哪个**参数**绑的（例 ``asset_id``）。
    parameter: str = ""
    #: 绑给哪个能力/工具（执行边界据此材料化）。
    tool_name: str = ""
    #: 绑定时所在的环境 revision（任务/视口 revision）：环境一变，旧 handle 自动失效。
    environment_revision: str = ""
    #: 资源自己声明的**业务语义**标签（空 = 未声明；不代表"不匹配"）。
    semantics: tuple[str, ...] = ()
    #: 模型原始提交的资源表达（名称/id/token/虚拟路径）；用于审计，不参与身份。
    model_ref: str = ""
    #: 本次身份是由精确名称还是 id/token/虚拟路径解析得到。
    resolved_by: str = "id"

    @property
    def kind(self) -> str:
        """兼容别名：老代码里的 "kind" == 基础类型。"""
        return self.base_type

    def can(self, use: str) -> bool:
        return str(use) in self.allowed_uses

    def facts(self) -> dict[str, Any]:
        """用于跨入口比对的规范化事实（id/版本/内容 hash/类型/角色/状态/用途）。"""
        return {
            "resource_id": self.resource_id,
            "version": int(self.version),
            "content_hash": self.content_hash,
            "type": self.base_type,
            "role": self.role,
            "status": self.status,
            "allowed_uses": tuple(self.allowed_uses),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.facts(),
            "allowed_uses": list(self.allowed_uses),
            "path": self.path,
            "name": self.name,
            "scope": self.scope,
            "record_type": self.record_type,
            "source": self.source,
            "origin": self.origin,
            "parameter": self.parameter,
            "tool_name": self.tool_name,
            "environment_revision": self.environment_revision,
            "semantics": list(self.semantics),
            "model_ref": self.model_ref,
            "resolved_by": self.resolved_by,
        }

    def virtual_path(self) -> str:
        """给模型看的稳定虚拟路径（只是 id + 可读名，见 :func:`render_resource_virtual_path`）。"""
        return render_resource_virtual_path(
            resource_id=self.resource_id, name=self.name, path=self.path
        )


@dataclass(frozen=True)
class BindingFailure:
    """绑定失败：**结构化**，带稳定诊断码与可操作细节。"""

    code: str
    message: str
    ref: str = ""
    resource_id: str = ""
    requested_version: Optional[int] = None
    latest_version: Optional[int] = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message, "ref": self.ref}
        if self.resource_id:
            out["resource_id"] = self.resource_id
        if self.requested_version is not None:
            out["requested_version"] = int(self.requested_version)
        if self.latest_version is not None:
            out["latest_version"] = int(self.latest_version)
        if self.details:
            out["details"] = dict(self.details)
        return out

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class ResourceBindingError(Exception):
    """绑定失败时抛出（携带 :class:`BindingFailure`，供调用方原样上报）。"""

    def __init__(self, failure: BindingFailure) -> None:
        super().__init__(str(failure))
        self.failure = failure

    @property
    def code(self) -> str:
        return self.failure.code


# ── 资源来源（两套存储各一个适配器，规则同一份）────────────────────────


class ResourceSource(Protocol):
    """Binder 需要的**全部**存储能力；实现方只做"取行 + 归一化"。"""

    def rows(self, resource_id: str) -> list[dict[str, Any]]: ...

    def by_name(self, name: str) -> list[dict[str, Any]]: ...

    def scope_allows(self, row: Mapping[str, Any], scope: str) -> bool: ...

    def permission_allows(self, row: Mapping[str, Any], permission: str) -> bool: ...


def normalized_role(row: Mapping[str, Any]) -> str:
    """资源的**真实角色**（原样保留，不折算成使用方式）。

    有 ``role`` 就用它（``probe_out`` 就是 ``probe_out``）；没有 role 时只按分组/
    记录类型补一个等价标签（``标注`` → ``annotation``、``探针`` → ``probe_out``、
    覆盖层记录 → ``overlay``），**绝不**把产出物说成 ``template``。
    """
    role = str(row.get("role") or "").strip()
    if role:
        return role
    group = str(row.get("group") or "").strip()
    if group == _ANNOTATION_GROUP:
        return "annotation"
    if group in _PROBE_GROUPS:
        return "probe_out"
    record_type = str(row.get("record_type") or row.get("type") or "").strip()
    if record_type in {"overlay", "annotation_set"}:
        return "overlay"
    return ""


def normalized_base_type(row: Mapping[str, Any]) -> str:
    """资源的**基础类型**：它是"图像资源"还是"覆盖层数据"（与用途无关）。"""
    role = normalized_role(row)
    record_type = str(row.get("record_type") or row.get("type") or "").strip()
    if role in _OVERLAY_ROLES:
        return "overlay"
    if role:
        # scene / template / crop / probe_out … 都是图像资源
        return "image"
    if record_type in {"overlay", "annotation_set"}:
        return "overlay"
    if record_type in {"image", "asset", ""}:
        return "image"
    return record_type


def is_auto_scene(row: Mapping[str, Any]) -> bool:
    """能不能**自动**被当作界面内容（与 ``canvas.is_scene_asset`` 同一判据）。

    产出物（``probe_out``）与标注图不在其列：它们只有被**显式点名**才会被使用。
    """
    role = str(row.get("role") or "").strip()
    group = str(row.get("group") or "").strip()
    if role in _OVERLAY_ROLES or role == "probe_out":
        return False
    if group == _ANNOTATION_GROUP or group in _PROBE_GROUPS:
        return False
    return normalized_base_type(row) == "image"


def allowed_uses_for(row: Mapping[str, Any]) -> tuple[str, ...]:
    """这份资源**允许被怎么用**（"用途"与"角色"分开表达）。

    - 覆盖层类（``annotation`` / ``overlay``）：只能叠加显示 / 被显式查看；
    - 图像类：至少可以被**显式点名**匹配与查看（``explicit_match`` / ``explicit_view``）；
      角色是真正的模板/场景时，才额外获得 ``auto_template`` / ``auto_scene``；
    - ``probe_out`` / ``crop`` 这类产出物：只有显式用途 —— 不属于 scene、也不会被
      自动选为界面内容或模板，但用户/模型按资源 id 指定时仍可用于模板匹配。
    """
    role = normalized_role(row)
    if normalized_base_type(row) == "overlay" or role in _OVERLAY_ROLES:
        uses = {USE_DISPLAY_OVERLAY, USE_EXPLICIT_VIEW}
        return tuple(use for use in _USE_ORDER if use in uses)
    uses = {USE_EXPLICIT_MATCH, USE_EXPLICIT_VIEW}
    if is_auto_scene(row):
        uses.add(USE_AUTO_SCENE)
        if role in {"", "template"}:
            uses.add(USE_AUTO_TEMPLATE)
    return tuple(use for use in _USE_ORDER if use in uses)


def normalized_kind(row: Mapping[str, Any]) -> str:
    """兼容别名：老代码里的 "kind" 现在指**基础类型**（不再折叠角色）。

    保留这个函数只为少改调用方；新代码请用 :func:`normalized_base_type` +
    :func:`normalized_role` + :func:`allowed_uses_for`。
    """
    return normalized_base_type(row)


class SessionResourceSource:
    """V2：``session.artifacts["resource_registry_v1"]`` 里的资源注册中心。"""

    label = "session_registry"

    def __init__(self, session: Any) -> None:
        self.session = session

    @staticmethod
    def _registry():
        from .collaborator import resource_registry

        return resource_registry

    def _normalize(self, row: Mapping[str, Any]) -> dict[str, Any]:
        path = str(row.get("path") or "")
        return {
            "resource_id": str(row.get("resource_id") or row.get("id") or ""),
            "version": int(row.get("version") or 1),
            "content_hash": str(row.get("content_hash") or ""),
            "status": str(row.get("status") or ""),
            "path": path,
            "name": str(row.get("name") or (Path(path).name if path else "")),
            "role": str(row.get("role") or ""),
            "group": str(row.get("group") or ""),
            "record_type": str(row.get("type") or ""),
            "kind": str(row.get("kind") or ""),
            "scope": str(row.get("scope") or "task"),
            "replaced_by": str(row.get("replaced_by") or ""),
            "invalid_reason": str(row.get("invalid_reason") or ""),
        }

    def rows(self, resource_id: str) -> list[dict[str, Any]]:
        registry = self._registry()
        registry.ensure_asset_resources(self.session)
        raw = registry.get_resource(self.session, resource_id)
        row = self._normalize(raw) if isinstance(raw, dict) else None
        return [row] if row else []

    def by_name(self, name: str) -> list[dict[str, Any]]:
        registry = self._registry()
        registry.ensure_asset_resources(self.session)
        wanted = str(name or "").strip().lower()
        out: list[dict[str, Any]] = []
        for raw in registry.all_resources(self.session):
            if str(raw.get("type") or "") != "asset":
                continue
            candidates = {
                str(raw.get("name") or "").lower(),
                Path(str(raw.get("path") or "")).name.lower(),
            }
            if wanted and wanted in candidates:
                out.append(self._normalize(raw))
        return out

    def scope_allows(self, row: Mapping[str, Any], scope: str) -> bool:
        if not scope or scope in {"any", "task"}:
            return True
        return str(row.get("scope") or "task") == scope

    def permission_allows(self, row: Mapping[str, Any], permission: str) -> bool:
        return True


class StoreResourceSource:
    """V3：``resources`` 表 + 当前会话素材区 / Task 作用域。"""

    label = "v3_store"

    def __init__(
        self,
        store: Any,
        *,
        task_id: str = "",
        current_turn_id: str = "",
        visible_resource_ids: tuple[str, ...] = (),
    ) -> None:
        self.store = store
        self.task_id = str(task_id or "")
        self.current_turn_id = str(current_turn_id or "")
        self.visible_resource_ids = tuple(
            dict.fromkeys(str(x or "").strip() for x in visible_resource_ids if str(x or "").strip())
        )

    def _normalize(self, record: Any) -> dict[str, Any]:
        metadata = dict(getattr(record, "metadata", None) or {})
        status = getattr(record, "status", "")
        return {
            "resource_id": str(getattr(record, "id", "") or ""),
            "version": int(getattr(record, "version", 0) or 0),
            "content_hash": str(getattr(record, "content_hash", "") or ""),
            "status": str(getattr(status, "value", status) or ""),
            "path": str(getattr(record, "content_path", "") or ""),
            "name": str(getattr(record, "display_name", "") or ""),
            "role": str(metadata.get("role") or ""),
            "group": str(metadata.get("group") or ""),
            # 业务语义（设计 §4.6）：资源自己声明的语义标签，用于参数用途校验。
            "semantics": tuple(
                str(x) for x in (metadata.get("semantics") or metadata.get("semantic_tags") or ())
            ),
            "record_type": str(getattr(record, "type", "") or ""),
            "kind": "",
            "scope": str(getattr(record, "reusable_scope", "") or "none"),
            "task_id": str(getattr(record, "task_id", "") or ""),
            "replaced_by": str(metadata.get("replaced_by") or ""),
            "invalid_reason": str(metadata.get("invalid_reason") or ""),
        }

    def rows(self, resource_id: str) -> list[dict[str, Any]]:
        return [self._normalize(record) for record in self.store.list_resource_versions(resource_id)]

    def by_name(self, name: str) -> list[dict[str, Any]]:
        wanted = str(name or "").strip().lower()
        if not wanted:
            return []
        rows: list[dict[str, Any]] = []
        for record in self.store.find_resources_by_display_name(
            str(name or "").strip(),
            task_id=self.task_id or None,
            current_turn_id=self.current_turn_id,
            visible_resource_ids=self.visible_resource_ids,
            include_global=True,
            limit=200,
        ):
            row = self._normalize(record)
            if wanted != row["name"].lower():
                continue
            rows.append(row)
        return rows

    def scope_allows(self, row: Mapping[str, Any], scope: str) -> bool:
        if not scope or scope in {"any", "global"}:
            return True
        reusable = str(row.get("scope") or "none")
        if reusable == "global":
            return True
        row_task = str(row.get("task_id") or "")
        if self.task_id:
            return row_task == self.task_id or reusable == "task"
        # 还没有 Task：只允许"本主轮素材区引用"这类按 id 直取的资源（V3 侧 task_id 为空时
        # 素材区资源同样是 task_id 空）→ 视为同作用域，但仍要求 status 可用（上游已判）。
        return row_task in {"", None}

    def permission_allows(self, row: Mapping[str, Any], permission: str) -> bool:
        return True


# ── Binder ──


class ResourceBinder:
    """唯一绑定实现：解析 → 查记录 → 逐条硬校验 → 产出 ResourceHandle。"""

    def __init__(
        self,
        source: ResourceSource,
        *,
        origin: str = "",
        permission_checker: Optional[Callable[[Mapping[str, Any], str], bool]] = None,
        path_exists: Optional[Callable[[str], bool]] = None,
        content_hasher: Optional[Callable[[str], str]] = None,
        scope: str = "task",
    ) -> None:
        self.source = source
        self.origin = str(origin or "")
        self.permission_checker = permission_checker
        self.scope = str(scope or "task")
        self._path_exists = path_exists
        self._content_hasher = content_hasher

    # ── 引用解析 ──

    @staticmethod
    def parse_ref(raw: str, *, allow_name: bool = False) -> tuple[str, bool]:
        """做**结构**解析（去引号 / 剥 token），返回 ``(候选引用, 是否按名字)``。

        注意：这里**不判断"它像不像 id"** —— V2 历史上允许自由 id（如 ``corner``），
        "是不是 id"只能靠查表决定（见 :meth:`try_bind`）。这里只拦截结构性错误：
        空引用、空 token、括号不成对。
        """
        text = _strip_quotes(raw)
        if not text:
            raise ResourceBindingError(
                BindingFailure(
                    code=RESOURCE_TOKEN_UNRESOLVED,
                    message="资源引用为空：必须给资源 id（禁止静默使用焦点素材或旧素材）",
                    ref="",
                )
            )
        token = _TOKEN_RE.match(text)
        if token:
            text = token.group(1).strip()
        if not text:
            raise ResourceBindingError(
                BindingFailure(
                    code=RESOURCE_TOKEN_UNRESOLVED,
                    message="素材 token 是空的：[[ ]] 里必须写资源 id",
                    ref=str(raw or ""),
                )
            )
        # Phase B4（设计 §4.4）：本地渲染的**虚拟路径**也是合法输入
        # （``__resources__\<id>\<可读名>``）。身份只取 id，名字/后缀不参与。
        if looks_like_resource_expression(text):
            expression = parse_resource_expression(text)
            if expression is None:
                raise ResourceBindingError(
                    BindingFailure(
                        code=RESOURCE_TOKEN_UNRESOLVED,
                        message=(
                            f"资源占位表达 {text!r} 解析不出资源 id；"
                            "虚拟路径形如 __resources__\\asset_xxx\\名字.png"
                        ),
                        ref=str(raw or ""),
                        details={"expected_shape": "__resources__\\asset_xxx\\名字.png"},
                    )
                )
            return str(expression[0]), False
        if text.startswith("[[") or text.endswith("]]"):
            raise ResourceBindingError(
                BindingFailure(
                    code=RESOURCE_TOKEN_UNRESOLVED,
                    message=f"素材 token 写法不合法：{text!r}；应写成 [[资源id]]",
                    ref=text,
                )
            )
        return text, False

    # ── 绑定 ──

    def bind(
        self,
        ref: str | ResourceRequest,
        *,
        version: Optional[int] = None,
        base_types: Sequence[str] = (),
        roles: Sequence[str] = (),
        uses: Sequence[str] = (),
        scope: str = "",
        allow_name: bool = False,
        permission: str = "",
        origin: str = "",
    ) -> BoundResource:
        request = (
            ref
            if isinstance(ref, ResourceRequest)
            else ResourceRequest(
                ref=ref,
                version=version,
                base_types=tuple(base_types),
                roles=tuple(roles),
                uses=tuple(uses),
                scope=scope or self.scope,
                allow_name=allow_name,
                origin=origin or self.origin,
                permission=permission,
            )
        )
        bound, failure = self.try_bind(request)
        if failure is not None:
            raise ResourceBindingError(failure)
        assert bound is not None  # try_bind 只会二者之一
        return bound

    def try_bind(
        self, request: ResourceRequest | str
    ) -> tuple[Optional[BoundResource], Optional[BindingFailure]]:
        if isinstance(request, str):
            request = ResourceRequest(ref=request, scope=self.scope, origin=self.origin)
        try:
            resource_id, _by_name = self.parse_ref(request.ref, allow_name=request.allow_name)
        except ResourceBindingError as exc:
            return None, exc.failure

        rows = self.source.rows(resource_id)
        resolved_by = "id"
        if not rows and request.allow_name:
            # 名字只能**在 id 查不到时**作为兼容通道（id 命中优先，判定仍然确定）。
            rows = self.source.by_name(resource_id)
            resolved_by = "name"
            distinct_ids = sorted(
                {str(row.get("resource_id") or "") for row in rows if row.get("resource_id")}
            )
            if len(distinct_ids) > 1:
                return None, BindingFailure(
                    code=RESOURCE_NAME_AMBIGUOUS,
                    message=(
                        f"资源名称 {resource_id!r} 对应多个当前可用资源，不能自动选择；"
                        "请重命名或明确选择其中一张"
                    ),
                    ref=request.ref,
                    details={
                        "resolved_by": "name",
                        "candidate_resource_ids": distinct_ids,
                    },
                )
        if not rows:
            if looks_like_file_path(resource_id):
                return None, BindingFailure(
                    code=RESOURCE_TOKEN_UNRESOLVED,
                    message=(
                        f"{resource_id!r} 看起来是文件名/路径，不是资源 id；"
                        "请先用 resource.search / search_resources 查到资源 id 再引用"
                    ),
                    ref=request.ref,
                    details={"expected_shape": "asset_xxx / res_xxx / [[asset_xxx]]"},
                )
            return None, BindingFailure(
                code=RESOURCE_NOT_FOUND,
                message=f"资源不存在：{resource_id}",
                ref=request.ref,
                resource_id=resource_id,
                details={"resolved_by": resolved_by},
            )

        latest = max(int(row.get("version") or 0) for row in rows)
        row: Optional[dict[str, Any]] = None
        if request.version is None:
            # token 里没有版本概念 → 取当前登记的最新版本；事实里照样写清版本号
            row = next((r for r in rows if int(r.get("version") or 0) == latest), None)
        else:
            wanted = int(request.version)
            row = next((r for r in rows if int(r.get("version") or 0) == wanted), None)
            if row is None:
                # 版本号根本没登记：比最新大 = 不存在；比最新小又查不到 = 历史被清理
                return None, BindingFailure(
                    code=RESOURCE_NOT_FOUND,
                    message=f"资源 {resource_id} 没有版本 {wanted}（当前最新 {latest}）",
                    ref=request.ref,
                    resource_id=resource_id,
                    requested_version=wanted,
                    latest_version=latest,
                )
            if wanted != latest:
                # **历史版本一律拒绝**：旧版本的坐标/证据已经跟着新版本漂移了，
                # 让它绑定成功等于静默用过期事实（设计 §4.1「检查版本」）。
                return None, BindingFailure(
                    code=RESOURCE_VERSION_EXPIRED,
                    message=(
                        f"资源 {resource_id} 的版本 {wanted} 已被 {latest} 取代；"
                        "请重新查询并绑定当前版本（旧版本的坐标/证据已失效）"
                    ),
                    ref=request.ref,
                    resource_id=resource_id,
                    requested_version=wanted,
                    latest_version=latest,
                    details={"latest_version": latest},
                )
        if row is None:  # pragma: no cover - 上面两个分支已覆盖
            return None, BindingFailure(
                code=RESOURCE_NOT_FOUND, message=f"资源不存在：{resource_id}", ref=request.ref
            )
        row = dict(row)
        resolved_id = str(row.get("resource_id") or resource_id)
        version = int(row.get("version") or 0)

        # 1) 语义状态：被替代 / 已归档删除 —— 这两类不用看文件也知道不能用。
        status = str(row.get("status") or "")
        replacement = str(row.get("replaced_by") or "")
        reason = str(row.get("invalid_reason") or "")
        if status == "superseded" or replacement:
            suffix = f"；请改用 {replacement}" if replacement else ""
            return None, BindingFailure(
                code=RESOURCE_VERSION_EXPIRED,
                message=f"资源 {resolved_id} 状态为 {status or 'replaced'}，不可使用{suffix}",
                ref=request.ref,
                resource_id=resolved_id,
                requested_version=request.version,
                latest_version=latest,
                details={"status": status, "replaced_by": replacement},
            )
        if status in {"archived", "deleted"}:
            detail = f"（{reason}）" if reason else ""
            return None, BindingFailure(
                code=RESOURCE_NOT_USABLE,
                message=f"资源 {resolved_id} 状态为 {status}{detail}，不可使用",
                ref=request.ref,
                resource_id=resolved_id,
                details={"status": status},
            )

        # 2) 物理事实：文件在不在、内容还是不是登记的那一份。
        #    **先于**泛化的"状态不可用"判定，是因为 registry 会把"文件不存在/内容已变化"
        #    也标成 invalid；直接报 NOT_USABLE 会把可操作的原因（换文件/重新登记版本）
        #    吞掉，而且两套存储给不出同一个码。
        path = str(row.get("path") or "")
        if not path:
            return None, BindingFailure(
                code=RESOURCE_FILE_MISSING,
                message=f"资源 {resolved_id} 没有可读文件路径",
                ref=request.ref,
                resource_id=resolved_id,
            )
        exists = self._path_exists or (lambda p: Path(p).is_file())
        if not exists(path):
            return None, BindingFailure(
                code=RESOURCE_FILE_MISSING,
                message=f"资源 {resolved_id} 的文件不存在：{path}",
                ref=request.ref,
                resource_id=resolved_id,
                details={"path": path},
            )
        recorded = str(row.get("content_hash") or "")
        if recorded:
            hasher = self._content_hasher
            if hasher is None:
                from .collaborator.resource_registry import file_hash as hasher  # type: ignore[assignment]

            actual = str(hasher(path) or "")
            if actual and actual != recorded:
                return None, BindingFailure(
                    code=RESOURCE_CONTENT_CHANGED,
                    message=(
                        f"资源 {resolved_id} 的文件内容已变化（登记 {recorded[:16]}… "
                        f"实际 {actual[:16]}…）；旧坐标/证据已失效，请重新登记版本"
                    ),
                    ref=request.ref,
                    resource_id=resolved_id,
                    details={"recorded_hash": recorded, "actual_hash": actual},
                )

        # 3) 剩下的状态不可用（invalid 的其它原因）
        if status not in USABLE_STATUSES:
            detail = f"（{reason}）" if reason else ""
            return None, BindingFailure(
                code=RESOURCE_NOT_USABLE,
                message=f"资源 {resolved_id} 状态为 {status}{detail}，不可使用",
                ref=request.ref,
                resource_id=resolved_id,
                requested_version=request.version,
                latest_version=latest,
                details={"status": status},
            )

        # 4) 作用域
        if not self.source.scope_allows(row, request.scope or self.scope):
            return None, BindingFailure(
                code=RESOURCE_SCOPE_MISMATCH,
                message=(
                    f"资源 {resolved_id} 的作用域是 {row.get('scope') or 'unknown'}，"
                    f"不在本轮允许的范围（{request.scope or self.scope}）内"
                ),
                ref=request.ref,
                resource_id=resolved_id,
                details={"scope": str(row.get("scope") or "")},
            )

        # 5) 权限
        checker = self.permission_checker or getattr(self.source, "permission_allows", None)
        if request.permission and checker is not None:
            if not checker(row, request.permission):
                return None, BindingFailure(
                    code=RESOURCE_PERMISSION_MISMATCH,
                    message=(
                        f"当前授权不允许对资源 {resolved_id} 执行 {request.permission}；"
                        "先申请权限或换资源"
                    ),
                    ref=request.ref,
                    resource_id=resolved_id,
                    details={"permission": request.permission},
                )

        # 6) 基础类型 / 真实角色 / 允许用途（三者分别判定，不互相冒充）
        base_type = normalized_base_type(row)
        role = normalized_role(row)
        allowed_uses = allowed_uses_for(row)
        if request.base_types and base_type not in request.base_types:
            return None, BindingFailure(
                code=RESOURCE_TYPE_MISMATCH,
                message=(
                    f"资源 {resolved_id} 的基础类型是 {base_type}（角色 {role or '未标注'}），"
                    f"这里只接受 {sorted(request.base_types)}"
                ),
                ref=request.ref,
                resource_id=resolved_id,
                details={
                    "type": base_type,
                    "role": role,
                    "allowed_types": list(request.base_types),
                },
            )
        if request.roles and role not in request.roles:
            return None, BindingFailure(
                code=RESOURCE_TYPE_MISMATCH,
                message=(
                    f"资源 {resolved_id} 的角色是 {role or '未标注'}，这里只接受 "
                    f"{sorted(request.roles)}。请先用 list_assets / search_resources 确认哪张才是你要的"
                ),
                ref=request.ref,
                resource_id=resolved_id,
                details={
                    "role": role,
                    "type": base_type,
                    "allowed_roles": list(request.roles),
                },
            )
        missing_uses = [use for use in request.uses if use not in allowed_uses]
        if missing_uses:
            return None, BindingFailure(
                code=RESOURCE_USE_MISMATCH,
                message=(
                    f"资源 {resolved_id}（角色 {role or '未标注'}）不允许这样用："
                    f"缺少 {sorted(missing_uses)}；它允许的用途是 {list(allowed_uses)}"
                ),
                ref=request.ref,
                resource_id=resolved_id,
                details={
                    "role": role,
                    "type": base_type,
                    "allowed_uses": list(allowed_uses),
                    "missing_uses": sorted(missing_uses),
                    "required_use": list(request.uses),
                },
            )
        # 业务语义（设计 §4.6）：资源**声明了**语义集合才比对；没声明只记遥测（见
        # RESOURCE_SEMANTIC_UNVERIFIED），因为"没声明"不等于"不匹配"。
        declared_semantics = {str(x) for x in (row.get("semantics") or ()) if str(x)}
        if request.semantic_tags and declared_semantics:
            missing_semantics = [
                tag for tag in request.semantic_tags if tag not in declared_semantics
            ]
            if missing_semantics:
                return None, BindingFailure(
                    code=RESOURCE_USE_MISMATCH,
                    message=(
                        f"资源 {resolved_id} 的业务语义是 {sorted(declared_semantics)}，"
                        f"这个参数要的是 {sorted(request.semantic_tags)}：名字/类型相同也不能顶替"
                    ),
                    ref=request.ref,
                    resource_id=resolved_id,
                    details={
                        "declared_semantics": sorted(declared_semantics),
                        "required_semantics": sorted(request.semantic_tags),
                    },
                )

        return (
            BoundResource(
                resource_id=resolved_id,
                version=version,
                content_hash=recorded,
                base_type=base_type,
                role=role,
                status=status,
                allowed_uses=allowed_uses,
                path=path,
                name=str(row.get("name") or ""),
                scope=str(row.get("scope") or ""),
                record_type=str(row.get("record_type") or ""),
                source=str(getattr(self.source, "label", "") or ""),
                origin=request.origin or self.origin,
                semantics=tuple(sorted(declared_semantics)),
                model_ref=str(request.ref or ""),
                resolved_by=resolved_by,
            ),
            None,
        )

    def bind_many(
        self, requests: Iterable[ResourceRequest | str]
    ) -> tuple[list[BoundResource], list[BindingFailure]]:
        """批量绑定：**不做部分成功静默降级** —— 失败项原样返回，由调用方决定。"""
        bound: list[BoundResource] = []
        failures: list[BindingFailure] = []
        for item in requests:
            resource, failure = self.try_bind(item)
            if failure is not None:
                failures.append(failure)
            elif resource is not None:
                bound.append(resource)
        return bound, failures


# ── 材料化侧入口：脚本里的素材引用 ──


def resolve_script_tokens(
    code: str,
    binder: ResourceBinder,
    *,
    base_types: Sequence[str] = (),
    roles: Sequence[str] = (),
    uses: Sequence[str] = (),
) -> tuple[str, list[BoundResource], list[BindingFailure]]:
    """把脚本里的素材引用换成绑定后的真实路径（**唯一**替换实现）。

    返回 ``(新代码, 已绑定资源, 失败清单)``。失败的引用**保留 token 原文**，
    由上层（artifact gate）按稳定诊断码阻断交付 —— 不静默删掉、也不猜替代品。
    """
    text = str(code or "")
    if not text or "[[" not in text:
        return text, [], []
    from .collaborator.collab_form import ASSET_REF_RE, py_literal  # 延迟导入：避免循环

    bound: list[BoundResource] = []
    failures: list[BindingFailure] = []
    seen: dict[str, BoundResource] = {}

    def _sub(match: "re.Match[str]") -> str:
        ref = (match.group(2) or "").strip()
        request = ResourceRequest(
            ref=ref,
            base_types=tuple(base_types),
            roles=tuple(roles),
            uses=tuple(uses),
            origin="script_token",
            # 历史兼容：脚本里写 [[素材名]] 曾经也能解析（``find_asset(name=...)``）。
            # 现在仍然允许，但解析规则与诊断码都来自**同一个** Binder（id 命中优先）。
            allow_name=True,
        )
        if ref in seen:
            resource = seen[ref]
            return py_literal(resource.path)
        resource, failure = binder.try_bind(request)
        if failure is not None:
            failures.append(failure)
            return match.group(0)
        assert resource is not None
        seen[ref] = resource
        bound.append(resource)
        return py_literal(resource.path)

    return ASSET_REF_RE.sub(_sub, text), bound, failures
