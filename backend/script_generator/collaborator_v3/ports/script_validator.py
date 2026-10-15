"""Port for validating a generated script before it can be committed."""

from __future__ import annotations

from typing import Any, Protocol


class ScriptValidator(Protocol):
    validator_id: str
    validator_version: str

    def validate(self, script: str) -> list[str]: ...

    #: 可选：最终产物闸门（设计 §4.5 / §8.3）。
    #: 实现者返回一个 ``script_artifact.ArtifactVerdict``：校验"素材替换/打包之后
    #: 真正要跑的那一份"，通过时由 Runtime 签发 ``ValidationReceipt``。
    #: 没有这个方法的实现（如测试里的假校验器）会让本轮降级为"仅静态校验"，
    #: Runtime 不会假装自己拿到了凭证。
    def verify_artifact(self, script: str, *, script_name: str = ...) -> Any | None: ...
