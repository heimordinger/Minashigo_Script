"""Evidence-backed, non-authoritative memory for the visual agent.

The package deliberately does not expose a planner.  It records what happened
and estimates reliability; runtime policy remains unchanged while the memory is
in shadow mode.
"""

from .store import VisualMemoryStore

__all__ = ["VisualMemoryStore"]
