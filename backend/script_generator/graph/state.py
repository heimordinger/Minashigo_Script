"""Shared state for the script-generation graph."""

from __future__ import annotations

from typing import Any, Optional, TypedDict


class ScriptGenState(TypedDict, total=False):
    # inputs
    explanation: str
    image_paths: list[str]
    source_dir: str
    provider: str
    api_key: str
    model: str
    api_endpoint: Optional[str]
    send_images: bool
    compress_images: bool
    enable_plan: bool
    max_fix_retries: int
    max_tokens: int
    free_mode: bool

    # intermediate
    plan: str  # display / fallback text
    plan_struct: dict  # normalized structured plan
    split_mode: bool  # True → generate_task × N → merge
    task_index: int
    task_codes: list[str]  # per-task fragments
    code: str
    errors: list[str]
    attempt: int
    stage: str

    # usage
    input_tokens: int
    output_tokens: int

    # callbacks (not serializable; only used in-process)
    on_partial: Any
    on_status: Any
    on_artifact: Any  # (kind: str, payload: str) -> None
