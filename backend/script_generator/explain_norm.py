"""把口语「脚本介绍」编译成生成 Agent 更易用的规范文本 + 轻量 IR 摘要。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


_GLOSSARY = """## 用语（生成时必须遵守）
- 「本步骤结束」= 辅助/导航成功，继续当前任务；代码 return 业务态名（如 '主界面'/'出击界面'），禁止 '__exit__'
- 「本任务完成」或「本任务结束」= 当前日常子任务业务做完；代码 return '__exit__'（仅结束当前 run_task）
- 导航成功（看到 rank / 出击_logo）≠ 本任务完成
- 「执行@某辅助」= 代码 return '某辅助'（走当前 TASK_*_STATES），禁止另写一套私有分支，也禁止擅自改成 return '未知'
- return「主界面」「出击界面」等场景名时，TASK_*_STATES 须注册同名键或 SCENE_TO_STEP 映射到下一步；禁止 handler None
- 介绍「有则…无则…」= 独立步骤 handler 内 if/else return 不同 STATES 键，形成显式分支
- 画面新鲜度由运行时保证：click/b_sleep 后自动失效，match 前补帧；可选 request_fps 提高持续截图频率
- unknown_state 全部标识未命中（return None）且通用导航按钮（如 home，非主界面会出现）也不可见 → 过场/loading；run_task 保持 state_name、重置 se_time
- 无场景标识但可见 home 等导航 chrome → 非过场，仍保持 state_name + 重置 se_time，由当前 handler 继续处理（禁止误判过场后乱导航）
"""

_PRIORITY_BANNER = """## PRIORITY / 权重（冲突时按此裁决）
1. **脚本介绍（最高）**：任务流程、@辅助跳转、图片语义、顺序与完成条件 —— 一律以介绍为准
2. **试运行 HARD / 修订 checklist**（若有）：可收紧实现，但不得推翻介绍已写明的流程
3. **系统 Rules**：通用默认；与介绍冲突时 **Rules 让步**
4. **Few-shot / 模板**：只学写法与结构，不得覆盖介绍步骤

例：Rules 写「辅助失败 return 未知」，介绍写「点不到出击.png 则执行@返回主界面」→ 必须 `return '返回主界面'`。
"""

# 辅助步骤段落内：结束该任务 / 如果存在则结束 → 本步骤结束
_HELPER_END_RE = re.compile(
    r"(已在主界面)?结束该任务|如果存在则结束|匹配成功则结束(?!该任务)",
)
_HELPER_BLOCK_RE = re.compile(
    r"(辅助步骤[\s\S]*?)(?=\n场景标识：|\n图片说明：|\n任务流程：|\n特殊规则：|\n## |\Z)",
    re.M,
)

# 业务完成类
_TASK_DONE_PHRASES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"没有则任务完成"),
        "没有则本任务完成（return '__exit__'）",
    ),
    (
        re.compile(r"没有room_ok\.png后任务结束|没有 room_ok\.png 后任务结束"),
        "没有 room_ok.png 后本任务完成（return '__exit__'）",
    ),
    (
        re.compile(r"累计\s*\d+\s*次.*?(?:本任务|任务)(?:结束|完成)"),
        "累计达介绍次数 → 本任务完成（__exit__）；未达次数须 return 介绍写的回退步",
    ),
    (
        re.compile(r"回到第\s*[（(]?\d+[）)]?\s*步"),
        "未满足条件时 return 介绍指定的 earlier 步（非 __exit__，除非已达累计次数）",
    ),
    (
        re.compile(r"返回第\s*[（(]?\d+[）)]?\s*步"),
        "未满足条件时 return 介绍指定的 earlier 步（非 __exit__，除非已达累计次数）",
    ),
    (
        re.compile(r"匹配成功则任务结束"),
        "匹配成功则本任务完成（return '__exit__'）",
    ),
    (
        re.compile(r"耗尽则结束(?!该)|已经耗尽，耗尽则结束"),
        "次数耗尽则本任务完成（return '__exit__'）",
    ),
    (
        re.compile(r"竞技场部分任务完成"),
        "竞技场本任务完成（return '__exit__'）",
    ),
    (
        re.compile(r"(?<![本])任务结束(?!条件)"),
        "本任务完成（return '__exit__'）",
    ),
    (
        re.compile(r"(?<![本])任务完成(?!并|条件|（)"),
        "本任务完成（return '__exit__'）",
    ),
]

# 导航/辅助语境中的「结束该任务」
_STEP_END_REPLACE = [
    (
        re.compile(
            r"如果匹配成功则已在主界面结束该任务，否则未在主界面进入下一步"
        ),
        "匹配成功则本步骤结束（return '主界面'），否则进入下一步",
    ),
    (
        re.compile(r"确认是否已在主界面，如果匹配成功则已在主界面结束该任务"),
        "确认是否已在主界面：匹配成功则本步骤结束（return '主界面'）",
    ),
    (
        re.compile(r"匹配出击_logo\.png，如果存在则结束"),
        "匹配出击_logo.png：匹配成功则本步骤结束（return '出击界面'）",
    ),
    (
        re.compile(r"匹配成功则已在主界面结束该任务"),
        "匹配成功则本步骤结束（return '主界面'）",
    ),
    (
        re.compile(r"结束该任务"),
        "本步骤结束",
    ),
    # @辅助：更具体的「没匹配到则执行@」须先于通用「执行@」
    (
        re.compile(r"如果没匹配到则执行@([^\s，。；;）)\]]+)"),
        r"若 click/match 失败则 return '\1'（走状态表；禁止改成 '未知'）",
    ),
    (
        re.compile(r"执行@([^\s，。；;）)\]]+)"),
        r"执行辅助「\1」：代码必须 return '\1'（该键须在当前 TASK_*_STATES；禁止改成 '未知'）",
    ),
]

_SCENE_LINE_RE = re.compile(
    r"^([^\n：:]+?\.(?:png|jpe?g))\s*[：:]\s*(.+)$",
    re.I | re.M,
)
_HELPER_NAME_RE = re.compile(r"[（(]([a-zA-Z])[）)]\s*([^\n]+)")
_TASK_HEAD_RE = re.compile(r"[（(](\d+)[）)]\s*([^\n]+)")
_IMG_RE = re.compile(r"[\w\u4e00-\u9fff\-]+\.(?:png|jpe?g)", re.I)
_IMG_CLEAN_RE = re.compile(
    r"((?:[A-Za-z][\w\-]*|[\u4e00-\u9fff][\w\u4e00-\u9fff\-]*)\.(?:png|jpe?g))$",
    re.I,
)


def _clean_img_token(tok: str) -> str:
    """去掉粘在文件名前面的口语（匹配/点击/还有…）。"""
    t = (tok or "").strip()
    m = _IMG_CLEAN_RE.search(t)
    return m.group(1) if m else t


def _findall_images(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in _IMG_RE.findall(text or ""):
        name = _clean_img_token(raw)
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


@dataclass
class ExplainNormResult:
    original: str
    normalized: str
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ir: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "warnings": list(self.warnings),
            "notes": list(self.notes),
            "ir": self.ir,
            "normalized_chars": len(self.normalized or ""),
            "original_chars": len(self.original or ""),
        }


def _dedupe_special_rules(text: str) -> tuple[str, list[str]]:
    """特殊规则若与图片说明大量重复则压缩提示。"""
    notes: list[str] = []
    m = re.search(r"\n特殊规则：\n([\s\S]*?)(?=\n## |\Z)", text)
    img = re.search(r"\n图片说明：\n([\s\S]*?)(?=\n任务流程：|\n特殊规则：|\n## |\Z)", text)
    if not m or not img:
        return text, notes
    special = m.group(1)
    # 若特殊规则几乎全是「xxx.png：」短句且与图片说明重叠，加一句指引即可
    spec_imgs = set(_findall_images(special))
    info_imgs = set(_findall_images(img.group(1)))
    overlap = spec_imgs & info_imgs
    if len(overlap) >= 3 and len(spec_imgs) <= len(overlap) + 1:
        notes.append("特殊规则与图片说明大量重复，已改为引用提示")
        replacement = (
            "\n特殊规则：\n"
            "- 偏移 / 阈值 / 波纹等以「图片说明」为准；勿重复发明 API。\n"
            "- jjc_刷新 点击偏移 (-200,0)；连续点击间隔 ≥ 0.3s（波纹）。\n"
        )
        text = text[: m.start()] + replacement + text[m.end() :]
    return text, notes


def _rewrite_phrases(text: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    out = text
    for pat, repl in _STEP_END_REPLACE:
        new, n = pat.subn(repl, out)
        if n:
            notes.append(f"导航/辅助用语规范化 ×{n}")
            out = new
    for pat, repl in _TASK_DONE_PHRASES:
        new, n = pat.subn(repl, out)
        if n:
            notes.append(f"本任务完成用语规范化 ×{n}")
            out = new
    # 「回到第N步」保留但加机器可读提示
    if re.search(r"回到第[一二三四五六七八九十\d]+步", out):
        notes.append("含「回到第N步」：生成时请映射为 return 对应业务态 / 循环同一 handler")
    if re.search(r"偏移\s*\(-200,\s*0\)", out):
        new, n = re.subn(
            r"(jjc_刷新[^\n]*?)偏移\s*\(-20,\s*0\)",
            r"\1偏移 (-200,0)",
            out,
            flags=re.I,
        )
        if n:
            notes.append(f"jjc_刷新 偏移统一为 (-200,0) ×{n}")
            out = new
        new2, n2 = re.subn(
            r"对该图点击偏移\s*\(-20,\s*0\)",
            "对该图点击偏移(-200,0)",
            out,
            flags=re.I,
        )
        if n2:
            notes.append(f"jjc_刷新 口语偏移统一为 (-200,0) ×{n2}")
            out = new2
    return out, notes


def _extract_ir(text: str) -> dict[str, Any]:
    helpers: list[dict[str, str]] = []
    hm = re.search(
        r"辅助步骤[^\n]*\n([\s\S]*?)(?=\n场景标识：|\n图片说明：|\n任务流程：|\Z)",
        text,
    )
    if hm:
        for m in _HELPER_NAME_RE.finditer(hm.group(1)):
            helpers.append({"id": m.group(1), "name": m.group(2).strip()})

    scenes: list[dict[str, str]] = []
    sm = re.search(
        r"场景标识：\n([\s\S]*?)(?=\n图片说明：|\n任务流程：|\n特殊规则：|\n## |\Z)",
        text,
    )
    if sm:
        for m in _SCENE_LINE_RE.finditer(sm.group(1)):
            scenes.append({"image": m.group(1).strip(), "role": m.group(2).strip()[:80]})

    tasks: list[dict[str, Any]] = []
    tm = re.search(
        r"任务流程：\n([\s\S]*?)(?=\n特殊规则：|\n## |\Z)",
        text,
    )
    if tm:
        body = tm.group(1)
        parts = re.split(r"\n(?=[（(]\d+[）)])", body)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            hm2 = _TASK_HEAD_RE.match(part)
            name = hm2.group(2).strip() if hm2 else part.split("\n", 1)[0][:40]
            exits = []
            if re.search(r"本任务完成|__exit__", part):
                exits.append("return '__exit__' when business done")
            if "jjc_end" in part:
                exits.append("jjc_end → __exit__")
            if "ta_cishu" in part or "耗尽" in part:
                exits.append("次数耗尽 → __exit__")
            if "room_收取奖励" in part and "没有" in part:
                exits.append("无收取/奖励按钮（且无弹窗待关）→ __exit__")
            if re.search(r"累计\s*\d+\s*次", part) and re.search(
                r"回到第|返回第|返回上一步|回到.*?步", part
            ):
                exits.append("未达次数：return earlier 步；达次数 → __exit__")
            helpers_used = re.findall(r"「([^」]+)」|@([^\s，。]+)", part)
            hnames = [a or b for a, b in helpers_used]
            tasks.append({
                "name": name,
                "helpers": hnames,
                "exit_hints": exits,
                "images": _findall_images(part)[:16],
            })

    goal = ""
    gm = re.search(r"目标：\s*\n([^\n#]+)", text)
    if gm:
        goal = gm.group(1).strip()

    return {
        "goal": goal,
        "helpers": helpers,
        "scenes": scenes,
        "tasks": tasks,
    }


def _format_ir_block(ir: dict[str, Any]) -> str:
    lines = ["## Agent IR（由介绍自动编译；细节冲突仍以介绍正文为准）"]
    if ir.get("goal"):
        lines.append(f"- goal: {ir['goal']}")
    helpers = ir.get("helpers") or []
    if helpers:
        lines.append("- helpers（成功=本步骤结束，return 业务态，禁止 __exit__）:")
        for h in helpers:
            lines.append(f"  - {h.get('id')}: {h.get('name')}")
    scenes = ir.get("scenes") or []
    if scenes:
        lines.append("- scene_map:")
        for s in scenes[:12]:
            lines.append(f"  - {s.get('image')} → {s.get('role')}")
    tasks = ir.get("tasks") or []
    if tasks:
        lines.append("- tasks:")
        for i, t in enumerate(tasks, 1):
            lines.append(f"  - ({i}) {t.get('name')}")
            if t.get("helpers"):
                lines.append(f"    helpers: {', '.join(t['helpers'])}")
            if t.get("exit_hints"):
                lines.append(f"    exit: {'; '.join(t['exit_hints'])}")
            if t.get("images"):
                lines.append(f"    images: {', '.join(t['images'][:8])}")
    lines.append("")
    return "\n".join(lines)


def _collect_warnings(text: str, ir: dict[str, Any]) -> list[str]:
    warns: list[str] = []
    if re.search(r"结束该任务", text):
        warns.append("仍含「结束该任务」：易被当成 __exit__；已尽量改写为本步骤结束")
    if re.search(r"回到第[一二三四五六七八九十\d]+步", text):
        warns.append("含「回到第N步」：请映射为状态循环，勿写成死递归无超时")
    if (ir.get("tasks") or []) and not any(
        (t.get("exit_hints") or []) for t in ir["tasks"]
    ):
        warns.append("任务流程未解析到明确本任务完成条件，请核对介绍")
    if len(ir.get("tasks") or []) >= 2 and not ir.get("helpers"):
        warns.append("多任务但未解析到辅助步骤块")
    if not ir.get("scenes"):
        warns.append("未解析到「场景标识」表")
    return warns


def normalize_explanation(explanation: str) -> ExplainNormResult:
    """规则编译：用语归一 + IR 摘要置顶。不改用户磁盘文件。"""
    original = explanation or ""
    # 已编译过则跳过，避免 generate→plan 双重注入
    if "## Agent IR（由介绍自动编译" in original:
        ir = _extract_ir(original)
        text = original
        if "## PRIORITY" not in text and "权重" not in text[:600]:
            text = _PRIORITY_BANNER + "\n" + text
        return ExplainNormResult(
            original=original,
            normalized=text,
            warnings=_collect_warnings(original, ir),
            notes=["skip: already normalized"],
            ir=ir,
        )

    text = original
    notes: list[str] = []

    text, n1 = _rewrite_phrases(text)
    notes.extend(n1)
    text, n2 = _dedupe_special_rules(text)
    notes.extend(n2)

    ir = _extract_ir(text)
    warns = _collect_warnings(text, ir)

    # 已有用语块则不重复插入
    if "本步骤结束" not in text[:800] or "用语" not in text[:500]:
        body = text.lstrip()
        # 保留文首「校验通过」行
        head = ""
        if body.startswith("校验通过"):
            first, _, rest = body.partition("\n")
            head = first + "\n\n"
            body = rest.lstrip()
        text = head + _GLOSSARY + "\n" + body
        notes.append("已注入用语表")

    ir_block = _format_ir_block(ir)
    # IR 放在 HARD 约束之后、正文之前更稳：若已有 HARD，插在其后
    if "## HARD CONSTRAINTS" in text or "HARD CONSTRAINTS from previous" in text:
        # hoist 会再处理；这里把 IR 紧贴文首用语后
        text = text.replace(_GLOSSARY, _GLOSSARY + "\n" + ir_block, 1)
    else:
        if _GLOSSARY in text:
            text = text.replace(_GLOSSARY, _GLOSSARY + "\n" + ir_block, 1)
        else:
            text = ir_block + "\n" + text

    if "## PRIORITY" not in text:
        # 权重裁决置顶（高于用语表 / IR）
        body = text.lstrip()
        head = ""
        if body.startswith("校验通过"):
            first, _, rest = body.partition("\n")
            head = first + "\n\n"
            body = rest.lstrip()
        text = head + _PRIORITY_BANNER + "\n" + body
        notes.append("已注入介绍优先权重")

    notes.append(
        f"IR: helpers={len(ir.get('helpers') or [])}, "
        f"scenes={len(ir.get('scenes') or [])}, "
        f"tasks={len(ir.get('tasks') or [])}"
    )

    return ExplainNormResult(
        original=original,
        normalized=text,
        warnings=warns,
        notes=notes,
        ir=ir,
    )


def prepare_explanation_for_codegen(
    explanation: str,
    *,
    hoist_feedback: bool = True,
    lean: bool = False,
) -> ExplainNormResult:
    """normalize →（可选）提升试运行反馈。lean=True 时仅保留介绍正文（自由模式生成）。"""
    if lean:
        text = (explanation or "").strip()
        for marker in (
            "## Agent IR（由介绍自动编译",
            "## PRIORITY / 权重",
            "## 用语（生成时必须遵守）",
            "## HARD CONSTRAINTS",
        ):
            if marker in text:
                text = text.split(marker)[0].strip()
        if hoist_feedback:
            try:
                from backend.script_generator.feedback_opt import hoist_trial_constraints
                text = hoist_trial_constraints(text)
            except Exception:
                pass
        return ExplainNormResult(
            original=explanation or "",
            normalized=text,
            warnings=[],
            notes=["lean: 自由模式省略 IR/用语注入"],
            ir={},
        )
    result = normalize_explanation(explanation or "")
    text = result.normalized
    if hoist_feedback:
        try:
            from backend.script_generator.feedback_opt import hoist_trial_constraints
            text = hoist_trial_constraints(text)
        except Exception:
            pass
    result.normalized = text
    return result
