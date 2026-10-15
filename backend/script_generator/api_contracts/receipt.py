"""``ValidationReceipt``：把"校验通过"绑定到确定的一份源码/产物/素材/契约/环境。

设计依据 §14.4：

> 该副轮对这一份确定的源码、最终产物、素材版本、运行环境和契约版本完成了检验。

任一绑定项变化，旧凭证失效并重新校验。**禁止**实现成"某个副轮曾经校验通过，
所以该主轮任意代码都能出闸"。

本模块是纯值对象 + 纯函数，不做 I/O；落盘见
``collaborator_v3/runtime/validation_store.py``。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

#: 校验器版本：契约校验 + 最终产物闸门 + 冒烟。
VALIDATOR_VERSION = "2"

#: 凭证结构版本。
RECEIPT_SCHEMA_VERSION = 1

#: 要求必须通过的检查项（设计 §14.4 checks）。
REQUIRED_CHECKS = ("source_gate", "artifact_gate", "import_smoke", "fake_runtime_smoke")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_validation_id() -> str:
    """新的凭证 id（Phase 3 会换成后端签发的层级 ID，格式不参与业务逻辑）。"""
    return "val_" + uuid.uuid4().hex[:16]


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ValidationReceipt:
    """一次完整校验的凭证。"""

    validation_id: str
    task_id: str = ""
    main_turn_id: str = ""
    run_id: str = ""
    subturn_id: str = ""
    action_id: str = ""
    runtime_profile: str = ""
    source_sha256: str = ""
    artifact_sha256: str = ""
    #: 素材解析 + 打包之后、伪录制注入之前的文本 hash（决定 §6：三段都要能溯源）。
    bundled_sha256: str = ""
    #: 产物脚本名。``IMG_DIR`` 与注入内容都依赖它 → 改名必须让凭证失效。
    script_name: str = ""
    #: 素材化流水线版本（materializer 改动必须 +1）。
    materializer_version: str = ""
    #: 实际发生的变换链，如 ``("asset_resolution","asset_bundle","pseudo_record_injection")``。
    transforms: tuple[str, ...] = ()
    #: 该凭证覆盖的 profile 列表。``portable_common`` 时必须同时通过两端契约。
    validated_profiles: tuple[str, ...] = ()
    #: profile 来源（权威 vs provisional）。决定 §1：注解只能作 provisional source。
    profile_source: str = ""
    #: 执行目标版本 + 账号引用。目标变化后旧凭证必须失效。
    target_revision: str = ""
    account_ref: str = ""
    api_contract_revision: str = ""
    asset_versions: Mapping[str, Any] = field(default_factory=dict)
    validator_version: str = VALIDATOR_VERSION
    checks: Mapping[str, bool] = field(default_factory=dict)
    created_at: str = ""
    schema_version: int = RECEIPT_SCHEMA_VERSION

    # ── 构造 / 序列化 ──

    @classmethod
    def create(
        cls,
        *,
        source_sha256: str,
        artifact_sha256: str,
        runtime_profile: str,
        api_contract_revision: str,
        checks: Mapping[str, bool],
        asset_versions: Mapping[str, Any] | None = None,
        bundled_sha256: str = "",
        script_name: str = "",
        materializer_version: str = "",
        transforms: Iterable[str] | None = None,
        validated_profiles: Iterable[str] | None = None,
        profile_source: str = "",
        target_revision: str = "",
        account_ref: str = "",
        task_id: str = "",
        main_turn_id: str = "",
        run_id: str = "",
        subturn_id: str = "",
        action_id: str = "",
        validation_id: str = "",
        validator_version: str = VALIDATOR_VERSION,
    ) -> "ValidationReceipt":
        return cls(
            validation_id=validation_id or new_validation_id(),
            task_id=str(task_id or ""),
            main_turn_id=str(main_turn_id or ""),
            run_id=str(run_id or ""),
            subturn_id=str(subturn_id or ""),
            action_id=str(action_id or ""),
            runtime_profile=str(runtime_profile or ""),
            source_sha256=str(source_sha256 or ""),
            artifact_sha256=str(artifact_sha256 or ""),
            bundled_sha256=str(bundled_sha256 or ""),
            script_name=str(script_name or ""),
            materializer_version=str(materializer_version or ""),
            transforms=tuple(str(t) for t in (transforms or ())),
            validated_profiles=tuple(str(p) for p in (validated_profiles or ())),
            profile_source=str(profile_source or ""),
            target_revision=str(target_revision or ""),
            account_ref=str(account_ref or ""),
            api_contract_revision=str(api_contract_revision or ""),
            asset_versions=dict(asset_versions or {}),
            validator_version=str(validator_version or VALIDATOR_VERSION),
            checks={k: bool(v) for k, v in dict(checks or {}).items()},
            created_at=_now(),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["asset_versions"] = dict(self.asset_versions)
        payload["checks"] = dict(self.checks)
        payload["transforms"] = list(self.transforms)
        payload["validated_profiles"] = list(self.validated_profiles)
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ValidationReceipt":
        data = dict(payload or {})
        data.setdefault("validation_id", "")
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        for key in ("transforms", "validated_profiles"):
            if key in data and isinstance(data[key], list):
                data[key] = tuple(str(x) for x in data[key])
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def from_json(cls, text: str) -> "ValidationReceipt":
        return cls.from_dict(json.loads(text or "{}"))

    # ── 判定 ──

    @property
    def failed_checks(self) -> tuple[str, ...]:
        return tuple(name for name in REQUIRED_CHECKS if not bool(self.checks.get(name)))

    @property
    def all_checks_passed(self) -> bool:
        return not self.failed_checks

    def mismatches(
        self,
        *,
        source_sha256: str | None = None,
        artifact_sha256: str | None = None,
        bundled_sha256: str | None = None,
        script_name: str | None = None,
        materializer_version: str | None = None,
        runtime_profile: str | None = None,
        profile_source: str | None = None,
        target_revision: str | None = None,
        api_contract_revision: str | None = None,
        asset_versions: Mapping[str, Any] | None = None,
    ) -> list[str]:
        """列出与当前绑定项不符的地方（空 = 凭证仍然有效）。"""
        out: list[str] = []
        if source_sha256 is not None and str(source_sha256) != self.source_sha256:
            out.append("源码 hash 与凭证不符（源码已变，必须重新校验）")
        if artifact_sha256 is not None and str(artifact_sha256) != self.artifact_sha256:
            out.append("最终产物 hash 与凭证不符（产物已变，必须重新校验）")
        if bundled_sha256 is not None and str(bundled_sha256) != self.bundled_sha256:
            out.append("打包产物 hash 与凭证不符（打包结果已变，必须重新校验）")
        if script_name is not None and str(script_name) != self.script_name:
            out.append(
                "脚本名不符（凭证={} 当前={}）；IMG_DIR 与注入内容都随名字变化".format(
                    self.script_name, script_name
                )
            )
        if (
            materializer_version is not None
            and str(materializer_version) != self.materializer_version
        ):
            out.append(
                "素材化流水线版本不符（凭证={} 当前={}）；产出文本可能已不同".format(
                    self.materializer_version, materializer_version
                )
            )
        if runtime_profile is not None and str(runtime_profile) != self.runtime_profile:
            out.append(
                "runtime profile 不符（凭证={} 当前={}）".format(self.runtime_profile, runtime_profile)
            )
        # 执行目标版本：两边都有值时才比较（拿不到目标版本时不假装"没变"）。
        if (
            target_revision
            and self.target_revision
            and str(target_revision) != self.target_revision
        ):
            out.append(
                "执行目标已变化（凭证={} 当前={}）；必须重新校验".format(
                    self.target_revision, target_revision
                )
            )
        # 权威性降级也算不符：凭证声称来自 Runtime 事实，现在只剩注解 provisional。
        if (
            profile_source is not None
            and str(profile_source) != self.profile_source
            and self.profile_source
            and str(profile_source)
            and str(profile_source) != "annotation"
        ):
            out.append(
                "profile 来源不符（凭证={} 当前={}）".format(
                    self.profile_source, profile_source
                )
            )
        if api_contract_revision is not None and str(api_contract_revision) != self.api_contract_revision:
            out.append(
                "API 契约 revision 不符（凭证={} 当前={}）".format(
                    self.api_contract_revision, api_contract_revision
                )
            )
        if asset_versions is not None and dict(asset_versions) != dict(self.asset_versions):
            out.append("素材版本与凭证不符（素材换版后必须重新校验）")
        failed = self.failed_checks
        if failed:
            out.append("凭证里有未通过的检查项: {}".format(", ".join(failed)))
        return out

    def is_valid_for(
        self,
        *,
        source_sha256: str | None = None,
        artifact_sha256: str | None = None,
        bundled_sha256: str | None = None,
        script_name: str | None = None,
        materializer_version: str | None = None,
        runtime_profile: str | None = None,
        profile_source: str | None = None,
        target_revision: str | None = None,
        api_contract_revision: str | None = None,
        asset_versions: Mapping[str, Any] | None = None,
    ) -> bool:
        return not self.mismatches(
            source_sha256=source_sha256,
            artifact_sha256=artifact_sha256,
            bundled_sha256=bundled_sha256,
            script_name=script_name,
            materializer_version=materializer_version,
            runtime_profile=runtime_profile,
            profile_source=profile_source,
            target_revision=target_revision,
            api_contract_revision=api_contract_revision,
            asset_versions=asset_versions,
        )
