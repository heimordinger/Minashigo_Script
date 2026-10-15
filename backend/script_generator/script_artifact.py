"""最终产物的构造与出闸闸门（设计 §4.5 / §8.3 / §9 阶段 D）。

链路（顺序不可换）：

```text
模型源码 → 素材解析 → 打包/替换为最终路径 → 契约校验（source/artifact）
        → import 冒烟 → 假运行冒烟 → ValidationReceipt → finish/commit
```

**Phase 0 §2 的教训**：V3 commit 里存的是带 ``[[asset]]`` 的 raw 稿，而试运行跑的是
GUI 侧 bundle 之后的 ``scripts/_trial/_gen_trial.py``——校验对象和运行对象不是同一份文本。
所以这里把"构造最终产物"抽成**纯函数**，并要求保存页 / 校验页 / 试运行共用它，
凭证同时绑定 ``source_sha256`` 与 ``artifact_sha256``（设计 §14.4）。

本模块**不做真实点击、不联网**；假运行用的是契约驱动的 ``FakeTarget``。
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import os
import threading
import traceback
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .api_contracts import registry, validator
from .api_contracts.receipt import ValidationReceipt, sha256_text

#: 冒烟阶段的 issue code
IMPORT_SMOKE_FAILED = "IMPORT_SMOKE_FAILED"
SMOKE_RUNTIME_ERROR = "SMOKE_RUNTIME_ERROR"
SMOKE_TIMEOUT = "SMOKE_TIMEOUT"
SMOKE_BUDGET = "SMOKE_BUDGET_EXCEEDED"
ENTRY_NOT_CALLABLE = "ENTRY_NOT_CALLABLE"

DEFAULT_SMOKE_BUDGET = 200
DEFAULT_SMOKE_TIMEOUT = 5.0

#: 必须全部通过的检查项（与 receipt.REQUIRED_CHECKS 一致）
CHECK_NAMES = ("source_gate", "artifact_gate", "import_smoke", "fake_runtime_smoke")

#: 素材化流水线版本：任何会改变产出文本的改动都要 +1（receipt 绑定它）。
MATERIALIZER_VERSION = "3"

#: ban gate 阻断：源码缺少必需的禁用区声明（**不是**文本变换，直接拦下）。
BAN_GATE_BLOCKED = "BAN_GATE_BLOCKED"

#: 素材绑定失败的兜底码（正常情况下每个失败都有自己的稳定诊断码；
#: 只有在连码都取不到时才退回这个，避免 issue 没有 code）。
RESOURCE_BINDING_FAILED = "RESOURCE_BINDING_FAILED"

#: 协作画布要求脚本自带的禁用区标记（见 ``collaborator/canvas.py``）。
BAN_ZONES_MARKER = "_COLLAB_BAN_ZONES"

#: 变换链名字（receipt.transforms 用；只记**实际发生**的步骤）。
TRANSFORM_ASSET_RESOLUTION = "asset_resolution"
TRANSFORM_ASSET_BUNDLE = "asset_bundle"
TRANSFORM_PSEUDO_INJECTION = "pseudo_record_injection"
TRANSFORM_PSEUDO_FALLBACK = "pseudo_record_injection_fallback"


# ── 产物构造 ──


@dataclass(frozen=True)
class ArtifactBuild:
    """一次「源码 → 最终产物」的结果。

    三段文本必须分清（决定 §6）：

    - ``source``：模型交付原稿（带 ``[[asset_id]]`` token）→ ``source_sha256``；
    - ``bundled``：素材解析 + 打包 + 路径替换之后、**注入伪录制之前** → ``bundled_sha256``；
    - ``artifact``：**实际写盘并运行**的文本（含伪录制注入/回退）→ ``artifact_sha256``。

    契约/AST/策略检查跑 ``bundled``（模型可写的部分），import/fake-runtime 冒烟跑
    ``artifact``（真正运行的那份）。注入块是我们自己的instrumentation，把它送进
    策略检查只会误报 ``import os``。
    """

    source: str
    normalized: str
    bundled: str
    artifact: str
    resolved_refs: tuple[str, ...] = ()
    unresolved_refs: tuple[str, ...] = ()
    #: 素材绑定的**结构化**失败（Phase B / 设计 §4）：每项含稳定诊断码 + 资源 id。
    #: 非空即代表"有引用没能绑定"→ 产物闸门必须阻断，不许降级成"能生成就生成"。
    resource_failures: tuple[Mapping[str, Any], ...] = ()
    #: 成功绑定的资源事实（id/版本/内容 hash/类型/状态），与工具调用侧同一组字段。
    bound_resources: tuple[Mapping[str, Any], ...] = ()
    asset_versions: Mapping[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    bundle_errors: tuple[str, ...] = ()
    copied: tuple[Mapping[str, str], ...] = ()
    img_root: str = ""
    dry_run: bool = False
    script_name: str = "collab_script"
    transforms: tuple[str, ...] = ()
    ban_blocked: bool = False
    ban_message: str = ""
    materializer_version: str = MATERIALIZER_VERSION

    @property
    def source_sha256(self) -> str:
        return sha256_text(self.source)

    @property
    def normalized_sha256(self) -> str:
        return sha256_text(self.normalized)

    @property
    def bundled_sha256(self) -> str:
        return sha256_text(self.bundled)

    @property
    def artifact_sha256(self) -> str:
        return sha256_text(self.artifact)

    @property
    def changed(self) -> bool:
        return self.artifact != self.source


def _asset_versions(session: Any, refs: Iterable[str]) -> dict[str, Any]:
    """记录素材版本（V2 会话没有版本概念时记 1，不伪造。设计 §21.1 / §28）。"""
    assets = [a for a in (getattr(session, "assets", None) or []) if isinstance(a, dict)]
    out: dict[str, Any] = {}
    for ref in refs or ():
        version: Any = 1
        for item in assets:
            if str(item.get("id") or "") == str(ref) or str(item.get("name") or "") == str(ref):
                version = item.get("version") or item.get("resource_version") or 1
                break
        out[str(ref)] = version
    return out


def _bound_facts(session: Any, resource_ids: Iterable[str]) -> list[Mapping[str, Any]]:
    """把已绑定的资源 id 取回**规范化事实**（与工具调用侧同一组字段）。

    Phase B 的完成标准要求"工具调用与脚本材料化得到相同的 id/版本/内容 hash/类型/状态"，
    所以这里不另算一套，而是让脚本侧也把事实交给同一个 :class:`ResourceBinder`。
    """
    from .resource_binding import ResourceBinder, ResourceBindingError, SessionResourceSource

    binder = ResourceBinder(SessionResourceSource(session), origin="script_token")
    out: list[Mapping[str, Any]] = []
    for resource_id in resource_ids or ():
        try:
            out.append(binder.bind(str(resource_id)).facts())
        except ResourceBindingError:
            continue
    return out


def build_artifact(
    source: str,
    *,
    session: Any = None,
    script_name: str = "collab_script",
    bundle: bool = True,
    img_root: str | Path | None = None,
    dry_run: bool = True,
) -> ArtifactBuild:
    """把模型源码变成**将要运行的那一份**文本。

    ``dry_run=True``（默认）不写任何用户文件——校验路径必须用它；
    保存 / 试运行路径用 ``dry_run=False``，文本与 dry_run 逐字节一致
    （``asset_bundle.bundle_script_assets`` 保证）。
    """
    from .collaborator.asset_bundle import bundle_script_assets
    from .collaborator.collab_form import normalize_collab_script, substitute_asset_refs

    raw = source or ""
    notes: list[str] = []
    normalized = raw
    resolved: list[str] = []
    unresolved: list[str] = []
    bound_facts: list[Mapping[str, Any]] = []
    binding_failures: list[Mapping[str, Any]] = []
    transforms: list[str] = [TRANSFORM_ASSET_RESOLUTION]
    if session is not None:
        try:
            normalized, notes = normalize_collab_script(raw, session)
        except Exception as exc:  # noqa: BLE001 - 归一失败不阻断，交给闸门报
            normalized = raw
            notes = ["归一失败：{}".format(exc)]
        try:
            from .collaborator.collab_form import substitute_asset_refs_detailed

            _text, done, failures = substitute_asset_refs_detailed(raw, session)
            resolved = list(done)
            binding_failures = [failure.to_dict() for failure in failures]
            unresolved = [str(item.get("ref") or "") for item in binding_failures]
            bound_facts = _bound_facts(session, resolved)
        except Exception:
            resolved, unresolved = [], []
    artifact = normalized
    bundle_errors: list[str] = []
    copied: list[Mapping[str, str]] = []
    out_root = ""
    if bundle and session is not None:
        try:
            artifact, result = bundle_script_assets(
                normalized,
                session=session,
                script_stem=script_name,
                img_root=Path(img_root) if img_root else None,
                dry_run=bool(dry_run),
            )
            transforms.append(TRANSFORM_ASSET_BUNDLE)
            bundle_errors = list(result.errors or [])
            copied = list(result.copied or [])
            out_root = str(result.out_dir)
            for ref in list(result.unresolved or []):
                if ref not in unresolved:
                    unresolved.append(ref)
        except Exception as exc:  # noqa: BLE001
            bundle_errors.append("打包异常：{}".format(exc))
            artifact = normalized
    return ArtifactBuild(
        source=raw,
        normalized=normalized,
        bundled=artifact,
        artifact=artifact,
        resolved_refs=tuple(resolved),
        unresolved_refs=tuple(unresolved),
        resource_failures=tuple(binding_failures),
        bound_resources=tuple(bound_facts),
        asset_versions=_asset_versions(session, resolved),
        notes=tuple(notes),
        bundle_errors=tuple(bundle_errors),
        copied=tuple(copied),
        img_root=out_root,
        dry_run=bool(dry_run),
        script_name=str(script_name or "collab_script"),
        transforms=tuple(transforms),
    )


def write_materialized(
    build: ArtifactBuild, path: str | Path, *, package_init: bool = True
) -> str:
    """把 materialize 的产物**原子写盘**，返回落盘文件的 sha256。

    试运行写盘必须走这里：它和校验共用同一个 ``build``，于是
    ``sha256(scripts/_trial/_gen_trial.py) == receipt.artifact_sha256`` 才成立
    （否则 GUI 自己拼一遍、校验再猜一遍，凭证就是空头支票）。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if package_init:
        init_py = target.parent / "__init__.py"
        if not init_py.exists():
            init_py.write_text("# trial package\n", encoding="utf-8")
    # 原子写：避免任务在写盘中途 import 到半截文件
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(build.artifact, encoding="utf-8")
    os.replace(tmp, target)
    return sha256_text(target.read_text(encoding="utf-8"))


def reset_pseudo_injection_cache() -> None:
    """测试用：清掉注入结果的缓存（见 :func:`_cached_injection`）。"""
    _INJECTION_CACHE.clear()


def _cached_injection(code: str, script_name: str) -> str:
    """注入结果缓存：同一 (code, script_name) 只算一次，保证逐字节可复现。"""
    key = (code, str(script_name or ""))
    hit = _INJECTION_CACHE.get(key)
    if hit is None:
        from .pseudo_codegen import inject_pseudo_record_for_trial

        hit = inject_pseudo_record_for_trial(code, script_name=str(script_name or ""))
        _INJECTION_CACHE[key] = hit
    return hit


_INJECTION_CACHE: dict[tuple[str, str], str] = {}


def materialize_trial_script(
    source: str,
    *,
    session: Any = None,
    script_name: str = "collab_script",
    bundle: bool = True,
    already_bundled: bool = False,
    img_root: str | Path | None = None,
    dry_run: bool = True,
    ban_snippet: str = "",
    with_pseudo_record: bool = True,
) -> ArtifactBuild:
    """**唯一**的「源码 → 实际运行文本」流水线（决定 §6）。

    顺序与真实试运行完全一致：

    1. **ban gate**：``ban_snippet`` 非空而源码缺 ``_COLLAB_BAN_ZONES`` → 阻断
       （``ban_blocked=True``，**不改文本**：它是 gate，不是变换）；
    2. 素材解析 / 打包 / 路径替换（``already_bundled=True`` 时跳过：GUI 的交付
       路径已经做过，避免二次打包）；
    3. **伪录制注入 + 编译失败回退原文**（与 ``_write_trial_file`` 原逻辑同一实现）；
    4. 产出 ``source/bundled/artifact`` 三个 hash 与 ``transforms`` 变换链。

    保存页、validate、commit、试运行都必须调用它，禁止各自再拼一遍。
    """
    ban_blocked = False
    ban_message = ""
    snippet = str(ban_snippet or "").strip()
    if snippet and BAN_ZONES_MARKER not in (source or ""):
        ban_blocked = True
        ban_message = (
            "源码缺少 {} 禁用区声明；禁止区是阻断条件，不会被自动补写".format(BAN_ZONES_MARKER)
        )

    if already_bundled:
        build = ArtifactBuild(
            source=source or "",
            normalized=source or "",
            bundled=source or "",
            artifact=source or "",
            script_name=str(script_name or "collab_script"),
            dry_run=bool(dry_run),
        )
    else:
        build = build_artifact(
            source,
            session=session,
            script_name=script_name,
            bundle=bundle,
            img_root=img_root,
            dry_run=dry_run,
        )

    transforms = list(build.transforms)
    artifact = build.bundled
    if with_pseudo_record:
        injected = _cached_injection(build.bundled, str(script_name or "collab_script"))
        if injected == build.bundled:
            transforms.append(TRANSFORM_PSEUDO_FALLBACK)
        else:
            transforms.append(TRANSFORM_PSEUDO_INJECTION)
        artifact = injected

    return dataclasses.replace(
        build,
        artifact=artifact,
        script_name=str(script_name or "collab_script"),
        transforms=tuple(transforms),
        ban_blocked=ban_blocked,
        ban_message=ban_message,
        materializer_version=MATERIALIZER_VERSION,
    )


# ── 冒烟：假执行器 ──


class SmokeBudgetExceeded(RuntimeError):
    """假运行调用次数超预算（说明脚本在假环境下停不下来）。"""


def _default_return(item) -> Any:
    kind = (item.returns.type or "").strip().lower()
    if kind.startswith("bool"):
        return False
    if kind.startswith("list"):
        return []
    if kind.startswith("dict"):
        return {}
    return None


class FakeTarget:
    """契约驱动的假执行器：只记录调用，不做任何真实副作用。

    方法表来自 ``registry.approved_contracts(profile, allow_login=True)``，
    因此"脚本调了契约外的方法"会直接 ``AttributeError``——与真实执行器的
    ``__getattr__`` 行为一致（不会有第二个方法表）。
    """

    def __init__(self, profile: str | None = None, *, budget: int = DEFAULT_SMOKE_BUDGET) -> None:
        self.runtime_profile = registry.effective_profile(profile)
        self.calls: list[dict[str, Any]] = []
        self.call_count = 0
        self.budget = max(1, int(budget))
        # 脚本常读的属性
        self.account: dict[str, Any] = {}
        self.title = ""
        self.hwnd = 0
        self.use_polling_temp_cache = False
        for item in registry.approved_contracts(self.runtime_profile, allow_login=True):
            setattr(self, item.name, self._make_method(item))

    def _make_method(self, item):
        def _record(*args: Any, **kwargs: Any) -> Any:
            self.call_count += 1
            if self.call_count > self.budget:
                raise SmokeBudgetExceeded(
                    "假运行调用超过 {} 次（脚本在假环境下没有收敛）".format(self.budget)
                )
            if len(self.calls) < 200:
                self.calls.append(
                    {
                        "name": item.name,
                        "positional": len(args),
                        "keywords": sorted(kwargs),
                    }
                )
            return _default_return(item)

        if item.is_async:

            async def _async(*args: Any, **kwargs: Any) -> Any:
                return _record(*args, **kwargs)

            return _async
        return _record

    def called_names(self) -> list[str]:
        return [c["name"] for c in self.calls]


def _exec_module(code: str, module_name: str) -> types.ModuleType:
    """按"模块导入"的语义执行顶层代码（不写文件、不 import 真实执行器）。

    ``dont_inherit=True`` 很关键：否则 ``compile`` 会继承**本模块**的
    ``from __future__ import annotations``，把产物里的注解变成字符串，
    于是"未定义的注解名"在冒烟里漏检（实测踩到）。真实 ``importlib``
    加载脚本时只认脚本自己的 future 声明，这里必须一致。
    """
    module = types.ModuleType(module_name)
    module.__dict__["__name__"] = module_name
    module.__dict__["__file__"] = "<collab-artifact>"
    exec(compile(code, "<collab-artifact>", "exec", dont_inherit=True), module.__dict__)
    return module


def _issue(
    code: str,
    message: str,
    *,
    repair_hint: str = "",
    line: int = 0,
    severity: str = "error",
) -> validator.ContractIssue:
    return validator.ContractIssue(
        code=code, message=message, line=line, repair_hint=repair_hint, severity=severity
    )


def _exception_issue(code: str, prefix: str, exc: BaseException) -> validator.ContractIssue:
    tail = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    ).strip()
    last = tail.splitlines()[-1] if tail else repr(exc)
    return _issue(
        code,
        "{}: {}（{}）".format(prefix, type(exc).__name__, last),
        repair_hint=tail[-600:],
    )


def import_smoke(
    artifact: str,
    *,
    module_name: str = "collab_smoke_artifact",
    entry_name: str = "do_work",
) -> tuple[types.ModuleType | None, list[validator.ContractIssue]]:
    """隔离命名空间加载最终产物，确认 import/注解/顶层代码不抛异常。

    不启动浏览器、不点击、不执行 ``do_work``（设计 §8.3）。
    """
    try:
        module = _exec_module(artifact, module_name)
    except Exception as exc:  # noqa: BLE001
        return None, [_exception_issue(IMPORT_SMOKE_FAILED, "import 冒烟失败", exc)]
    fn = getattr(module, entry_name, None)
    if fn is None:
        return module, [
            _issue(ENTRY_NOT_CALLABLE, "最终产物里没有 {}".format(entry_name), repair_hint="补 async def do_work(browser)")
        ]
    if not callable(fn):
        return module, [_issue(ENTRY_NOT_CALLABLE, "{} 不是可调用对象".format(entry_name))]
    return module, []


def _run_coroutine(coro_factory, timeout: float) -> None:
    """跑一段协程；已在事件循环里时换线程跑，避免 asyncio.run 报错。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(asyncio.wait_for(coro_factory(), timeout))
        return

    box: dict[str, BaseException] = {}

    def _worker() -> None:
        try:
            asyncio.run(asyncio.wait_for(coro_factory(), timeout))
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout + 5.0)
    if thread.is_alive():
        raise TimeoutError("假运行冒烟线程超时未结束")
    if "error" in box:
        raise box["error"]


def fake_runtime_smoke(
    artifact: str,
    *,
    profile: str | None = None,
    entry_name: str = "do_work",
    budget: int = DEFAULT_SMOKE_BUDGET,
    timeout: float = DEFAULT_SMOKE_TIMEOUT,
) -> tuple[FakeTarget | None, list[validator.ContractIssue]]:
    """用假执行器跑一遍 ``do_work``：证明"不立即异常"，不证明业务正确（设计 §19.2）。"""
    module, issues = import_smoke(artifact, entry_name=entry_name)
    if issues:
        return None, issues
    assert module is not None
    fn = getattr(module, entry_name)
    if not inspect.iscoroutinefunction(fn):
        return None, [
            _issue(ENTRY_NOT_CALLABLE, "{} 必须是 async 函数".format(entry_name), repair_hint="加 async")
        ]
    fake = FakeTarget(profile, budget=budget)

    async def _call() -> None:
        await fn(fake)

    try:
        _run_coroutine(_call, float(timeout))
    except SmokeBudgetExceeded as exc:
        # 有界即通过：脚本在假环境下停不下来，只是"没收敛"，不是 API 用错。
        # 记成 warning：设计 §19.2 明确 Fake Runtime 只证明"不立即异常"。
        return fake, [
            _issue(
                SMOKE_BUDGET,
                str(exc),
                repair_hint="假环境下命中分支会一直循环；确认真实环境下有退出条件",
                severity="warning",
            )
        ]
    except (asyncio.TimeoutError, TimeoutError) as exc:
        return fake, [_issue(SMOKE_TIMEOUT, "假运行冒烟超时：{}".format(exc))]
    except Exception as exc:  # noqa: BLE001
        return fake, [_exception_issue(SMOKE_RUNTIME_ERROR, "假运行冒烟异常", exc)]
    return fake, []


# ── 出闸闸门 ──


@dataclass(frozen=True)
class ArtifactVerdict:
    """两阶段校验 + 冒烟的结论。"""

    build: ArtifactBuild
    runtime_profile: str
    issues: tuple[validator.ContractIssue, ...] = ()
    checks: Mapping[str, bool] = field(default_factory=dict)
    fake_calls: tuple[str, ...] = ()
    #: profile 的来源：``explicit``（权威）/ ``annotation``（provisional）/ ``unresolved``。
    #: 只有 explicit 才允许签发绑定 profile 的凭证（决定 §13.1）。
    profile_source: str = ""
    #: 执行目标版本与账号引用（决定 §1：目标变化后旧凭证必须失效）。
    target_revision: str = ""
    account_ref: str = ""

    @property
    def errors(self) -> tuple[validator.ContractIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "error")

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.checks) and all(self.checks.values())

    @property
    def api_contract_revision(self) -> str:
        return registry.contracts_revision()

    def legacy_strings(self) -> list[str]:
        return validator.legacy_strings(self.issues)

    def to_receipt(self, **ids: Any) -> ValidationReceipt | None:
        """通过时签发凭证；没通过返回 None（禁止给没过闸的产物发凭证）。"""
        if not self.ok:
            return None
        return ValidationReceipt.create(
            source_sha256=self.build.source_sha256,
            artifact_sha256=self.build.artifact_sha256,
            bundled_sha256=self.build.bundled_sha256,
            script_name=self.build.script_name,
            materializer_version=self.build.materializer_version,
            transforms=self.build.transforms,
            validated_profiles=(self.runtime_profile,),
            runtime_profile=self.runtime_profile,
            profile_source=self.profile_source,
            target_revision=self.target_revision,
            account_ref=self.account_ref,
            api_contract_revision=self.api_contract_revision,
            checks=dict(self.checks),
            asset_versions=self.build.asset_versions,
            **ids,
        )


def verify_artifact(
    source: str,
    *,
    session: Any = None,
    profile: str | None = None,
    script_name: str = "collab_script",
    allow_login: bool = False,
    bundle: bool = True,
    img_root: str | Path | None = None,
    smoke: bool = True,
    budget: int = DEFAULT_SMOKE_BUDGET,
    timeout: float = DEFAULT_SMOKE_TIMEOUT,
    ban_snippet: str = "",
    with_pseudo_record: bool = True,
    profile_source: str = "",
    target_revision: str = "",
    account_ref: str = "",
) -> ArtifactVerdict:
    """完整的最终产物闸门。默认 ``dry_run`` 打包：校验不写用户文件。

    校验分工（决定 §6）：AST/契约/策略检查跑 ``bundled``（模型可写的那部分），
    import / fake-runtime 冒烟跑 ``artifact``（**实际运行**的那份，含我们自己的
    伪录制注入）。这样既不放过模型代码里的问题，也不会把 instrumentation 的
    ``import os`` 误报成越权。
    """
    raw = source or ""
    # profile 政策（决定 §13.1）：显式 > 注解（provisional）> unresolved。
    # unresolved 不绑定任何真实类，必须落到 portable_common 安全交集再校验/冒烟。
    explicit_source = str(profile_source or "")
    profile_source = explicit_source or validator.profile_source_for_code(raw, explicit=profile)
    if not explicit_source and str(profile or ""):
        profile_source = validator.PROFILE_SOURCE_EXPLICIT
    target_profile = registry.effective_profile(
        validator.profile_for_code(raw, explicit=profile)
    )
    build = materialize_trial_script(
        raw,
        session=session,
        script_name=script_name,
        bundle=bundle,
        img_root=img_root,
        dry_run=True,
        ban_snippet=ban_snippet,
        with_pseudo_record=with_pseudo_record,
    )

    issues: list[validator.ContractIssue] = []
    # ban gate：阻断式，不是文本变换
    if build.ban_blocked:
        issues.append(
            validator.ContractIssue(
                code=BAN_GATE_BLOCKED,
                message=build.ban_message,
                severity="error",
                repair_hint="让引擎重新产出带 {} 的脚本；不要依赖自动补写".format(
                    BAN_ZONES_MARKER
                ),
            )
        )
    # 素材绑定（Phase B / 设计 §4.2）：绑不上就是**结构化失败**，不许降级成"能生成就生成"。
    # 每个失败带自己的稳定诊断码（RESOURCE_NOT_FOUND / VERSION_EXPIRED / TYPE_MISMATCH …）。
    for failure in build.resource_failures:
        code = str(failure.get("code") or RESOURCE_BINDING_FAILED)
        issues.append(
            validator.ContractIssue(
                code=code,
                message="素材绑定失败：{}".format(failure.get("message") or failure.get("ref") or ""),
                symbol=str(failure.get("ref") or failure.get("resource_id") or ""),
                severity="error",
                repair_hint=(
                    "先用 resource.search / search_resources 查到**当前**资源 id 与版本，"
                    "再写 [[资源id]]；不要用文件名或旧 id 碰运气"
                ),
            )
        )
    # source gate：裸 token（原稿） + 归一后产物的入口/契约检查
    issues.extend(validator.asset_token_source_issues(raw))
    source_issues = validator.validate_source(
        build.normalized or raw, profile=target_profile, allow_login=allow_login
    )
    issues.extend(source_issues)
    source_gate = not [i for i in source_issues if i.severity == "error"]

    # artifact gate：打包后的**模型可写文本**（含残留 token），不含我们的注入块
    artifact_issues = validator.validate_artifact(
        build.bundled, profile=target_profile, allow_login=allow_login
    )
    issues.extend(artifact_issues)
    artifact_gate = not [i for i in artifact_issues if i.severity == "error"]
    if build.ban_blocked:
        artifact_gate = False

    checks: dict[str, bool] = {
        "source_gate": source_gate,
        "artifact_gate": artifact_gate,
        "import_smoke": False,
        "fake_runtime_smoke": False,
    }
    fake_calls: tuple[str, ...] = ()
    if smoke and source_gate and artifact_gate:
        _module, import_issues = import_smoke(build.artifact)
        issues.extend(import_issues)
        checks["import_smoke"] = not import_issues
        if not import_issues and checks["import_smoke"]:
            fake, smoke_issues = fake_runtime_smoke(
                build.artifact, profile=target_profile, budget=budget, timeout=timeout
            )
            # 只有真正的运行异常/超时才挡住出闸；"假环境没收敛"记为警告。
            blocking = [i for i in smoke_issues if i.code in (SMOKE_RUNTIME_ERROR, SMOKE_TIMEOUT)]
            issues.extend(smoke_issues)
            checks["fake_runtime_smoke"] = not blocking
            if fake is not None:
                fake_calls = tuple(fake.called_names())
    if not smoke:
        # 明确声明未做冒烟：不允许拿它当"通过"（设计 §11）。
        checks["import_smoke"] = False
        checks["fake_runtime_smoke"] = False

    return ArtifactVerdict(
        build=build,
        runtime_profile=target_profile,
        issues=tuple(issues),
        checks=checks,
        fake_calls=fake_calls,
        profile_source=profile_source,
        target_revision=str(target_revision or ""),
        account_ref=str(account_ref or ""),
    )
