"""Minashigo collaborative Agent V4 MVP.

V4 owns the form protocol and the main-turn loop.  Legacy code is only used
behind adapters for deterministic capabilities while the new runtime matures.
"""

from .runtime import run_v4_turn

__all__ = ["run_v4_turn"]
