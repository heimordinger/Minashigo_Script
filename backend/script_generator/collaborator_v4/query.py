"""Read-only V4 query gateway: current session data plus system capability metadata."""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any


_CATEGORIES: dict[str, dict[str, str]] = {
    "visual_observation": {
        "name": "视觉查看与复核",
        "meaning": "查看一张或多张会话图片，获取内容、坐标、数量并复核结果。",
        "use_when": "需要先看懂图片，或验证生成结果是否正确。",
    },
    "image_matching": {
        "name": "图像定位与匹配",
        "meaning": "模板匹配、重复目标定位、裁剪及尺度测量。",
        "use_when": "已知道目标外观，需要得到精确位置或派生模板。",
    },
    "image_annotation": {
        "name": "图片标注与展示",
        "meaning": "在图片上画圈、画框、编号，生成并展示结果图。",
        "use_when": "用户要求圈出、框出、标记或展示图片结果。",
    },
    "session_resources": {
        "name": "会话素材与资源",
        "meaning": "列出、读取和管理当前会话的素材、资源与状态。",
        "use_when": "需要确认当前会话里有什么，或读取精确资源信息。",
    },
    "skills_workflow": {
        "name": "技能与工作流",
        "meaning": "查找并读取可跨版本复用的 SKILL.md 工作流说明书；skill 只指导工具编排，不可直接调用。",
        "use_when": "任务需要组合两个以上工具，或属于已有工作流时优先查看；找到匹配 skill 后先读正文，再进入具体工具类别。",
    },
    "runtime_control": {
        "name": "运行环境与窗口",
        "meaning": "读取运行环境、绑定窗口、点击诊断和运行时控制。",
        "use_when": "任务涉及实际窗口、账号、点击或运行环境。",
    },
    "script_extension": {
        "name": "脚本扩展",
        "meaning": "运行临时探针或登记可复用自造工具。",
        "use_when": "其他类别没有现成能力，才用脚本补足缺口。",
    },
    "diagnostics": {
        "name": "诊断与权限",
        "meaning": "查看权限、状态和诊断信息。",
        "use_when": "能力因权限或环境状态无法执行。",
    },
}


def _category_for(name: str, purpose: str = "") -> str:
    explicit = {
        "describe_current_frame": "visual_observation",
        "inspect_asset": "visual_observation",
        "compare_assets": "visual_observation",
        "analyze_current_frame": "visual_observation",
        "fit_repeated_regions": "image_matching",
        "commit_repeated_regions": "image_matching",
        "crop_asset": "image_matching",
        "match_template": "image_matching",
        "match_template_multi": "image_matching",
        "mark_on_image": "image_annotation",
        "show_asset_in_viewport": "image_annotation",
        "list_session_assets": "session_resources",
        "get_resource": "session_resources",
        "get_session_status": "session_resources",
        "list_skills": "skills_workflow",
        "select_skill": "skills_workflow",
        "get_runtime_context": "runtime_control",
        "inspect_click_target": "runtime_control",
        "message_click_probe": "runtime_control",
        "retarget_click_window": "runtime_control",
        "run_probe_script": "script_extension",
        "define_tool": "script_extension",
        "delete_custom_tool": "script_extension",
        "get_permissions": "diagnostics",
    }
    if name in explicit:
        return explicit[name]
    text = f"{name} {purpose}".casefold()
    heuristics = (
        ("image_annotation", ("mark", "annotat", "draw", "circle", "标注", "画圈", "画框")),
        ("image_matching", ("match", "crop", "locate", "template", "匹配", "裁剪", "定位")),
        ("visual_observation", ("vision", "inspect", "compare", "识图", "查看图片", "复核")),
        ("session_resources", ("asset", "resource", "session", "素材", "资源", "会话")),
        ("runtime_control", ("click", "window", "runtime", "窗口", "点击", "运行环境")),
        ("script_extension", ("script", "probe", "python", "脚本", "探针")),
        ("skills_workflow", ("skill", "workflow", "技能", "工作流")),
    )
    for category_id, words in heuristics:
        if any(word in text for word in words):
            return category_id
    return "diagnostics"


def capability_catalog() -> list[dict[str, Any]]:
    from backend.script_generator.collaborator.llm_turn import all_collab_tools
    from backend.script_generator.collaborator_v3.adapters.skill_capabilities import (
        SKILL_ID_PREFIX,
        load_skill_docs,
    )

    out: list[dict[str, Any]] = []
    for item in all_collab_tools():
        fn = dict(item.get("function") or {})
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        schema = dict(fn.get("parameters") or {"type": "object", "properties": {}})
        purpose = str(fn.get("description") or "")
        out.append(
            {
                "capability_id": f"local:{name}",
                "kind": "tool",
                "version": "v2-adapter-1",
                "name": name,
                "purpose": purpose,
                "input_summary": list((schema.get("properties") or {}).keys()),
                "required_inputs": list(schema.get("required") or []),
                "output_summary": "本地能力返回的 JSON 结果；失败时包含 error",
                "actual_effect": _effect(name),
                "side_effect": _side_effect(name),
                "category_id": _category_for(name, purpose),
                "input_schema": schema,
            }
        )
    # SKILL.md is the single source of workflow instructions for V2/V3/V4.
    # It lives beside tools in discovery, but is deliberately non-executable.
    for doc in load_skill_docs():
        out.append(
            {
                "capability_id": f"{SKILL_ID_PREFIX}{doc.name}",
                "kind": "skill",
                "version": doc.content_hash,
                "name": doc.name,
                "purpose": doc.description,
                "input_summary": [],
                "required_inputs": [],
                "output_summary": "完整 SKILL.md 正文；按说明编排工具，不产生直接执行结果",
                "actual_effect": "读取跨版本共用的工作流说明书；不会执行工具或修改用户数据",
                "side_effect": "none_or_read_only",
                "category_id": "skills_workflow",
                "input_schema": {},
                "body": doc.body,
                "path": doc.path,
            }
        )
    return out


def capability_categories() -> list[dict[str, Any]]:
    catalog = capability_catalog()
    counts: dict[str, int] = {key: 0 for key in _CATEGORIES}
    for item in catalog:
        category_id = str(item.get("category_id") or "diagnostics")
        counts[category_id] = counts.get(category_id, 0) + 1
    return [
        {
            "category_id": key,
            **meta,
            "capability_count": counts.get(key, 0),
            "resource_kinds": sorted(
                {
                    str(item.get("kind") or "tool")
                    for item in catalog
                    if item.get("category_id") == key
                }
            ),
        }
        for key, meta in _CATEGORIES.items()
        if counts.get(key, 0) > 0
    ]


def capabilities_in_category(category_id: str) -> list[dict[str, Any]]:
    if category_id not in _CATEGORIES:
        return []
    return [
        {k: v for k, v in item.items() if k not in {"input_schema", "body", "path"}}
        for item in capability_catalog()
        if item.get("category_id") == category_id
    ]


def _effect(name: str) -> str:
    effects = {
        "list_session_assets": "读取当前会话素材清单，不查看图片内容",
        "inspect_asset": "调用视觉能力检查一张当前会话素材",
        "compare_assets": "调用视觉能力联合比较多张当前会话素材",
        "match_template": "在指定目标图片中执行单目标模板匹配",
        "match_template_multi": "在指定目标图片中执行多目标模板匹配",
        "crop_asset": "从当前会话图片派生一张裁剪图片并登记为新素材",
        "mark_on_image": "生成标注图片并登记为新素材",
        "run_probe_script": "执行一次临时 Python 探针并回收结构化结果和产物",
    }
    return effects.get(name, "执行已登记的本地能力；以完整输入契约和返回结果为准")


def _side_effect(name: str) -> str:
    if name in {"crop_asset", "mark_on_image", "run_probe_script", "define_tool", "delete_custom_tool"}:
        return "可能创建、修改或登记本地资源"
    if name in {"show_asset_in_viewport", "retarget_click_window", "message_click_probe"}:
        return "会改变本地界面或运行时状态"
    return "none_or_read_only"


def _words(text: str) -> list[str]:
    return [x for x in re.split(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", text.casefold()) if x]


def search_capabilities(purpose: str, *, limit: int = 12) -> list[dict[str, Any]]:
    catalog = capability_catalog()
    words = _words(purpose)
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in catalog:
        haystack = (
            f"{item['name']} {item['purpose']} {item['actual_effect']} {item.get('body', '')}"
        ).casefold()
        score = sum(1 for word in words if word in haystack)
        if score:
            scored.append((score, item))
    # An empty fuzzy result must not trap the model. Return a bounded catalog as
    # fallback, clearly marked, so it can select by semantics instead of retrying synonyms.
    selected = [item for _, item in sorted(scored, key=lambda pair: (-pair[0], pair[1]["name"]))]
    if not selected:
        selected = catalog
    return [
        {k: v for k, v in item.items() if k not in {"input_schema", "body", "path"}}
        | {"search_match": bool(scored)}
        for item in selected[: max(1, min(int(limit or 12), 30))]
    ]


def contract_for(capability_id: str) -> dict[str, Any] | None:
    for item in capability_catalog():
        if item["capability_id"] == capability_id and item.get("kind") == "tool":
            return item
    return None


def resource_for(capability_id: str) -> dict[str, Any] | None:
    """Return one discovered tool or skill from the shared catalog."""
    for item in capability_catalog():
        if item["capability_id"] == capability_id:
            return item
    return None


def source_for(capability_id: str, *, max_lines: int = 160) -> dict[str, Any]:
    contract = contract_for(capability_id)
    if not contract:
        return {"ok": False, "error": "CAPABILITY_NOT_FOUND"}
    name = contract["name"]
    from backend.script_generator.collaborator import llm_turn

    path = Path(inspect.getsourcefile(llm_turn) or "")
    if not path.is_file():
        return {"ok": False, "error": "SOURCE_UNAVAILABLE"}
    lines = path.read_text(encoding="utf-8").splitlines()
    marker = f'name == "{name}"'
    start = next((i for i, line in enumerate(lines) if marker in line), -1)
    if start < 0:
        return {
            "ok": True,
            "capability_id": capability_id,
            "version": contract["version"],
            "note": "未找到独立执行分支；以下仅返回权威调用契约",
            "contract": contract,
        }
    end = min(len(lines), start + max(20, min(int(max_lines or 160), 400)))
    return {
        "ok": True,
        "capability_id": capability_id,
        "version": contract["version"],
        "source_file": str(path),
        "start_line": start + 1,
        "source": "\n".join(f"{i + 1}: {lines[i]}" for i in range(start, end)),
        "note": "源码仅供理解；调用仍以 input_schema 为准",
    }


class SessionQueryGateway:
    def __init__(self, session) -> None:
        self.session = session
        self.category_menu_shown = False
        self.discovered_capability_ids: set[str] = set()

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        args = dict(arguments or {})
        if name == "session.assets":
            from backend.script_generator.collaborator.resource_registry import active_session_assets

            query = str(args.get("query") or "").casefold()
            role = str(args.get("role") or "")
            assets = []
            for item in active_session_assets(self.session):
                if not isinstance(item, dict):
                    continue
                if query and query not in f"{item.get('id', '')} {item.get('name', '')}".casefold():
                    continue
                if role and str(item.get("role") or "") != role:
                    continue
                assets.append({k: item.get(k) for k in ("id", "name", "role", "group", "source", "path")})
            return {"ok": True, "query": name, "scope": "current_session", "items": assets, "returned": len(assets)}
        if name == "session.messages":
            limit = max(1, min(int(args.get("limit") or 12), 30))
            items = [
                {"role": str(m.get("role") or ""), "text": str(m.get("text") or "")}
                for m in list(self.session.messages or [])[-limit:]
                if str(m.get("role") or "") in {"user", "agent"}
            ]
            return {"ok": True, "query": name, "scope": "current_session", "items": items}
        if name == "capability.search":
            if not self.category_menu_shown:
                return {
                    "ok": False,
                    "query": name,
                    "error": "CATEGORY_MENU_REQUIRED",
                    "meaning": "先调用 capability.list_categories，再选择类别深入查看。",
                    "next_query": "capability.list_categories",
                }
            purpose = str(args.get("purpose") or "")
            items = search_capabilities(purpose, limit=int(args.get("limit") or 12))
            self.discovered_capability_ids.update(str(item.get("capability_id") or "") for item in items)
            return {"ok": True, "query": name, "scope": "system_metadata", "purpose": purpose, "items": items, "returned": len(items)}
        if name == "capability.list_categories":
            self.category_menu_shown = True
            items = capability_categories()
            return {"ok": True, "query": name, "scope": "system_metadata", "items": items, "returned": len(items)}
        if name == "capability.list_by_category":
            if not self.category_menu_shown:
                return {
                    "ok": False,
                    "query": name,
                    "error": "CATEGORY_MENU_REQUIRED",
                    "meaning": "先调用 capability.list_categories 查看类别语义。",
                }
            category_id = str(args.get("category_id") or "")
            items = capabilities_in_category(category_id)
            if not items:
                return {"ok": False, "query": name, "error": "CATEGORY_NOT_FOUND", "category_id": category_id}
            self.discovered_capability_ids.update(str(item.get("capability_id") or "") for item in items)
            return {
                "ok": True,
                "query": name,
                "scope": "system_metadata",
                "category": {"category_id": category_id, **_CATEGORIES[category_id]},
                "items": items,
                "returned": len(items),
            }
        if name == "capability.get_contract":
            capability_id = str(args.get("capability_id") or "")
            if capability_id not in self.discovered_capability_ids:
                return {
                    "ok": False,
                    "query": name,
                    "error": "CAPABILITY_NOT_DISCOVERED",
                    "meaning": "先从能力类别进入并查看该类工具摘要，再取得完整契约。",
                }
            resource = resource_for(capability_id)
            if resource and resource.get("kind") != "tool":
                return {
                    "ok": False,
                    "query": name,
                    "error": "CAPABILITY_NOT_EXECUTABLE",
                    "meaning": "skill 是工作流说明书；请用 capability.get 读取正文，不能取得工具调用契约。",
                }
            item = contract_for(capability_id)
            return {"ok": bool(item), "query": name, "scope": "system_metadata", "contract": item, "error": "" if item else "CAPABILITY_NOT_FOUND"}
        if name == "capability.get":
            capability_id = str(args.get("capability_id") or "")
            if capability_id not in self.discovered_capability_ids:
                return {
                    "ok": False,
                    "query": name,
                    "error": "CAPABILITY_NOT_DISCOVERED",
                    "meaning": "先从能力类别或搜索结果中发现该能力，再读取完整内容。",
                }
            item = resource_for(capability_id)
            if not item:
                return {"ok": False, "query": name, "error": "CAPABILITY_NOT_FOUND"}
            if item.get("kind") == "skill":
                payload = {
                    k: item.get(k)
                    for k in ("capability_id", "kind", "version", "name", "purpose", "body")
                }
            else:
                payload = {k: v for k, v in item.items() if k not in {"body", "path"}}
            return {
                "ok": True,
                "query": name,
                "scope": "system_metadata",
                "capability": payload,
            }
        if name == "capability.get_source":
            return source_for(str(args.get("capability_id") or ""), max_lines=int(args.get("max_lines") or 160))
        return {"ok": False, "query": name, "error": "QUERY_NOT_ALLOWED"}
