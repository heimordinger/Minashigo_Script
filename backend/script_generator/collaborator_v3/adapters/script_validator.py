"""Adapter to the same local script gate used by the V2 trial page.

``validate`` 走 V2 的静态交付闸；``verify_artifact`` 在其之上做**最终产物闸门**
（素材替换/打包 → 两阶段契约校验 → import 冒烟 → 假运行冒烟 → ValidationReceipt），
对应设计 §4.5 / §8.3 / §14.4。
"""

from __future__ import annotations

from typing import Any


class V2ScriptValidator:
    validator_id = "local.collab_script_validator"
    #: v2 = 增加最终产物闸门与凭证（v1 只有静态字符串检查）。
    validator_version = "2"

    def __init__(self, session: Any) -> None:
        self.session = session

    def validate(self, script: str, *, profile: str | None = None) -> list[str]:
        from backend.script_generator.collaborator.collab_form import collab_script_issues

        # profile 由 Runtime 传下来（权威执行目标）；None 时交付闸按注解 provisional 判定。
        return list(collab_script_issues(script, self.session, profile=profile))

    def _ban_snippet(self) -> str:
        """本会话声明的禁用区片段（= 必须出现在源码里的 ``_COLLAB_BAN_ZONES`` 声明）。

        与试运行写盘用的是**同一个来源**（``session.artifacts["ban_script_snippet"]``）。
        以前校验侧没传它，于是"缺禁用区声明"的脚本照样过校验、拿到凭证，
        直到用户点试运行才报错 —— 凭证在交付那一刻就是假的（设计 §4.5/§8.3）。
        """
        artifacts = getattr(self.session, "artifacts", None)
        if isinstance(artifacts, dict):
            return str(artifacts.get("ban_script_snippet") or "").strip()
        return ""

    def verify_artifact(
        self,
        script: str,
        *,
        script_name: str = "collab_script",
        profile: str | None = None,
        profile_source: str = "",
        target_revision: str = "",
        account_ref: str = "",
    ) -> Any | None:
        """跑最终产物闸门；闸门自身不可用时返回 None（由 Runtime 降级处理）。"""
        from backend.script_generator.script_artifact import verify_artifact

        if self.session is None:
            return None
        return verify_artifact(
            script,
            session=self.session,
            script_name=script_name or "collab_script",
            profile=profile,
            profile_source=profile_source,
            target_revision=target_revision,
            account_ref=account_ref,
            # 禁用区是**阻断式**闸门，校验侧必须和写盘侧用同一份要求
            ban_snippet=self._ban_snippet(),
        )
