"""协作运行时桥：账号窗口绑定、日志、改绑。

由 GUI 注入；Worker 线程通过本模块读取快照 / 执行 retarget。
无桥时工具仍可对显式 hwnd 做纯 Win32 诊断。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from backend.script_generator.runtime_diag import trim_runtime_logs


class RuntimeProvider(Protocol):
    def list_window_accounts(self) -> list[dict[str, Any]]:
        """每项至少可含: name, window_hwnd, effective_hwnd, title, class_name."""

    def recent_log_lines(self, account: str | None = None, *, limit: int = 80) -> list[str]:
        ...

    def apply_window_hwnd(self, account: str, hwnd: int) -> dict[str, Any]:
        """持久化并刷新 UserWindow；返回结果 dict。"""

    def selected_account_name(self) -> str:
        ...


_provider: Optional[RuntimeProvider] = None


def set_runtime_provider(provider: Optional[RuntimeProvider]) -> None:
    global _provider
    _provider = provider


def get_runtime_provider() -> Optional[RuntimeProvider]:
    return _provider


@dataclass
class RuntimeContext:
    accounts: list[dict[str, Any]] = field(default_factory=list)
    selected: str = ""
    logs: list[str] = field(default_factory=list)
    available: bool = False

    def to_prompt_block(self) -> str:
        if not self.available:
            return (
                "【运行时上下文】暂未接入主程序账号/日志桥"
                "（工具仍可对显式 hwnd 做检查）。"
            )
        lines = ["【运行时上下文·窗口与点击】"]
        if self.selected:
            lines.append(f"- 当前选中账号: {self.selected}")
        if not self.accounts:
            lines.append("- 尚无已绑定窗口的账号")
        else:
            for a in self.accounts[:8]:
                name = a.get("name") or "?"
                bh = a.get("window_hwnd")
                eh = a.get("effective_hwnd") or bh
                title = a.get("title") or ""
                extra = ""
                if eh and bh and int(eh) != int(bh):
                    extra = f" → 实际点击窗 {eh}"
                lines.append(
                    f"- {name}: 绑定 hwnd={bh}{extra}"
                    + (f" 「{title}」" if title else "")
                )
        if self.logs:
            lines.append("- 近期相关日志:")
            for ln in self.logs[-24:]:
                lines.append(f"  · {ln[:160]}")
        else:
            lines.append("- 近期相关日志: （空）")
        lines.append(
            "若用户说点了没反应/识图成功但不动：先 inspect_click_target，"
            "必要时 message_click_probe / retarget_click_window。"
        )
        return "\n".join(lines)


def build_runtime_context(
    *,
    account: str | None = None,
    log_limit: int = 40,
) -> RuntimeContext:
    p = _provider
    if p is None:
        return RuntimeContext(available=False)
    try:
        accounts = list(p.list_window_accounts() or [])
        selected = (account or p.selected_account_name() or "").strip()
        raw_logs = p.recent_log_lines(selected or None, limit=max(log_limit * 2, 80))
        logs = trim_runtime_logs(raw_logs, limit=log_limit)
        return RuntimeContext(
            accounts=accounts,
            selected=selected,
            logs=logs,
            available=True,
        )
    except Exception as e:
        return RuntimeContext(
            available=True,
            logs=[f"（读取运行时失败: {e}）"],
        )


def resolve_account_hwnd(
    account: str | None = None,
    hwnd: int | None = None,
) -> dict[str, Any]:
    """从参数或桥解析要检查的 hwnd。"""
    if hwnd:
        return {"ok": True, "hwnd": int(hwnd), "account": account or ""}
    p = _provider
    if p is None:
        return {
            "ok": False,
            "error": "未指定 hwnd，且未接入运行时桥；请传入 hwnd 或从主程序打开协作",
        }
    name = (account or p.selected_account_name() or "").strip()
    accounts = p.list_window_accounts() or []
    hit = None
    if name:
        for a in accounts:
            if str(a.get("name") or "") == name:
                hit = a
                break
    if hit is None and accounts:
        hit = accounts[0]
        name = str(hit.get("name") or "")
    if hit is None:
        return {"ok": False, "error": "没有已绑定窗口的账号"}
    h = hit.get("effective_hwnd") or hit.get("window_hwnd")
    if not h:
        return {"ok": False, "error": f"账号「{name}」无 window_hwnd"}
    return {
        "ok": True,
        "hwnd": int(h),
        "account": name,
        "bound_hwnd": int(hit.get("window_hwnd") or h),
        "title": hit.get("title") or "",
    }
