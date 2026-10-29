"""Persistent, application-scoped memory of observed UI transitions.

The memory contains no screenshots.  It stores semantic state labels, coarse
control identities and compact visual fingerprints so observations from one
application are not promoted into universal button meanings.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any


_SPACE_RE = re.compile(r"\s+")


def _norm(value: object, limit: int = 160) -> str:
    return _SPACE_RE.sub(" ", str(value or "").strip().lower())[:limit]


_CONCLUSION_RE = re.compile(
    r"(最高|最低|最大|最小|最难|最後|最后|末端|到顶|已到头|禁用|disabled|highest|lowest|maximum)",
    re.I,
)


def _neutral_state(value: object) -> str:
    """Keep scene labels but discard model conclusions masquerading as facts."""
    text = _SPACE_RE.sub(" ", str(value or "").strip())
    text = re.sub(
        r"[（(][^）)]*(?:最高|最低|最大|最小|最难|最後|最后|末端|到顶|禁用|disabled|highest|lowest)[^）)]*[）)]",
        "", text, flags=re.I,
    )
    # A scene identity is deliberately coarse. Dynamic option values, counters,
    # rewards and predictions belong in structured fields, not in the state key.
    parts = re.split(r"[，,；;。]", text)
    neutral = [part.strip() for part in parts if part.strip() and not _CONCLUSION_RE.search(part)]
    return _norm((neutral[0] if neutral else "unknown"), limit=60)


class UITransitionMemory:
    """Small JSON-backed transition table with outcome counts and confidence."""

    def __init__(self, path: Path | str, *, max_entries: int = 500):
        self.path = Path(path)
        self.max_entries = max(20, int(max_entries))
        self._data: dict[str, Any] = {"version": 2, "transitions": [], "controls": []}
        self._load()

    def _load(self) -> None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("transitions"), list):
                self._data = value
                self._data.setdefault("controls", [])
                migrated = False
                for row in self._data["transitions"]:
                    before = _neutral_state(row.get("before_state"))
                    if before != row.get("before_state"):
                        row["before_state"], migrated = before, True
                    for outcome in row.get("outcomes") or []:
                        after = _neutral_state(outcome.get("after_state"))
                        if after != outcome.get("after_state"):
                            outcome["after_state"], migrated = after, True
                for row in self._data["controls"]:
                    states = [_neutral_state(item) for item in row.get("states_seen") or []]
                    states = list(dict.fromkeys(states))
                    if states != row.get("states_seen"):
                        row["states_seen"], migrated = states, True
                if migrated:
                    try:
                        self._save()
                    except OSError:
                        pass
        except (FileNotFoundError, OSError, ValueError, TypeError):
            pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.path)

    @staticmethod
    def control_signature(action: dict[str, Any], frame_size: tuple[int, int]) -> dict[str, str]:
        width, height = frame_size
        label = _norm(action.get("target") or action.get("message") or action.get("action"))
        position = ""
        region = _norm(action.get("target_region"))
        try:
            x, y = float(action["x"]), float(action["y"])
            col = min(4, max(0, int(5 * x / max(1, width))))
            row = min(4, max(0, int(5 * y / max(1, height))))
            position = f"grid-{col}-{row}"
            if not region:
                xr, yr = x / max(1, width), y / max(1, height)
                if xr < 0.24:
                    region = "left_sidebar"
                elif xr > 0.76:
                    region = "right_sidebar"
                elif yr < 0.18:
                    region = "top_navigation"
                elif yr > 0.78:
                    region = "bottom_action_bar"
                else:
                    region = "main_content"
        except (KeyError, TypeError, ValueError):
            pass
        container = _norm(action.get("target_container"))
        if not container:
            container = region
        return {
            "label": label,
            "position": position,
            "region": region,
            "container": container,
            "declared_type": _norm(action.get("target_type") or "unknown"),
        }

    def observe_controls(self, *, scope: str, state: str,
                         controls: list[dict[str, Any]]) -> None:
        """Track where controls recur; presence is separate from transition meaning."""
        scope, state = _norm(scope), _neutral_state(state)
        if not scope or not state:
            return
        profiles = self._data.setdefault("controls", [])
        changed = False
        seen_keys: set[str] = set()
        for control in controls[:20]:
            label = _norm(control.get("text") or control.get("label"))
            region = _norm(control.get("region"))
            container = _norm(control.get("container") or region)
            if not label:
                continue
            key = f"{label}|{region}|{container}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            row = next((item for item in profiles if (
                item.get("scope") == scope and item.get("control_key") == key
            )), None)
            if row is None:
                row = {
                    "scope": scope, "control_key": key, "label": label,
                    "region": region, "container": container,
                    "states_seen": [], "sightings": 0, "declared_types": {},
                }
                profiles.append(row)
            if state not in row["states_seen"]:
                row["states_seen"].append(state)
                row["states_seen"] = row["states_seen"][-30:]
            declared = _norm(control.get("type") or "unknown")
            row["declared_types"][declared] = int(row["declared_types"].get(declared) or 0) + 1
            row["sightings"] = int(row.get("sightings") or 0) + 1
            row["last_seen"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            changed = True
        if changed:
            try:
                self._save()
            except OSError:
                pass

    def observe(
        self, *, scope: str, before_state: str, before_fingerprint: str,
        control: dict[str, str], action: str, after_state: str,
        after_fingerprint: str, changed: bool,
        before_value: str = "", after_value: str = "",
    ) -> dict[str, Any] | None:
        scope, action = _norm(scope), _norm(action)
        before_state, after_state = _neutral_state(before_state), _neutral_state(after_state)
        if not scope or not before_state or not action:
            return None
        effect = "no_effect" if not changed else (
            "state_change" if after_state and after_state != before_state else "visual_change"
        )
        control_key = "|".join((
            _norm(control.get("label")), _norm(control.get("region")),
            _norm(control.get("container")), _norm(control.get("position")),
        ))
        transitions = self._data["transitions"]
        row = next((item for item in transitions if (
            item.get("scope") == scope
            and item.get("before_state") == before_state
            and item.get("control_key") == control_key
            and item.get("action") == action
        )), None)
        if row is None:
            row = {
                "scope": scope,
                "before_state": before_state,
                "before_fingerprint": before_fingerprint,
                "control": dict(control),
                "control_key": control_key,
                "action": action,
                "outcomes": [],
            }
            transitions.append(row)
        before_value, after_value = _norm(before_value, 80), _norm(after_value, 80)
        if before_value:
            row["before_value"] = before_value
        outcome = next((item for item in row["outcomes"] if (
            item.get("after_state") == after_state and item.get("effect") == effect
            and _norm(item.get("after_value"), 80) == after_value
        )), None)
        if outcome is None:
            outcome = {
                "after_state": after_state or "unknown",
                "after_fingerprint": after_fingerprint,
                "effect": effect,
                "after_value": after_value,
                "count": 0,
            }
            row["outcomes"].append(outcome)
        outcome["count"] = int(outcome.get("count") or 0) + 1
        outcome["after_fingerprint"] = after_fingerprint
        row["observations"] = sum(int(item.get("count") or 0) for item in row["outcomes"])
        row["confidence"] = round(min(0.98, 0.45 + 0.15 * row["observations"]), 2)
        row["last_seen"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if len(transitions) > self.max_entries:
            transitions.sort(key=lambda item: item.get("last_seen") or "")
            del transitions[:-self.max_entries]
        try:
            self._save()
        except OSError:
            return row
        return row

    def relevant(self, scope: str, *, limit: int = 8) -> list[dict[str, Any]]:
        scope = _norm(scope)
        rows = [item for item in self._data["transitions"] if item.get("scope") == scope]
        rows.sort(key=lambda item: item.get("last_seen") or "", reverse=True)
        result = []
        for row in rows[:max(1, int(limit))]:
            outcomes = sorted(
                row.get("outcomes") or [], key=lambda item: int(item.get("count") or 0), reverse=True
            )
            result.append({
                "before_state": row.get("before_state"),
                "before_value": row.get("before_value") or "",
                "control": row.get("control"),
                "action": row.get("action"),
                "outcomes": outcomes[:3],
                "confidence": row.get("confidence"),
            })
        return result

    def control_profiles(self, scope: str, *, limit: int = 12) -> list[dict[str, Any]]:
        scope = _norm(scope)
        rows = [item for item in self._data.get("controls", []) if item.get("scope") == scope]
        rows.sort(key=lambda item: (
            len(item.get("states_seen") or []), int(item.get("sightings") or 0)
        ), reverse=True)
        result = []
        for row in rows[:max(1, int(limit))]:
            states_seen = row.get("states_seen") or []
            sightings = int(row.get("sightings") or 0)
            persistence = "persistent" if len(states_seen) >= 3 else (
                "recurring" if len(states_seen) >= 2 or sightings >= 3 else "contextual"
            )
            result.append({
                "label": row.get("label"), "region": row.get("region"),
                "container": row.get("container"), "persistence": persistence,
                "states_seen": states_seen[-6:], "sightings": sightings,
                "declared_types": row.get("declared_types") or {},
            })
        return result
