# -*- coding: utf-8 -*-
"""方案 A：修订权威 — 反馈 ＞ 介绍 ＞ 诊断。"""

from __future__ import annotations

import re
from typing import Any


AUTHORITY_BANNER = """## Authority (CRITICAL — revise & diagnosis)
Priority: user trial feedback > script introduction > diagnosis (must_fix / frame caption).
- Feedback is a PATCH: only override introduction steps that feedback explicitly negates.
- Unmentioned introduction steps (esp. @helpers like 确定属性 → 2_back → 出击界面) MUST stay.
- Diagnosis must_fix that conflict with feedback or introduction MUST be ignored or demoted
  to 「请人工确认」— never treat diagnosis as license to delete intro middle steps.
- Stop-frame caption must NOT invent `_img('...')` names unless they appear in the
  introduction / IMAGE PARTS whitelist.
"""


def authority_banner() -> str:
    return AUTHORITY_BANNER


def _intro_mentions_back_to_sortie(explanation: str) -> bool:
    expl = explanation or ""
    return bool(
        re.search(r"2_back|返回出击界面|点击.{0,8}2_back.{0,16}出击", expl)
    )


def diagnosis_item_conflicts_with_intro(
    item: str,
    *,
    explanation: str = "",
    feedback: str = "",
) -> str | None:
    """若 must_fix 与介绍/反馈冲突，返回冲突原因；否则 None。"""
    text = (item or "").strip()
    if not text:
        return None
    expl = explanation or ""
    fb = feedback or ""

    # 反馈显式否定介绍某步时，以反馈为准（不标冲突）
    if fb and re.search(r"(不要|禁止|别|无需).{0,10}2_back|不必.{0,6}返回出击", fb):
        return None

    if _intro_mentions_back_to_sortie(expl):
        if re.search(
            r"(不要|禁止|勿|别|不再).{0,16}(回|返回|切).{0,10}出击|"
            r"确认属性后.{0,20}(不要|禁止).{0,12}出击界面|"
            r"留在编队.{0,24}(直接)?(点|点击)?.{0,8}出击|"
            r"映射为.{0,6}编队界面.{0,20}不要.{0,8}出击",
            text,
            re.I,
        ):
            return "与介绍「确定属性 → 2_back → 出击界面」冲突（反馈未否定该步）"

        if re.search(r"禁止.{0,8}2_back|不要.{0,8}点.{0,6}2_back|跳过.{0,6}2_back", text, re.I):
            return "与介绍要求 2_back 冲突"

    # 反馈要求保留回流时，诊断删除回流 → 冲突
    if fb and re.search(r"2_back|返回出击|切助战|助战", fb):
        if re.search(r"(不要|禁止).{0,12}(回|返回).{0,8}出击|不要.{0,8}2_back", text, re.I):
            return "与用户反馈（保留回流/助战）冲突"

    return None


def filter_diagnosis_for_authority(
    diagnosis: Any,
    *,
    explanation: str = "",
    feedback: str = "",
) -> Any:
    """过滤/降级与介绍·反馈冲突的 must_fix / do_not。原地修改并返回。"""
    if diagnosis is None:
        return diagnosis
    must = list(getattr(diagnosis, "must_fix", None) or [])
    do_not = list(getattr(diagnosis, "do_not", None) or [])
    demoted: list[str] = []
    kept_must: list[str] = []
    for item in must:
        # 停帧 caption 整段：去掉擅自绑 _img 的句子风险 — 降级提示
        reason = diagnosis_item_conflicts_with_intro(
            item, explanation=explanation, feedback=feedback,
        )
        if reason:
            demoted.append(f"【请人工确认·已降级】{reason}：{item[:180]}")
            continue
        kept_must.append(item)

    kept_do_not: list[str] = []
    for item in do_not:
        reason = diagnosis_item_conflicts_with_intro(
            item, explanation=explanation, feedback=feedback,
        )
        if reason:
            demoted.append(f"【请人工确认·已降级 do_not】{reason}：{item[:180]}")
            continue
        kept_do_not.append(item)

    # caption 禁止发明白名单外的 _img 名 — 软提示进 notes via demoted
    cap = str(getattr(diagnosis, "frame_caption", "") or "")
    if cap and re.search(r"_img\s*\(|`[A-Za-z0-9_/\u4e00-\u9fff]+\.png`", cap):
        # 若 caption 点名的图不在介绍里，降级整段 caption 的约束力
        from backend.script_generator.agent import _stems_mentioned_in_explanation
        expl_keys = _stems_mentioned_in_explanation(explanation or "")
        named = re.findall(
            r"[A-Za-z0-9_\u4e00-\u9fff\-]+(?:/[A-Za-z0-9_\u4e00-\u9fff\-]+)*\.png",
            cap,
            re.I,
        )
        bad = []
        for n in named:
            k = n.rsplit(".", 1)[0].replace("\\", "/").lower()
            bases = {x.rsplit("/", 1)[-1].lower() for x in expl_keys}
            if expl_keys and k not in expl_keys and k.rsplit("/", 1)[-1] not in bases:
                bad.append(n)
        if bad:
            demoted.append(
                "【请人工确认·停帧 caption】点名了介绍未列出的图，勿据此改 `_img`："
                + ", ".join(bad[:6])
            )

    diagnosis.must_fix = kept_must + demoted
    diagnosis.do_not = kept_do_not
    notes = str(getattr(diagnosis, "vision_reason", "") or "")
    if demoted:
        extra = f"权威过滤：降级 {len(demoted)} 条与介绍/反馈冲突的诊断"
        diagnosis.vision_reason = (notes + "；" + extra).strip("；") if notes else extra
    return diagnosis


def revise_authority_system_addendum() -> str:
    return "\n\n" + AUTHORITY_BANNER
