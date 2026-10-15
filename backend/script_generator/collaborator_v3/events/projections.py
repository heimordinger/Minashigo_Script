"""最小投影：把"当前状态"从既有表里读出来，不让模型自己重放历史（设计 §18.3）。

设计要点：

- **只读**既有事实表（``tasks`` / ``resources`` / ``commits`` / 凭证文件 / 试运行记录），
  不新建第二套存储；投影是可重建的（幂等）。
- 老记录保持可读：缺少 hash/version 的老行会被标注 ``legacy=True``，
  **不伪造成新式可信记录**（设计 §28）。
- 主轮开始时只给模型一张**短索引**，投影按需查询（设计 §21.3）。

对外入口：:func:`projection_index` 给短索引，:func:`project` 取某一张投影。
"""

from __future__ import annotations

from typing import Any, Mapping

#: 可用的投影名（短索引用它告诉模型"去哪儿查"）。
PROJECTION_NAMES: tuple[str, ...] = (
    "current_task",
    "current_resources",
    "latest_script",
    "latest_validation",
    "latest_trial",
    "outstanding_blockers",
    # 设计 §24.6：新主轮要能查"尚未验证的模型判断"（明确标记），
    # 以及用户对外部现实的说法 —— 两者都**不是事实**，必须分得开。
    "hypothesis",
    "user_claims",
)


def _claims_projection(store: Any, task_id: str, *, layer: str) -> dict[str, Any]:
    """未验证主张的投影（`hypothesis` / `user_claims` 共用）。

    只列 **candidate**（已 `verified` 的不属于"尚未验证"），并明确标注
    `verified: false` 与那句"不是事实"的提示 —— 设计 §24.6 要求"明确标记"，
    否则下一主轮会把推测当成已知。
    """
    getter = getattr(store, "list_claims", None)
    rows: list[dict[str, Any]] = []
    if callable(getter):
        try:
            rows = list(getter(task_id, layer=layer) or [])
        except Exception:  # noqa: BLE001
            rows = []
    if not task_id:
        return {"available": False, "reason": "NO_TASK_ID", "items": []}
    if not rows:
        return {
            "available": False,
            "reason": "NO_CLAIMS",
            "task_id": task_id,
            "layer": layer,
            "items": [],
            "note": "当前任务没有这类未验证主张",
        }
    return {
        "available": True,
        "task_id": task_id,
        "layer": layer,
        "count": len(rows),
        "items": rows,
        "note": (
            "以下都是**未验证主张**（verified=false），**不是事实**；"
            "不要当成已知条件，需要时先取得证据或请用户确认"
        ),
    }


def _task_projection(store: Any, task_id: str) -> dict[str, Any]:
    if not task_id:
        return {"available": False, "reason": "NO_TASK_ID"}
    task = store.get_task(task_id)
    if task is None:
        return {"available": False, "reason": "TASK_NOT_FOUND", "task_id": task_id}
    return {
        "available": True,
        "task_id": str(task.id),
        "objective": str(task.objective or ""),
        "status": task.status.value,
        "revision": int(getattr(task, "revision", 0) or 0),
        "plan_revision": int(getattr(task, "plan_revision", 0) or 0),
        "constraints": list(getattr(task, "constraints", ()) or ()),
        "acceptance_criteria": list(getattr(task, "acceptance_criteria", ()) or ()),
    }


def _resource_projection(store: Any, task_id: str, *, limit: int = 20) -> dict[str, Any]:
    try:
        found = store.search_resources(task_id=task_id or None, limit=limit)
    except Exception:  # noqa: BLE001 - 不同 store 版本的签名差异
        found = []
    items = []
    for item in found or ():
        items.append(
            {
                "id": str(getattr(item, "id", "") or ""),
                "version": int(getattr(item, "version", 0) or 0),
                "type": str(getattr(item, "type", "") or ""),
                "name": str(getattr(item, "display_name", "") or ""),
                "content_hash": str(getattr(item, "content_hash", "") or ""),
                "reusable_scope": str(getattr(item, "reusable_scope", "") or ""),
                # 老素材没有 version/hash → 标 legacy，不假装是新式资源身份
                "legacy": not bool(getattr(item, "content_hash", "")),
            }
        )
    return {"available": True, "count": len(items), "items": items}


def _latest_script_projection(store: Any, task_id: str) -> dict[str, Any]:
    """上次交付的脚本（验收场景 5：新主轮没有旧上下文也要找得到）。

    ⚠️ 两个曾经的硬伤（都会**静默**给出"没有剧本"的错误结论）：

    * `store.list_commits` 以前不存在，`hasattr` 守卫把它吃成空列表 →
      投影永远返回 ``NO_COMMIT``；
    * 脚本正文在 ``result["final_response"]["script"]``，不是 ``result["script"]``
      （与 ``SqliteDomainStore.latest_committed_script`` 保持一致）。
    """
    getter = getattr(store, "list_commits", None)
    commits = []
    if callable(getter):
        try:
            commits = list(getter(task_id) or [])
        except Exception:  # noqa: BLE001
            commits = []
    if not commits:
        return {"available": False, "reason": "NO_COMMIT", "task_id": task_id}

    latest = commits[0] or {}
    payload = dict(latest.get("result") or {})
    final = payload.get("final_response")
    final = dict(final) if isinstance(final, dict) else {}
    script = final.get("script")
    script = script if isinstance(script, str) else ""
    return {
        "available": True,
        "commit_id": str(latest.get("id") or ""),
        "turn_id": str(latest.get("turn_id") or ""),
        "created_at": str(latest.get("created_at") or ""),
        "has_script": bool(script.strip()),
        "script_length": len(script),
        "script_name": str(final.get("script_name") or ""),
        # 由正文算出的 hash（供与凭证/试运行证据比对）；**不是**记录自带的字段
        "script_sha256": _sha256_text(script) if script.strip() else "",
        # 提交行本身没带 hash → 老式记录，不冒充"新式可信凭证"（设计 §28）
        "legacy": not bool(final.get("script_sha256")),
    }


def _sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(str(text or "").strip().encode("utf-8")).hexdigest()


def _validation_projection(task_id: str) -> dict[str, Any]:
    from ..runtime import validation_store

    receipt = validation_store.latest_receipt(task_id) if task_id else None
    if receipt is None:
        return {"available": False, "reason": "NO_RECEIPT", "task_id": task_id}
    return {
        "available": True,
        "validation_id": receipt.validation_id,
        "runtime_profile": receipt.runtime_profile,
        "profile_source": receipt.profile_source,
        "target_revision": receipt.target_revision,
        "artifact_sha256": receipt.artifact_sha256,
        "source_sha256": receipt.source_sha256,
        "bundled_sha256": receipt.bundled_sha256,
        "script_name": receipt.script_name,
        "checks": dict(receipt.checks),
        "created_at": receipt.created_at,
    }


def _trial_projection(task_id: str) -> dict[str, Any]:
    from ..runtime import trial_runs

    try:
        run = trial_runs.latest_run(task_id) if task_id else None
    except Exception:  # noqa: BLE001
        run = None
    if not run:
        # 空试运行必须给原因码，不能只有 run: null（设计 §17.1）
        return {
            "available": False,
            "status": "empty",
            "reason": "NO_RUN_FOR_CURRENT_TASK" if task_id else "NO_TASK_ID",
            "task_id": task_id,
            "recommended_next_action": "请用户重新试运行以采集运行证据",
        }
    return {
        "available": True,
        "run_id": str(run.get("run_id") or ""),
        "status": str(run.get("status") or ""),
        "script_name": str(run.get("script_name") or ""),
        "account": str(run.get("account") or ""),
        "started_at": str(run.get("started_at") or ""),
    }


def _blockers_projection(
    store: Any, task_id: str, *, validation: Mapping[str, Any], trial: Mapping[str, Any]
) -> dict[str, Any]:
    """仍未解决的阻塞：缺脚本 / 缺凭证 / 缺试运行证据 / 缺确认。"""
    out: list[dict[str, str]] = []
    task = _task_projection(store, task_id)
    if not task.get("available"):
        out.append({"code": "TASK_MISSING", "message": "还没有建立任务"})
    if not validation.get("available"):
        out.append({"code": "VALIDATION_MISSING", "message": "还没有通过校验的产物凭证"})
    if not trial.get("available"):
        out.append(
            {
                "code": "TRIAL_EVIDENCE_MISSING",
                "message": "还没有试运行证据（静态校验不能证明运行行为）",
            }
        )
    return {"available": True, "count": len(out), "items": out}


def projection_index(task_id: str = "", *, task_revision: int = 0, objective: str = "") -> dict[str, Any]:
    """主轮开始时的**短索引**：告诉模型去哪儿查，而不是把全部上下文塞给它。"""
    return {
        "task_id": str(task_id or ""),
        "task_revision": int(task_revision or 0),
        "objective_summary": str(objective or "")[:200],
        "available_projections": list(PROJECTION_NAMES),
    }


def project(store: Any, name: str, task_id: str = "") -> dict[str, Any]:
    """取一张投影（其余投影按需查询，设计 §21.4）。"""
    key = str(name or "").strip()
    if key not in PROJECTION_NAMES:
        return {
            "available": False,
            "reason": "UNKNOWN_PROJECTION",
            "requested": key,
            "available_projections": list(PROJECTION_NAMES),
        }
    if key == "current_task":
        return _task_projection(store, task_id)
    if key == "current_resources":
        return _resource_projection(store, task_id)
    if key == "latest_script":
        return _latest_script_projection(store, task_id)
    if key == "latest_validation":
        return _validation_projection(task_id)
    if key == "latest_trial":
        return _trial_projection(task_id)
    if key == "hypothesis":
        return _claims_projection(store, task_id, layer="hypothesis")
    if key == "user_claims":
        return _claims_projection(store, task_id, layer="user_claim")
    validation = _validation_projection(task_id)
    trial = _trial_projection(task_id)
    return _blockers_projection(store, task_id, validation=validation, trial=trial)


def all_projections(store: Any, task_id: str = "") -> dict[str, dict[str, Any]]:
    """一次取全部（排障/体检用；**不要**每轮都塞给模型）。"""
    return {name: project(store, name, task_id) for name in PROJECTION_NAMES}
