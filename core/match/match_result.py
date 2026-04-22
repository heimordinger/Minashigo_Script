# core/match/match_result.py
import math
from dataclasses import dataclass
from typing import Optional


@dataclass
class MatchResult:
    x: Optional[int]
    y: Optional[int]
    max_val: float
    match_success : bool = None

    def __iter__(self):
        yield self.x
        yield self.y
        yield self.max_val

    def __getitem__(self, key):
        if key in ("x", 0):
            return self.x
        if key in ("y", 1):
            return self.y
        if key in ("score", "max_val", 2):
            return self.max_val
        raise KeyError(key)

    @property
    def score(self):
        return self.max_val

    def __bool__(self):
        if self.match_success is not None:
            return self.match_success
        return self.x is not None and self.y is not None

    def to_dict(self):
        return {
            "x": self.x,
            "y": self.y,
            "score": self.max_val
        }