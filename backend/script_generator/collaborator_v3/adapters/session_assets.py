"""Import the current V2 material rail into task-scoped V3 resources."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..domain.enums import ResourceStatus
from ..domain.models import ResourceRecord, ResourceRef
from ..stores.sqlite_store import SqliteDomainStore


def _file_hash(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"
    except OSError:
        return None


def sync_session_assets(
    store: SqliteDomainStore,
    assets: list[dict[str, Any]],
    *,
    task_id: str | None,
    turn_id: str,
) -> list[ResourceRecord]:
    imported: list[ResourceRecord] = []
    for raw in assets:
        if not isinstance(raw, dict):
            continue
        resource_id = str(raw.get("id") or "").strip()
        path_text = str(raw.get("path") or "").strip()
        if not resource_id or not path_text:
            continue
        path = Path(path_text)
        digest = _file_hash(path)
        if digest is None:
            continue
        latest = store.latest_resource_version(resource_id)
        if latest is not None:
            existing = store.get_resource(ResourceRef(resource_id, latest))
            if existing.content_hash == digest and existing.task_id == task_id:
                imported.append(existing)
                continue
            version = latest + 1
        else:
            version = 1
        role = str(raw.get("role") or "template")
        # Phase B4（设计 §4.6）：素材自己声明的**业务语义**（`semantics`/`semantic_tags`）。
        # 只有资源声明了语义，Runtime 才拦得住"勋章模板当空槽模板"这类错误；没声明就
        # 不拦（记 RESOURCE_SEMANTIC_UNVERIFIED 遥测）。
        declared_semantics = [
            str(x) for x in (raw.get("semantics") or raw.get("semantic_tags") or ()) if str(x)
        ]
        resource = ResourceRecord(
            id=resource_id,
            type="overlay" if role in {"annotation", "probe_out"} else "image",
            version=version,
            revision=1,
            status=ResourceStatus.ACTIVE,
            display_name=str(raw.get("name") or path.name),
            content_path=str(path),
            content_hash=digest,
            source_kind="legacy_session",
            source_ref=str(raw.get("group") or "material_rail"),
            metadata={
                "role": role,
                "caption": str(raw.get("caption") or ""),
                **({"semantics": declared_semantics} if declared_semantics else {}),
            },
            reusable_scope="task" if task_id else "none",
            task_id=task_id,
            created_in_turn=turn_id,
        )
        store.put_resource(resource)
        imported.append(resource)
    return imported


def sync_viewport_overlay(
    store: SqliteDomainStore,
    overlays: list[dict[str, Any]],
    *,
    task_id: str | None,
    turn_id: str,
) -> ResourceRecord | None:
    """Persist the non-destructive annotation layer as a content-addressed resource."""
    normalized = [dict(item) for item in overlays if isinstance(item, dict)]
    if not normalized:
        return None
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    resource_id = f"res_view_overlay_{digest[:20]}"
    latest = store.latest_resource_version(resource_id)
    if latest is not None:
        return store.get_resource(ResourceRef(resource_id, latest))
    resource = ResourceRecord(
        id=resource_id,
        type="annotation_set",
        version=1,
        revision=1,
        status=ResourceStatus.ACTIVE,
        display_name="共同视口标记层",
        content_hash=f"sha256:{digest}",
        source_kind="viewport_overlay",
        metadata={"overlays": normalized},
        reusable_scope="task" if task_id else "none",
        task_id=task_id,
        created_in_turn=turn_id,
    )
    store.put_resource(resource)
    return resource
