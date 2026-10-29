"""SQLite source of truth for visual-agent task graphs and control checkpoints."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from uuid import uuid4

from .models import INSERTION_POLICIES, TERMINAL_STATUSES, TaskNode, TaskRun


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: object, fallback: Any) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


class VisualTaskStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=5.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        return con

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

    def initialize(self) -> None:
        with self.transaction() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS task_runs(
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    period_key TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    root_node_id TEXT NOT NULL,
                    tree_revision INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_task_run_period
                    ON task_runs(title, period_key) WHERE period_key <> '';
                CREATE TABLE IF NOT EXISTS task_nodes(
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
                    parent_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    node_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    required INTEGER NOT NULL DEFAULT 1,
                    operator TEXT NOT NULL DEFAULT '',
                    required_count INTEGER NOT NULL DEFAULT 0,
                    goal_json TEXT NOT NULL DEFAULT '{}',
                    scheduling_json TEXT NOT NULL DEFAULT '{}',
                    source_json TEXT NOT NULL DEFAULT '{}',
                    revision_created INTEGER NOT NULL,
                    revision_updated INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_task_nodes_run ON task_nodes(run_id, parent_id, status);
                CREATE TABLE IF NOT EXISTS task_edges(
                    run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    data_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(run_id, source_id, target_id, relation)
                );
                CREATE TABLE IF NOT EXISTS task_observations(
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
                    node_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_inbox(
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    insertion_policy TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    processed_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS ix_task_inbox_pending ON task_inbox(run_id, status, created_at);
                CREATE TABLE IF NOT EXISTS task_plans(
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
                    based_on_revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_checkpoints(
                    run_id TEXT PRIMARY KEY REFERENCES task_runs(id) ON DELETE CASCADE,
                    graph_node TEXT NOT NULL,
                    active_task_id TEXT NOT NULL DEFAULT '',
                    active_plan_id TEXT NOT NULL DEFAULT '',
                    tree_revision INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _bump(con: sqlite3.Connection, run_id: str) -> int:
        now = _now()
        con.execute(
            "UPDATE task_runs SET tree_revision=tree_revision+1, updated_at=? WHERE id=?",
            (now, run_id),
        )
        row = con.execute("SELECT tree_revision FROM task_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"task run not found: {run_id}")
        revision = int(row["tree_revision"])
        con.execute(
            "UPDATE task_plans SET status='invalidated', updated_at=? "
            "WHERE run_id=? AND status IN ('ready','running') AND based_on_revision<>?",
            (now, run_id, revision),
        )
        return revision

    def create_run(self, title: str, *, period_key: str = "",
                   metadata: dict[str, Any] | None = None,
                   run_id: str | None = None) -> TaskRun:
        run_id = run_id or f"run_{uuid4().hex}"
        root_id = f"task_{uuid4().hex}"
        now = _now()
        with self.transaction() as con:
            con.execute(
                "INSERT INTO task_runs VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, title, period_key, "pending", root_id, 1,
                 _json(metadata or {}), now, now),
            )
            con.execute(
                "INSERT INTO task_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (root_id, run_id, "", title, "composite", "pending", 1, "all", 0,
                 "{}", "{}", _json({"type": "session"}), 1, 1, now, now),
            )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> TaskRun:
        with self.connect() as con:
            row = con.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"task run not found: {run_id}")
        return TaskRun(
            id=row["id"], title=row["title"], period_key=row["period_key"],
            status=row["status"], root_node_id=row["root_node_id"],
            tree_revision=int(row["tree_revision"]), metadata=_load(row["metadata_json"], {}),
        )

    def get_node(self, node_id: str) -> TaskNode:
        with self.connect() as con:
            row = con.execute("SELECT * FROM task_nodes WHERE id=?", (node_id,)).fetchone()
        if row is None:
            raise KeyError(f"task node not found: {node_id}")
        return self._node(row)

    def list_nodes(self, run_id: str) -> list[TaskNode]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM task_nodes WHERE run_id=? ORDER BY created_at,id", (run_id,)
            ).fetchall()
        return [self._node(row) for row in rows]

    @staticmethod
    def _node(row: sqlite3.Row) -> TaskNode:
        return TaskNode(
            id=row["id"], run_id=row["run_id"], parent_id=row["parent_id"],
            title=row["title"], node_type=row["node_type"], status=row["status"],
            required=bool(row["required"]), operator=row["operator"],
            required_count=int(row["required_count"]), goal_spec=_load(row["goal_json"], {}),
            scheduling=_load(row["scheduling_json"], {}), source=_load(row["source_json"], {}),
            revision_created=int(row["revision_created"]),
            revision_updated=int(row["revision_updated"]),
        )

    def add_node(self, run_id: str, *, title: str, parent_id: str = "",
                 node_type: str = "goal", status: str = "unexpanded",
                 required: bool = True, operator: str = "", required_count: int = 0,
                 goal_spec: dict[str, Any] | None = None,
                 scheduling: dict[str, Any] | None = None,
                 source: dict[str, Any] | None = None,
                 node_id: str | None = None) -> TaskNode:
        node_id = node_id or f"task_{uuid4().hex}"
        now = _now()
        with self.transaction() as con:
            run = con.execute("SELECT root_node_id FROM task_runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(f"task run not found: {run_id}")
            parent_id = parent_id or str(run["root_node_id"])
            revision = self._bump(con, run_id)
            con.execute(
                "INSERT INTO task_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (node_id, run_id, parent_id, title, node_type, status, int(required),
                 operator, int(required_count), _json(goal_spec or {}),
                 _json(scheduling or {}), _json(source or {}), revision, revision, now, now),
            )
            con.execute(
                "INSERT OR IGNORE INTO task_edges VALUES(?,?,?,?,?)",
                (run_id, parent_id, node_id, "parent_of", "{}"),
            )
        self.recompute(run_id)
        return self.get_node(node_id)

    def update_node(self, node_id: str, *, status: str | None = None,
                    goal_spec: dict[str, Any] | None = None,
                    scheduling: dict[str, Any] | None = None) -> TaskNode:
        with self.transaction() as con:
            row = con.execute("SELECT * FROM task_nodes WHERE id=?", (node_id,)).fetchone()
            if row is None:
                raise KeyError(f"task node not found: {node_id}")
            revision = self._bump(con, str(row["run_id"]))
            con.execute(
                "UPDATE task_nodes SET status=?, goal_json=?, scheduling_json=?, "
                "revision_updated=?, updated_at=? WHERE id=?",
                (status or row["status"], _json(goal_spec) if goal_spec is not None else row["goal_json"],
                 _json(scheduling) if scheduling is not None else row["scheduling_json"],
                 revision, _now(), node_id),
            )
            run_id = str(row["run_id"])
        self.recompute(run_id)
        return self.get_node(node_id)

    def enqueue_task(self, run_id: str, payload: dict[str, Any], *,
                     insertion_policy: str = "append", event_type: str = "insert_task") -> str:
        if insertion_policy not in INSERTION_POLICIES:
            raise ValueError(f"invalid insertion policy: {insertion_policy}")
        event_id = f"inbox_{uuid4().hex}"
        with self.transaction() as con:
            con.execute(
                "INSERT INTO task_inbox VALUES(?,?,?,?,?,?,?,?)",
                (event_id, run_id, event_type, insertion_policy, _json(payload),
                 "pending", _now(), ""),
            )
        return event_id

    def pending_inbox_count(self, run_id: str) -> int:
        with self.connect() as con:
            row = con.execute(
                "SELECT COUNT(*) AS n FROM task_inbox WHERE run_id=? AND status='pending'", (run_id,)
            ).fetchone()
        return int(row["n"])

    def drain_inbox(self, run_id: str) -> list[str]:
        """Apply queued mutations at a safe point and return affected task ids."""
        affected: list[str] = []
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM task_inbox WHERE run_id=? AND status='pending' ORDER BY created_at,id",
                (run_id,),
            ).fetchall()
        for row in rows:
            payload, policy = _load(row["payload_json"], {}), str(row["insertion_policy"])
            if policy == "merge":
                target = str(payload.get("target_task_id") or "")
                node = self.get_node(target)
                merged_goal = {**node.goal_spec, **dict(payload.get("goal_spec") or {})}
                self.update_node(target, goal_spec=merged_goal)
                affected.append(target)
            else:
                if policy == "replace":
                    run = self.get_run(run_id)
                    for node in self.list_nodes(run_id):
                        if node.parent_id == run.root_node_id and node.status not in TERMINAL_STATUSES:
                            self.update_node(node.id, status="cancelled")
                scheduling = dict(payload.get("scheduling") or {})
                scheduling["insertion_policy"] = policy
                if policy == "interrupt_at_safe_point":
                    scheduling.setdefault("priority_class", "urgent")
                node = self.add_node(
                    run_id, title=str(payload.get("title") or payload.get("text") or "新任务"),
                    parent_id=str(payload.get("parent_id") or ""),
                    node_type=str(payload.get("node_type") or "goal"),
                    status=str(payload.get("status") or "unexpanded"),
                    required=bool(payload.get("required", True)),
                    operator=str(payload.get("operator") or ""),
                    required_count=int(payload.get("required_count") or 0),
                    goal_spec=dict(payload.get("goal_spec") or {}), scheduling=scheduling,
                    source={"type": "inbox", "event_id": row["id"], **dict(payload.get("source") or {})},
                )
                affected.append(node.id)
            with self.transaction() as con:
                con.execute(
                    "UPDATE task_inbox SET status='processed', processed_at=? WHERE id=?",
                    (_now(), row["id"]),
                )
        return affected

    def recompute(self, run_id: str) -> str:
        """Recalculate composites bottom-up; LLMs never set parent success."""
        nodes = self.list_nodes(run_id)
        by_parent: dict[str, list[TaskNode]] = {}
        for node in nodes:
            by_parent.setdefault(node.parent_id, []).append(node)
        changed = True
        while changed:
            changed = False
            for node in reversed(nodes):
                if node.node_type != "composite":
                    continue
                children = [child for child in by_parent.get(node.id, []) if child.required]
                if not children:
                    desired = "unexpanded" if node.parent_id else "pending"
                else:
                    success_count = sum(child.status in {"succeeded", "obsolete"} for child in children)
                    terminal_count = sum(child.status in TERMINAL_STATUSES for child in children)
                    if node.operator == "all":
                        success = success_count == len(children)
                    elif node.operator == "any":
                        success = success_count >= 1
                    else:
                        success = success_count >= max(1, node.required_count)
                    desired = "succeeded" if success else (
                        "failed" if terminal_count == len(children) else "pending"
                    )
                if desired != node.status:
                    with self.transaction() as con:
                        con.execute(
                            "UPDATE task_nodes SET status=?, updated_at=? WHERE id=?",
                            (desired, _now(), node.id),
                        )
                    node.status, changed = desired, True
        run = self.get_run(run_id)
        root = self.get_node(run.root_node_id)
        with self.transaction() as con:
            con.execute(
                "UPDATE task_runs SET status=?, updated_at=? WHERE id=?",
                (root.status, _now(), run_id),
            )
        return root.status

    def ready_nodes(self, run_id: str) -> list[TaskNode]:
        nodes = [node for node in self.list_nodes(run_id) if node.status in {"ready", "unexpanded"}]
        rank = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
        nodes.sort(key=lambda node: (rank.get(str(node.scheduling.get("priority_class") or "normal"), 2), node.id))
        return nodes

    def create_plan(self, run_id: str, payload: dict[str, Any]) -> str:
        plan_id, now = f"plan_{uuid4().hex}", _now()
        revision = self.get_run(run_id).tree_revision
        with self.transaction() as con:
            con.execute(
                "INSERT INTO task_plans VALUES(?,?,?,?,?,?,?)",
                (plan_id, run_id, revision, "ready", _json(payload), now, now),
            )
        return plan_id

    def plan_is_current(self, plan_id: str) -> bool:
        with self.connect() as con:
            row = con.execute(
                "SELECT p.based_on_revision,p.status,r.tree_revision FROM task_plans p "
                "JOIN task_runs r ON r.id=p.run_id WHERE p.id=?", (plan_id,),
            ).fetchone()
        return bool(row and row["status"] in {"ready", "running"}
                    and int(row["based_on_revision"]) == int(row["tree_revision"]))

    def save_checkpoint(self, run_id: str, graph_node: str, state: dict[str, Any]) -> None:
        revision = self.get_run(run_id).tree_revision
        with self.transaction() as con:
            con.execute(
                "INSERT INTO task_checkpoints VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET graph_node=excluded.graph_node,"
                "active_task_id=excluded.active_task_id,active_plan_id=excluded.active_plan_id,"
                "tree_revision=excluded.tree_revision,state_json=excluded.state_json,"
                "updated_at=excluded.updated_at",
                (run_id, graph_node, str(state.get("active_task_id") or ""),
                 str(state.get("active_plan_id") or ""), revision, _json(state), _now()),
            )

    def get_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM task_checkpoints WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        return {
            "graph_node": row["graph_node"], "active_task_id": row["active_task_id"],
            "active_plan_id": row["active_plan_id"], "tree_revision": int(row["tree_revision"]),
            "state": _load(row["state_json"], {}), "updated_at": row["updated_at"],
        }
