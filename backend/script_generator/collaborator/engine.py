"""CollaboratorEngine：事件进、ViewUpdate 出。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from backend.script_generator.collaborator.phases import CollabPhase, PHASE_BANNER
from backend.script_generator.collaborator.session import CollabSession
from backend.script_generator.collaborator.skills.base import CollabSkill
from backend.script_generator.collaborator.skills.registry import (
    SkillRegistry,
    default_registry,
)
from backend.script_generator.collaborator.view import CollabViewUpdate
from backend.script_generator.logic_graph import LogicGraph, LogicStep, parse_step_params


_CODE_FENCE = re.compile(r"```(?:\w+)?\s*([\s\S]*?)```", re.MULTILINE)
_STEP_LINE = re.compile(
    r"^\s*(?:\d+[\.\)、]\s*)?(?:[-*]\s*)?"
    r"((?:wait|click|sleep|match|find|swipe|press)\b[^\n#]*|"
    r"等待[^\n#]*|点击[^\n#]*|休眠[^\n#]*)",
    re.IGNORECASE,
)


def extract_draft_logic_from_text(text: str) -> tuple[str, list[LogicStep]]:
    """从 Agent 气泡伪代码抽出草稿步骤，供右侧逻辑抽屉显示。

    返回 (伪代码正文, steps)。抽不出则 steps 为空。
    """
    raw = (text or "").strip()
    if not raw:
        return "", []
    blocks = [m.group(1).strip() for m in _CODE_FENCE.finditer(raw) if m.group(1).strip()]
    body = "\n".join(blocks) if blocks else raw
    steps: list[LogicStep] = []
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("循环") or s.lower().startswith("while"):
            continue
        m = _STEP_LINE.match(s)
        if not m:
            continue
        label = m.group(1).strip().rstrip("，,；;")
        if len(label) < 2:
            continue
        # 去行尾注释
        if "#" in label:
            label = label.split("#", 1)[0].strip()
        sid = f"draft_{len(steps) + 1}"
        steps.append(
            LogicStep(
                id=sid,
                label=label[:80],
                hint="草稿（来自对话伪代码）",
                status="draft",
                params=parse_step_params(label[:80]),
            )
        )
        if len(steps) >= 12:
            break
    draft = blocks[0] if blocks else ("\n".join(s.label for s in steps) if steps else "")
    return draft, steps


class CollaboratorEngine:
    def __init__(self, registry: SkillRegistry | None = None) -> None:
        self.registry = registry or default_registry()
        self.session = CollabSession()

    def reset(self) -> CollabViewUpdate:
        self.session.reset()
        self.session.ensure_id()
        msg = (
            "协作已就绪。直接说你想自动化什么（一句话即可）；"
            "有游戏画面时放到右侧预览，或点输入旁「截图」。\n"
            "当前先测通用对话与画面收纳；复杂玩法可去「经典」Tab 写介绍生成。"
        )
        self.session.add_message("system", msg)
        return CollabViewUpdate.from_phase(
            CollabPhase.IDLE,
            agent_message=msg,
            chips=[],
            steps=[],
            shape_label="尚未开始",
            cta_enabled=False,
            trial_ready=False,
            bubble_kind="system",
        )

    def _skill(self) -> Optional[CollabSkill]:
        if not self.session.skill_id:
            return None
        return self.registry.get(self.session.skill_id)

    def _view(
        self,
        phase: CollabPhase,
        *,
        agent_message: str = "",
        chips: list[str] | None = None,
        bubble_kind: str = "agent",
        **kwargs,
    ) -> CollabViewUpdate:
        self.session.phase = phase
        kind = (bubble_kind or "agent").strip() or "agent"
        if kind not in ("agent", "system"):
            kind = "agent"
        if agent_message:
            self.session.add_message(kind, agent_message)
        vu = CollabViewUpdate.from_phase(
            phase,
            agent_message=agent_message,
            chips=chips,
            bubble_kind=kind,
            **kwargs,
        )
        if not vu.banner:
            vu.banner = PHASE_BANNER.get(phase, phase.value)
        if self.session.logic.steps and "steps" not in kwargs:
            vu.steps = self.session.logic.to_ui_steps()
        self._attach_canvas(vu)
        self._remember_view(vu)
        return vu

    def _attach_canvas(self, vu: CollabViewUpdate) -> None:
        vu.assets = list(self.session.assets or [])
        vu.overlays = list(self.session.overlays or [])
        vu.focused_asset_ids = list(self.session.focused_asset_ids or [])

    def _remember_view(self, vu: CollabViewUpdate) -> None:
        self.session.last_chips = list(vu.chips or [])
        self.session.last_cta_text = vu.cta_text or "看起来对，生成脚本"
        self.session.last_cta_enabled = bool(vu.cta_enabled)
        self.session.last_trial_ready = bool(vu.trial_ready)
        self.session.last_trial_hint = vu.trial_hint or ""
        self.session.last_shape_label = vu.shape_label or ""
        if vu.preview_path:
            self.session.last_preview_path = vu.preview_path
            self.session.last_preview_caption = vu.preview_caption or ""
        self.session.ensure_id()
        self.session.touch()

    def restore_session(self, session: CollabSession) -> CollabViewUpdate:
        self.session = session
        self.session.ensure_id()
        # 旧会话可能挂着已卸载的 skill（如结界数章），清掉以免误出逻辑步
        if self.session.skill_id and self.registry.get(self.session.skill_id) is None:
            self.session.skill_id = None
            self.session.probe = None
            self.session.logic = LogicGraph()
            self.session.last_chips = ["去经典生成"]
            self.session.last_cta_enabled = False
            self.session.last_shape_label = "已有画面" if self.session.frame_path else ""
        # 历史气泡里有伪代码、逻辑图却空 → 补抽草稿，恢复时就能看见
        if not self.session.logic.steps:
            for m in reversed(list(self.session.messages or [])[-20:]):
                if str(m.get("role") or "") != "agent":
                    continue
                draft, draft_steps = extract_draft_logic_from_text(str(m.get("text") or ""))
                if draft_steps:
                    self.session.logic = LogicGraph(
                        title="脚本草稿",
                        architecture="scene_driven",
                        steps=draft_steps,
                    )
                    if not isinstance(self.session.artifacts, dict):
                        self.session.artifacts = {}
                    if draft:
                        self.session.artifacts["draft_pseudo"] = draft
                    self.session.phase = CollabPhase.PROPOSE_LOGIC
                    self.session.last_shape_label = "脚本草稿"
                    self.session.last_cta_enabled = True
                    self.session.last_cta_text = "看起来对，生成脚本"
                    break
        return self.view_for_restore()

    def view_for_restore(self) -> CollabViewUpdate:
        s = self.session
        preview = s.last_preview_path or (str(s.frame_path) if s.frame_path else None)
        vu = CollabViewUpdate.from_phase(
            s.phase,
            agent_message="",
            chips=list(s.last_chips),
            steps=s.logic.to_ui_steps(),
            shape_label=s.last_shape_label or s.logic.title or "",
            cta_text=s.last_cta_text,
            cta_enabled=s.last_cta_enabled,
            preview_path=preview or None,
            preview_caption=s.last_preview_caption,
            trial_ready=s.last_trial_ready,
            trial_hint=s.last_trial_hint,
            append_agent=False,
            code=None,  # 恢复时不重复落盘
            intro=s.intro_text or None,
            script_name=s.script_name or None,
        )
        self._attach_canvas(vu)
        return vu

    def note_user(self, text: str) -> None:
        self.session.add_message("user", text)

    def begin_user_turn(self, text: str) -> None:
        """记下用户话，进入 understand（随后由 LLM 或本地规则接手）。"""
        text = (text or "").strip()
        if not text:
            return
        self.session.add_message("user", text)
        self.session.phase = CollabPhase.UNDERSTAND
        # 目标句：非纯闲聊时更新
        if not self._is_meta_question(text):
            self.session.goal_text = text

    @staticmethod
    def _is_meta_question(text: str) -> bool:
        t = text or ""
        keys = (
            "你是", "什么模型", "哪个模型", "谁啊", "怎么用", "帮助",
            "能干", "能力", "你好", "在吗", "谢谢",
            "有哪些技能", "哪些技能", "技能列表", "你会什么", "有什么技能",
        )
        return any(k in t for k in keys)

    @staticmethod
    def _is_skills_catalog_question(text: str) -> bool:
        t = (text or "").strip()
        keys = ("有哪些技能", "哪些技能", "技能列表", "你会什么", "有什么技能", "支持什么")
        return any(k in t for k in keys)

    def _reply_skills_catalog(self) -> CollabViewUpdate:
        catalog = "\n".join(self.registry.catalog_lines()) or "（暂无预挂玩法，先聊目标或去经典生成）"
        msg = (
            f"当前可挂能力：\n{catalog}\n\n"
            "直接说目标即可；需要画面时用右侧预览或「截图」。\n"
            "更自由的玩法可去「经典」Tab 用介绍生成。"
        )
        return self._view(
            CollabPhase.CLARIFY,
            agent_message=msg,
            chips=["去经典生成"],
            cta_enabled=False,
            shape_label="可以开始",
            bubble_kind="agent",
        )

    def apply_llm_reply(self, visible: str, meta: Optional[dict] = None) -> CollabViewUpdate:
        """把 LLM 填表结果落到 UI（reply / logic_draft / script）。"""
        from backend.script_generator.collaborator.collab_form import (
            form_ui_state,
            logic_draft_to_graph,
            normalize_collab_script,
        )

        meta = meta or {}
        plan = meta.get("plan")
        if isinstance(plan, list) and plan:
            if not isinstance(self.session.artifacts, dict):
                self.session.artifacts = {}
            self.session.artifacts["agent_plan"] = plan
            self.session.artifacts["agent_done_when"] = str(meta.get("done_when") or "")
            self.session.artifacts["agent_task_done"] = bool(meta.get("task_done"))
        # 1) 表内 logic_draft 优先
        draft = meta.get("logic_draft") or []
        if isinstance(draft, list) and draft:
            graph = logic_draft_to_graph(draft, title="脚本草稿")
            if graph.steps:
                self.session.logic = graph
                if not isinstance(self.session.artifacts, dict):
                    self.session.artifacts = {}
                self.session.artifacts["logic_draft"] = draft
                self.session.phase = CollabPhase.PROPOSE_LOGIC
        # 2) 否则从气泡伪代码兜底抽取
        elif not self.session.logic.steps:
            draft_txt, draft_steps = extract_draft_logic_from_text(visible or "")
            if not draft_steps:
                for m in reversed(list(self.session.messages or [])[-12:]):
                    if str(m.get("role") or "") != "agent":
                        continue
                    draft_txt, draft_steps = extract_draft_logic_from_text(
                        str(m.get("text") or "")
                    )
                    if draft_steps:
                        break
            if draft_steps:
                self.session.logic = LogicGraph(
                    title="脚本草稿",
                    architecture="scene_driven",
                    steps=draft_steps,
                )
                if not isinstance(self.session.artifacts, dict):
                    self.session.artifacts = {}
                if draft_txt:
                    self.session.artifacts["draft_pseudo"] = draft_txt
                self.session.phase = CollabPhase.PROPOSE_LOGIC

        # 3) 成品脚本
        script = meta.get("script")
        script_name = str(meta.get("script_name") or "").strip() or None
        code_out = None
        name_out = None
        trial_ready = False
        if isinstance(script, str) and script.strip():
            from backend.script_generator.collaborator.collab_form import (
                collab_script_issues,
            )

            script, norm_notes = normalize_collab_script(script, self.session)
            if not isinstance(self.session.artifacts, dict):
                self.session.artifacts = {}
            self.session.artifacts["script_normalize_notes"] = list(norm_notes)
            issues = collab_script_issues(script)
            self.session.generated_code = script.strip()
            if script_name:
                self.session.script_name = script_name
            elif not self.session.script_name:
                self.session.script_name = "collab_script.py"
            code_out = self.session.generated_code
            name_out = self.session.script_name
            trial_ready = not issues
            if trial_ready:
                self.session.phase = CollabPhase.READY_TRIAL
            if not isinstance(self.session.artifacts, dict):
                self.session.artifacts = {}
            self.session.artifacts["script_from_form"] = True

        chips = [str(c) for c in (meta.get("chips") or []) if str(c).strip()]
        sid = str(meta.get("select_skill") or "").strip() or None
        need_frame = bool(meta.get("need_frame"))
        analyze = meta.get("analyze_result")
        if isinstance(analyze, dict) and analyze.get("ok"):
            sid = str(analyze.get("skill_id") or sid or "").strip() or sid
            if analyze.get("chips") and not chips:
                chips = [str(c) for c in analyze.get("chips") or [] if str(c).strip()]
            body = (visible or "").strip()
            brief = str(
                analyze.get("probe_brief") or analyze.get("summary") or ""
            ).strip()
            if brief:
                if not body:
                    body = brief
                elif brief not in body and (
                    "技能" in body or "skill" in body.lower() or len(body) > 80
                ):
                    body = f"{brief}\n\n{body}"
                elif brief not in body:
                    body = f"{brief}\n\n{body}"
            if sid:
                self.session.skill_id = sid
            preview = (
                str(self.session.frame_path)
                if self.session.frame_path and self.session.frame_path.is_file()
                else None
            )
            return self._view(
                CollabPhase.PROPOSE_LOGIC
                if self.session.logic.steps
                else CollabPhase.CLARIFY,
                agent_message=body or brief or "（已分析画面）",
                chips=chips
                or (["看起来对", "换一张图"] if self.session.logic.steps else []),
                steps=self.session.logic.to_ui_steps(),
                shape_label=str(
                    analyze.get("shape_label")
                    or self.session.last_shape_label
                    or self.session.logic.title
                    or "协作中"
                ),
                cta_text=str(analyze.get("cta_text") or "看起来对，生成脚本"),
                cta_enabled=bool(analyze.get("can_ack", True)),
                preview_path=preview,
                preview_caption=Path(preview).name if preview else "",
                trial_ready=trial_ready,
                code=code_out,
                script_name=name_out,
            )

        if sid:
            sk = self.registry.get(sid)
            if sk is not None:
                self.session.skill_id = sid
                body = (visible or "").strip()
                has_frame = bool(
                    self.session.frame_path and self.session.frame_path.is_file()
                )
                if has_frame and not need_frame:
                    return self.on_frame(Path(self.session.frame_path))
                msg2, chips2 = sk.on_matched(self.session)
                try:
                    if not self.session.logic.steps and hasattr(sk, "_build_logic"):
                        self.session.logic = sk._build_logic(None, confirmed=False)  # type: ignore[attr-defined]
                except Exception:
                    pass
                if msg2 and msg2 not in body:
                    body = (body + "\n\n" + msg2).strip() if body else msg2
                if not chips:
                    chips = list(chips2)
                return self._view(
                    CollabPhase.CLARIFY,
                    agent_message=body or msg2,
                    chips=chips or chips2,
                    steps=self.session.logic.to_ui_steps(),
                    shape_label="待画面",
                    cta_enabled=False,
                    trial_ready=trial_ready,
                    code=code_out,
                    script_name=name_out,
                )

        ui = form_ui_state(meta, self.session)
        if ui["mode"] in ("ask", "continue"):
            chips = list(ui["chips"] or chips)
            phase = (
                CollabPhase.PROPOSE_LOGIC
                if self.session.logic.steps
                else CollabPhase.CLARIFY
            )
            return self._view(
                phase,
                agent_message=visible or "（无回复）",
                chips=chips,
                cta_enabled=False,
                cta_text="",
                shape_label=self.session.logic.title
                or self.session.last_shape_label
                or "对话中",
                steps=self.session.logic.to_ui_steps(),
                trial_ready=False,
                trial_hint=ui["activity"],
                code=code_out,
                script_name=name_out,
            )
        if ui["mode"] == "trial":
            chips = list(ui["chips"] or chips)
        elif not chips:
            chips = ["去经典生成"] if not self.session.skill_id else []
        if trial_ready or ui["mode"] == "trial":
            phase = CollabPhase.READY_TRIAL
            if "去试运行" not in chips:
                chips = (["去试运行"] + chips)[:3]
            cta_text = "去试运行"
            cta_on = True
            trial_hint = "下一步：点「去试运行」"
        elif self.session.logic.steps:
            phase = CollabPhase.PROPOSE_LOGIC
            cta_text = "看起来对，生成脚本"
            cta_on = True
            trial_hint = ""
        else:
            phase = CollabPhase.CLARIFY
            cta_text = ""
            cta_on = False
            trial_hint = ""
        return self._view(
            phase,
            agent_message=visible or "（无回复）",
            chips=chips,
            cta_enabled=cta_on,
            cta_text=cta_text,
            shape_label=self.session.logic.title
            or self.session.last_shape_label
            or ("成品已就绪" if trial_ready else "对话中"),
            steps=self.session.logic.to_ui_steps(),
            trial_ready=trial_ready,
            trial_hint=trial_hint,
            code=code_out,
            script_name=name_out,
        )

    def on_user_text_local(
        self, text: str, *, llm_configured: bool = False
    ) -> CollabViewUpdate:
        """本地规则路径：无 API，或 GUI 未能读到 API 参数时的降级。"""
        text = (text or "").strip()
        if not text:
            return CollabViewUpdate.from_phase(
                self.session.phase,
                agent_message="",
                append_agent=False,
            )
        self.begin_user_turn(text)

        if self._is_skills_catalog_question(text):
            return self._reply_skills_catalog()

        if (
            self.session.skill_id
            and self.session.phase in (CollabPhase.CLARIFY, CollabPhase.UNDERSTAND)
            and not self._is_meta_question(text)
        ):
            return self._enter_skill_need_frame()

        skill = self.registry.match_best(text)
        if skill is None:
            self.session.clarify_count += 1
            if llm_configured:
                msg = (
                    "我还不太确定你的目标。\n"
                    "换一句更具体的；或去「经典」Tab 写介绍生成。"
                )
            else:
                msg = (
                    "当前协作页没有读到可用的 API 配置。\n"
                    "请到「1. API 配置」确认提供商 / Key / 模型；"
                    "或去「经典」Tab。"
                )
            return self._view(
                CollabPhase.CLARIFY,
                agent_message=msg,
                chips=["去经典生成"],
                cta_enabled=False,
                shape_label="待澄清",
            )

        self.session.skill_id = skill.id
        return self._enter_skill_need_frame()

    def on_user_text(self, text: str) -> CollabViewUpdate:
        """兼容旧调用：等同本地路径。"""
        return self.on_user_text_local(text)

    def _enter_skill_need_frame(self) -> CollabViewUpdate:
        skill = self._skill()
        if skill is None:
            return self.reset()
        msg, chips = skill.on_matched(self.session)
        # 草案逻辑图（无探针）
        try:
            # explore 未跑时用空 probe 建草案
            if not self.session.logic.steps and hasattr(skill, "_build_logic"):
                self.session.logic = skill._build_logic(None, confirmed=False)  # type: ignore[attr-defined]
        except Exception:
            pass
        return self._view(
            CollabPhase.CLARIFY,
            agent_message=msg,
            chips=chips,
            steps=self.session.logic.to_ui_steps(),
            shape_label="待画面",
            cta_enabled=False,
            trial_ready=False,
        )

    def on_frame(self, path: Path) -> CollabViewUpdate:
        path = Path(path)
        if not path.is_file():
            return self._view(
                self.session.phase,
                agent_message=f"无效画面：{path}",
                chips=["开始截图", "选已有截图", "用当前窗口"],
                bubble_kind="system",
            )
        self.session.frame_path = path
        skill = self._skill()
        if skill is None:
            # 无玩法 / 旧会话 skill 已卸载：只收图，不自动出逻辑步
            if self.session.skill_id:
                self.session.skill_id = None
            self.session.probe = None
            # 换图后清掉上一张的辅助识图缓存
            if isinstance(self.session.artifacts, dict):
                self.session.artifacts.pop("assist_vision", None)
            from backend.script_generator.logic_graph import LogicGraph

            self.session.logic = LogicGraph()
            return self._view(
                CollabPhase.CLARIFY,
                agent_message=f"已载入画面「{path.name}」。说一句你想做什么就行。",
                chips=["去经典生成"],
                preview_path=str(path),
                preview_caption=path.name,
                steps=[],
                shape_label="已有画面",
                cta_enabled=False,
                bubble_kind="system",
            )

        self.session.generated_code = ""
        self.session.phase = CollabPhase.EXPLORE
        try:
            result = skill.explore(self.session, path)
        except Exception as e:
            return self._view(
                CollabPhase.CLARIFY,
                agent_message=f"探针失败：{e}",
                chips=[],
                cta_enabled=False,
                bubble_kind="system",
            )
        if result.logic is not None:
            self.session.logic = result.logic
        self.session.probe = result.probe
        self.session.artifacts["last_explore"] = {
            "summary": result.summary,
            "debug_path": result.debug_path,
        }
        return self._view(
            CollabPhase.PROPOSE_LOGIC,
            agent_message=result.summary,
            chips=result.chips,
            steps=self.session.logic.to_ui_steps(),
            shape_label=result.shape_label or self.session.logic.title,
            cta_text=result.cta_text,
            cta_enabled=result.can_ack,
            preview_path=result.debug_path or str(path),
            preview_caption=(result.summary or "")[:120].replace("\n", " "),
            trial_ready=False,
        )

    def on_ack(self) -> CollabViewUpdate:
        if (self.session.generated_code or "").strip():
            return self._trial_guide(
                "脚本已经写好。下一步：点「去试运行」，先看顶栏账号是否选对。"
            )

        skill = self._skill()
        if skill is not None and (
            self.session.probe or self.session.logic.steps
        ):
            self.session.phase = CollabPhase.CODEGEN
            try:
                emitted = skill.emit(self.session)
            except Exception as e:
                return self._view(
                    CollabPhase.PROPOSE_LOGIC,
                    agent_message=f"生成失败：{e}",
                    chips=["再改一改"],
                    cta_enabled=True,
                    cta_text="确认生成",
                )
            self.session.generated_code = emitted.code
            self.session.intro_text = emitted.intro
            self.session.script_name = emitted.script_name
            warn_msgs = "\n".join(emitted.warnings)
            body = (
                f"已生成「{emitted.script_name}」。\n"
                f"{warn_msgs}\n"
                "下一步：点「去试运行」。先确认顶栏账号。"
            )
            return self._trial_guide(body, code=emitted.code, intro=emitted.intro)

        if self.session.logic.steps:
            return self._view(
                CollabPhase.CODEGEN,
                agent_message="逻辑已确认。下一步：我按这几步写脚本，写完再点「去试运行」。",
                chips=[],
                steps=self.session.logic.to_ui_steps(),
                shape_label="正在写脚本",
                cta_text="写脚本中",
                cta_enabled=False,
                request_script=True,
            )

        return self._view(
            CollabPhase.CLARIFY,
            agent_message="还没有逻辑步骤。先说你要自动化什么，右侧出现步骤后再确认生成。",
            chips=["去经典生成"],
            cta_enabled=False,
        )

    def _trial_guide(
        self,
        message: str,
        *,
        code: str | None = None,
        intro: str | None = None,
    ) -> CollabViewUpdate:
        return self._view(
            CollabPhase.READY_TRIAL,
            agent_message=message,
            chips=["去试运行", "再改一改"],
            steps=self.session.logic.to_ui_steps(),
            shape_label="已确认",
            cta_text="去试运行",
            cta_enabled=True,
            trial_ready=True,
            trial_hint="下一步：去试运行",
            code=code if code is not None else self.session.generated_code,
            intro=intro if intro is not None else self.session.intro_text,
            script_name=self.session.script_name,
        )

    def on_chip(self, text: str) -> CollabViewUpdate | None:
        """处理引擎可识别的芯片；返回 None 表示交给 GUI 做截图/抓窗等 IO。"""
        t = (text or "").strip()
        # 用户气泡由 GUI 绘制；此处只推进状态（避免与 on_user_text 双记）

        if t in ("看起来对",):
            return self.on_ack()
        if t == "去经典生成":
            return self._view(
                CollabPhase.CLARIFY,
                agent_message="请切换到「2. 描述与素材」填写介绍，再到「3. 生成」。",
                chips=[],
            )
        if t in ("再改一改", "有问题再说"):
            return self.on_problem()
        if t == "重新分析画面":
            if self.session.frame_path and self.session.frame_path.is_file():
                return self.on_frame(self.session.frame_path)
            return self._view(
                CollabPhase.CLARIFY,
                agent_message="没有可用原图，请重新截图或选图。",
                chips=[],
                bubble_kind="system",
            )
        # IO 类芯片：GUI 处理
        if t in (
            "开始截图",
            "重新截图",
            "选已有截图",
            "换一张图",
            "用当前窗口",
            "去试运行",
            "不用",
        ):
            return None
        # 未知芯片当用户文本
        return self.on_user_text(t)

    def on_problem(self) -> CollabViewUpdate:
        return self._view(
            self.session.phase if self.session.phase != CollabPhase.IDLE else CollabPhase.CLARIFY,
            agent_message="说说哪里不对？或点某一步的「这一步不对」。",
            chips=["重新截图", "重新分析画面", "换一张图"],
            cta_enabled=self.session.phase == CollabPhase.PROPOSE_LOGIC,
        )

    def on_step_wrong(self, step_id: str) -> CollabViewUpdate:
        skill = self._skill()
        tip = skill.tip_for_step(step_id) if skill else "收到，请补充现象或换图。"
        self.session.logic.mark(step_id, "failed")
        return self._view(
            CollabPhase.PROPOSE_LOGIC if self.session.probe else CollabPhase.CLARIFY,
            agent_message=f"步骤不对：{step_id}\n{tip}",
            chips=["重新分析画面", "有问题再说"],
            steps=self.session.logic.to_ui_steps(),
            cta_enabled=bool(self.session.probe),
        )

    def on_trial_ok(self) -> CollabViewUpdate:
        self.session.phase = CollabPhase.DONE
        return self._view(
            CollabPhase.DONE,
            agent_message="很好。可到试运行页确认保存，或主界面任务列表跑该脚本。",
            chips=["去试运行"],
            trial_ready=True,
            trial_hint="反馈：能用",
            cta_text="去试运行",
            cta_enabled=True,
            steps=self.session.logic.to_ui_steps(),
        )

    def on_trial_bad(self) -> CollabViewUpdate:
        return self._view(
            CollabPhase.READY_TRIAL,
            agent_message="哪里不对？可以说「点歪了 / 没反应 / 识别错了」。",
            chips=["重新分析画面", "重新截图", "换一张图"],
            trial_ready=True,
            trial_hint="反馈：不行 · 请描述现象或换图",
            cta_text="去试运行",
            cta_enabled=True,
            steps=self.session.logic.to_ui_steps(),
        )

    def welcome(self) -> CollabViewUpdate:
        return self.reset()
