"""Explicit V3 bootstrap.  No import-time switch changes the V2 application."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .stores.sqlite_store import SqliteDomainStore


@dataclass(frozen=True)
class V3Services:
    store: SqliteDomainStore


def initialize_v3(data_dir: str | Path) -> V3Services:
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    store = SqliteDomainStore(root / "collaborator_v3.sqlite3")
    store.initialize()
    # 能力表是 Runtime 对当前工具/skill 契约的投影，不是手工维护的
    # 第二份事实源。应用启动时必须同步，否则工具 schema 已升级，
    # capability.get 仍会把旧版本发给模型（实况会导致同一参数永久绑定失败）。
    from .adapters.skill_capabilities import sync_skill_capabilities
    from .adapters.v2_capabilities import sync_v2_capabilities

    sync_v2_capabilities(store)
    sync_skill_capabilities(store)
    # 试运行生命周期事件必须**自动**写上（设计 §19.6）：这里把 trial_runs 的
    # 事件出口接到本地事件存储，脚本不需要主动调用任何录制 API。
    try:
        from .events.store import EventStore
        from .runtime import trial_runs

        trial_runs.set_event_sink(EventStore(store).append)
    except Exception:  # noqa: BLE001 - 接线失败不影响主流程
        pass
    # 契约凭据（Phase 1，用户决定 9）：登记**进程默认** ticket 服务，
    # 让 V2 的 lookup_api 与 V3 的 api.get 用同一个服务、同一个库。
    try:
        from .runtime.ticket_store import register_default_service

        register_default_service(store)
    except Exception:  # noqa: BLE001 - 同上：留痕是加固，不是闸门
        pass
    return V3Services(store=store)
