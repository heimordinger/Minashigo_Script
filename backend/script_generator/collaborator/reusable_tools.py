"""模型自造工具：把跑通的临时探针脚本沉淀成可复用的协作工具。

协作时模型常遇到"没有对应工具"的情况。与其停下来反问用户，它可以先写一段
临时探针脚本解决问题（run_probe_script），跑通后用 define_tool 把它登记成一个
带 JSON Schema 的正式工具；之后每轮工具表都会带上它，不用再重写一遍脚本。

持久化在 user_data/collab_tools.json（全局，跨会话可用）。这些工具执行时仍然
需要 probe_script 权限——用户随时可以在「查看内部能力/权限」里收回。
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional

from core.path import PROJECT_ROOT

from backend.script_generator.collaborator.bundled_tools import (
    bundled_tool_names,
    load_bundled_tools,
)

STORE_PATH = PROJECT_ROOT / "user_data" / "collab_tools.json"
MAX_TOOLS = 24
MAX_CODE_CHARS = 20_000
MAX_DESC_CHARS = 300

# 与内置工具重名会让分发歧义，直接禁止
RESERVED_TOOL_NAMES = {
    "list_skills",
    "get_skill_doc",
    "select_skill",
    "get_session_status",
    "analyze_current_frame",
    "describe_current_frame",
    "inspect_asset",
    "compare_assets",
    "fit_repeated_regions",
    "commit_repeated_regions",
    "crop_asset",
    "match_template",
    "match_template_multi",
    "list_session_assets",
    "show_asset_in_viewport",
    "run_probe_script",
    "get_permissions",
    "get_runtime_context",
    "inspect_click_target",
    "message_click_probe",
    "retarget_click_window",
    "mark_on_image",
    "define_tool",
    "list_custom_tools",
    "delete_custom_tool",
}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")

#: Phase B4 第 6 项（设计 §10）：**退役的领域专用旧技能**。
#:
#: 这些工具把某个具体界面的数字写死在代码里（``count_medals`` 的"每卡 5 槽"、
#: ``combined_mark`` 的"勋章 + 空槽"双模板），换一个游戏界面就完全不可用，而且会把
#: 模型往"猜槽位/猜固定数量"上带。通用做法是：模板 + 目标图 → ``match_template_multi``
#: → 结构化 count（见技能 ``visual-template-count``）。
#:
#: 退役按**名字**生效（而不是只删 bundled 文件）：``user_data\collab_tools.json`` 里
#: 可能还留着同名本机条目，只删 bundled 会让它们立刻重新变成可用工具。
RETIRED_TOOL_NAMES: frozenset[str] = frozenset({"count_medals", "combined_mark"})

#: 退役工具的替代做法（报错时直接告诉模型下一步该用什么）。
RETIRED_TOOL_REPLACEMENT = (
    "用通用做法代替：match_template_multi（模板 + 目标图 → count），"
    "具体流程见技能 visual-template-count"
)

#: 退役原因（写进归档，供事后追溯与恢复决策）。
RETIRED_REASON = (
    "领域专用旧技能：把某个具体界面的槽位/数量写死在代码里，换界面即失效，"
    "并会把模型带向猜固定数量。Phase B4 第 6 项退役（设计 §10）。"
)


def retired_tool_names() -> tuple[str, ...]:
    """已退役的领域专用工具名（稳定顺序，便于测试与展示）。"""
    return tuple(sorted(RETIRED_TOOL_NAMES))


#: 退役归档文件（与被清理的 store 同目录）：保留原始定义与退役原因，可恢复。
RETIRED_ARCHIVE_PATH = STORE_PATH.with_name("collab_tools.retired.json")


def retire_stored_tools(
    *,
    store_path: Optional[Path] = None,
    archive_path: Optional[Path] = None,
    reason: str = "",
    now: Optional[float] = None,
) -> dict[str, Any]:
    """**幂等迁移**：把 store 里的退役工具移到独立归档，不动其它任何条目。

    - 精确匹配 :data:`RETIRED_TOOL_NAMES`（不做前缀/模糊匹配）；
    - 删除前备份原文件（``collab_tools.json.bak-<UTC 时间戳>``）；
    - 退役记录进 ``collab_tools.retired.json``，带原始定义、原因、时间与替代能力；
    - 重复运行不产生重复记录；store 里没有退役名字时**一个字节都不写**；
    - 归档过的名字再出现在 store 里（同步/旧备份）也不会被"复活"：
      迁移会把它移走，加载器另有 :data:`RETIRED_TOOL_NAMES` 兜底。
    """
    store_file = Path(store_path or STORE_PATH)
    archive_file = Path(archive_path or store_file.with_name("collab_tools.retired.json"))
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now if now is not None else time.time()))
    report: dict[str, Any] = {
        "ok": True,
        "store": str(store_file),
        "archive": str(archive_file),
        "changed": False,
        "moved": [],
        "already_retired": [],
        "kept": [],
        "backup": "",
        "reason": str(reason or RETIRED_REASON),
    }
    if not store_file.is_file():
        report["ok"] = False
        report["error"] = f"找不到工具库：{store_file}"
        return report
    try:
        data = json.loads(store_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report["ok"] = False
        report["error"] = f"工具库读不出来：{exc}"
        return report
    tools = [t for t in (data.get("tools") or []) if isinstance(t, dict)]
    kept = [t for t in tools if str(t.get("name") or "") not in RETIRED_TOOL_NAMES]
    victims = [t for t in tools if str(t.get("name") or "") in RETIRED_TOOL_NAMES]
    report["kept"] = [str(t.get("name") or "") for t in kept]

    archived: list[dict[str, Any]] = []
    if archive_file.is_file():
        try:
            existing = json.loads(archive_file.read_text(encoding="utf-8"))
            archived = [r for r in (existing.get("retired") or []) if isinstance(r, dict)]
        except (OSError, ValueError):
            archived = []
    archived_names = {str(r.get("name") or "") for r in archived}
    report["already_retired"] = sorted({str(t.get("name") or "") for t in victims} & archived_names)

    if not victims:
        # 幂等：没有可迁移条目时不动 store、不建归档、不做备份
        return report

    backup = store_file.with_name(f"{store_file.name}.bak-{stamp}")
    try:
        shutil.copy2(store_file, backup)
    except OSError as exc:
        report["ok"] = False
        report["error"] = f"备份失败，未改动工具库：{exc}"
        return report
    report["backup"] = str(backup)

    for tool in victims:
        name = str(tool.get("name") or "")
        if name in archived_names:
            continue
        archived.append(
            {
                "name": name,
                "retired_at": stamp,
                "reason": str(reason or RETIRED_REASON),
                "replacement": RETIRED_TOOL_REPLACEMENT,
                "uses_at_retirement": int(tool.get("uses") or 0),
                "retired_by": "phase_b4_item6",
                # 原始定义整条留档：要恢复就把它搬回 collab_tools.json 的 tools
                "original": dict(tool),
            }
        )
        archived_names.add(name)
        report["moved"].append(name)

    data["tools"] = kept
    try:
        archive_file.parent.mkdir(parents=True, exist_ok=True)
        archive_file.write_text(
            json.dumps({"retired": archived}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _write_store_path(store_file, data)
    except OSError as exc:
        report["ok"] = False
        report["error"] = f"写入失败（备份已在 {backup}）：{exc}"
        return report
    report["changed"] = True
    return report


def _write_store_path(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_tool_name(name: str) -> tuple[bool, str]:
    n = str(name or "").strip()
    if not _NAME_RE.match(n):
        return False, "工具名只能用小写字母/数字/下划线，3~40 字符，字母开头"
    if n in RESERVED_TOOL_NAMES:
        return False, f"「{n}」是内置工具名，换一个"
    if n in RETIRED_TOOL_NAMES:
        return False, f"「{n}」是已退役的领域专用旧技能（写死了某个界面的槽位/数量）。{RETIRED_TOOL_REPLACEMENT}"
    if n in bundled_tool_names():
        return False, f"「{n}」是随仓库分发的内置工具，直接用它，不用自己再造一个"
    return True, n


def _empty_store() -> dict[str, Any]:
    return {"tools": []}


def _read_store() -> dict[str, Any]:
    try:
        raw = STORE_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return _empty_store()
    if not isinstance(data, dict):
        return _empty_store()
    tools = data.get("tools")
    data["tools"] = [t for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []
    return data


def _write_store(data: dict[str, Any]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def load_tool_defs() -> list[dict[str, Any]]:
    """全部可用的自造形态工具：**内置**（随仓库）+ 本机登记（user_data）。

    内置的排在前面且带 ``bundled=True``：它们随仓库分发、不可删除；
    本机登记的同名条目会被内置的顶掉（避免同名歧义）。

    Phase B4 第 6 项：**退役名字在任何一层都不再进工具表** —— 无论它来自 bundled
    还是 ``user_data\\collab_tools.json``（同名本机条目只删 bundled 会"复活"）。
    """
    bundled = [
        tool.as_tool_def()
        for tool in load_bundled_tools()
        if str(tool.name or "") not in RETIRED_TOOL_NAMES
    ]
    taken = {str(t.get("name") or "") for t in bundled}
    tools = _read_store().get("tools") or []
    mine = [
        t
        for t in tools
        if str(t.get("name") or "").strip()
        and str(t.get("name") or "") not in taken
        and str(t.get("name") or "") not in RETIRED_TOOL_NAMES
    ]
    mine.sort(key=lambda t: float(t.get("created_at") or 0), reverse=True)
    return bundled + mine


def is_bundled_tool(name: str) -> bool:
    return str(name or "").strip() in bundled_tool_names()


def find_tool_def(name: str) -> Optional[dict[str, Any]]:
    n = str(name or "").strip()
    if not n:
        return None
    for t in load_tool_defs():
        if str(t.get("name") or "") == n:
            return t
    return None


def register_tool(
    *,
    name: str,
    description: str,
    parameters: Optional[dict[str, Any]] = None,
    code: str = "",
) -> dict[str, Any]:
    """登记（或同覆盖）一个自造工具。"""
    ok, normalized = validate_tool_name(name)
    if not ok:
        return {"ok": False, "error": normalized}
    src = str(code or "").strip()
    if not src:
        return {"ok": False, "error": "缺少 code：把跑通的脚本源码一起给我"}
    if len(src) > MAX_CODE_CHARS:
        return {"ok": False, "error": f"code 过长（上限 {MAX_CODE_CHARS} 字符）"}
    try:
        compile(src, f"<custom_tool:{normalized}>", "exec")
    except SyntaxError as e:
        return {"ok": False, "error": f"code 语法错误：{e}"}
    # 实况教训：combined_mark 里写了 `json.loads(sys.argv[1])`，而探针执行器注入的是
    # ARGS，于是每次调用都 KeyError，25 次 0 成功，模型还看不出为什么。这里当场拦下。
    if re.search(r"\bsys\.argv\b", src):
        return {
            "ok": False,
            "error": (
                "code 里用了 sys.argv —— 自造工具的参数从 ARGS 取，没有命令行参数。"
                "改成 A = ARGS if isinstance(ARGS, dict) else {}；"
                "读图用 read_image(名字) / resolve_image(名字)，不要自己拼相对路径。"
            ),
        }
    desc = str(description or "").strip()[:MAX_DESC_CHARS]
    if not desc:
        return {"ok": False, "error": "缺少 description：一句话说清这个工具做什么、返回什么"}
    schema = parameters if isinstance(parameters, dict) else {"type": "object", "properties": {}}
    props = schema.get("properties")
    if not isinstance(props, dict):
        schema = {"type": "object", "properties": {}}
    # Phase B 收口：资源参数必须由 **schema** 声明（x-resource），不能靠参数名推断。
    # 新登记的工具如果带了疑似资源参数却没有声明，当场要求补 schema ——
    # 否则以后出现"名字相同但不是资源"的普通参数时，Binder 会错拦。
    from backend.script_generator.resource_binding import (
        legacy_inferred_resource_params,
        resource_id_param_schema,
    )

    undeclared = legacy_inferred_resource_params(schema)
    if undeclared:
        return {
            "ok": False,
            "error": (
                "参数 {} 看起来是资源引用，但 schema 里没有 x-resource 声明。"
                "请用 resource_param_schema() / resource_id_param_schema() 声明它们"
                "（资源参数必须是资源 id，Runtime 会在调用前绑定并校验版本/状态/内容 hash）。"
            ).format("、".join(undeclared)),
            "missing_x_resource": list(undeclared),
            "suggested_schema": {
                name: resource_id_param_schema() for name in undeclared
            },
        }

    data = _read_store()
    old_tool = next(
        (t for t in (data.get("tools") or []) if str(t.get("name") or "") == normalized),
        None,
    )
    tools = [t for t in (data.get("tools") or []) if str(t.get("name") or "") != normalized]
    if len(tools) >= MAX_TOOLS:
        return {"ok": False, "error": f"自造工具已达上限 {MAX_TOOLS} 个，先删掉不用的"}
    now = time.time()
    created_at = (old_tool or {}).get("created_at") or now
    tools.append(
        {
            "name": normalized,
            "description": desc,
            "parameters": schema,
            "code": src,
            "created_at": created_at,
            "updated_at": now,
            "uses": int((old_tool or {}).get("uses") or 0),
        }
    )
    data["tools"] = tools
    _write_store(data)
    return {
        "ok": True,
        "name": normalized,
        "description": desc,
        "parameters": schema,
        "created_at": created_at,
        "updated_at": now,
        "note": (
            f"已登记为可复用工具「{normalized}」，之后每轮工具表都会带上它，"
            "不用再写临时脚本；用户可以在权限面板里删掉。"
        ),
    }


def delete_tool(name: str) -> dict[str, Any]:
    n = str(name or "").strip()
    if not n:
        return {"ok": False, "error": "缺少 name"}
    if n in bundled_tool_names():
        return {
            "ok": False,
            "error": f"「{n}」是随仓库分发的内置工具，不能删；要改就在探针里跑通后重新导出",
        }
    data = _read_store()
    tools = data.get("tools") or []
    kept = [t for t in tools if str(t.get("name") or "") != n]
    if len(kept) == len(tools):
        return {"ok": False, "error": f"没有这个自造工具：{n}"}
    data["tools"] = kept
    _write_store(data)
    return {"ok": True, "name": n, "removed": len(tools) - len(kept)}


def bump_uses(name: str) -> None:
    data = _read_store()
    changed = False
    for t in data.get("tools") or []:
        if str(t.get("name") or "") == str(name or ""):
            t["uses"] = int(t.get("uses") or 0) + 1
            t["last_used_at"] = time.time()
            changed = True
    if changed:
        try:
            _write_store(data)
        except Exception:
            pass


def openai_custom_tool_schemas(session: Any = None) -> list[dict[str, Any]]:
    """自造工具的 OpenAI function schema；调用时仍受 probe_script 权限约束。"""
    out: list[dict[str, Any]] = []
    for t in load_tool_defs():
        params = t.get("parameters")
        if not isinstance(params, dict):
            params = {"type": "object", "properties": {}}
        out.append(
            {
                "type": "function",
                "function": {
                    "name": str(t.get("name") or ""),
                    "description": (
                        str(t.get("description") or "")
                        + "（模型自造工具，等价于跑一段预置探针脚本）"
                    ),
                    "parameters": params,
                },
            }
        )
    return out


def custom_tools_brief() -> str:
    """给提示看的一行清单。"""
    defs = load_tool_defs()
    if not defs:
        return ""
    items = []
    for t in defs[:12]:
        items.append(f"{t.get('name')}（{str(t.get('description') or '')[:36]}）")
    return "、".join(items)


def build_callable_code(tool_def: dict[str, Any], args: dict[str, Any]) -> str:
    """把工具参数绑成 ARGS 再拼上工具源码，交给探针执行器跑。"""
    payload = json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False)
    return (
        "import json as _json\n"
        f"ARGS = _json.loads({payload!r})\n"
        "params = ARGS\n"
        + str(tool_def.get("code") or "")
    )


def _main(argv: list[str] | None = None) -> int:
    """命令行：执行一次退役迁移（幂等），打印备份路径与迁移条目。

        python -m backend.script_generator.collaborator.reusable_tools --retire
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if "--retire" not in args:
        print("用法：python -m backend.script_generator.collaborator.reusable_tools --retire")
        return 2
    report = retire_stored_tools()
    print(f"工具库   ：{report['store']}")
    print(f"归档文件 ：{report['archive']}")
    print(f"备份文件 ：{report['backup'] or '（无需备份：没有可迁移条目）'}")
    print(f"迁移条目 ：{report['moved'] or '（无）'}")
    print(f"已在归档 ：{report['already_retired'] or '（无）'}")
    print(f"保留条目 ：{report['kept']}")
    print(f"是否改动 ：{report['changed']}")
    if not report["ok"]:
        print(f"错误     ：{report.get('error')}")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(_main())
