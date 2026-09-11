# -*- coding: utf-8 -*-
"""方案 E：架构标签 scene_driven vs multi_task。"""

from __future__ import annotations

import re
from typing import Any, Optional


ARCH_SCENE = "scene_driven"
ARCH_MULTI = "multi_task"
ARCH_UNKNOWN = ""

_TAG_RE = re.compile(
    r"(?m)^\s*架构\s*[：:]\s*(scene_driven|multi_task|场景驱动|多任务|single_fsm)\s*$",
    re.I,
)


def parse_architecture_tag(explanation: str) -> str:
    """介绍显式「架构：scene_driven|multi_task」。"""
    m = _TAG_RE.search(explanation or "")
    if not m:
        return ARCH_UNKNOWN
    raw = m.group(1).strip().lower()
    if raw in ("scene_driven", "场景驱动", "single_fsm"):
        return ARCH_SCENE
    if raw in ("multi_task", "多任务"):
        return ARCH_MULTI
    return ARCH_UNKNOWN


def infer_architecture(explanation: str) -> tuple[str, str]:
    """返回 (arch, reason)。有标签用标签；否则启发式。"""
    tagged = parse_architecture_tag(explanation)
    if tagged:
        return tagged, "介绍显式架构标签"

    from backend.script_generator.agent import (
        is_scene_driven_explanation,
        _count_explanation_tasks,
        _explanation_task_titles,
    )

    expl = explanation or ""
    score_scene = 0
    score_multi = 0
    reasons: list[str] = []

    if is_scene_driven_explanation(expl):
        score_scene += 5
        reasons.append("场景识别为驱动类措辞")
    if re.search(r"并行匹配|逗留时间|重识别|重识屏", expl):
        score_scene += 2
        reasons.append("逗留/并行/重识别")
    titles = _explanation_task_titles(expl)
    ui_like = sum(
        1
        for t in titles
        if re.search(r"界面|窗口|结算|广告|剧情|选关|编队|出击界面|关卡信息", t)
    )
    biz_like = sum(
        1
        for t in titles
        if re.search(r"房间|体力|竞技|爬塔|每日|礼物|任务奖励|领", t)
    )
    if titles and ui_like >= max(2, len(titles) // 2):
        score_scene += 3
        reasons.append("任务标题多为界面名")
    if titles and biz_like >= max(2, len(titles) // 2):
        score_multi += 4
        reasons.append("任务标题多为业务名")

    if re.search(r"完成日常|房间领体力\s*→|do_work[：:].{0,40}→", expl):
        score_multi += 4
        reasons.append("日常顺序目标/入口链")
    if re.search(r"@返回主界面|@返回出击界面", expl):
        score_multi += 2
        reasons.append("导航辅助 @返回*")

    n = _count_explanation_tasks(expl)
    if n >= 2 and score_scene == 0 and score_multi == 0:
        # 无其它信号时不武断
        return ARCH_UNKNOWN, "任务数≥2 但架构信号不足"

    if score_scene > score_multi and score_scene >= 3:
        return ARCH_SCENE, "启发式: " + ",".join(reasons[:4])
    if score_multi > score_scene and score_multi >= 3:
        return ARCH_MULTI, "启发式: " + ",".join(reasons[:4])
    if score_scene >= 3:
        return ARCH_SCENE, "启发式: " + ",".join(reasons[:4])
    if score_multi >= 3:
        return ARCH_MULTI, "启发式: " + ",".join(reasons[:4])
    return ARCH_UNKNOWN, "无明确架构信号"


def architecture_plan_kind(arch: str) -> Optional[str]:
    if arch == ARCH_SCENE:
        return "single_fsm"
    if arch == ARCH_MULTI:
        return "multi_task"
    return None


def architecture_prompt_block(explanation: str) -> str:
    arch, reason = infer_architecture(explanation)
    if not arch:
        return (
            "## Architecture (undetected)\n"
            "Choose single_fsm vs multi_task from explanation; "
            "if 场景识别为驱动, prefer one loop + unknown_state dispatch "
            "(do NOT split scene handlers into sequential run_task queue).\n"
        )
    if arch == ARCH_SCENE:
        return (
            f"## Architecture (LOCKED): scene_driven — {reason}\n"
            "- One main loop; unknown_state identifies scene; each (1)(2)… block is a "
            "scene HANDLER, not an independent business task.\n"
            "- FORBIDDEN: sequential `for name, st, to in [('选关',…),('关卡信息',…)]` "
            "run_task queue of scene blocks.\n"
            "- kind for plan: single_fsm (or utility); leave multi_task tasks=[] "
            "unless truly independent goals exist outside scene dispatch.\n"
        )
    return (
        f"## Architecture (LOCKED): multi_task — {reason}\n"
        "- Independent goals (房间/竞技/塔…); shared navigation helpers; "
        "sequential run_task per business task.\n"
        "- Task titles are business goals, not merely UI screen names.\n"
    )


def validate_architecture_code(
    code: str,
    explanation: str = "",
    arch: str | None = None,
) -> list[str]:
    """scene_driven 却顺序跑场景块 → 报错。"""
    errors: list[str] = []
    if arch is None:
        arch, _ = infer_architecture(explanation)
    if arch != ARCH_SCENE:
        return errors
    raw = code or ""
    # 多个 TASK_*_STATES + do_work 里 for 循环依次 run_task 场景名
    task_tables = re.findall(r"^TASK_\w+_STATES\s*=", raw, re.M)
    if len(task_tables) >= 3 and re.search(
        r"for\s+\w+.*in\s*\[[^\]]{0,400}(选关|编队|结算|剧情)",
        raw,
    ):
        errors.append(
            "架构 scene_driven：禁止把场景块拆成顺序 TASK_* + for run_task 队列；"
            "应单环 unknown_state 分发"
        )
    # 典型：tasks = [('选关界面', TASK_task1...), ...]
    if re.search(
        r"\(\s*['\"]选关[^'\"]*['\"]\s*,\s*TASK_\w+_STATES",
        raw,
    ) and re.search(
        r"\(\s*['\"]编队[^'\"]*['\"]\s*,\s*TASK_\w+_STATES|"
        r"\(\s*['\"]战斗结算[^'\"]*['\"]\s*,\s*TASK_\w+_STATES",
        raw,
    ):
        errors.append(
            "架构 scene_driven：检测到场景名作为顺序 multi_task 入口，"
            "请改为 STATES/单环场景分发"
        )
    return errors


def apply_architecture_to_plan(plan: dict[str, Any], explanation: str) -> dict[str, Any]:
    """写入 plan['architecture'] 并在标签/强启发时覆盖 kind。"""
    arch, reason = infer_architecture(explanation)
    plan = dict(plan or {})
    if arch:
        plan["architecture"] = arch
        plan["architecture_reason"] = reason
        kind = architecture_plan_kind(arch)
        tagged = parse_architecture_tag(explanation)
        # 显式标签：强制 kind；启发式 scene 也强制，避免抄日常
        if tagged or arch == ARCH_SCENE:
            if kind:
                plan["kind"] = kind
            if arch == ARCH_SCENE:
                # 场景驱动不要拆分生成
                plan["tasks"] = []
        elif arch == ARCH_MULTI and kind:
            if not plan.get("kind") or plan.get("kind") == "single_fsm":
                # 仅当计划空或误成 single 时抬成 multi
                if not (plan.get("tasks") or []):
                    plan["kind"] = kind
    return plan
