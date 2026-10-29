"""Logical-set matching and a small extensible concept lattice."""
from __future__ import annotations

from typing import Any


def _field(value: Any, path: str) -> Any:
    current = value
    for part in str(path or "").split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


class ConceptRegistry:
    def __init__(self) -> None:
        self._parents: dict[str, str] = {}
        self._aliases: dict[str, str] = {}
        self._verified: set[str] = set()

    def register(self, concept_id: str, *, parent: str = "",
                 aliases: list[str] | None = None, verified: bool = True) -> None:
        concept_id = str(concept_id).strip()
        if not concept_id:
            raise ValueError("concept_id is required")
        if parent:
            self._parents[concept_id] = str(parent).strip()
        for alias in aliases or []:
            self._aliases[str(alias).strip().casefold()] = concept_id
        if verified:
            self._verified.add(concept_id)

    def provisional(self, concept_id: str, *, parent: str = "",
                    aliases: list[str] | None = None) -> None:
        self.register(concept_id, parent=parent, aliases=aliases, verified=False)

    def resolve(self, value: object) -> str:
        text = str(value or "").strip()
        return self._aliases.get(text.casefold(), text)

    def is_verified(self, concept_id: str) -> bool:
        return self.resolve(concept_id) in self._verified

    def is_a(self, actual: object, expected: object) -> bool:
        current, target = self.resolve(actual), self.resolve(expected)
        seen: set[str] = set()
        while current and current not in seen:
            if current == target:
                return True
            seen.add(current)
            current = self._parents.get(current, "")
        return False


class SetEvaluator:
    """Evaluate the deliberately small selector language used by task goals."""

    def __init__(self, concepts: ConceptRegistry | None = None):
        self.concepts = concepts or ConceptRegistry()

    def matches(self, item: dict[str, Any], selector: dict[str, Any] | None) -> bool:
        selector = selector or {}
        if not selector:
            return True
        if "all" in selector:
            return all(self.matches(item, child) for child in selector["all"])
        if "any" in selector:
            return any(self.matches(item, child) for child in selector["any"])
        if "not" in selector:
            return not self.matches(item, selector["not"])
        return self._predicate(item, selector)

    def _predicate(self, item: dict[str, Any], predicate: dict[str, Any]) -> bool:
        actual = _field(item, str(predicate.get("field") or ""))
        operator = str(predicate.get("operator") or "eq")
        expected = predicate.get("value")
        if operator == "eq":
            return actual == expected
        if operator == "ne":
            return actual != expected
        if operator == "in":
            return actual in (expected or [])
        if operator == "not_in":
            return actual not in (expected or [])
        if operator == "gte":
            return actual is not None and actual >= expected
        if operator == "lte":
            return actual is not None and actual <= expected
        if operator == "exists":
            return (actual is not None) is bool(expected)
        if operator == "is_a":
            return self.concepts.is_a(actual, expected)
        raise ValueError(f"unsupported selector operator: {operator}")

    def aggregate(self, items: list[dict[str, Any]], set_spec: dict[str, Any],
                  aggregate: dict[str, Any]) -> float:
        selected = [item for item in items if self.matches(item, set_spec.get("selector"))]
        operator = str(aggregate.get("operator") or "count_distinct")
        field = str(aggregate.get("field") or "event_id")
        if operator == "count_distinct":
            return float(len({_field(item, field) for item in selected if _field(item, field) is not None}))
        if operator == "sum":
            return float(sum(float(_field(item, field) or 0) for item in selected))
        raise ValueError(f"unsupported aggregate operator: {operator}")

    @staticmethod
    def satisfies(value: float, constraint: dict[str, Any]) -> bool:
        operator = str(constraint.get("operator") or "eq")
        expected = float(constraint.get("value") or 0)
        if operator in {"eq", "empty"}:
            return value == expected
        if operator == "gte":
            return value >= expected
        if operator == "lte":
            return value <= expected
        if operator == "non_empty":
            return value > 0
        raise ValueError(f"unsupported constraint operator: {operator}")
