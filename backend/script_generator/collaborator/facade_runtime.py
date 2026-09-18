# -*- coding: utf-8 -*-
"""GUI → 协作运行时桥实现。"""

from __future__ import annotations

from typing import Any, Optional


class FacadeRuntimeProvider:
    """从 ScriptGenerator host + facade.controller 取窗口与日志。"""

    def __init__(self, host_panel) -> None:
        self._host = host_panel

    def _facade(self):
        return getattr(self._host, "_facade", None)

    def _ctrl(self):
        fac = self._facade()
        return getattr(fac, "controller", None) if fac else None

    def selected_account_name(self) -> str:
        try:
            acc = self._host._selected_account()
            return str((acc or {}).get("name") or "")
        except Exception:
            return ""

    def list_window_accounts(self) -> list[dict[str, Any]]:
        ctrl = self._ctrl()
        fac = self._facade()
        if fac is None:
            return []
        out: list[dict[str, Any]] = []
        try:
            accounts = list(fac.list_accounts() or [])
        except Exception:
            accounts = []
        wins = getattr(ctrl, "_window_instances", {}) if ctrl else {}
        for acc in accounts:
            name = str(acc.get("name") or "")
            if not name:
                continue
            hwnd = acc.get("window_hwnd")
            uw = wins.get(name) if isinstance(wins, dict) else None
            effective = None
            title = ""
            cls = ""
            if uw is not None:
                try:
                    effective = int(uw.hwnd)
                except Exception:
                    effective = None
                try:
                    t = getattr(uw, "_target", None)
                    title = str(getattr(t, "title", "") or "")
                    cls = str(getattr(t, "class_name", "") or "")
                except Exception:
                    pass
            if hwnd is None and effective is None:
                continue
            out.append(
                {
                    "name": name,
                    "window_hwnd": int(hwnd) if hwnd else effective,
                    "effective_hwnd": effective or (int(hwnd) if hwnd else None),
                    "title": title,
                    "class_name": cls,
                }
            )
        return out

    def recent_log_lines(
        self, account: str | None = None, *, limit: int = 80
    ) -> list[str]:
        ctrl = self._ctrl()
        if ctrl is None:
            return []
        buf = getattr(ctrl, "_log_buffer", None)
        if not isinstance(buf, dict):
            return []
        names: list[str] = []
        if account:
            names = [account]
        else:
            sel = self.selected_account_name()
            if sel:
                names = [sel]
            else:
                names = list(buf.keys())[-3:]
        lines: list[str] = []
        for name in names:
            dq = buf.get(name)
            if not dq:
                continue
            for ev in list(dq)[-limit:]:
                try:
                    msg = getattr(ev, "message", None) or str(ev)
                    src = getattr(ev, "source", None)
                    src_s = getattr(src, "value", None) or str(src or "")
                    prefix = f"[{name}]"
                    if src_s:
                        prefix += f"[{src_s}]"
                    lines.append(f"{prefix} {msg}")
                except Exception:
                    lines.append(str(ev))
        return lines[-limit:]

    def apply_window_hwnd(self, account: str, hwnd: int) -> dict[str, Any]:
        fac = self._facade()
        ctrl = self._ctrl()
        name = (account or "").strip()
        if not name:
            return {"ok": False, "error": "未指定账号名"}
        if fac is None or ctrl is None:
            return {"ok": False, "error": "主程序 facade 不可用"}
        try:
            accounts = list(fac.list_accounts() or [])
        except Exception:
            accounts = []
        hit = None
        for a in accounts:
            if str(a.get("name") or "") == name:
                hit = a
                break
        if hit is None:
            return {"ok": False, "error": f"找不到账号「{name}」"}
        try:
            hwnd = int(hwnd)
        except Exception:
            return {"ok": False, "error": "hwnd 无效"}
        old = hit.get("window_hwnd")
        hit["window_hwnd"] = hwnd
        hit["_target"] = "window"
        try:
            # 刷新内存中的账号引用
            for i, a in enumerate(getattr(fac.state, "accounts", []) or []):
                if str(a.get("name") or "") == name:
                    fac.state.accounts[i] = hit
                    break
        except Exception:
            pass
        try:
            ctrl.register_window_target(hit)
        except Exception as e:
            return {
                "ok": False,
                "error": f"已写入 hwnd={hwnd}，但重建 UserWindow 失败: {e}",
                "old_hwnd": old,
                "new_hwnd": hwnd,
            }
        uw = getattr(ctrl, "_window_instances", {}).get(name)
        effective = int(uw.hwnd) if uw is not None else hwnd
        return {
            "ok": True,
            "account": name,
            "old_hwnd": int(old) if old else None,
            "bound_hwnd": hwnd,
            "effective_hwnd": effective,
            "human_summary": (
                f"账号「{name}」窗口已改绑 {old} → {hwnd}"
                + (f"（实际点击窗 {effective}）" if effective != hwnd else "")
            ),
        }
