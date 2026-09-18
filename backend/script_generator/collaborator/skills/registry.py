"""Skill 注册表。"""

from __future__ import annotations

from typing import Optional

from backend.script_generator.collaborator.skills.base import CollabSkill


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, CollabSkill] = {}

    def register(self, skill: CollabSkill) -> None:
        self._skills[skill.id] = skill

    def get(self, skill_id: str) -> Optional[CollabSkill]:
        return self._skills.get(skill_id)

    def all(self) -> list[CollabSkill]:
        return list(self._skills.values())

    def match_best(self, text: str, *, min_score: float = 0.45) -> Optional[CollabSkill]:
        best: Optional[CollabSkill] = None
        best_score = 0.0
        for sk in self._skills.values():
            try:
                score = float(sk.match(text))
            except Exception:
                continue
            if score > best_score:
                best_score = score
                best = sk
        if best is None or best_score < min_score:
            return None
        return best

    def catalog_lines(self) -> list[str]:
        lines = []
        for sk in self._skills.values():
            lines.append(f"· {sk.title}（skill: {sk.id}）")
        return lines


def default_registry() -> SkillRegistry:
    """当前不预挂玩法 skill：协作页测通用对话 / 画面收纳 / 经典生成分流。"""
    return SkillRegistry()
