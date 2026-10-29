"""Small statistical helpers used to promote observed UI experience."""
from __future__ import annotations

import math
from typing import Any


P0 = 0.70
P1 = 0.95
ALPHA_ERROR = 0.01
BETA_ERROR = 0.10


def sprt_update(llr: float, success: bool, *, p0: float = P0, p1: float = P1) -> float:
    """Update a Bernoulli sequential probability ratio without hiding priors."""
    increment = math.log(p1 / p0) if success else math.log((1.0 - p1) / (1.0 - p0))
    return float(llr) + increment


def sprt_bounds(*, alpha_error: float = ALPHA_ERROR,
                beta_error: float = BETA_ERROR) -> tuple[float, float]:
    accept_reliable = math.log((1.0 - beta_error) / alpha_error)
    accept_unreliable = math.log(beta_error / (1.0 - alpha_error))
    return accept_unreliable, accept_reliable


def reliability_status(*, observations: int, llr: float) -> str:
    lower, upper = sprt_bounds()
    if llr >= upper:
        return "trusted"
    if llr <= lower:
        return "contested"
    if observations >= 2:
        return "provisional"
    return "candidate"


def posterior(alpha: float, beta: float) -> dict[str, Any]:
    total = max(1e-9, float(alpha) + float(beta))
    return {
        "alpha": float(alpha),
        "beta": float(beta),
        "mean": round(float(alpha) / total, 6),
    }
