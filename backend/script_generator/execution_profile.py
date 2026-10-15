"""执行目标 → runtime profile 的**唯一**本地解析入口（决定 §1）。

profile 是 **Runtime 事实**，优先级：

1. 当前任务明确绑定的执行目标（``account["_target"]``，由 GUI 在用户选择后写入）；
2. 当前选中账号在 Controller 里对应的**真实实例类型**
   （``_window_instances`` / ``_browser_instances``，权威来源）；
3. 基线脚本的 profile（provisional，调用方自行传入 ``baseline_profile``）；
4. 都没有 → ``unresolved``（**不猜** desktop/browser）。

历史上这段逻辑在 GUI 里有两份，而且查的字典不同
（``mixin_collab`` 查 ``ctrl._browsers``，``mixin_runtime`` 查 ``ctrl._browser_instances``），
可能给出相反倾向。现在统一到这里。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from .api_contracts import registry

#: 解析来源（写进日志/凭证，便于回答"这个 profile 从哪来"）。
SOURCE_TASK_TARGET = "task_target"
SOURCE_CONTROLLER_WINDOW = "controller.window_instances"
SOURCE_CONTROLLER_BROWSER = "controller.browser_instances"
SOURCE_BASELINE = "baseline_script"
SOURCE_UNRESOLVED = "unresolved"

#: 可信度：authoritative = 可直接签发绑定 profile 的凭证；provisional = 只能暂用。
CONFIDENCE_AUTHORITATIVE = "authoritative"
CONFIDENCE_PROVISIONAL = "provisional"
CONFIDENCE_NONE = "none"

_WINDOW_PROFILE = "desktop_user_window"
_BROWSER_PROFILE = "browser_user_browser"


@dataclass(frozen=True)
class ProfileResolution:
    """一次 profile 解析的结果（可结构化保存，禁止靠猜）。"""

    profile: str
    source: str
    account: str = ""
    confidence: str = CONFIDENCE_NONE
    #: 执行目标版本。目标变化（换账号 / 换执行类型 / 重新绑定窗口）后必须变化，
    #: 从而让绑定它的旧 ``ValidationReceipt`` 失效。
    target_revision: str = ""

    @property
    def resolved(self) -> bool:
        return self.profile != registry.UNRESOLVED_PROFILE

    @property
    def authoritative(self) -> bool:
        return self.confidence == CONFIDENCE_AUTHORITATIVE

    def to_dict(self) -> dict[str, str]:
        """用户指定的记录形状（贯穿 runner → validator → materializer → receipt）。"""
        return {
            "runtime_profile": self.profile,
            "profile_source": self.source,
            "target_revision": self.target_revision,
            "account_ref": self.account,
            "confidence": self.confidence,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> "ProfileResolution":
        """从 ``to_dict()`` 形状或任意 dict 还原（GUI → worker 线程靠它传递）。"""
        data = dict(payload or {})
        profile = str(data.get("runtime_profile") or data.get("profile") or "")
        if profile and not registry.has_profile(profile):
            profile = registry.UNRESOLVED_PROFILE
        return cls(
            profile=profile or registry.UNRESOLVED_PROFILE,
            source=str(data.get("profile_source") or data.get("source") or SOURCE_UNRESOLVED),
            account=str(data.get("account_ref") or data.get("account") or ""),
            confidence=str(data.get("confidence") or CONFIDENCE_NONE),
            target_revision=str(data.get("target_revision") or ""),
        )


def _window_fingerprint(controller: Any, name: str) -> str:
    inst = (getattr(controller, "_window_instances", None) or {}).get(name)
    if inst is None:
        return ""
    hwnd = getattr(getattr(inst, "_target", None), "hwnd", None)
    if callable(hwnd):
        try:
            hwnd = hwnd()
        except Exception:  # noqa: BLE001
            hwnd = None
    return "hwnd:{}".format(hwnd) if hwnd else ""


def _browser_fingerprint(controller: Any, name: str) -> str:
    inst = (getattr(controller, "_browser_instances", None) or {}).get(name)
    for obj in (getattr(inst, "_obj", None), inst):
        if obj is None:
            continue
        for attr in ("port", "debug_port"):
            value = getattr(obj, attr, None)
            if value:
                return "port:{}".format(value)
    return ""


def target_revision_for(
    controller: Any, name: str, profile: str, source: str
) -> str:
    """算执行目标版本（短 hash）。

    组成：``profile + source + account + 目标实例指纹``。指纹是**尽力而为**：
    窗口取 ``hwnd``，浏览器取调试端口；取不到就退化为只按
    ``profile+source+account`` 计 —— 此时"同一账号换了个窗口"不会改变版本，
    这是已知限制（比伪造一个版本号更诚实）。
    """
    fingerprint = ""
    if profile == _WINDOW_PROFILE:
        fingerprint = _window_fingerprint(controller, name)
    elif profile == _BROWSER_PROFILE:
        fingerprint = _browser_fingerprint(controller, name)
    raw = "|".join([profile, source, name, fingerprint])
    return "tgt_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _has(container: Any, name: str) -> bool:
    try:
        return name in (container or {})
    except TypeError:
        return False


def _resolved(profile: str, source: str, name: str, controller: Any, confidence: str) -> ProfileResolution:
    return ProfileResolution(
        profile=profile,
        source=source,
        account=name,
        confidence=confidence,
        target_revision=target_revision_for(controller, name, profile, source),
    )


def profile_from_target(
    target: Mapping[str, Any] | None,
) -> tuple[str | None, str, str]:
    """执行目标事实 → ``(具体 profile | None, profile_source, target_revision)``。

    **V2 与 V3 都必须经这一个函数解释执行目标**（决定 1：不得各写一套判断）：

    - 缺失 / ``unresolved`` / 未知 profile → ``(None, "unresolved", rev)``：
      调用方必须走 ``portable_common`` 语义，**禁止**落成 desktop/browser；
    - 具体 profile → ``("desktop_user_window", "runtime_instance", "tgt_…")``。
    """
    res = ProfileResolution.from_payload(target)
    profile = None if res.profile == registry.UNRESOLVED_PROFILE else res.profile
    source = res.source if profile else SOURCE_UNRESOLVED
    return profile, source, res.target_revision


def resolve_execution_target(
    account: Any = None,
    controller: Any = None,
    *,
    baseline_profile: str | None = None,
) -> dict[str, str]:
    """**唯一**取执行目标的入口（GUI / V2 / V3 共用，决定 1）。

    返回 :meth:`ProfileResolution.to_dict` 形状，可直接放进 worker 参数、
    ``runtime profile`` 判定与 ticket 签发。拿不到事实时返回
    ``unresolved``（绝不猜 desktop/browser）。
    """
    return resolve_execution_profile(
        account, controller, baseline_profile=baseline_profile
    ).to_dict()


def resolve_execution_profile(
    account: Any = None,
    controller: Any = None,
    *,
    baseline_profile: str | None = None,
) -> ProfileResolution:
    """解析当前应该用哪个 runtime profile（见模块 docstring 的优先级）。"""
    name = ""
    if isinstance(account, dict):
        name = str(account.get("name") or account.get("account") or "")
    elif account is not None:
        name = str(account)

    window_instances = getattr(controller, "_window_instances", None) or {}
    browser_instances = getattr(controller, "_browser_instances", None) or {}
    has_win = _has(window_instances, name) if name else False
    has_browser = _has(browser_instances, name) if name else False

    # 1) 任务明确绑定的执行目标（仅当该目标真的就绪，避免拿着过期偏好硬套）
    target = ""
    if isinstance(account, dict):
        target = str(account.get("_target") or "").strip().lower()
    if target == "window" and has_win:
        return _resolved(_WINDOW_PROFILE, SOURCE_TASK_TARGET, name, controller, CONFIDENCE_AUTHORITATIVE)
    if target == "browser" and has_browser:
        return _resolved(_BROWSER_PROFILE, SOURCE_TASK_TARGET, name, controller, CONFIDENCE_AUTHORITATIVE)

    # 2) Controller 里的真实实例类型（权威）
    if has_win and not has_browser:
        return _resolved(
            _WINDOW_PROFILE, SOURCE_CONTROLLER_WINDOW, name, controller, CONFIDENCE_AUTHORITATIVE
        )
    if has_browser and not has_win:
        return _resolved(
            _BROWSER_PROFILE, SOURCE_CONTROLLER_BROWSER, name, controller, CONFIDENCE_AUTHORITATIVE
        )
    if has_win and has_browser:
        # 两端都就绪且没有明确绑定 → 不能替用户选，交回调用方（可退回注解 provisional）
        if baseline_profile:
            return _resolved(str(baseline_profile), SOURCE_BASELINE, name, controller, CONFIDENCE_PROVISIONAL)
        return ProfileResolution(
            profile=registry.UNRESOLVED_PROFILE,
            source=SOURCE_UNRESOLVED,
            account=name,
            confidence=CONFIDENCE_NONE,
            target_revision=target_revision_for(controller, name, registry.UNRESOLVED_PROFILE, SOURCE_UNRESOLVED),
        )

    # 3) 基线脚本 profile（provisional）
    if baseline_profile:
        return _resolved(str(baseline_profile), SOURCE_BASELINE, name, controller, CONFIDENCE_PROVISIONAL)

    # 4) 没有事实 → unresolved，禁止静默猜
    return ProfileResolution(
        profile=registry.UNRESOLVED_PROFILE,
        source=SOURCE_UNRESOLVED,
        account=name,
        confidence=CONFIDENCE_NONE,
        target_revision=target_revision_for(controller, name, registry.UNRESOLVED_PROFILE, SOURCE_UNRESOLVED),
    )
