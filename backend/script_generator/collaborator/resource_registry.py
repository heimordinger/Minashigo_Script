"""协作 Agent 的统一资源注册中心。

资源库保存在 ``session.artifacts``，因此跟随会话持久化。GUI 仍使用
``session.assets`` 作为兼容视图；这里负责版本、状态、内容哈希、作用域、来源关系
和执行前校验。模型只应看到当前工作集里的 active/candidate 资源。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

REGISTRY_KEY = "resource_registry_v1"
WORKSPACE_KEY = "resource_workspace_v1"
SNAPSHOTS_KEY = "resource_snapshots_v1"
RESOURCE_STATUSES = {"candidate", "active", "verified", "superseded", "invalid", "archived", "deleted"}
USABLE_STATUSES = {"candidate", "active", "verified"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def file_hash(path: str | Path | None) -> str:
    if not path:
        return ""
    try:
        p = Path(path)
        if not p.is_file():
            return ""
        h = hashlib.sha256()
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return "sha256:" + h.hexdigest()
    except Exception:
        return ""


def _store(session) -> dict[str, Any]:
    if not isinstance(getattr(session, "artifacts", None), dict):
        session.artifacts = {}
    raw = session.artifacts.get(REGISTRY_KEY)
    if not isinstance(raw, dict):
        raw = {"schema": 1, "resources": {}, "updated_at": _now()}
        session.artifacts[REGISTRY_KEY] = raw
    if not isinstance(raw.get("resources"), dict):
        raw["resources"] = {}
    return raw


def workspace(session) -> dict[str, Any]:
    if not isinstance(getattr(session, "artifacts", None), dict):
        session.artifacts = {}
    ws = session.artifacts.get(WORKSPACE_KEY)
    if not isinstance(ws, dict):
        ws = {"asset_ids": [], "evidence_ids": [], "tool_ids": [], "skill_ids": [], "current_frame_id": ""}
        session.artifacts[WORKSPACE_KEY] = ws
    return ws


def all_resources(session) -> list[dict[str, Any]]:
    ensure_asset_resources(session)
    return [dict(x) for x in _store(session)["resources"].values() if isinstance(x, dict)]


def get_resource(session, resource_id: str) -> Optional[dict[str, Any]]:
    ensure_asset_resources(session)
    row = _store(session)["resources"].get(str(resource_id or ""))
    return dict(row) if isinstance(row, dict) else None


def resource_for_path(session, path: str | Path) -> Optional[dict[str, Any]]:
    try:
        wanted = Path(path).resolve()
    except Exception:
        return None
    for row in _store(session)["resources"].values():
        if not isinstance(row, dict) or row.get("type") != "asset":
            continue
        try:
            if Path(str(row.get("path") or "")).resolve() == wanted:
                return dict(row)
        except Exception:
            continue
    return None


def _asset_kind(asset: dict[str, Any]) -> str:
    role = str(asset.get("role") or "template")
    group = str(asset.get("group") or "")
    if role == "annotation" or group == "标注":
        return "annotation"
    if role == "screenshot":
        return "scene"
    return "template"


def register_asset(
    session,
    asset: dict[str, Any],
    *,
    status: str = "active",
    derived_from: Optional[Iterable[str]] = None,
    provenance: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    rid = str(asset.get("id") or "").strip()
    if not rid:
        raise ValueError("asset 缺少稳定 id")
    store = _store(session)
    old = store["resources"].get(rid)
    old = old if isinstance(old, dict) else {}
    path = str(asset.get("path") or "")
    try:
        stat = Path(path).stat()
        file_size, file_mtime_ns = int(stat.st_size), int(stat.st_mtime_ns)
    except Exception:
        file_size, file_mtime_ns = 0, 0
    if (
        old.get("content_hash")
        and int(old.get("file_size") or 0) == file_size
        and int(old.get("file_mtime_ns") or 0) == file_mtime_ns
    ):
        digest = str(old.get("content_hash"))
    else:
        digest = file_hash(path)
    version = max(1, int(old.get("version") or 1))
    if old.get("content_hash") and digest and old.get("content_hash") != digest:
        version += 1
    row = {
        "resource_id": rid,
        "type": "asset",
        "kind": _asset_kind(asset),
        "version": version,
        "status": status if status in RESOURCE_STATUSES else "active",
        "scope": "task",
        "name": str(asset.get("name") or Path(path).name),
        "path": path,
        "content_hash": digest,
        "file_size": file_size,
        "file_mtime_ns": file_mtime_ns,
        "role": str(asset.get("role") or "template"),
        "group": str(asset.get("group") or ""),
        "caption": str(asset.get("caption") or ""),
        "derived_from": [str(x) for x in (derived_from or old.get("derived_from") or []) if str(x)],
        "provenance": dict(provenance or old.get("provenance") or {}),
        "created_at": str(old.get("created_at") or _now()),
        "updated_at": _now(),
        "supersedes": str(old.get("supersedes") or ""),
        "replaced_by": str(old.get("replaced_by") or "") if status == old.get("status") else "",
        "invalid_reason": str(old.get("invalid_reason") or "") if status == old.get("status") else "",
        "status_reason": str(old.get("status_reason") or "") if status == old.get("status") else "",
    }
    store["resources"][rid] = row
    store["updated_at"] = _now()
    ws = workspace(session)
    ids = [str(x) for x in ws.get("asset_ids") or [] if str(x) != rid]
    if row["status"] in USABLE_STATUSES:
        ids.append(rid)
    ws["asset_ids"] = ids
    return dict(row)


def ensure_asset_resources(session) -> None:
    """把旧 ``session.assets`` 幂等迁移为资源记录，并刷新当前工作集。"""
    assets = [x for x in (getattr(session, "assets", None) or []) if isinstance(x, dict)]
    present = set()
    for asset in assets:
        rid = str(asset.get("id") or "")
        if not rid:
            continue
        present.add(rid)
        old = _store(session)["resources"].get(rid)
        if isinstance(old, dict) and old.get("content_hash"):
            try:
                stat = Path(str(asset.get("path") or "")).stat()
                unchanged = (
                    int(old.get("file_size") or -1) == int(stat.st_size)
                    and int(old.get("file_mtime_ns") or -1) == int(stat.st_mtime_ns)
                )
            except Exception:
                unchanged = False
            current_hash = str(old.get("content_hash")) if unchanged else file_hash(asset.get("path"))
            if not current_hash:
                old["status"] = "invalid"
                old["invalid_reason"] = "文件不存在"
                old["updated_at"] = _now()
                continue
            if current_hash != old.get("content_hash"):
                old["status"] = "invalid"
                old["invalid_reason"] = "文件内容已变化；需要重新登记为新版本"
                old["updated_at"] = _now()
                continue
        status = str(old.get("status") or "active") if isinstance(old, dict) else "active"
        register_asset(session, asset, status=status)
    store = _store(session)
    for rid, row in list(store["resources"].items()):
        if isinstance(row, dict) and row.get("type") == "asset" and rid not in present:
            if row.get("status") in USABLE_STATUSES:
                row["status"] = "archived"
                row["updated_at"] = _now()
    ws = workspace(session)
    ws["asset_ids"] = [
        rid for rid in present
        if isinstance(store["resources"].get(rid), dict)
        and store["resources"][rid].get("status") in USABLE_STATUSES
    ]
    frame = getattr(session, "frame_path", None)
    ws["current_frame_id"] = ""
    if frame:
        try:
            target = Path(frame).resolve()
            for asset in assets:
                if Path(str(asset.get("path") or "")).resolve() == target:
                    ws["current_frame_id"] = str(asset.get("id") or "")
                    break
            if not ws["current_frame_id"] and target.is_file():
                digest = file_hash(target)
                rid = "frame_" + (digest.split(":", 1)[-1][:12] or hashlib.sha256(str(target).encode()).hexdigest()[:12])
                pseudo = {
                    "id": rid,
                    "path": str(target),
                    "name": target.name,
                    "role": "screenshot",
                    "caption": "当前共同视口（未加入素材区）",
                    "group": "共同视口",
                }
                register_asset(session, pseudo, status="active", provenance={"source": "viewport"})
                present.add(rid)
                ws["current_frame_id"] = rid
        except Exception:
            pass


def register_capability(
    session,
    *,
    resource_type: str,
    name: str,
    version: str = "1",
    description: str = "",
    schema: Optional[dict[str, Any]] = None,
    requires: Optional[list[str]] = None,
) -> dict[str, Any]:
    blob = repr((resource_type, name, version, schema or {})).encode("utf-8")
    short = hashlib.sha256(blob).hexdigest()[:12]
    rid = f"{resource_type}:{name}@{version}:{short}"
    row = {
        "resource_id": rid,
        "type": resource_type,
        "kind": "capability",
        "version": str(version),
        "status": "active",
        "scope": "task",
        "name": str(name),
        "description": str(description)[:500],
        "schema": dict(schema or {}),
        "requires": [str(x) for x in (requires or [])],
        "content_hash": "sha256:" + short,
        "created_at": _now(),
        "updated_at": _now(),
    }
    resources = _store(session)["resources"]
    for old_id, old in resources.items():
        if (
            old_id != rid
            and isinstance(old, dict)
            and old.get("type") == resource_type
            and old.get("name") == str(name)
            and old.get("status") in USABLE_STATUSES
        ):
            old["status"] = "superseded"
            old["replaced_by"] = rid
            old["updated_at"] = _now()
    resources[rid] = row
    key = "tool_ids" if resource_type == "tool" else "skill_ids"
    ws = workspace(session)
    ids = [x for x in ws.get(key) or [] if not str(x).startswith(f"{resource_type}:{name}@")]
    ids.append(rid)
    ws[key] = ids
    return dict(row)


def register_record_resource(
    session,
    *,
    resource_type: str,
    kind: str,
    name: str,
    payload: dict[str, Any],
    derived_from: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """登记 evidence/overlay/claim 等无文件资源。"""
    created_at = _now()
    blob = repr((resource_type, kind, name, payload, created_at)).encode("utf-8")
    digest = hashlib.sha256(blob).hexdigest()
    prefix = {"evidence": "ev", "overlay": "ov", "claim": "claim"}.get(resource_type, "res")
    rid = f"{prefix}_{digest[:12]}"
    row = {
        "resource_id": rid,
        "type": str(resource_type),
        "kind": str(kind),
        "version": 1,
        "status": "active",
        "scope": "task",
        "name": str(name),
        "content_hash": "sha256:" + digest,
        "payload": dict(payload),
        "derived_from": [str(x) for x in (derived_from or []) if str(x)],
        "created_at": created_at,
        "updated_at": created_at,
    }
    _store(session)["resources"][rid] = row
    if resource_type == "evidence":
        ws = workspace(session)
        evidence_ids = [str(x) for x in ws.get("evidence_ids") or [] if str(x) != rid]
        evidence_ids.append(rid)
        ws["evidence_ids"] = evidence_ids[-20:]
    return dict(row)


def sync_capabilities(session, *, tool_schemas: list[dict], skills: Iterable[Any]) -> None:
    """把本轮实际可调用的 tool/skill 快照登记进任务工作集。"""
    for raw in tool_schemas or []:
        fn = raw.get("function") if isinstance(raw, dict) else None
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        register_capability(
            session,
            resource_type="tool",
            name=str(fn["name"]),
            version="1",
            description=str(fn.get("description") or ""),
            schema=fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {},
        )
    for skill in skills or []:
        sid = str(getattr(skill, "id", "") or "").strip()
        if not sid:
            continue
        register_capability(
            session,
            resource_type="skill",
            name=sid,
            version=str(getattr(skill, "version", "1") or "1"),
            description=str(getattr(skill, "title", "") or sid),
            requires=[str(x) for x in (getattr(skill, "requires_tools", None) or [])],
        )


def archive_resource(session, resource_id: str, *, deleted: bool = False) -> Optional[dict[str, Any]]:
    row = _store(session)["resources"].get(str(resource_id or ""))
    if not isinstance(row, dict):
        return None
    row["status"] = "deleted" if deleted else "archived"
    row["updated_at"] = _now()
    ws = workspace(session)
    for key in ("asset_ids", "tool_ids", "skill_ids"):
        ws[key] = [x for x in ws.get(key) or [] if str(x) != resource_id]
    return dict(row)


def set_resource_status(session, resource_id: str, status: str, *, reason: str = "") -> Optional[dict[str, Any]]:
    row = _store(session)["resources"].get(str(resource_id or ""))
    if not isinstance(row, dict) or status not in RESOURCE_STATUSES:
        return None
    row["status"] = status
    row["updated_at"] = _now()
    if reason:
        row["status_reason"] = str(reason)[:300]
    ws = workspace(session)
    key = "asset_ids" if row.get("type") == "asset" else ("tool_ids" if row.get("type") == "tool" else "skill_ids")
    ids = [str(x) for x in ws.get(key) or [] if str(x) != resource_id]
    if status in USABLE_STATUSES:
        ids.append(str(resource_id))
    ws[key] = ids
    return dict(row)


def supersede_resource(session, old_id: str, new_id: str) -> tuple[bool, str]:
    store = _store(session)["resources"]
    old, new = store.get(old_id), store.get(new_id)
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False, "旧资源或新资源不存在"
    if old.get("type") != new.get("type"):
        return False, "只能用同类型资源相互替代"
    old["status"] = "superseded"
    old["replaced_by"] = new_id
    old["updated_at"] = _now()
    new["supersedes"] = old_id
    new["updated_at"] = _now()
    return True, ""


def invalidate_resource(session, resource_id: str, reason: str) -> None:
    store = _store(session)["resources"]
    pending = [str(resource_id)]
    seen = set()
    while pending:
        rid = pending.pop(0)
        if rid in seen:
            continue
        seen.add(rid)
        row = store.get(rid)
        if isinstance(row, dict):
            row["status"] = "invalid"
            row["invalid_reason"] = str(reason)[:300]
            row["updated_at"] = _now()
        for child_id, child in store.items():
            if isinstance(child, dict) and rid in (child.get("derived_from") or []):
                pending.append(child_id)


def resolve_asset_resource(
    session,
    resource_id: str,
    *,
    allowed_kinds: Optional[set[str]] = None,
) -> tuple[Optional[dict[str, Any]], str]:
    """工具执行前的硬校验；不做名称猜测，也不回退到焦点素材。"""
    ensure_asset_resources(session)
    rid = str(resource_id or "").strip()
    if not rid:
        return None, "必须提供 resource_id/asset_id，禁止静默使用旧素材或焦点素材"
    row = _store(session)["resources"].get(rid)
    if not isinstance(row, dict) or row.get("type") != "asset":
        return None, f"未知素材资源：{rid}"
    if row.get("status") not in USABLE_STATUSES:
        replacement = str(row.get("replaced_by") or "")
        suffix = f"；请改用 {replacement}" if replacement else ""
        return None, f"素材 {rid} 状态为 {row.get('status')}，不可调用{suffix}"
    if allowed_kinds and row.get("kind") not in allowed_kinds:
        return None, f"素材 {rid} 类型为 {row.get('kind')}，这里仅允许 {sorted(allowed_kinds)}"
    path = Path(str(row.get("path") or ""))
    if not path.is_file():
        row["status"] = "invalid"
        row["invalid_reason"] = "文件不存在"
        return None, f"素材文件不存在：{path}"
    digest = file_hash(path)
    if row.get("content_hash") and digest != row.get("content_hash"):
        row["status"] = "invalid"
        row["invalid_reason"] = "文件内容已变化"
        return None, f"素材 {rid} 的文件内容已变化，旧坐标/证据已失效，请重新登记"
    return dict(row), ""


def search_resources(
    session,
    *,
    query: str = "",
    resource_type: str = "",
    statuses: Optional[list[str]] = None,
    active_only: bool = True,
    limit: int = 40,
) -> list[dict[str, Any]]:
    ensure_asset_resources(session)
    q = str(query or "").strip().lower()
    allowed_status = set(statuses or (USABLE_STATUSES if active_only else RESOURCE_STATUSES))
    rows = []
    for row in _store(session)["resources"].values():
        if not isinstance(row, dict):
            continue
        if resource_type and row.get("type") != resource_type:
            continue
        if row.get("status") not in allowed_status:
            continue
        blob = " ".join(str(row.get(k) or "") for k in ("resource_id", "name", "caption", "description", "kind")).lower()
        if q and q not in blob:
            continue
        rows.append(dict(row))
    rows.sort(key=lambda x: (str(x.get("type")), str(x.get("name")), str(x.get("resource_id"))))
    return rows[: max(1, min(100, int(limit or 40)))]


def active_asset_ids(session) -> set[str]:
    ensure_asset_resources(session)
    return {
        str(rid)
        for rid, row in _store(session)["resources"].items()
        if isinstance(row, dict)
        and row.get("type") == "asset"
        and row.get("status") in USABLE_STATUSES
    }


def active_session_assets(session) -> list[dict[str, Any]]:
    """返回兼容素材列表中当前仍可用的条目。

    ``session.assets`` 可能在主轮 worker 回写时短暂带回已经归档的旧条目；
    模型目录和查询器必须以资源注册表状态为准，不能把这种幽灵条目暴露给 LLM。
    """
    active = active_asset_ids(session)
    return [
        item
        for item in list(getattr(session, "assets", None) or [])
        if isinstance(item, dict) and str(item.get("id") or "") in active
    ]


def prune_inactive_session_assets(session) -> list[str]:
    """从兼容列表物理剔除已归档/删除素材，返回被清掉的 id。"""
    before = [item for item in list(getattr(session, "assets", None) or []) if isinstance(item, dict)]
    kept = active_session_assets(session)
    kept_ids = {str(item.get("id") or "") for item in kept}
    removed = [str(item.get("id") or "") for item in before if str(item.get("id") or "") not in kept_ids]
    if removed:
        session.assets = kept
        session.focused_asset_ids = [
            item for item in list(getattr(session, "focused_asset_ids", None) or [])
            if str(item) in kept_ids
        ]
        session.overlays = [
            item for item in list(getattr(session, "overlays", None) or [])
            if str(item.get("asset_id") or "") not in set(removed)
        ]
        session.touch()
    return removed


def resource_brief(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "resource_id", "type", "kind", "version", "status", "name", "role", "group",
        "caption", "content_hash", "path", "derived_from", "supersedes", "replaced_by",
        "invalid_reason", "description", "requires",
    )
    return {k: row[k] for k in keys if k in row and row[k] not in (None, "", [], {})}


def workspace_block(session, *, limit: int = 30) -> str:
    ensure_asset_resources(session)
    ws = workspace(session)
    store = _store(session)["resources"]
    ids = (
        list(ws.get("asset_ids") or [])
        + list(ws.get("evidence_ids") or [])
        + list(ws.get("skill_ids") or [])
        + list(ws.get("tool_ids") or [])
    )
    lines = ["【当前资源工作集】只能调用这里的 candidate/active/verified 资源；旧名称不能代替 resource_id。"]
    current = str(ws.get("current_frame_id") or "")
    for rid in ids[:limit]:
        row = store.get(rid)
        if not isinstance(row, dict) or row.get("status") not in USABLE_STATUSES:
            continue
        mark = " current-frame" if rid == current else ""
        lines.append(
            f"- [{row.get('type')}:{row.get('kind')}] {rid} v{row.get('version')} "
            f"status={row.get('status')} name=「{row.get('name')}」{mark}"
        )
    if len(ids) > limit:
        lines.append(f"- 另有 {len(ids) - limit} 项；用 search_resources 查询，不要从旧对话猜。")
    return "\n".join(lines)


def snapshot_workspace(session, *, reason: str = "turn_start") -> dict[str, Any]:
    """冻结本轮实际可见资源版本，便于恢复和排查前后资源漂移。"""
    ensure_asset_resources(session)
    ws = workspace(session)
    store = _store(session)["resources"]
    ids = list(dict.fromkeys(
        list(ws.get("asset_ids") or [])
        + list(ws.get("evidence_ids") or [])
        + list(ws.get("tool_ids") or [])
        + list(ws.get("skill_ids") or [])
    ))
    rows = []
    for rid in ids:
        row = store.get(rid)
        if not isinstance(row, dict) or row.get("status") not in USABLE_STATUSES:
            continue
        rows.append({
            "resource_id": rid,
            "type": row.get("type"),
            "version": row.get("version"),
            "status": row.get("status"),
            "content_hash": row.get("content_hash"),
        })
    digest = hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()[:16]
    snap = {
        "snapshot_id": f"rs_{digest}",
        "reason": str(reason),
        "created_at": _now(),
        "current_frame_id": str(ws.get("current_frame_id") or ""),
        "resources": rows,
    }
    history = session.artifacts.setdefault(SNAPSHOTS_KEY, [])
    if not isinstance(history, list):
        history = []
        session.artifacts[SNAPSHOTS_KEY] = history
    if not history or history[-1].get("snapshot_id") != snap["snapshot_id"]:
        history.append(snap)
        del history[:-20]
    return snap
