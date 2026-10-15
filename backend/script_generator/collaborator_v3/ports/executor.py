"""Capability execution port."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..domain.models import ResourceRef


@dataclass(frozen=True)
class ExecutionRequest:
    action_id: str
    capability_id: str
    capability_version: str
    arguments: dict[str, Any]
    #: 能力声明的**结果契约**（设计 §4.8 结果校验阶段）：执行边界用它校验输出，不合格就报
    #: ``TOOL_RESULT_INVALID``（空 = 宽松，不校验）。
    output_schema: dict[str, Any] = field(default_factory=dict)
    input_resources: tuple[ResourceRef, ...] = ()
    #: Runtime 在执行前**已经绑定**好的资源句柄（Phase B / 设计 §4.2）。
    #: 工具不该再自己猜 token 路径：绑定事实从这里取
    #: （id / 版本 / 内容 hash / 类型 / 状态 / 路径）。类型是
    #: ``resource_binding.BoundResource``；用 ``Any`` 避免 ports 反向依赖具体模块。
    bound_resources: tuple[Any, ...] = ()
    permission_grant_ids: tuple[str, ...] = ()
    expected_task_revision: int | None = None
    expected_viewport_revision: int | None = None
    idempotency_key: str = ""
    timeout_seconds: float = 30.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionResult:
    handle_id: str
    status: str
    output: dict[str, Any] = field(default_factory=dict)
    resource_refs: tuple[ResourceRef, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    error_code: str | None = None
    recoverable: bool = False


class Executor(Protocol):
    def execute(self, request: ExecutionRequest) -> ExecutionResult: ...

    def cancel(self, handle_id: str) -> bool: ...
