"""SQLite source of truth for evidence-backed visual-agent experience.

This store is intentionally write-only from the executor's point of view.
Nothing returned here is injected into prompts or used to allow/refuse an
action yet.  That makes the first rollout observable and reversible.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics
from threading import RLock
from typing import Any
from uuid import uuid4

import cv2
import numpy as np

from .reliability import posterior, reliability_status, sprt_update


SCHEMA_VERSION = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _norm(value: object, limit: int = 240) -> str:
    return " ".join(str(value or "").strip().lower().split())[:limit]


def _serialized(method):
    """Serialize access to the one connection while allowing nested calls."""
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    byte_size INTEGER NOT NULL,
    retention TEXT NOT NULL DEFAULT 'temporary',
    created_at TEXT NOT NULL,
    UNIQUE(sha256, kind)
);
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step INTEGER,
    scope TEXT NOT NULL,
    task TEXT NOT NULL,
    status TEXT NOT NULL,
    action_kind TEXT NOT NULL,
    action_json TEXT NOT NULL,
    control_key TEXT NOT NULL,
    before_scene TEXT NOT NULL,
    before_fingerprint TEXT NOT NULL,
    before_evidence_id TEXT,
    after_scene TEXT,
    after_fingerprint TEXT,
    after_evidence_id TEXT,
    stable INTEGER,
    grounding_result TEXT NOT NULL DEFAULT 'unknown',
    effect_result TEXT NOT NULL DEFAULT 'unknown',
    transition_result TEXT NOT NULL DEFAULT 'unknown',
    goal_effect TEXT NOT NULL DEFAULT 'unknown',
    policy_result TEXT NOT NULL DEFAULT 'not_assessed',
    evidence_status TEXT NOT NULL DEFAULT 'pending',
    censor_reason TEXT,
    started_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY(before_evidence_id) REFERENCES evidence(id),
    FOREIGN KEY(after_evidence_id) REFERENCES evidence(id)
);
CREATE INDEX IF NOT EXISTS idx_episodes_scope_time ON episodes(scope, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);
CREATE TABLE IF NOT EXISTS transition_stats (
    context_key TEXT NOT NULL,
    outcome_key TEXT NOT NULL,
    scope TEXT NOT NULL,
    before_scene TEXT NOT NULL,
    control_key TEXT NOT NULL,
    action_kind TEXT NOT NULL,
    after_scene TEXT NOT NULL,
    successes INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    ignored INTEGER NOT NULL DEFAULT 0,
    beta_alpha REAL NOT NULL DEFAULT 1.0,
    beta_beta REAL NOT NULL DEFAULT 1.0,
    sprt_llr REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'candidate',
    last_episode_id TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(context_key, outcome_key)
);
CREATE TABLE IF NOT EXISTS action_wait_stats (
    context_key TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    before_scene TEXT NOT NULL,
    control_key TEXT NOT NULL,
    action_kind TEXT NOT NULL,
    successful_waits_json TEXT NOT NULL DEFAULT '[]',
    insufficient_waits_json TEXT NOT NULL DEFAULT '[]',
    recommended_wait REAL NOT NULL DEFAULT 0.35,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS predicates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_key TEXT NOT NULL UNIQUE,
    value_type TEXT NOT NULL,
    scope_pattern TEXT NOT NULL DEFAULT '*',
    status TEXT NOT NULL DEFAULT 'candidate',
    created_by TEXT NOT NULL DEFAULT 'runtime',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS predicate_aliases (
    alias TEXT PRIMARY KEY,
    predicate_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(predicate_id) REFERENCES predicates(id)
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id TEXT,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(episode_id) REFERENCES episodes(id)
);
"""


class VisualMemoryStore:
    """Persistent observation store. All public writes are deterministic/local."""

    def __init__(self, path: Path | str, *, evidence_root: Path | str | None = None):
        self.path = Path(path)
        self.evidence_root = Path(evidence_root) if evidence_root else self.path.with_suffix("")
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        # The GUI constructs the runtime on its own thread and executes it on a
        # worker thread.  Access remains sequential, but SQLite's default
        # thread-affinity check would reject that legitimate hand-off.
        self._db = sqlite3.connect(str(self.path), timeout=5.0, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        # exFAT does not provide the same locking guarantees as NTFS. DELETE is
        # slower than WAL but avoids sidecar-file assumptions on this workspace.
        self._db.execute("PRAGMA journal_mode=DELETE")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_DDL)
        self._db.execute(
            "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('version',?)",
            (str(SCHEMA_VERSION),),
        )
        self._db.commit()

    @_serialized
    def close(self) -> None:
        self._db.close()

    @staticmethod
    def wait_context_key(*, scope: str, before_scene: str,
                         control: dict[str, Any], action_kind: str) -> tuple[str, str]:
        control_key = "|".join(_norm(control.get(key)) for key in (
            "label", "region", "container", "position", "declared_type"
        ))
        material = "|".join((_norm(scope), _norm(before_scene, 120), control_key,
                             _norm(action_kind, 40)))
        return hashlib.sha256(material.encode("utf-8")).hexdigest(), control_key

    @_serialized
    def recommended_wait(self, *, scope: str, before_scene: str,
                         control: dict[str, Any], action_kind: str,
                         default: float = 0.35) -> float:
        context_key, _ = self.wait_context_key(
            scope=scope, before_scene=before_scene, control=control,
            action_kind=action_kind,
        )
        row = self._db.execute(
            "SELECT recommended_wait FROM action_wait_stats WHERE context_key=?",
            (context_key,),
        ).fetchone()
        value = float(row["recommended_wait"]) if row else float(default)
        return round(min(8.0, max(0.15, value)), 2)

    @_serialized
    def observe_wait(self, *, scope: str, before_scene: str,
                     control: dict[str, Any], action_kind: str,
                     waited: float, usable: bool) -> float:
        """Learn action delay from whether the next observation was usable.

        A transient/loading observation means the tried delay was insufficient;
        a stable semantic scene means it was sufficient.  Only small bounded
        sample lists are retained so old UI behaviour naturally ages out.
        """
        context_key, control_key = self.wait_context_key(
            scope=scope, before_scene=before_scene, control=control,
            action_kind=action_kind,
        )
        row = self._db.execute(
            "SELECT * FROM action_wait_stats WHERE context_key=?", (context_key,)
        ).fetchone()
        successes = json.loads(row["successful_waits_json"]) if row else []
        failures = json.loads(row["insufficient_waits_json"]) if row else []
        value = round(min(8.0, max(0.15, float(waited))), 2)
        (successes if usable else failures).append(value)
        successes, failures = successes[-30:], failures[-30:]
        floor = max(failures, default=0.0) * 1.35
        if successes:
            typical = float(statistics.median(successes))
            # Keep probing slightly faster after several successes; a later
            # transient sample raises the floor again.
            if len(successes) >= 4 and not failures[-3:]:
                typical *= 0.9
            recommended = max(0.15, typical, floor)
        else:
            recommended = max(0.35, floor)
        recommended = round(min(8.0, recommended), 2)
        self._db.execute(
            """INSERT INTO action_wait_stats
               (context_key,scope,before_scene,control_key,action_kind,
                successful_waits_json,insufficient_waits_json,recommended_wait,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(context_key) DO UPDATE SET
                 successful_waits_json=excluded.successful_waits_json,
                 insufficient_waits_json=excluded.insufficient_waits_json,
                 recommended_wait=excluded.recommended_wait,
                 updated_at=excluded.updated_at""",
            (context_key, _norm(scope), _norm(before_scene, 120), control_key,
             _norm(action_kind, 40), _json(successes), _json(failures),
             recommended, _now()),
        )
        self._db.commit()
        return recommended

    @_serialized
    def save_frame(self, frame: np.ndarray, *, kind: str,
                   retention: str = "temporary") -> str | None:
        if not isinstance(frame, np.ndarray) or frame.size == 0:
            return None
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not ok:
            return None
        payload = encoded.tobytes()
        digest = hashlib.sha256(payload).hexdigest()
        row = self._db.execute(
            "SELECT id FROM evidence WHERE sha256=? AND kind=?", (digest, kind)
        ).fetchone()
        if row:
            return str(row["id"])
        evidence_id = uuid4().hex
        folder = self.evidence_root / digest[:2]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{digest}.jpg"
        if not path.exists():
            temporary = path.with_suffix(".jpg.tmp")
            temporary.write_bytes(payload)
            temporary.replace(path)
        height, width = frame.shape[:2]
        self._db.execute(
            """INSERT INTO evidence
               (id,sha256,kind,path,width,height,byte_size,retention,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (evidence_id, digest, kind, str(path), width, height, len(payload), retention, _now()),
        )
        self._db.commit()
        return evidence_id

    @_serialized
    def begin_episode(self, *, run_id: str, step: int, scope: str, task: str,
                      action: dict[str, Any], control: dict[str, Any],
                      before_scene: str, before_fingerprint: str,
                      before_frame: np.ndarray | None = None) -> str:
        episode_id = uuid4().hex
        evidence_id = self.save_frame(before_frame, kind="action_before") if before_frame is not None else None
        control_key = "|".join(_norm(control.get(key)) for key in (
            "label", "region", "container", "position", "declared_type"
        ))
        self._db.execute(
            """INSERT INTO episodes
               (id,run_id,step,scope,task,status,action_kind,action_json,control_key,
                before_scene,before_fingerprint,before_evidence_id,started_at)
               VALUES(?,?,?,?,?,'pending',?,?,?,?,?,?,?)""",
            (episode_id, run_id, int(step), _norm(scope), task,
             _norm(action.get("action")), _json(action), control_key,
             _norm(before_scene, 120), before_fingerprint, evidence_id, _now()),
        )
        self._db.commit()
        return episode_id

    @_serialized
    def resolve_episode(self, episode_id: str, *, after_scene: str,
                        after_fingerprint: str, after_frame: np.ndarray | None,
                        stable: bool, measured_effect: dict[str, Any] | None) -> dict[str, Any]:
        row = self._db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        if row is None:
            raise KeyError(episode_id)
        effect = _norm((measured_effect or {}).get("classification") or "unknown")
        evidence_id = self.save_frame(after_frame, kind="action_after") if after_frame is not None else None
        grounding = "unknown"
        transition = "unknown"
        evidence_status = "partial"
        if stable:
            evidence_status = "resolved"
            if effect in {"effective_change", "productive_repeat"}:
                transition = "changed"
            elif effect == "confirmed_no_effect":
                transition = "no_effect"
            elif effect in {"suspected_no_effect", "visual_change_outside_region"}:
                transition = "ambiguous"
        else:
            effect = "unsettled_after_action"
        self._db.execute(
            """UPDATE episodes SET status='resolved',after_scene=?,after_fingerprint=?,
               after_evidence_id=?,stable=?,grounding_result=?,effect_result=?,
               transition_result=?,evidence_status=?,resolved_at=? WHERE id=?""",
            (_norm(after_scene, 120), after_fingerprint, evidence_id, int(bool(stable)),
             grounding, effect, transition, evidence_status, _now(), episode_id),
        )
        eligible = bool(stable) and transition in {"changed", "no_effect"}
        if eligible:
            self._update_transition(row, _norm(after_scene, 120), episode_id)
        self._db.commit()
        return self.episode(episode_id)

    @_serialized
    def censor_episode(self, episode_id: str, reason: str) -> None:
        self._db.execute(
            """UPDATE episodes SET status='censored',evidence_status='censored',
               censor_reason=?,resolved_at=? WHERE id=? AND status='pending'""",
            (_norm(reason), _now(), episode_id),
        )
        self._db.commit()

    def _update_transition(self, episode: sqlite3.Row, after_scene: str,
                           episode_id: str) -> None:
        context_key = hashlib.sha256("|".join((
            str(episode["scope"]), str(episode["before_scene"]),
            str(episode["control_key"]), str(episode["action_kind"]),
        )).encode("utf-8")).hexdigest()
        rows = self._db.execute(
            "SELECT * FROM transition_stats WHERE context_key=?", (context_key,)
        ).fetchall()
        existing_outcomes = {str(item["after_scene"]) for item in rows}
        if after_scene not in existing_outcomes:
            outcome_key = hashlib.sha256(after_scene.encode("utf-8")).hexdigest()
            self._db.execute(
                """INSERT INTO transition_stats
                   (context_key,outcome_key,scope,before_scene,control_key,action_kind,
                    after_scene,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                (context_key, outcome_key, episode["scope"], episode["before_scene"],
                 episode["control_key"], episode["action_kind"], after_scene, _now()),
            )
            rows = self._db.execute(
                "SELECT * FROM transition_stats WHERE context_key=?", (context_key,)
            ).fetchall()
        for item in rows:
            success = str(item["after_scene"]) == after_scene
            successes = int(item["successes"]) + int(success)
            failures = int(item["failures"]) + int(not success)
            alpha = float(item["beta_alpha"]) + int(success)
            beta = float(item["beta_beta"]) + int(not success)
            llr = sprt_update(float(item["sprt_llr"]), success)
            status = reliability_status(observations=successes + failures, llr=llr)
            self._db.execute(
                """UPDATE transition_stats SET successes=?,failures=?,beta_alpha=?,
                   beta_beta=?,sprt_llr=?,status=?,last_episode_id=?,updated_at=?
                   WHERE context_key=? AND outcome_key=?""",
                (successes, failures, alpha, beta, llr, status, episode_id, _now(),
                 context_key, item["outcome_key"]),
            )

    @_serialized
    def episode(self, episode_id: str) -> dict[str, Any]:
        row = self._db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        return dict(row) if row else {}

    @_serialized
    def transition_rows(self, scope: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM transition_stats WHERE scope=? ORDER BY updated_at DESC", (_norm(scope),)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["posterior"] = posterior(item["beta_alpha"], item["beta_beta"])
            result.append(item)
        return result

    @_serialized
    def register_predicate_candidate(self, key: str, *, value_type: str,
                                     scope_pattern: str = "*",
                                     aliases: list[str] | None = None) -> int:
        """Register a candidate only; callers cannot create a trusted key."""
        canonical = _norm(key).replace(" ", "_")
        if not canonical:
            raise ValueError("predicate key is empty")
        known = self.resolve_predicate(canonical)
        if known:
            return int(known["id"])
        cursor = self._db.execute(
            """INSERT INTO predicates
               (canonical_key,value_type,scope_pattern,status,created_by,created_at)
               VALUES(?,?,?,'candidate','llm_proposal',?)""",
            (canonical, _norm(value_type, 40) or "unknown", scope_pattern, _now()),
        )
        predicate_id = int(cursor.lastrowid)
        for alias in aliases or []:
            normalized = _norm(alias).replace(" ", "_")
            if normalized:
                self._db.execute(
                    "INSERT OR IGNORE INTO predicate_aliases(alias,predicate_id,created_at) VALUES(?,?,?)",
                    (normalized, predicate_id, _now()),
                )
        self._db.commit()
        return predicate_id

    @_serialized
    def resolve_predicate(self, key_or_alias: str) -> dict[str, Any]:
        key = _norm(key_or_alias).replace(" ", "_")
        row = self._db.execute(
            """SELECT p.* FROM predicates p LEFT JOIN predicate_aliases a ON a.predicate_id=p.id
               WHERE p.canonical_key=? OR a.alias=? LIMIT 1""", (key, key)
        ).fetchone()
        return dict(row) if row else {}

    @_serialized
    def record_feedback(self, *, episode_id: str | None, kind: str,
                        payload: dict[str, Any] | None = None) -> int:
        """Append a correction without rewriting the original observation.

        Evidence for corrected/contested episodes is protected from automatic
        cleanup so later code or a human can re-audit what was actually shown.
        """
        normalized = _norm(kind, 60)
        allowed = {
            "confirmed", "incorrect_transition", "incorrect_grounding",
            "wrong_scene", "user_correction", "ignore_episode",
        }
        if normalized not in allowed:
            raise ValueError(f"unsupported feedback kind: {kind}")
        if episode_id and not self.episode(episode_id):
            raise KeyError(episode_id)
        cursor = self._db.execute(
            "INSERT INTO feedback(episode_id,kind,payload_json,created_at) VALUES(?,?,?,?)",
            (episode_id, normalized, _json(payload or {}), _now()),
        )
        if episode_id:
            self._db.execute(
                """UPDATE evidence SET retention='protected' WHERE id IN (
                     SELECT before_evidence_id FROM episodes WHERE id=?
                     UNION SELECT after_evidence_id FROM episodes WHERE id=?
                   )""",
                (episode_id, episode_id),
            )
        self._db.commit()
        return int(cursor.lastrowid)

    @_serialized
    def prune_evidence(self, *, temporary_days: int = 7,
                       max_files: int = 2000) -> dict[str, int]:
        """Prune old temporary image bytes while retaining evidence metadata."""
        days = max(1, int(temporary_days))
        limit = max(1, int(max_files))
        rows = self._db.execute(
            """SELECT id,path FROM evidence
               WHERE retention='temporary'
                 AND julianday(created_at) < julianday('now', ?)
               ORDER BY created_at ASC LIMIT ?""",
            (f"-{days} days", limit),
        ).fetchall()
        removed = 0
        missing = 0
        for row in rows:
            path = Path(str(row["path"] or ""))
            try:
                if path.is_file():
                    path.unlink()
                    removed += 1
                else:
                    missing += 1
            except OSError:
                continue
            self._db.execute(
                "UPDATE evidence SET path='',byte_size=0,retention='pruned' WHERE id=?",
                (row["id"],),
            )
        self._db.commit()
        return {"removed": removed, "already_missing": missing, "examined": len(rows)}

    @_serialized
    def summary(self) -> dict[str, Any]:
        """Compact diagnostics for settings pages and runtime logs."""
        def count(table: str, where: str = "") -> int:
            row = self._db.execute(f"SELECT COUNT(*) AS n FROM {table} {where}").fetchone()
            return int(row["n"])

        statuses = {
            str(row["status"]): int(row["n"])
            for row in self._db.execute(
                "SELECT status,COUNT(*) AS n FROM transition_stats GROUP BY status"
            ).fetchall()
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "episodes": count("episodes"),
            "pending_episodes": count("episodes", "WHERE status='pending'"),
            "censored_episodes": count("episodes", "WHERE status='censored'"),
            "evidence_files": count("evidence", "WHERE path<>''"),
            "protected_evidence": count("evidence", "WHERE retention='protected'"),
            "transition_statuses": statuses,
            "wait_profiles": count("action_wait_stats"),
            "feedback": count("feedback"),
        }
