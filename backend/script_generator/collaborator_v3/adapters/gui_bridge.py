"""Opt-in bridge from the existing collaborative GUI payload to V3."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from datetime import datetime, timezone
from uuid import uuid4

from core.path import PROJECT_ROOT

from ..bootstrap import initialize_v3
from ..domain.contracts import PermissionGrantRecord
from ..domain.models import ResourceRef, ViewportSnapshot
from ..domain.validate import is_placeholder_task_objective, looks_corrupted_text
from ..interaction.facade import V3RuntimeFacade
from ..runtime.runner import ActionRunner
from .provider_model import ProviderLogicModel
from .codegen import V2CodeGenerator
from .session_assets import sync_session_assets, sync_viewport_overlay
from .script_validator import V2ScriptValidator
from .skill_capabilities import sync_skill_capabilities
from .v2_capabilities import LegacyResolverExecutor, sync_v2_capabilities


def _grant_allowed(action: str, permissions: dict[str, Any]) -> bool:
    if action == "create_reversible":
        return bool(permissions.get("probe_script") or permissions.get("write_files"))
    if action == "modify":
        return bool(permissions.get("write_files"))
    if action == "external_side_effect":
        return bool(permissions.get("runtime_control"))
    return False


#: 只有这两种状态的轮次才能 resume（见 TurnMachine.resume）。
_RESUMABLE_TURN_STATES = {"paused", "waiting_user"}


def _turn_is_resumable(store, turn_id: str) -> bool:
    """那个续跑指针还指得动吗。

    旧会话里可能留着一个已作废的指针：`map_view_update_to_v2` 会 pop 掉
    `v3_waiting_turn`，但 GUI 回写 artifacts 用的是 `update()`（只增不删），
    pop 传不过去，于是下一句话会被判成"续跑一个已经 committed 的轮次"，
    直接抛 INVALID_STATE_TRANSITION，用户只看到「✗ 失败」。
    这里先问一句轮次状态，指不动就丢弃指针、开新主轮。
    """
    try:
        turn = store.get_turn(str(turn_id or ""))
    except Exception:
        return False
    status = getattr(turn, "status", None)
    return str(getattr(status, "value", status) or "") in _RESUMABLE_TURN_STATES


def _drop_stale_resume_pointer(artifacts: dict[str, Any]) -> None:
    artifacts.pop("v3_waiting_turn", None)
    artifacts.pop("v3_resume_token", None)


def _sync_grants(store, task_id: str, permissions: dict[str, Any]) -> None:
    for capability in store.search_capabilities(task_id=task_id, kind="tool"):
        allowed = tuple(
            action for action in capability.permission_actions if _grant_allowed(action, permissions)
        )
        if set(allowed) != set(capability.permission_actions) or not allowed:
            continue
        key = hashlib.sha256(
            f"{task_id}|{capability.id}|{capability.version}|{','.join(allowed)}".encode("utf-8")
        ).hexdigest()[:16]
        store.put_grant(
            PermissionGrantRecord(
                id=f"grant_{key}", capability_id=capability.id,
                capability_version=capability.version, actions=allowed,
                task_id=task_id, expires_kind="task",
                environment_fingerprint="default", target_fingerprint="default",
            )
        )


def map_view_update_to_v2(update, artifacts: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Translate authoritative V3 output into the existing GUI message contract."""
    artifacts["v3_task_id"] = update.task_id or ""
    artifacts["v3_turn_id"] = update.turn_id
    artifacts["v3_runtime_status"] = update.status
    meta: dict[str, Any] = {"turn": "ask" if update.interaction else "stop", "_trace": []}
    if update.interaction:
        artifacts["v3_waiting_turn"] = update.turn_id
        artifacts["v3_resume_token"] = update.resume_token or ""
        raw_options = list(update.interaction.get("options") or [])
        meta["chips"] = [
            str(x.get("label") or x.get("id") or "") for x in raw_options
        ]
        # 芯片的结构化形态（id + label 都在）。它留在会话里，用户点芯片时
        # GUI 才能回传"点的是哪一个"，而不用把点击降级成一句话再猜。
        artifacts["v3_chip_options"] = [
            {"id": str(x.get("id") or ""), "label": str(x.get("label") or "")}
            for x in raw_options
            if isinstance(x, dict)
        ]
        if update.interaction.get("reason_code") == "LOGIC_DRAFT_CONFIRMATION":
            meta["logic_draft"] = list(update.logic_draft)
            meta["done_when"] = "等待用户确认初版脚本逻辑"
            if not meta["chips"]:
                meta["chips"] = ["确认初版逻辑", "需要修改"]
    elif update.status == "paused":
        if update.pause_reason in ("TOOL_NO_PROGRESS", "COMPLETION_NOT_PROVEN"):
            artifacts.pop("v3_waiting_turn", None)
            artifacts.pop("v3_resume_token", None)
            meta["turn"] = "stop"
            meta["chips"] = []
        else:
            artifacts["v3_waiting_turn"] = update.turn_id
            artifacts["v3_resume_token"] = ""
            meta["turn"] = "ask"
            meta["chips"] = ["继续"]
    else:
        artifacts.pop("v3_waiting_turn", None)
        artifacts.pop("v3_resume_token", None)
        meta["task_done"] = update.status == "committed"
    if update.trial_page_ready:
        meta["turn"] = "ask"
        meta["chips"] = ["去试运行"]
        meta["task_done"] = True
    final = update.final_response or {}
    for key in ("logic_draft", "script", "intro", "script_name", "plan", "done_when"):
        if key in final:
            meta[key] = final[key]
    return update.status_text, meta


def run_v3_gui_turn(params: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any], int, int]:
    """Return the same shape as CollabLlmWorker expects.

    V3 state is stored independently.  The V2 session is retained only as the
    current GUI/material view and tool-resolver compatibility object.

    这里是 GUI 触发 V3 主轮的**唯一真实入口**：声明样本来源为 ``real_user``，
    影子闸的 readiness 只统计这种样本（测试/探针里调到这里也会被判成 test/probe，
    见 ``shadow.detect_sample_origin``）。
    """
    from backend.script_generator.collaborator_v3.runtime.shadow import (
        SAMPLE_REAL_USER,
        set_sample_origin,
    )

    set_sample_origin(SAMPLE_REAL_USER)
    from backend.script_generator.collaborator.llm_turn import make_tool_resolver
    from backend.script_generator.collaborator.session import CollabSession
    from backend.script_generator.collaborator.session_workspace import (
        record_event,
        record_subturn,
    )
    from backend.script_generator.collaborator.skills.registry import default_registry

    session = CollabSession.from_dict(dict(params.get("session") or {}))
    session_id = str(session.session_id or "")
    permissions = dict(params.get("permissions") or {})
    # 执行目标事实（决定 §1）：GUI 线程用账号 + Controller 实例解析好，这里只做透传。
    # 拿不到就是 unresolved —— runtime 会用注解 provisional 兜底，绝不静默猜。
    execution_target = dict(params.get("execution_target") or {}) or None
    services = initialize_v3(PROJECT_ROOT / "user_data" / "collaborator_v3")
    # 工具和技能进同一张能力表：同一个 capability.search / capability.get 就能查到。
    sync_v2_capabilities(services.store)
    sync_skill_capabilities(services.store)

    resolver = make_tool_resolver(
        default_registry(), session,
        primary_vision={
            "provider": params.get("provider"), "api_key": params.get("api_key"),
            "model": params.get("model"), "api_endpoint": params.get("api_endpoint"),
        },
        vision_assist=params.get("vision_assist"), permissions=permissions,
        # Phase B2：视觉证据的 L2 权威源就是 V3 的 evidence 表（跨主轮复用）
        evidence_store=services.store,
    )
    active_task: dict[str, str] = {
        "task_id": str((session.artifacts or {}).get("v3_task_id") or "")
    }

    def _sync_after_tool(_request, _result) -> None:
        if active_task["task_id"]:
            sync_session_assets(
                services.store, list(session.assets or []),
                task_id=active_task["task_id"], turn_id="tool_result",
            )

    executor = LegacyResolverExecutor(resolver, on_success=_sync_after_tool)
    model = ProviderLogicModel(
        provider=str(params.get("provider") or ""), api_key=str(params.get("api_key") or ""),
        model=str(params.get("model") or ""), api_endpoint=params.get("api_endpoint"),
        max_tokens=params.get("max_tokens"), on_partial=params.get("on_partial"),
    )

    def _sync_task(task_id: str, turn_id: str) -> None:
        active_task["task_id"] = task_id
        sync_session_assets(
            services.store, list(session.assets or []), task_id=task_id, turn_id=turn_id
        )
        _sync_grants(services.store, task_id, permissions)

    supplement_box = params.get("supplement_box")

    def _take_supplements(workspace) -> list[dict[str, Any]]:
        if supplement_box is None or not hasattr(supplement_box, "drain_context"):
            return []
        raw_items = supplement_box.drain_context()
        out: list[dict[str, Any]] = []
        for item in raw_items:
            context = item.get("context") if isinstance(item.get("context"), dict) else {}
            frame_path = str(context.get("frame_path") or "")
            base = None
            for asset in session.assets or []:
                if str(asset.get("path") or "") == frame_path and asset.get("id"):
                    latest = services.store.latest_resource_version(str(asset["id"]))
                    if latest:
                        base = ResourceRef(str(asset["id"]), latest)
                    break
            revision = max(1, int(context.get("revision") or 1))
            overlay_resource = sync_viewport_overlay(
                services.store,
                list(context.get("overlays") or []),
                task_id=workspace.task_id,
                turn_id=workspace.turn_id,
            )
            selections = []
            for asset_id in context.get("focused_asset_ids") or []:
                latest = services.store.latest_resource_version(str(asset_id))
                if latest:
                    selections.append(ResourceRef(str(asset_id), latest))
            snapshot = ViewportSnapshot(
                id=f"snapshot_{uuid4().hex}", turn_id=workspace.turn_id,
                revision=revision, base=base,
                overlays=(ResourceRef(overlay_resource.id, overlay_resource.version),)
                if overlay_resource else (),
                selections=tuple(selections),
                captured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            services.store.put_viewport_snapshot(snapshot)
            out.append(
                {
                    "text": str(item.get("text") or ""),
                    "snapshot_id": snapshot.id,
                    "viewport_changed": snapshot.id != workspace.viewport_snapshot_id,
                }
            )
        return out

    runner = ActionRunner(
        store=services.store, model=model, executor=executor,
        on_task_established=_sync_task,
        supplement_provider=_take_supplements,
        on_progress=params.get("on_status"),
        on_subturn=(
            (lambda record: record_subturn(session_id, record)) if session_id else None
        ),
        script_validator=V2ScriptValidator(session),
        enforce_requirement_contract=True,
        # 经典生成器仅作备用：默认 LLM 手写；用户点名传统状态机或多次校验失败才启用。
        code_generator=V2CodeGenerator(
            session,
            provider=str(params.get("provider") or ""),
            api_key=str(params.get("api_key") or ""),
            model=str(params.get("model") or ""),
            api_endpoint=params.get("api_endpoint"),
            max_tokens=params.get("max_tokens"),
            vision_assist=params.get("vision_assist"),
            on_status=params.get("on_status"),
        ),
    )
    facade = V3RuntimeFacade(runner)
    artifacts = session.artifacts if isinstance(session.artifacts, dict) else {}
    session.artifacts = artifacts
    task_id = str(artifacts.get("v3_task_id") or "") or None
    if task_id:
        task = services.store.get_task(task_id)
        recovery_objective = str(session.goal_text or "").strip()
        if (
            (looks_corrupted_text(task.objective) or is_placeholder_task_objective(task.objective))
            and recovery_objective
            and not looks_corrupted_text(recovery_objective)
        ):
            services.store.repair_task_objective(
                task_id,
                recovery_objective,
                expected_revision=task.revision,
                source="legacy_session.goal_text",
            )
        _sync_task(task_id, str(artifacts.get("v3_turn_id") or "import"))
    else:
        # The start snapshot may refer to an image before a Task exists.  Import
        # it as turn-local/unscoped so viewport.get_current -> resource.get is
        # still valid; task establishment re-imports it into task scope.
        sync_session_assets(
            services.store,
            list(session.assets or []),
            task_id=None,
            turn_id="main_turn_start",
        )

    last_user = ""
    for message in reversed(session.messages or []):
        if str(message.get("role") or "") == "user":
            last_user = str(message.get("text") or "")
            break

    # 素材区是"最基本的资源"：即使还没有 Task（所以资源没有 task 作用域），
    # 也必须让模型查得到。把本轮素材 id 显式交给 Runtime。
    session_asset_ids = tuple(
        str(asset.get("id"))
        for asset in (session.assets or [])
        if isinstance(asset, dict) and asset.get("id")
    )

    waiting_turn = str(artifacts.get("v3_waiting_turn") or "")
    if waiting_turn and not _turn_is_resumable(services.store, waiting_turn):
        # 指针作废（轮次已提交/已取消，或根本查不到）：别再拿它 resume，
        # 否则只会换来一个 INVALID_STATE_TRANSITION 失败气泡。
        _drop_stale_resume_pointer(artifacts)
        waiting_turn = ""
    if waiting_turn:
        # 用户可能点的是我们递出去的芯片。那"点了哪一个"是已知的，
        # 别丢了它再去猜那句话的意思。
        selection = artifacts.get("v3_chip_selection") or {}
        option_id = ""
        if isinstance(selection, dict) and str(selection.get("turn_id") or "") == waiting_turn:
            option_id = str(selection.get("option_id") or "")
        update = facade.resume(
            turn_id=waiting_turn, text=last_user,
            resume_token=str(artifacts.get("v3_resume_token") or "") or None,
            session_asset_ids=session_asset_ids,
            option_id=option_id,
            execution_target=execution_target,
        )
    else:
        base = None
        frame = str(session.frame_path or "")
        for asset in session.assets or []:
            if str(asset.get("path") or "") == frame and asset.get("id"):
                latest = services.store.latest_resource_version(str(asset["id"]))
                base = ResourceRef(str(asset["id"]), latest or 1)
                break
        overlay_resource = sync_viewport_overlay(
            services.store,
            list(session.overlays or []),
            task_id=task_id,
            turn_id="main_turn_start",
        )
        selections = []
        for asset_id in session.focused_asset_ids or []:
            latest = services.store.latest_resource_version(str(asset_id))
            if latest:
                selections.append(ResourceRef(str(asset_id), latest))
        update = facade.send_message(
            last_user,
            task_id=task_id,
            base=base,
            overlays=(ResourceRef(overlay_resource.id, overlay_resource.version),)
            if overlay_resource else (),
            selections=tuple(selections),
            session_asset_ids=session_asset_ids,
            execution_target=execution_target,
        )

    visible, meta = map_view_update_to_v2(update, artifacts)
    if session_id:
        # 主轮生命周期留痕：与副轮轨迹放在同一个工作区里，便于对着时间线复现。
        record_event(
            session_id,
            "V3MainTurnFinished",
            {
                "turn_id": update.turn_id,
                "task_id": update.task_id,
                "status": update.status,
                "pause_reason": update.pause_reason,
                "checkpoint_id": update.checkpoint_id,
                "model": model.model_id,
                "input_tokens": model.input_tokens,
                "output_tokens": model.output_tokens,
                "user_text": last_user,
            },
        )
    return visible, meta, session.to_dict(), model.input_tokens, model.output_tokens
