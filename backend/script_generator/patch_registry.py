"""校验错误 → 确定性 patch 的显式映射表。

用途（Script Generator 质量改进 P0）：
- 把"高频、可静态判定"的校验错误从「交给 LLM 修复」降级为「确定性 AST/regex patch」；
- 对每个高频错误类别评估三态：
    * ``covered`` — 已有确定性 patch（apply_codegen_patches 已含，或需补上下文参数）；
    * ``gap``     — 建议新增确定性 patch（当前只能靠 LLM / 多轮补修）；
    * ``llm``     — 语义问题，必须留给人/LLM（给出应注入的约束提示）。
- 纯 stdlib，禁止 import backend 业务模块（供 tools/analyze_gen_sessions.py 离线使用，
  也供后续 fix 路径选择修复策略）。

匹配规则按「错误原文」做小写正则搜索；新错误类别请追加到 PATCH_ENTRIES。
"""

from __future__ import annotations

import re
from typing import Iterable

# 三态
COVERED = "covered"   # 已有确定性 patch
GAP = "gap"           # 建议新增确定性 patch
LLM = "llm"           # 留给 LLM / 人工

PATCH_ENTRIES: list[dict] = [
    # covered：apply_codegen_patches 已包含的确定性修复
    {
        "id": "scene_nav_threshold",
        "category": "场景路由阈值",
        "status": COVERED,
        "patch": "patch_scene_id_nav_threshold",
        "note": "场景标识 match_image 应用 CFG.nav_threshold。已在生成+修订路径应用；"
                "自由模式亦应用（依赖 explanation 精确匹配时需传 explanation）。",
        "errors": [
            r"场景路由 match_image 应使用 threshold",
            r"nav_threshold",
            r"scene.*threshold",
        ],
    },
    {
        "id": "missing_image_refs",
        "category": "素材文件缺失/幻觉图",
        "status": COVERED,
        "patch": "patch_strip_missing_image_refs",
        "note": "目录无且介绍未点名的 _img（含 home/1_home 等范式 chrome）本地剥离；"
                "自由模式与 identifiers_only 亦执行；残留则生成校验硬失败。",
        "errors": [
            r"图片文件不存在",
            r"图片不存在",
            r"幻觉图",
            r"png.*不存在",
        ],
    },
    {
        "id": "condition_image_complete",
        "category": "结束条件素材漏引",
        "status": COVERED,
        "patch": "patch_exit_require_complete_from_intro",
        "note": "介绍用 1_complete 等作通关/切栏结束条件时，代码必须引用；"
                "可补 __exit__ 前 complete 确认。自由模式生成环亦硬拦。",
        "errors": [
            r"介绍把 .+ 用于结束/通关判定",
            r"1_complete",
            r"结束条件会漏判",
        ],
    },
    {
        "id": "settlement_click_priority",
        "category": "结算点击优先级",
        "status": COVERED,
        "patch": "patch_settlement_click_priority",
        "note": "4_next → 4_next_1 → 4_cihe → 4_rank；连续 if 可本地重排。",
        "errors": [
            r"结算点击优先级",
            r"4_next.*4_rank",
            r"4_rank.*4_next",
        ],
    },
    {
        "id": "guard_identifier_pair",
        "category": "守卫标识/按钮成对",
        "status": COVERED,
        "patch": "patch_guard_identifier_pairs",
        "note": "err1/err1_1、err2/err2_2、1_shop/1_close：只点按钮未匹配标识时补 check_guards 块。",
        "errors": [
            r"介绍以 .+ 为标识",
            r"未匹配 err1",
            r"未匹配 1_shop",
        ],
    },
    {
        "id": "missing_state_keys",
        "category": "状态表缺键",
        "status": COVERED,
        "patch": "patch_missing_task_state_keys",
        "note": "handler return / 场景名未出现在 STATES / TASK*_STATES。已有 patch 按 plan 补键；"
                "无 plan（revise 路径）时自由模式用介绍推导的伪 plan。",
        "errors": [
            r"状态表无此键",
            r"missing (task )?state keys?",
            r"未定义.*键",
            r"缺少.*STATES.*键",
        ],
    },
    {
        "id": "missing_stdlib_imports",
        "category": "缺 stdlib import",
        "status": COVERED,
        "patch": "patch_missing_stdlib_imports",
        "errors": [r"import time|import asyncio|name .* is not defined|缺少 import"],
    },
    {
        "id": "invalid_unicode_arrows",
        "category": "非法 Unicode 箭头",
        "status": COVERED,
        "patch": "patch_invalid_unicode_arrows",
        "errors": [r"箭头|→|←|⇒"],
    },
    {
        "id": "unknown_trap_bootstrap",
        "category": "未知态/boot 起跑",
        "status": COVERED,
        "patch": "patch_bootstrap_no_unknown_start / patch_run_task_escape_unknown_trap / "
                "patch_run_task_transition_hold_on_no_scene",
        "note": "run_task 长期停 未知/过场、bootstrap 从未知起跑、无场景时保持态等。",
        "errors": [r"bootstrap 跳过 未知|从 未知 起跑|停在 未知|长期.*未知|过场"],
    },
    {
        "id": "room_ok_popup_loop",
        "category": "弹窗循环(room_ok)",
        "status": COVERED,
        "patch": "patch_room_ok_loop_exit_from_intro",
        "note": "『点击收取后须处理 room_ok 弹窗直至消失』由介绍驱动；依赖 explanation，"
                "revise 路径调用时务必传 explanation（此前传空 → patch 空转）。",
        "errors": [r"room_ok|收取奖励|弹窗"],
    },
    {
        "id": "multitask_skeleton",
        "category": "多任务骨架/接线",
        "status": COVERED,
        "patch": "patch_ensure_multitask_skeleton / patch_do_work_multitask_loop / "
                "patch_multitask_scene_to_step_hubs",
        "note": "run_task + 2+ 个 TASK*_STATES + do_work 循环 + 场景枢纽接线。",
        "errors": [r"缺少.*run_task|缺少 async def do_work|多任务.*STATES|run_task 循环"],
    },
    {
        "id": "scene_resolve_first",
        "category": "场景→步骤解析顺序",
        "status": COVERED,
        "patch": "patch_resolve_state_scene_first",
        "errors": [r"先 .*SCENE_TO_STEP|_resolve_state|先识场景"],
    },
    # gap：暂无确定性 patch，建议新增（按 P0 报告评估优先级）
    {
        "id": "chinese_punct_in_code",
        "category": "代码区中文标点",
        "status": COVERED,
        "patch": "patch_chinese_punct_in_code",
        "note": "tokenize 级把代码区（非字符串/注释）全角标点换成 ASCII；生成/修订 apply_codegen_patches 已挂。",
        "errors": [r"中文标点|全角.*标点|标点（非字符串/注释）"],
    },
    {
        "id": "cross_task_image_hub",
        "category": "跨任务图/枢纽混点",
        "status": COVERED,
        "patch": "patch_cross_task_hub_images",
        "note": "枢纽 handler（主界面/出击/未知）中其它任务专属 _img 改回本任务入口图；"
                "依赖 plan 图集或前缀启发式。多任务共用同一 handler 时跳过并提示拆函数。",
        "errors": [
            r"其它任务|别的任务|另一任务|点击了其它任务|应点击本任务|跨任务|混入其它任务图",
        ],
    },
    {
        "id": "stale_frame_after_click",
        "category": "点击后旧帧/缺等待",
        "status": GAP,
        "patch": "",
        "note": "运行时已处理帧失效；静态检查 click/b_sleep 后同帧 match 属误报面小，"
                "可做成『action 后未 await 直接 match 同帧』AST 检测自动插 update_frame/等待。",
        "errors": [r"旧帧|未刷新|stale frame|click 后.*match|点击后.*未等待"],
    },
    # llm：语义问题，确定性 patch 风险高，留给 LLM（应注入约束提示）
    {
        "id": "exit_semantics",
        "category": "__exit__ 语义（步骤 vs 任务）",
        "status": LLM,
        "patch": "patch_go_home_return_main / patch_nav_helper_return_unknown（部分）",
        "note": "本步骤结束 不等于 本任务完成。最高频语义错误之一；确定性 patch 只能覆盖少数模式"
                "（导航成功返回主界面），其余靠规则提示 + 独立合规审查（_review_feedback_compliance）。",
        "errors": [r"__exit__|本步骤结束|本任务完成|返回主界面.*__exit__"],
    },
    {
        "id": "retry_dead_loop",
        "category": "死循环/次数耗尽重试",
        "status": LLM,
        "patch": "patch_jjc_refresh_offset_from_intro / patch_jjc_duanwei_max_x_from_intro（部分）",
        "note": "jjc_end 次数耗尽未短路、弹窗循环无上限等。需结合业务介绍语义，建议"
                "『介绍要求次数→代码字面量』对齐校验（可静态）＋ 其余语义留 LLM。",
        "errors": [r"死循环|无限循环|次数耗尽|重试|循环.*上限"],
    },
    {
        "id": "cross_file_api",
        "category": "API/import 域外方法",
        "status": LLM,
        "patch": "",
        "note": "调用未导入函数/越权方法等，多数已被 allowed methods 约束；出现时通常需改逻辑。",
        "errors": [r"未导入|undefined|未定义函数|not allowed|不允许的方法"],
    },
]


def _match_entry(entry: dict, text: str) -> bool:
    low = text.lower()
    for pat in entry["errors"]:
        try:
            if re.search(pat, low):
                return True
        except re.error:
            continue
    return False


def match_registry(text: str) -> list[dict]:
    """返回命中该错误文本的全部映射条目（可多条）。"""
    return [e for e in PATCH_ENTRIES if _match_entry(e, text)]


def classify_text(text: str) -> tuple[str, str]:
    """给单条错误文本打 (status, category)。多条目命中取优先级 covered > gap > llm。"""
    hits = match_registry(text)
    if not hits:
        return LLM, "other"
    order = {COVERED: 0, GAP: 1, LLM: 2}
    best = min(hits, key=lambda e: order.get(e["status"], 3))
    return best["status"], best["category"]


def summary() -> str:
    rows = []
    for e in PATCH_ENTRIES:
        rows.append(
            f"- [{e['status']}] {e['id']}（{e['category']}）patch={e['patch'] or '—'} :: {e['note']}"
        )
    return "\n".join(rows)


def entries_by_status(status: str) -> list[dict]:
    return [e for e in PATCH_ENTRIES if e["status"] == status]


if __name__ == "__main__":  # 自检
    samples = [
        "unknown_state@40: 场景路由 match_image 应使用 threshold=CFG.nav_threshold",
        "图片文件不存在于所选目录：jjc_奖励.png",
        "第 148 行代码区含中文标点（非字符串/注释）",
        "TASK_jjc_STATES: handler `unknown_state` return 'room_claim' 但状态表无此键",
        "返回主界面 导航成功必须 return '主界面'，禁止 __exit__",
        "jjc 任务主界面 handler 点击了其它任务的图 (room_logo)",
    ]
    for s in samples:
        st, cat = classify_text(s)
        ids = [e["id"] for e in match_registry(s)]
        print(f"[{st}/{cat}] ids={ids} :: {s}")