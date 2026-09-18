"""Collaborator Agent：多轮共创状态机（GUI 只渲染 ViewUpdate）。"""

from __future__ import annotations

from backend.script_generator.collaborator.engine import CollaboratorEngine
from backend.script_generator.collaborator.phases import CollabPhase
from backend.script_generator.collaborator.view import CollabViewUpdate

__all__ = ["CollaboratorEngine", "CollabPhase", "CollabViewUpdate"]
