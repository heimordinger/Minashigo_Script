"""SQLite persistence for the V3 domain.

The adapter intentionally exposes a narrow API.  Runtime code will eventually
write through Committer; direct methods here are primarily P0 bootstrap tools.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping

from ..domain.contracts import (
    CapabilityRecord,
    CheckpointRecord,
    CommitProposal,
    InteractionRequestRecord,
    PermissionGrantRecord,
)
from ..domain.enums import ClaimStatus, EvidenceKind, ResourceStatus, RuntimePhase, TaskStatus, TurnStatus
from ..domain.errors import DomainError, ErrorCode
from ..domain.models import (
    MainTurnRecord,
    ResourceRecord,
    ResourceRef,
    SubturnRecord,
    TaskRecord,
    ViewportSnapshot,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


#: ``contract_tickets`` 的列顺序（migration 6）。写在 store 里而不是各处 INSERT：
#: 唯一一份列清单，避免两个写入口慢慢漂移。
_TICKET_COLUMNS: tuple[str, ...] = (
    "ticket_id",
    "schema_version",
    "main_turn_id",
    "view_event_id",
    "contract_revision",
    "capability_id",
    "receiver_type",
    "requested_method",
    "resolved_owner",
    "resolution_chain_json",
    "runtime_profile",
    "profile_source",
    "call_kind",
    "parameters_required_json",
    "parameters_allowed_json",
    "return_model_json",
    "permission_id",
    "permission_scope_json",
    "resource_scope_json",
    "environment_revision",
    "issued_at",
    "integrity",
    "revoked_at",
    "revoked_reason",
)

#: 语义上"没有值"的可空列：写入时空串必须落成 NULL。
#: 否则 ``revoked_at IS NULL`` 这类判定会**静默失效**（老坑：字段缺失 ≠ 字段为空）。
_NULLABLE_TICKET_COLUMNS: tuple[str, ...] = (
    "main_turn_id",
    "view_event_id",
    "revoked_at",
    "revoked_reason",
)


class SqliteDomainStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.path), timeout=5.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA journal_mode = DELETE")
        con.execute("PRAGMA synchronous = FULL")
        con.execute("PRAGMA busy_timeout = 5000")
        return con

    def initialize(self) -> None:
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self.connect() as con:
            con.executescript(schema)
            con.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)",
                (_utc_now(),),
            )
            checkpoint_columns = {
                str(row["name"])
                for row in con.execute("PRAGMA table_info(checkpoints)").fetchall()
            }
            if "facts_json" not in checkpoint_columns:
                con.execute(
                    "ALTER TABLE checkpoints ADD COLUMN facts_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "runtime_json" not in checkpoint_columns:
                con.execute(
                    "ALTER TABLE checkpoints ADD COLUMN runtime_json TEXT NOT NULL DEFAULT '{}'"
                )
            con.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(2, ?)",
                (_utc_now(),),
            )
            con.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(3, ?)",
                (_utc_now(),),
            )
            con.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(4, ?)",
                (_utc_now(),),
            )
            # ── migration 5：层级 ID 列（设计 §14.2/§14.3）──────────────
            # 老库缺列就补，列可为 NULL（legacy 记录不得冒充新式可信凭证）。
            turn_columns = {
                str(row["name"])
                for row in con.execute("PRAGMA table_info(main_turns)").fetchall()
            }
            if turn_columns and "run_id" not in turn_columns:
                con.execute("ALTER TABLE main_turns ADD COLUMN run_id TEXT")
            event_columns = {
                str(row["name"])
                for row in con.execute("PRAGMA table_info(domain_events)").fetchall()
            }
            for column in ("run_id", "subturn_id", "action_id"):
                if event_columns and column not in event_columns:
                    con.execute(f"ALTER TABLE domain_events ADD COLUMN {column} TEXT")
            # 引用新列的索引必须等 ALTER 之后再建（见 schema.sql 末尾注释）。
            con.execute(
                "CREATE INDEX IF NOT EXISTS ix_events_subturn "
                "ON domain_events(subturn_id, sequence)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS ix_events_run "
                "ON domain_events(run_id, sequence)"
            )
            con.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(5, ?)",
                (_utc_now(),),
            )
            # ── migration 6：契约凭据表（设计 §6/§11）────────────────────
            # 表与索引由 schema.sql 的 CREATE ... IF NOT EXISTS 建好（老库同样生效，
            # 上面 executescript 已经跑过），这里只登记版本号，让迁移可审计。
            # ticket 用**独立表**而不是文件：它要跨副轮查询、失效和审计。
            con.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(6, ?)",
                (_utc_now(),),
            )
            # ── migration 7：主轮任务定向列（设计 §3 / Phase A）────────────
            # 老库升级只按"列在不在"补齐（与版本号无关），所以 ALTER 用列存在性判定；
            # 版本号按**顺序分配**，不做预留：以后视觉证据迁移（设计 §5）实施时用 8。
            if turn_columns and "task_type" not in turn_columns:
                con.execute("ALTER TABLE main_turns ADD COLUMN task_type TEXT")
            if turn_columns and "intent_json" not in turn_columns:
                con.execute(
                    "ALTER TABLE main_turns ADD COLUMN intent_json TEXT NOT NULL DEFAULT '{}'"
                )
            if turn_columns and "intent_revision" not in turn_columns:
                con.execute(
                    "ALTER TABLE main_turns ADD COLUMN intent_revision INTEGER NOT NULL DEFAULT 0"
                )
            con.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(7, ?)",
                (_utc_now(),),
            )
            # ── migration 8：视觉证据指纹（设计 §5 / Phase B2）────────────
            # ``fingerprint`` 可空：老证据行没有指纹就是 NULL（**不伪造**），
            # 新写入的视觉证据带上它；跨主轮按 (kind, fingerprint, status) 查。
            evidence_columns = {
                str(row["name"])
                for row in con.execute("PRAGMA table_info(evidence)").fetchall()
            }
            if evidence_columns and "fingerprint" not in evidence_columns:
                con.execute("ALTER TABLE evidence ADD COLUMN fingerprint TEXT")
            con.execute(
                "CREATE INDEX IF NOT EXISTS ix_evidence_fingerprint "
                "ON evidence(kind, fingerprint, status)"
            )
            con.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(8, ?)",
                (_utc_now(),),
            )
            # ── migration 9：副轮结构化决策留痕 ──────────────────────────
            # 不增加模型必填字段；Runtime 把动作、依据、重复次数与结果摘要自动写入。
            # 老副轮没有这些事实，保留空对象，绝不回填猜测。
            subturn_columns = {
                str(row["name"])
                for row in con.execute("PRAGMA table_info(subturns)").fetchall()
            }
            if subturn_columns and "trace_json" not in subturn_columns:
                con.execute(
                    "ALTER TABLE subturns ADD COLUMN trace_json TEXT NOT NULL DEFAULT '{}'"
                )
            con.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(9, ?)",
                (_utc_now(),),
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def put_task(self, task: TaskRecord, *, expected_revision: int | None = None) -> None:
        with self.transaction() as con:
            row = con.execute("SELECT revision FROM tasks WHERE id = ?", (task.id,)).fetchone()
            if row is not None and expected_revision is not None and row["revision"] != expected_revision:
                raise DomainError(ErrorCode.REVISION_CONFLICT, f"task {task.id} revision changed")
            if row is None and expected_revision not in (None, 0):
                raise DomainError(ErrorCode.REVISION_CONFLICT, f"task {task.id} does not exist")
            con.execute(
                """
                INSERT INTO tasks(
                    id, objective, constraints_json, acceptance_json, status,
                    current_step_id, plan_revision, revision, created_at,
                    updated_at, schema_version
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    objective=excluded.objective,
                    constraints_json=excluded.constraints_json,
                    acceptance_json=excluded.acceptance_json,
                    status=excluded.status,
                    current_step_id=excluded.current_step_id,
                    plan_revision=excluded.plan_revision,
                    revision=excluded.revision,
                    updated_at=excluded.updated_at,
                    schema_version=excluded.schema_version
                """,
                (
                    task.id,
                    task.objective,
                    canonical_json(task.constraints),
                    canonical_json(task.acceptance_criteria),
                    task.status.value,
                    task.current_step_id,
                    task.plan_revision,
                    task.revision,
                    task.created_at,
                    task.updated_at,
                    task.schema_version,
                ),
            )

    def put_turn(self, turn: MainTurnRecord) -> None:
        with self.transaction() as con:
            con.execute(
                """
                INSERT INTO main_turns(
                    id, task_id, user_event_id, status, phase, active_snapshot_id,
                    checkpoint_id, commit_id, model_id, model_version,
                    started_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    task_id=excluded.task_id,
                    status=excluded.status,
                    phase=excluded.phase,
                    active_snapshot_id=excluded.active_snapshot_id,
                    checkpoint_id=excluded.checkpoint_id,
                    commit_id=excluded.commit_id,
                    model_id=excluded.model_id,
                    model_version=excluded.model_version,
                    updated_at=excluded.updated_at
                """,
                (
                    turn.id,
                    turn.task_id,
                    turn.user_event_id,
                    turn.status.value,
                    turn.phase.value,
                    turn.active_snapshot_id,
                    turn.checkpoint_id,
                    turn.commit_id,
                    turn.model_id,
                    turn.model_version,
                    turn.started_at,
                    turn.updated_at,
                ),
            )

    def put_viewport_snapshot(self, snapshot: ViewportSnapshot) -> None:
        with self.transaction() as con:
            con.execute(
                """
                INSERT INTO viewport_snapshots(
                    id, turn_id, revision, base_id, base_version, overlays_json,
                    zoom, pan_x, pan_y, selections_json, captured_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.id, snapshot.turn_id, snapshot.revision,
                    snapshot.base.id if snapshot.base else None,
                    snapshot.base.version if snapshot.base else None,
                    canonical_json(snapshot.overlays), snapshot.zoom, snapshot.pan_x,
                    snapshot.pan_y, canonical_json(snapshot.selections), snapshot.captured_at,
                ),
            )

    def get_viewport_snapshot(self, snapshot_id: str) -> ViewportSnapshot:
        with self.connect() as con:
            row = con.execute("SELECT * FROM viewport_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise DomainError(ErrorCode.INVALID_ARGUMENTS, "viewport snapshot not found")
        base = ResourceRef(row["base_id"], row["base_version"]) if row["base_id"] else None
        overlays = tuple(ResourceRef(str(x["id"]), int(x["version"])) for x in json.loads(row["overlays_json"]))
        selections = tuple(ResourceRef(str(x["id"]), int(x["version"])) for x in json.loads(row["selections_json"]))
        return ViewportSnapshot(
            id=row["id"], turn_id=row["turn_id"], revision=row["revision"], base=base,
            overlays=overlays, zoom=row["zoom"], pan_x=row["pan_x"], pan_y=row["pan_y"],
            selections=selections, captured_at=row["captured_at"],
        )

    def get_turn(self, turn_id: str) -> MainTurnRecord:
        with self.connect() as con:
            row = con.execute("SELECT * FROM main_turns WHERE id = ?", (turn_id,)).fetchone()
        if row is None:
            raise DomainError(ErrorCode.TURN_NOT_FOUND, f"turn {turn_id} not found")
        return MainTurnRecord(
            id=row["id"], task_id=row["task_id"], user_event_id=row["user_event_id"],
            status=TurnStatus(row["status"]), phase=RuntimePhase(row["phase"]),
            active_snapshot_id=row["active_snapshot_id"], checkpoint_id=row["checkpoint_id"],
            commit_id=row["commit_id"], model_id=row["model_id"], model_version=row["model_version"],
            task_type=str(row["task_type"] or "") if "task_type" in row.keys() else "",
            intent_json=str(row["intent_json"] or "{}") if "intent_json" in row.keys() else "{}",
            intent_revision=(
                int(row["intent_revision"] or 0) if "intent_revision" in row.keys() else 0
            ),
            started_at=row["started_at"], updated_at=row["updated_at"],
        )

    def set_turn_intent(
        self,
        turn_id: str,
        *,
        task_type: str,
        intent: Any,
        revision: int,
    ) -> None:
        """写入主轮任务定向结论（设计 §3，唯一写入口）。

        ``intent`` 收 ``TurnIntent``（或等价的 dict）：这里用 ``canonical_json``
        落盘，读侧只认 ``main_turns`` 一处事实源，不再另存一份到 workspace。
        """
        payload = intent.to_dict() if hasattr(intent, "to_dict") else dict(intent or {})
        with self.transaction() as con:
            con.execute(
                "UPDATE main_turns SET task_type=?, intent_json=?, intent_revision=? WHERE id=?",
                (
                    str(task_type or "") or None,
                    canonical_json(payload),
                    int(revision or 0),
                    str(turn_id),
                ),
            )

    # ── 层级 ID：Run / Subturn（设计 §14.1/§14.2）─────────────────────

    def set_turn_run(self, turn_id: str, run_id: str) -> None:
        """记下主轮最近一次执行的 run_id；恢复时被新的 run_id 覆盖。"""
        with self.transaction() as con:
            con.execute(
                "UPDATE main_turns SET run_id=? WHERE id=?",
                (str(run_id or "") or None, turn_id),
            )

    def get_turn_run(self, turn_id: str) -> str:
        with self.connect() as con:
            row = con.execute(
                "SELECT run_id FROM main_turns WHERE id=?", (turn_id,)
            ).fetchone()
        return str((row["run_id"] if row else "") or "")

    def put_subturn(self, subturn: SubturnRecord) -> bool:
        """登记一次副轮身份；返回 True 表示这次真的写进去了。

        ``INSERT OR IGNORE`` 是刻意的：``(main_turn_id, seq)`` 上有唯一索引，
        同一身份重复登记（崩溃后重放、或有人拿同一个序号换一个 id 来伪造）都不
        会产生第二行、也不会覆盖第一行——**首次记录为准**，审计上不会被改写。
        """
        with self.transaction() as con:
            cursor = con.execute(
                """
                INSERT OR IGNORE INTO subturns(
                    subturn_id, main_turn_id, run_id, seq, index_in_run,
                    action_type, action_id, outcome, trace_json, created_at, schema_version
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    subturn.subturn_id,
                    subturn.main_turn_id,
                    str(subturn.run_id or "") or None,
                    int(subturn.seq),
                    None if subturn.index_in_run is None else int(subturn.index_in_run),
                    str(subturn.action_type or ""),
                    str(subturn.action_id or ""),
                    str(subturn.outcome or ""),
                    canonical_json(dict(subturn.trace or {})),
                    subturn.created_at or _utc_now(),
                    int(subturn.schema_version),
                ),
            )
            return cursor.rowcount > 0

    def get_subturn(self, subturn_id: str) -> SubturnRecord | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM subturns WHERE subturn_id=?", (subturn_id,)
            ).fetchone()
        return self._subturn_row(row) if row is not None else None

    def list_subturns(self, main_turn_id: str) -> list[SubturnRecord]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM subturns WHERE main_turn_id=? ORDER BY seq", (main_turn_id,)
            ).fetchall()
        return [self._subturn_row(row) for row in rows]

    def max_subturn_seq(self, main_turn_id: str) -> int:
        """已登记的最大副轮序号（0 = 还没登记过）。恢复时用来对齐工作区计数。"""
        with self.connect() as con:
            row = con.execute(
                "SELECT MAX(seq) AS n FROM subturns WHERE main_turn_id=?", (main_turn_id,)
            ).fetchone()
        return int((row["n"] if row else 0) or 0)

    def run_ids_for_turn(self, main_turn_id: str) -> list[str]:
        """该主轮出现过的 run_id（按首次出现顺序），用于核对"恢复必须换 run"。"""
        with self.connect() as con:
            rows = con.execute(
                "SELECT run_id FROM subturns WHERE main_turn_id=? AND run_id IS NOT NULL "
                "GROUP BY run_id ORDER BY MIN(seq)",
                (main_turn_id,),
            ).fetchall()
        return [str(row["run_id"]) for row in rows]

    @staticmethod
    def _subturn_row(row: sqlite3.Row) -> SubturnRecord:
        return SubturnRecord(
            subturn_id=str(row["subturn_id"]),
            main_turn_id=str(row["main_turn_id"]),
            run_id=str(row["run_id"] or ""),
            seq=int(row["seq"]),
            index_in_run=None if row["index_in_run"] is None else int(row["index_in_run"]),
            action_type=str(row["action_type"] or ""),
            action_id=str(row["action_id"] or ""),
            outcome=str(row["outcome"] or ""),
            trace=json.loads(str(row["trace_json"] or "{}")),
            created_at=str(row["created_at"] or ""),
            schema_version=int(row["schema_version"]),
        )

    def put_interaction(self, interaction: InteractionRequestRecord) -> None:
        with self.transaction() as con:
            self._insert_interaction(con, interaction)

    def _insert_interaction(
        self, con: sqlite3.Connection, interaction: InteractionRequestRecord
    ) -> None:
        con.execute(
            """
            INSERT INTO interactions(
                id, turn_id, reason_code, question, reason, options_json,
                allow_free_text, blocking, resume_token_hash, status
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interaction.id, interaction.turn_id, interaction.reason_code,
                interaction.question, interaction.reason, canonical_json(interaction.options),
                int(interaction.allow_free_text), int(interaction.blocking),
                interaction.resume_token_hash, interaction.status,
            ),
        )

    def resolve_interaction(self, interaction_id: str) -> None:
        with self.transaction() as con:
            cur = con.execute(
                "UPDATE interactions SET status = 'resolved' WHERE id = ? AND status = 'open'",
                (interaction_id,),
            )
            if cur.rowcount != 1:
                raise DomainError(ErrorCode.INVALID_ARGUMENTS, "interaction is not open")

    def put_checkpoint(self, checkpoint: CheckpointRecord) -> None:
        with self.transaction() as con:
            con.execute(
                """
                INSERT INTO checkpoints(
                    id, turn_id, task_id, phase, viewport_snapshot_id,
                    completed_steps_json, acquired_refs_json, facts_json, runtime_json,
                    unresolved_interaction_id, budget_json, resume_phase, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.id, checkpoint.turn_id, checkpoint.task_id, checkpoint.phase,
                    checkpoint.viewport_snapshot_id, canonical_json(checkpoint.completed_step_ids),
                    canonical_json(checkpoint.acquired_refs), canonical_json(checkpoint.fact_snapshot),
                    canonical_json(checkpoint.runtime_snapshot),
                    checkpoint.unresolved_interaction_id,
                    canonical_json(checkpoint.budget_summary), checkpoint.resume_phase,
                    checkpoint.created_at,
                ),
            )

    def get_checkpoint(self, checkpoint_id: str) -> CheckpointRecord:
        with self.connect() as con:
            row = con.execute("SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,)).fetchone()
        if row is None:
            raise DomainError(ErrorCode.INVALID_ARGUMENTS, "checkpoint not found")
        return CheckpointRecord(
            id=row["id"], turn_id=row["turn_id"], task_id=row["task_id"], phase=row["phase"],
            viewport_snapshot_id=row["viewport_snapshot_id"],
            completed_step_ids=tuple(json.loads(row["completed_steps_json"])),
            acquired_refs=tuple(json.loads(row["acquired_refs_json"])),
            fact_snapshot=json.loads(row["facts_json"]),
            runtime_snapshot=json.loads(row["runtime_json"]),
            unresolved_interaction_id=row["unresolved_interaction_id"],
            budget_summary=json.loads(row["budget_json"]), resume_phase=row["resume_phase"],
            created_at=row["created_at"],
        )

    def get_interaction(self, interaction_id: str) -> InteractionRequestRecord:
        with self.connect() as con:
            row = con.execute("SELECT * FROM interactions WHERE id=?", (interaction_id,)).fetchone()
        if row is None:
            raise DomainError(ErrorCode.INVALID_ARGUMENTS, "interaction not found")
        return InteractionRequestRecord(
            id=row["id"], turn_id=row["turn_id"], reason_code=row["reason_code"],
            question=row["question"], reason=row["reason"],
            options=tuple(json.loads(row["options_json"])),
            allow_free_text=bool(row["allow_free_text"]), blocking=bool(row["blocking"]),
            resume_token_hash=row["resume_token_hash"], status=row["status"],
        )

    def put_capability(self, capability: CapabilityRecord) -> None:
        with self.transaction() as con:
            con.execute(
                """
                INSERT INTO capabilities(
                    id, version, kind, name, input_schema_json,
                    output_schema_json, permission_actions_json, cost_class,
                    availability, scope, task_id, metadata_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id, version) DO UPDATE SET
                    availability=excluded.availability,
                    metadata_json=excluded.metadata_json
                """,
                (
                    capability.id, capability.version, capability.kind, capability.name,
                    canonical_json(capability.input_schema), canonical_json(capability.output_schema),
                    canonical_json(capability.permission_actions), capability.cost_class,
                    capability.availability, capability.scope, capability.task_id,
                    canonical_json(capability.metadata),
                ),
            )

    def get_capability(self, capability_id: str, version: str) -> CapabilityRecord:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM capabilities WHERE id=? AND version=?",
                (capability_id, version),
            ).fetchone()
        if row is None:
            raise DomainError(ErrorCode.CAPABILITY_NOT_FOUND, f"capability {capability_id}@{version} not found")
        return CapabilityRecord(
            id=row["id"], version=row["version"], kind=row["kind"], name=row["name"],
            input_schema=json.loads(row["input_schema_json"]),
            output_schema=json.loads(row["output_schema_json"]),
            permission_actions=tuple(json.loads(row["permission_actions_json"])),
            cost_class=row["cost_class"], availability=row["availability"],
            scope=row["scope"], task_id=row["task_id"], metadata=json.loads(row["metadata_json"]),
        )

    def latest_capability_version(self, capability_id: str) -> str | None:
        """取某能力当前可发现的版本；历史/退役版本不作为默认值。"""
        with self.connect() as con:
            row = con.execute(
                "SELECT version FROM capabilities "
                "WHERE id=? AND availability='available' ORDER BY rowid DESC LIMIT 1",
                (str(capability_id),),
            ).fetchone()
        return str(row["version"]) if row else None

    def reconcile_capability_catalog(
        self,
        *,
        id_prefix: str,
        current: set[tuple[str, str]],
    ) -> None:
        """把一个同步源的目录收敛为“当前版本”，同时保留历史行用于复现。

        ``capability.search`` 只应展示本次同步仍存在的版本；旧版本不能因为数据库
        是追加式存储就继续诱导模型。精确 ``get(id, version)`` 仍可读取历史记录。
        """
        pairs = {(str(cap_id), str(version)) for cap_id, version in current}
        current_ids = {cap_id for cap_id, _version in pairs}
        with self.transaction() as con:
            rows = con.execute(
                "SELECT id, version, availability FROM capabilities WHERE id LIKE ?",
                (f"{str(id_prefix)}%",),
            ).fetchall()
            for row in rows:
                pair = (str(row["id"]), str(row["version"]))
                if pair in pairs:
                    desired = "available"
                elif pair[0] in current_ids:
                    desired = "superseded"
                else:
                    desired = "retired"
                if str(row["availability"]) != desired:
                    con.execute(
                        "UPDATE capabilities SET availability=? WHERE id=? AND version=?",
                        (desired, pair[0], pair[1]),
                    )

    def search_capabilities(self, *, task_id: str | None, kind: str | None = None) -> list[CapabilityRecord]:
        where = ["availability = 'available'", "(scope = 'global' OR task_id = ?)"]
        params: list[Any] = [task_id]
        if kind:
            where.append("kind = ?")
            params.append(kind)
        with self.connect() as con:
            rows = con.execute(
                f"SELECT id, version FROM capabilities WHERE {' AND '.join(where)} ORDER BY name, version",
                params,
            ).fetchall()
        return [self.get_capability(row["id"], row["version"]) for row in rows]

    def put_grant(self, grant: PermissionGrantRecord) -> None:
        with self.transaction() as con:
            con.execute(
                """
                INSERT INTO permission_grants(
                    id, capability_id, capability_version, actions_json, task_id,
                    resource_refs_json, stage_id, batch_id, expires_kind,
                    expires_value, environment_fingerprint, target_fingerprint,
                    revision, revoked_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    actions_json=excluded.actions_json,
                    resource_refs_json=excluded.resource_refs_json,
                    revision=excluded.revision,
                    revoked_at=excluded.revoked_at
                """,
                (
                    grant.id, grant.capability_id, grant.capability_version,
                    canonical_json(grant.actions), grant.task_id,
                    canonical_json(grant.resource_refs), grant.stage_id, grant.batch_id,
                    grant.expires_kind, grant.expires_value, grant.environment_fingerprint,
                    grant.target_fingerprint, grant.revision, grant.revoked_at,
                ),
            )

    def get_grant(self, grant_id: str) -> PermissionGrantRecord:
        with self.connect() as con:
            row = con.execute("SELECT * FROM permission_grants WHERE id=?", (grant_id,)).fetchone()
        if row is None:
            raise DomainError(ErrorCode.PERMISSION_REQUIRED, f"grant {grant_id} not found")
        refs = tuple(ResourceRef(str(x["id"]), int(x["version"])) for x in json.loads(row["resource_refs_json"]))
        return PermissionGrantRecord(
            id=row["id"], capability_id=row["capability_id"],
            capability_version=row["capability_version"], actions=tuple(json.loads(row["actions_json"])),
            task_id=row["task_id"], resource_refs=refs, stage_id=row["stage_id"],
            batch_id=row["batch_id"], expires_kind=row["expires_kind"],
            expires_value=row["expires_value"], environment_fingerprint=row["environment_fingerprint"],
            target_fingerprint=row["target_fingerprint"], revision=row["revision"],
            revoked_at=row["revoked_at"],
        )

    def find_grants(self, *, task_id: str, capability_id: str) -> list[PermissionGrantRecord]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT id FROM permission_grants
                WHERE task_id=? AND capability_id=? AND revoked_at IS NULL
                ORDER BY id
                """,
                (task_id, capability_id),
            ).fetchall()
        return [self.get_grant(str(row["id"])) for row in rows]

    def consume_single_grants(self, grant_ids: tuple[str, ...]) -> None:
        if not grant_ids:
            return
        with self.transaction() as con:
            now = _utc_now()
            for grant_id in grant_ids:
                con.execute(
                    """
                    UPDATE permission_grants SET revoked_at=?, revision=revision+1
                    WHERE id=? AND expires_kind='single' AND revoked_at IS NULL
                    """,
                    (now, grant_id),
                )

    # ── 契约凭据 ticket（migration 6，设计 §6/§11/§12）──────────────────
    #
    # ticket 由**本地**签发（api_contracts.ticket_service），这里只是它的持久化：
    # 行本身才是权威，模型手里的 ticket_id 必须能查到这一行才算数。
    # 权限只存引用与作用域摘要，事实本体仍在 permission_grants（表在上面）。

    def put_ticket(self, row: Mapping[str, Any]) -> None:
        """写一张 ticket；同 id 覆盖（签发幂等，身份就是 ticket_id）。

        **失效是粘性的**：重复写同一张票不得把已有的 ``revoked_at`` 抹掉，
        否则"撤销后再签发一次"就能悄悄复活旧凭据。
        """
        data = {key: row.get(key) for key in _TICKET_COLUMNS}
        data["schema_version"] = int(data["schema_version"] or 1)
        for column in _NULLABLE_TICKET_COLUMNS:
            if data[column] == "":
                data[column] = None
        placeholders = ", ".join("?" for _ in _TICKET_COLUMNS)
        updates = ", ".join(
            f"{column}=excluded.{column}"
            for column in _TICKET_COLUMNS
            if column != "ticket_id"
        )
        with self.transaction() as con:
            con.execute(
                f"""
                INSERT INTO contract_tickets({", ".join(_TICKET_COLUMNS)})
                VALUES({placeholders})
                ON CONFLICT(ticket_id) DO UPDATE SET
                    {updates},
                    revoked_at=COALESCE(excluded.revoked_at, contract_tickets.revoked_at),
                    revoked_reason=COALESCE(
                        excluded.revoked_reason, contract_tickets.revoked_reason
                    )
                """,
                tuple(data[column] for column in _TICKET_COLUMNS),
            )

    def get_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        """按 id 取 ticket 行；不存在返回 ``None``（**不抛**：调用方要判 missing）。"""
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM contract_tickets WHERE ticket_id=?", (str(ticket_id),)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_tickets(
        self,
        *,
        main_turn_id: str = "",
        capability_id: str = "",
        include_revoked: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if main_turn_id:
            where.append("main_turn_id = ?")
            params.append(str(main_turn_id))
        if capability_id:
            where.append("capability_id = ?")
            params.append(str(capability_id))
        if not include_revoked:
            where.append("revoked_at IS NULL")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(max(1, int(limit)))
        with self.connect() as con:
            rows = con.execute(
                f"SELECT * FROM contract_tickets{clause} "
                "ORDER BY issued_at DESC, ticket_id LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke_ticket(self, ticket_id: str, *, reason: str, at: str = "") -> bool:
        """本地失效一张票（保留行，写入撤销原因）。已撤销的票返回 ``False``。"""
        with self.transaction() as con:
            cursor = con.execute(
                """
                UPDATE contract_tickets
                SET revoked_at=?, revoked_reason=?
                WHERE ticket_id=? AND revoked_at IS NULL
                """,
                (str(at or _utc_now()), str(reason or ""), str(ticket_id)),
            )
            return int(cursor.rowcount or 0) > 0

    def get_task(self, task_id: str) -> TaskRecord:
        with self.connect() as con:
            row = con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise DomainError(ErrorCode.TASK_NOT_FOUND, f"task {task_id} not found")
        return TaskRecord(
            id=row["id"],
            objective=row["objective"],
            constraints=tuple(json.loads(row["constraints_json"])),
            acceptance_criteria=tuple(json.loads(row["acceptance_json"])),
            status=TaskStatus(row["status"]),
            current_step_id=row["current_step_id"],
            plan_revision=row["plan_revision"],
            revision=row["revision"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            schema_version=row["schema_version"],
        )

    def get_task_facts(self, task_id: str) -> dict[str, dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT key, value_json FROM task_facts WHERE task_id=? ORDER BY key",
                (task_id,),
            ).fetchall()
        return {str(row["key"]): dict(json.loads(row["value_json"])) for row in rows}

    def repair_task_objective(
        self,
        task_id: str,
        objective: str,
        *,
        expected_revision: int,
        source: str,
    ) -> TaskRecord:
        """Audited recovery for previously persisted corrupt provider text."""
        with self.transaction() as con:
            row = con.execute("SELECT revision FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise DomainError(ErrorCode.TASK_NOT_FOUND, f"task {task_id} not found")
            if int(row["revision"]) != expected_revision:
                raise DomainError(ErrorCode.REVISION_CONFLICT, f"task {task_id} revision changed")
            con.execute(
                "UPDATE tasks SET objective=?, revision=revision+1, updated_at=? WHERE id=?",
                (objective, _utc_now(), task_id),
            )
            self._append_event(
                con,
                event_id=f"event_repair_{task_id}_{expected_revision}",
                event_type="TaskObjectiveRepaired",
                task_id=task_id,
                turn_id=None,
                aggregate_id=task_id,
                payload={"source": source, "old_revision": expected_revision},
            )
        return self.get_task(task_id)

    def establish_task_for_turn(self, task: TaskRecord, turn_id: str) -> None:
        """Atomically attach a newly validated task to an active main turn."""
        with self.transaction() as con:
            turn = con.execute("SELECT task_id FROM main_turns WHERE id=?", (turn_id,)).fetchone()
            if turn is None:
                raise DomainError(ErrorCode.TURN_NOT_FOUND, f"turn {turn_id} not found")
            if turn["task_id"]:
                raise DomainError(ErrorCode.COMMIT_CONFLICT, "turn already has a task")
            con.execute(
                """
                INSERT INTO tasks(
                    id, objective, constraints_json, acceptance_json, status,
                    current_step_id, plan_revision, revision, created_at,
                    updated_at, schema_version
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id, task.objective, canonical_json(task.constraints),
                    canonical_json(task.acceptance_criteria), task.status.value,
                    task.current_step_id, task.plan_revision, task.revision,
                    task.created_at, task.updated_at, task.schema_version,
                ),
            )
            con.execute("UPDATE main_turns SET task_id=?, updated_at=? WHERE id=?", (task.id, _utc_now(), turn_id))
            self._append_event(
                con,
                event_id=f"event_task_started_{task.id}",
                event_type="TaskStarted",
                task_id=task.id,
                turn_id=turn_id,
                aggregate_id=task.id,
                payload={"objective": task.objective},
            )

    def put_resource(self, resource: ResourceRecord) -> None:
        with self.transaction() as con:
            self._insert_resource(con, resource)

    def _insert_resource(self, con: sqlite3.Connection, resource: ResourceRecord) -> None:
        con.execute(
            """
            INSERT INTO resources(
                id, version, revision, type, status, display_name, content_path,
                content_hash, source_kind, source_ref, metadata_json,
                reusable_scope, task_id, created_in_turn, schema_version
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resource.id,
                resource.version,
                resource.revision,
                resource.type,
                resource.status.value,
                resource.display_name,
                resource.content_path,
                resource.content_hash,
                resource.source_kind,
                resource.source_ref,
                canonical_json(resource.metadata),
                resource.reusable_scope,
                resource.task_id,
                resource.created_in_turn,
                resource.schema_version,
            ),
        )
        for parent in resource.derived_from:
            con.execute(
                """
                INSERT INTO resource_parents(
                    resource_id, resource_version, parent_id, parent_version
                ) VALUES(?, ?, ?, ?)
                """,
                (resource.id, resource.version, parent.id, parent.version),
            )

    def get_resource(self, ref: ResourceRef) -> ResourceRecord:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM resources WHERE id = ? AND version = ?",
                (ref.id, ref.version),
            ).fetchone()
            parents = con.execute(
                """
                SELECT parent_id, parent_version FROM resource_parents
                WHERE resource_id = ? AND resource_version = ?
                ORDER BY parent_id, parent_version
                """,
                (ref.id, ref.version),
            ).fetchall()
        if row is None:
            raise DomainError(ErrorCode.RESOURCE_NOT_FOUND, f"resource {ref.id}@{ref.version} not found")
        return ResourceRecord(
            id=row["id"],
            type=row["type"],
            version=row["version"],
            revision=row["revision"],
            status=ResourceStatus(row["status"]),
            display_name=row["display_name"],
            content_path=row["content_path"],
            content_hash=row["content_hash"],
            source_kind=row["source_kind"],
            source_ref=row["source_ref"],
            derived_from=tuple(ResourceRef(p["parent_id"], p["parent_version"]) for p in parents),
            metadata=json.loads(row["metadata_json"]),
            reusable_scope=row["reusable_scope"],
            task_id=row["task_id"],
            created_in_turn=row["created_in_turn"],
            schema_version=row["schema_version"],
        )

    def latest_resource_version(self, resource_id: str) -> int | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT MAX(version) AS version FROM resources WHERE id=?", (resource_id,)
            ).fetchone()
        return int(row["version"]) if row and row["version"] is not None else None

    def list_resource_versions(self, resource_id: str) -> list[ResourceRecord]:
        """列出某个资源 id 的**所有**版本（升序）。

        Phase B 的统一 ResourceBinder 需要它：只有拿到全部版本，才能区分
        "资源不存在" / "版本已被取代（RESOURCE_VERSION_EXPIRED）" / "版本号根本没登记"。
        只查最新版本会把这三种情况混成一句"not found"。
        """
        rid = str(resource_id or "").strip()
        if not rid:
            return []
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM resources WHERE id=? ORDER BY version", (rid,)
            ).fetchall()
            parents = con.execute(
                "SELECT resource_version, parent_id, parent_version FROM resource_parents "
                "WHERE resource_id=? ORDER BY resource_version, parent_id, parent_version",
                (rid,),
            ).fetchall()
        by_version: dict[int, list[Any]] = {}
        for item in parents:
            by_version.setdefault(int(item["resource_version"]), []).append(item)
        out: list[ResourceRecord] = []
        for row in rows:
            version = int(row["version"])
            out.append(
                ResourceRecord(
                    id=row["id"],
                    type=row["type"],
                    version=version,
                    revision=row["revision"],
                    status=ResourceStatus(row["status"]),
                    display_name=row["display_name"],
                    content_path=row["content_path"],
                    content_hash=row["content_hash"],
                    source_kind=row["source_kind"],
                    source_ref=row["source_ref"],
                    derived_from=tuple(
                        ResourceRef(p["parent_id"], p["parent_version"])
                        for p in by_version.get(version, ())
                    ),
                    metadata=json.loads(row["metadata_json"]),
                    reusable_scope=row["reusable_scope"],
                    task_id=row["task_id"],
                    created_in_turn=row["created_in_turn"],
                    schema_version=row["schema_version"],
                )
            )
        return out

    def search_resources(
        self,
        *,
        task_id: str | None,
        resource_type: str | None = None,
        include_global: bool = False,
        limit: int = 50,
        extra_ids: tuple[str, ...] = (),
        query: str | None = None,
        role: str | None = None,
    ) -> list[ResourceRecord]:
        if limit < 1 or limit > 200:
            raise DomainError(ErrorCode.INVALID_ARGUMENTS, "resource search limit must be 1..200")
        where = ["status IN ('candidate', 'active', 'verified')"]
        params: list[Any] = []
        # 作用域顺序：当前任务 → 本主轮显式引用（素材区）→ 明确可复用的全局资源。
        # 注意 task_id 可能是 None（还没有 Task）：此时不能拼 `task_id = NULL`，
        # 那会永远为假，把"素材区有图却查不到"变成静默空结果。
        scope_clauses: list[str] = []
        scope_params: list[Any] = []
        if task_id is not None:
            scope_clauses.append("task_id = ?")
            scope_params.append(task_id)
        if extra_ids:
            placeholders = ",".join("?" for _ in extra_ids)
            scope_clauses.append(f"id IN ({placeholders})")
            scope_params.extend(str(x) for x in extra_ids)
        if include_global:
            scope_clauses.append("reusable_scope = 'global'")
        if not scope_clauses:
            return []
        where.append("(" + " OR ".join(scope_clauses) + ")")
        params.extend(scope_params)
        if resource_type:
            where.append("type = ?")
            params.append(resource_type)
        term = str(query or "").strip()
        if term:
            where.append("(display_name LIKE ? OR id LIKE ?)")
            like = f"%{term}%"
            params.extend((like, like))
        role_term = str(role or "").strip()
        if role_term:
            where.append("json_extract(metadata_json, '$.role') = ?")
            params.append(role_term)
        # ORDER BY 需要在 LIMIT 之前再补一次任务 id；task_id 为 None 时用不可能命中的
        # 空串占位，保持列数一致（scope_clauses 为空已提前返回，所以这里必有子句）。
        order_task_id = task_id if task_id is not None else ""
        params.append(order_task_id)
        params.append(limit)
        query_sql = f"""
            SELECT id, version FROM resources
            WHERE {' AND '.join(where)}
            ORDER BY CASE WHEN task_id = ? THEN 0 ELSE 1 END, id, version DESC
            LIMIT ?
        """
        with self.connect() as con:
            rows = con.execute(query_sql, params).fetchall()
        return [self.get_resource(ResourceRef(row["id"], row["version"])) for row in rows]

    def find_resources_by_display_name(
        self,
        name: str,
        *,
        task_id: str | None = None,
        current_turn_id: str = "",
        visible_resource_ids: tuple[str, ...] = (),
        include_global: bool = True,
        limit: int = 50,
    ) -> list[ResourceRecord]:
        """在当前可见作用域内按显示名称**精确**查找资源。

        这是模型名称层的本地解析入口；不做 LIKE/相似度、不扫描磁盘。返回多个不同
        id 时由 Binder 报名称歧义，调用方不得任选。
        """
        wanted = str(name or "").strip()
        if not wanted:
            return []
        scopes: list[str] = []
        params: list[Any] = [wanted]
        if task_id is not None:
            scopes.append("task_id = ?")
            params.append(str(task_id))
        if current_turn_id:
            scopes.append("created_in_turn = ?")
            params.append(str(current_turn_id))
        visible_ids = tuple(dict.fromkeys(str(x or "").strip() for x in visible_resource_ids if str(x or "").strip()))
        if visible_ids:
            scopes.append("id IN (" + ",".join("?" for _ in visible_ids) + ")")
            params.extend(visible_ids)
        if include_global:
            scopes.append("reusable_scope = 'global'")
        if not scopes:
            return []
        params.append(int(limit))
        with self.connect() as con:
            rows = con.execute(
                "SELECT id, version FROM resources "
                "WHERE status IN ('candidate','active','verified') "
                "AND display_name = ? AND (" + " OR ".join(scopes) + ") "
                "ORDER BY id, version DESC LIMIT ?",
                params,
            ).fetchall()
        return [self.get_resource(ResourceRef(row["id"], row["version"])) for row in rows]

    def list_commits(self, task_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """某任务的提交记录（新→旧），每项 ``{"id", "turn_id", "created_at", "result"}``。

        为什么需要：`latest_script` 投影本来要按它找"上次交付的脚本"，
        但访问器**根本不存在**，`hasattr` 守卫把缺失静默吃掉了 ——
        投影于是永远报 ``NO_COMMIT``（验收场景 5 因此不成立）。

        ``result`` 是提交时落库的原始字典（形状见 ``Committer.commit``：
        ``{"commit_id", "turn_id", "final_response", "memory_summary"}``；
        **脚本正文在 ``result["final_response"]["script"]``**）。
        读不出来时返回空列表，不猜。
        """
        import json as _json

        key = str(task_id or "").strip()
        if not key:
            return []
        try:
            with self.connect() as con:
                rows = con.execute(
                    "SELECT id, turn_id, result_json, created_at FROM commits "
                    "WHERE task_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (key, max(1, int(limit or 20))),
                ).fetchall()
        except Exception:  # noqa: BLE001 - 老库/缺表时不阻断
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                result = _json.loads(row["result_json"] or "{}")
            except (TypeError, ValueError):
                result = {}
            out.append(
                {
                    "id": str(row["id"]),
                    "turn_id": str(row["turn_id"] or ""),
                    "created_at": str(row["created_at"] or ""),
                    "result": result if isinstance(result, dict) else {},
                }
            )
        return out

    def list_claims(
        self, task_id: str, *, layer: str = "", status: str = "", limit: int = 20
    ) -> list[dict[str, Any]]:
        """某任务的候选主张（新→旧），每项含 ``predicate`` / ``value`` / ``layer`` /
        ``verified`` / ``subject``（``{"id","version"}`` 或 None）。

        为什么需要：设计 §24.6 要求新主轮能查"尚未验证的模型判断"（hypothesis 投影），
        而 claims 表以前**只有写入没有读取**。分层的 `layer` 存在 ``scope_json`` 里。

        ⚠️ claims 表**没有 task_id 列**：作用域靠 ``created_in_turn`` → ``main_turns``
        关联（这也是"不引入第二条真相"的做法）。读不出来返回空列表，不猜。
        """
        import json as _json

        key = str(task_id or "").strip()
        if not key:
            return []
        sql = (
            "SELECT c.id, c.predicate, c.value_json, c.confidence, c.status, "
            "c.scope_json, c.revision, c.created_in_turn, c.subject_id, c.subject_version "
            "FROM claims c JOIN main_turns t ON t.id = c.created_in_turn "
            "WHERE t.task_id=?"
        )
        params: list[Any] = [key]
        if status:
            sql += " AND c.status=?"
            params.append(str(status))
        sql += " ORDER BY c.rowid DESC LIMIT ?"
        params.append(max(1, int(limit or 20)))
        try:
            with self.connect() as con:
                rows = con.execute(sql, tuple(params)).fetchall()
        except Exception:  # noqa: BLE001 - 老库/缺表时不阻断
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                scope = _json.loads(row["scope_json"] or "{}")
            except (TypeError, ValueError):
                scope = {}
            try:
                value = _json.loads(row["value_json"] or "null")
            except (TypeError, ValueError):
                value = None
            scope = scope if isinstance(scope, dict) else {}
            item = {
                "id": str(row["id"]),
                "predicate": str(row["predicate"]),
                "value": value,
                "confidence": row["confidence"],
                "status": str(row["status"]),
                "verified": str(row["status"]) == ClaimStatus.VERIFIED.value,
                "layer": str(scope.get("layer") or ""),
                "subject_class": str(scope.get("subject_class") or ""),
                "proposer": str(scope.get("proposer") or ""),
                "resource_refs": [str(x) for x in (scope.get("resource_refs") or ())],
                "created_in_turn": str(row["created_in_turn"] or ""),
                "revision": int(row["revision"] or 1),
                "subject": (
                    {
                        "id": str(row["subject_id"]),
                        "version": int(row["subject_version"] or 0),
                    }
                    if row["subject_id"]
                    else None
                ),
            }
            if layer and item["layer"] != str(layer):
                continue
            out.append(item)
        return out

    def latest_committed_script(self, task_id: str) -> tuple[str, str]:
        """最近一次提交里交付的**脚本正文**；返回 ``(code, commit_id)``，没有则 ``("", "")``。

        只读。用于"用户要求只替换资源/局部修复"时**自动绑定基线**（设计 §15.1）：
        拿不到就返回空，**不猜、不编造**基线。
        """
        import json as _json

        key = str(task_id or "").strip()
        if not key:
            return "", ""
        try:
            with self.connect() as con:
                rows = con.execute(
                    "SELECT id, result_json FROM commits WHERE task_id=? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 20",
                    (key,),
                ).fetchall()
        except Exception:  # noqa: BLE001 - 老库/缺表时不阻断
            return "", ""
        for row in rows:
            try:
                payload = _json.loads(row["result_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            final = payload.get("final_response")
            script = final.get("script") if isinstance(final, dict) else ""
            if isinstance(script, str) and script.strip():
                return script, str(row["id"])
        return "", ""

    def record_commit(
        self,
        *,
        commit_id: str,
        idempotency_key: str,
        turn_id: str,
        task_id: str | None,
        request: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        request_json = canonical_json(request)
        request_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        with self.transaction() as con:
            existing = con.execute(
                "SELECT request_hash, result_json FROM commits WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise DomainError(ErrorCode.COMMIT_CONFLICT, "idempotency key reused with different request")
                return json.loads(existing["result_json"])
            con.execute(
                """
                INSERT INTO commits(
                    id, idempotency_key, request_hash, task_id, turn_id,
                    result_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    commit_id,
                    idempotency_key,
                    request_hash,
                    task_id,
                    turn_id,
                    canonical_json(result),
                    _utc_now(),
                ),
            )
            self._append_event(
                con,
                event_id=f"event_{commit_id}",
                event_type="MainTurnCommitted",
                task_id=task_id,
                turn_id=turn_id,
                aggregate_id=commit_id,
                payload={"idempotency_key": idempotency_key},
            )
        return result

    def apply_commit(
        self,
        *,
        commit_id: str,
        proposal: CommitProposal,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically publish a validated proposal and its commit event."""
        request_json = canonical_json(proposal)
        request_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        with self.transaction() as con:
            existing = con.execute(
                "SELECT request_hash, result_json FROM commits WHERE idempotency_key = ?",
                (proposal.idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise DomainError(ErrorCode.COMMIT_CONFLICT, "idempotency key reused with different proposal")
                return json.loads(existing["result_json"])

            turn = con.execute(
                "SELECT task_id FROM main_turns WHERE id = ?", (proposal.turn_id,)
            ).fetchone()
            if turn is None:
                raise DomainError(ErrorCode.TURN_NOT_FOUND, f"turn {proposal.turn_id} not found")
            task_id = turn["task_id"]
            if task_id and proposal.expected_task_revision is not None:
                task = con.execute("SELECT revision FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if task is None:
                    raise DomainError(ErrorCode.TASK_NOT_FOUND, f"task {task_id} not found")
                if task["revision"] != proposal.expected_task_revision:
                    raise DomainError(ErrorCode.REVISION_CONFLICT, f"task {task_id} revision changed")

            if proposal.task_patch:
                if not task_id:
                    raise DomainError(ErrorCode.TASK_NOT_FOUND, "turn has no task")
                allowed = {"objective", "status", "current_step_id", "constraints", "acceptance_criteria"}
                unknown = set(proposal.task_patch) - allowed
                if unknown:
                    raise DomainError(
                        ErrorCode.INVALID_ARGUMENTS,
                        "task patch contains unsupported fields",
                        {"fields": sorted(unknown)},
                    )
                row = con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                patch = proposal.task_patch
                con.execute(
                    """
                    UPDATE tasks SET objective=?, constraints_json=?, acceptance_json=?,
                        status=?, current_step_id=?, revision=revision+1, updated_at=?
                    WHERE id=?
                    """,
                    (
                        patch.get("objective", row["objective"]),
                        canonical_json(patch.get("constraints", json.loads(row["constraints_json"]))),
                        canonical_json(patch.get("acceptance_criteria", json.loads(row["acceptance_json"]))),
                        patch.get("status", row["status"]),
                        patch.get("current_step_id", row["current_step_id"]),
                        _utc_now(),
                        task_id,
                    ),
                )

            for resource in proposal.resources_to_create:
                self._insert_resource(con, resource)
            for evidence in proposal.evidence_to_create:
                con.execute(
                    """
                    INSERT INTO evidence(
                        id, kind, subject_id, subject_version, payload_json,
                        producer_capability_id, producer_version, confidence,
                        status, turn_id, created_at, schema_version, fingerprint
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evidence.id, evidence.kind.value,
                        evidence.subject.id if evidence.subject else None,
                        evidence.subject.version if evidence.subject else None,
                        canonical_json(evidence.payload), evidence.producer_capability_id,
                        evidence.producer_version, evidence.confidence, evidence.status,
                        evidence.turn_id, evidence.created_at, evidence.schema_version,
                        str(getattr(evidence, "fingerprint", "") or "") or None,
                    ),
                )
            for claim in proposal.claims_to_create:
                con.execute(
                    """
                    INSERT INTO claims(
                        id, subject_id, subject_version, predicate, value_json,
                        confidence, status, scope_json, revision, created_in_turn,
                        schema_version
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        claim.id, claim.subject.id if claim.subject else None,
                        claim.subject.version if claim.subject else None, claim.predicate,
                        canonical_json(claim.value), claim.confidence, claim.status.value,
                        canonical_json(claim.scope), claim.revision, claim.created_in_turn,
                        claim.schema_version,
                    ),
                )
                for evidence_id in claim.evidence_ids:
                    con.execute(
                        "INSERT INTO claim_evidence(claim_id, evidence_id) VALUES(?, ?)",
                        (claim.id, evidence_id),
                    )
            for interaction in proposal.interactions_to_create:
                self._insert_interaction(con, interaction)

            facts = proposal.memory_summary.get("facts")
            if task_id and isinstance(facts, dict):
                for key, value in facts.items():
                    if not isinstance(value, dict):
                        continue
                    con.execute(
                        """
                        INSERT INTO task_facts(task_id, key, value_json, updated_turn_id, updated_at)
                        VALUES(?, ?, ?, ?, ?)
                        ON CONFLICT(task_id, key) DO UPDATE SET
                            value_json=excluded.value_json,
                            updated_turn_id=excluded.updated_turn_id,
                            updated_at=excluded.updated_at
                        """,
                        (task_id, str(key), canonical_json(value), proposal.turn_id, _utc_now()),
                    )

            result_json = canonical_json(result)
            con.execute(
                """
                INSERT INTO commits(
                    id, idempotency_key, request_hash, task_id, turn_id,
                    result_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    commit_id, proposal.idempotency_key, request_hash, task_id,
                    proposal.turn_id, result_json, _utc_now(),
                ),
            )
            con.execute(
                """
                UPDATE main_turns SET status='committed', phase='respond',
                    commit_id=?, updated_at=? WHERE id=?
                """,
                (commit_id, _utc_now(), proposal.turn_id),
            )
            self._append_event(
                con,
                event_id=f"event_{commit_id}",
                event_type="MainTurnCommitted",
                task_id=task_id,
                turn_id=proposal.turn_id,
                aggregate_id=commit_id,
                payload={"idempotency_key": proposal.idempotency_key},
            )
        return result

    def _append_event(
        self,
        con: sqlite3.Connection,
        *,
        event_id: str,
        event_type: str,
        task_id: str | None,
        turn_id: str | None,
        aggregate_id: str | None,
        payload: dict[str, Any],
        run_id: str | None = None,
        subturn_id: str | None = None,
        action_id: str | None = None,
    ) -> None:
        con.execute(
            """
            INSERT INTO domain_events(
                id, type, task_id, turn_id, aggregate_id, run_id, subturn_id,
                action_id, payload_json, created_at, schema_version
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                event_id,
                event_type,
                task_id,
                turn_id,
                aggregate_id,
                run_id or None,
                subturn_id or None,
                action_id or None,
                canonical_json(payload),
                _utc_now(),
            ),
        )

    def event_count(self) -> int:
        with self.connect() as con:
            row = con.execute("SELECT COUNT(*) AS n FROM domain_events").fetchone()
        return int(row["n"])

    def append_event(
        self,
        *,
        event_id: str,
        event_type: str,
        task_id: str | None,
        turn_id: str | None,
        aggregate_id: str | None = None,
        payload: dict[str, Any] | None = None,
        run_id: str | None = None,
        subturn_id: str | None = None,
        action_id: str | None = None,
    ) -> None:
        with self.transaction() as con:
            self._append_event(
                con,
                event_id=event_id,
                event_type=event_type,
                task_id=task_id,
                turn_id=turn_id,
                aggregate_id=aggregate_id,
                payload=dict(payload or {}),
                run_id=run_id,
                subturn_id=subturn_id,
                action_id=action_id,
            )

    def list_events(self, *, turn_id: str) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM domain_events WHERE turn_id=? ORDER BY sequence", (turn_id,)
            ).fetchall()
        return [self._event_row(row) for row in rows]

    def list_events_for_subturn(self, subturn_id: str) -> list[dict[str, Any]]:
        """按副轮下钻（设计 §14.3：试运行反馈能反查到副轮）。"""
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM domain_events WHERE subturn_id=? ORDER BY sequence",
                (subturn_id,),
            ).fetchall()
        return [self._event_row(row) for row in rows]

    def list_events_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM domain_events WHERE run_id=? ORDER BY sequence", (run_id,)
            ).fetchall()
        return [self._event_row(row) for row in rows]

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        return {
            "sequence": row["sequence"],
            "id": row["id"],
            "type": row["type"],
            "task_id": row["task_id"],
            "turn_id": row["turn_id"],
            "aggregate_id": row["aggregate_id"],
            "run_id": row["run_id"] if "run_id" in keys else None,
            "subturn_id": row["subturn_id"] if "subturn_id" in keys else None,
            "action_id": row["action_id"] if "action_id" in keys else None,
            "payload": json.loads(row["payload_json"]),
            "created_at": row["created_at"],
        }

    def evidence_exists(self, evidence_id: str) -> bool:
        with self.connect() as con:
            row = con.execute("SELECT 1 FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        return row is not None

    # ── 视觉证据（Phase B2 / 设计 §5）：跨主轮 L2 权威源 ────────────────

    def put_evidence(
        self,
        evidence: EvidenceRecord,
        *,
        fingerprint: str = "",
    ) -> None:
        """写一条证据（含可空 fingerprint）；重复 id 直接覆盖，不动别的行。"""
        with self.transaction() as con:
            con.execute(
                """
                INSERT INTO evidence(
                    id, kind, subject_id, subject_version, payload_json,
                    producer_capability_id, producer_version, confidence,
                    status, turn_id, created_at, schema_version, fingerprint
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    status=excluded.status,
                    confidence=excluded.confidence,
                    fingerprint=excluded.fingerprint
                """,
                (
                    evidence.id,
                    evidence.kind.value if hasattr(evidence.kind, "value") else str(evidence.kind),
                    evidence.subject.id if evidence.subject else None,
                    evidence.subject.version if evidence.subject else None,
                    canonical_json(evidence.payload),
                    evidence.producer_capability_id,
                    evidence.producer_version,
                    evidence.confidence,
                    evidence.status,
                    evidence.turn_id,
                    evidence.created_at or _utc_now(),
                    evidence.schema_version,
                    str(fingerprint or getattr(evidence, "fingerprint", "") or "") or None,
                ),
            )

    def find_evidence_by_fingerprint(
        self,
        fingerprint: str,
        *,
        kind: str = "",
        status: str = "active",
    ) -> dict[str, Any] | None:
        """按 ``(kind, fingerprint, status)`` 找一条证据（**走索引**）。

        返回 dict（含 ``id/kind/payload/status/...``），找不到返回 None。
        老行 fingerprint 是 NULL → 永远不会被这个查询命中（不冒充有指纹）。
        """
        target = str(fingerprint or "").strip()
        if not target:
            return None
        where = ["fingerprint = ?"]
        params: list[Any] = [target]
        if kind:
            where.append("kind = ?")
            params.append(str(kind))
        if status:
            where.append("status = ?")
            params.append(str(status))
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM evidence WHERE " + " AND ".join(where) +
                " ORDER BY created_at DESC, id DESC LIMIT 1",
                tuple(params),
            ).fetchone()
        return self._evidence_dict(row) if row is not None else None

    def list_evidence_with_fingerprint(
        self,
        *,
        status: str = "active",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """列出**带指纹**的证据（老行 fingerprint 为 NULL，不在此列）。"""
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM evidence WHERE fingerprint IS NOT NULL AND status = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (str(status or "active"), max(1, min(500, int(limit or 100)))),
            ).fetchall()
        return [self._evidence_dict(row) for row in rows]

    def set_evidence_status(
        self,
        evidence_id: str,
        status: str,
        *,
        reason: str = "",
    ) -> bool:
        """改状态（失效先更新 L2）。``reason`` 写进 payload_json 的 meta，便于复盘。"""
        target = str(evidence_id or "")
        if not target:
            return False
        with self.transaction() as con:
            row = con.execute(
                "SELECT payload_json FROM evidence WHERE id=?", (target,)
            ).fetchone()
            if row is None:
                return False
            payload = json.loads(row["payload_json"] or "{}")
            if not isinstance(payload, dict):
                payload = {}
            if reason:
                meta = payload.get("_meta")
                meta = dict(meta) if isinstance(meta, dict) else {}
                meta["status_reason"] = str(reason)[:300]
                payload["_meta"] = meta
            con.execute(
                "UPDATE evidence SET status=?, payload_json=? WHERE id=?",
                (str(status or "active"), canonical_json(payload), target),
            )
        return True

    def invalidate_evidence_rows(self, evidence_ids: Iterable[str], *, reason: str = "") -> int:
        """按 id 批量失效（L2 权威源先改，再清 L1）。返回受影响行数。"""
        ids = [str(x) for x in (evidence_ids or ()) if str(x)]
        if not ids:
            return 0
        changed = 0
        for evidence_id in ids:
            if self.set_evidence_status(evidence_id, "invalid", reason=reason):
                changed += 1
        return changed

    @staticmethod
    def _evidence_dict(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        return {
            "id": row["id"],
            "kind": row["kind"],
            "subject_id": row["subject_id"],
            "subject_version": row["subject_version"],
            "payload": json.loads(row["payload_json"] or "{}"),
            "producer_capability_id": row["producer_capability_id"],
            "producer_version": row["producer_version"],
            "confidence": row["confidence"],
            "status": row["status"],
            "turn_id": row["turn_id"],
            "created_at": row["created_at"],
            "schema_version": row["schema_version"],
            "fingerprint": row["fingerprint"] if "fingerprint" in keys else None,
        }

    def resource_exists(self, resource_id: str) -> bool:
        with self.connect() as con:
            row = con.execute(
                "SELECT 1 FROM resources WHERE id=? AND status IN ('candidate','active','verified') LIMIT 1",
                (resource_id,),
            ).fetchone()
        return row is not None
