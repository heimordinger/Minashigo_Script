"""脚本性能优化：伪录制分析 + LLM 修订（generate → trial → optimize）。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

_SKILL_PATH = (
    Path(__file__).resolve().parent
    / "skills"
    / "optimize-script-perf"
    / "SKILL.md"
)


def _load_skill_excerpt(*, max_chars: int = 6000) -> str:
    if not _SKILL_PATH.is_file():
        return (
            "Optimize script-side waste only. Metric: effective = total - black. "
            "Prefer staged matching, black-aware polls, crop fallback, cooldowns."
        )
    text = _SKILL_PATH.read_text(encoding="utf-8")
    # 去掉 YAML front matter
    if text.startswith("---"):
        end = text.find("---", 3)
        if end > 0:
            text = text[end + 3 :].lstrip()
    if len(text) > max_chars:
        return text[: max_chars - 24] + "\n…(skill truncated)"
    return text


def _parse_optimize_response(raw: str) -> tuple[str, str]:
    text = (raw or "").strip()
    summary = ""
    code = text
    m_sum = re.search(r"<<<SUMMARY>>>\s*(.*?)(?=<<<CODE>>>|\Z)", text, re.S | re.I)
    m_code = re.search(r"<<<CODE>>>\s*(.*)\Z", text, re.S | re.I)
    if m_sum:
        summary = m_sum.group(1).strip()
    if m_code:
        code = m_code.group(1).strip()
    return summary, code


async def optimize_script(
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    explanation_text: str,
    current_code: str,
    source_dir: str = "",
    script_name_hint: str = "",
    account_hint: str = "",
    user_feedback: str = "",
    trial_log: str = "",
    pseudo_record_dir: str = "",
    force: bool = False,
    on_partial=None,
    on_status=None,
    on_artifact=None,
    max_tokens: Optional[int] = None,
) -> tuple[str, str, int, int, dict]:
    """根据用户反馈 / 伪录制 / 试跑日志优化脚本。

    返回 (代码, 修改摘要, 输入tokens, 输出tokens, meta)。
    """
    from backend.script_generator.agent import (
        ALLOWED_BROWSER_METHODS,
        apply_codegen_patches,
        build_img_dir_line,
        call_llm,
        resolve_max_tokens,
        strip_code_fences,
        validate_generated_code,
        _load_config,
    )
    from backend.script_generator.api_catalog import api_contracts_block
    from backend.script_generator.feasibility import assess_trial_feasibility
    from backend.script_generator.pseudo_analyze import (
        analyze_pseudo_record_dir,
        assess_optimization_ceiling,
        compare_recent_effective,
        find_latest_pseudo_record,
        format_analysis_for_prompt,
        format_ceiling_for_prompt,
        guess_script_hint_from_code,
    )

    cfg = _load_config()
    defaults = cfg.get("defaults", {})
    mt = resolve_max_tokens(max_tokens if max_tokens is not None else defaults.get("max_tokens"))

    def _status(msg: str) -> None:
        if on_status:
            on_status(msg)

    def _artifact(kind: str, payload: str) -> None:
        if on_artifact:
            try:
                on_artifact(kind, payload)
            except Exception:
                pass

    original = (current_code or "").strip()
    if not original:
        raise RuntimeError("当前无代码可优化")

    hint = (script_name_hint or "").strip() or guess_script_hint_from_code(original)

    record_path: Path | None = None
    if (pseudo_record_dir or "").strip():
        record_path = Path(pseudo_record_dir)
        if not (record_path / "timeline.jsonl").is_file():
            record_path = None
    if record_path is None:
        record_path = find_latest_pseudo_record(
            script_hint=hint,
            account_hint=account_hint or "",
        )

    feas = assess_trial_feasibility(
        trial_log or "",
        record_dir=record_path,
    )
    _artifact("optimize_feasibility", feas.get("user_message") or "")

    analysis: dict[str, Any] | None = None
    ceiling: dict[str, Any] = {"status": "insufficient_data", "near_ceiling": False}
    if record_path is not None:
        _status(f"分析伪录制: {record_path.name}…")
        analysis = analyze_pseudo_record_dir(record_path)
        prior = compare_recent_effective(
            script_hint=hint,
            account_hint=account_hint or "",
        )
        ceiling = assess_optimization_ceiling(analysis, prior_compare=prior)
        _artifact("optimize_analysis", format_analysis_for_prompt(analysis))
        _artifact("optimize_ceiling", ceiling.get("user_message") or "")
        mode = ceiling.get("assessment_mode") or "full"
        _status(
            "边界评估: "
            + ("接近极限" if ceiling.get("near_ceiling") else ceiling.get("status", "?"))
            + (f" · {mode}" if mode != "full" else "")
            + (
                f" · 质量={'ok' if (analysis or {}).get('metrics_reliable') else 'BAD'}"
            )
        )
    else:
        _status("未找到伪录制目录，将仅依据代码与试跑日志优化…")

    # 脏指标 / L1 未过：禁止「接近边界跳过」
    if ceiling.get("recommend_skip") and not force:
        if not feas.get("optimize_ready", True):
            ceiling = dict(ceiling)
            ceiling["recommend_skip"] = False
            _status("L1 可行性未过，忽略「跳过优化」建议")
        elif ceiling.get("status") == "unreliable_metrics":
            pass  # assess 已 recommend_skip=False
        else:
            msg = ceiling.get("user_message") or "已接近优化边界，跳过 LLM 修订。"
            _artifact("stage", f"optimize|skipped|接近边界|{msg[:300]}")
            meta = {
                "pseudo_record_dir": str(record_path) if record_path else "",
                "baseline": (analysis or {}).get("summary") or {},
                "ceiling": ceiling,
                "feasibility": feas,
                "validation_errors": [],
                "trial_blocked": False,
                "script_hint": hint,
                "skipped_llm": True,
            }
            summary = (
                "【未调用模型】本地评估认为已接近脚本优化边界。\n\n"
                + msg
                + "\n\n若仍要 AI 修订，请带上明确优化方向后重试（将强制修订）。"
            )
            return original, summary.strip(), 0, 0, meta

    analysis_block = format_analysis_for_prompt(analysis or {})
    ceiling_block = format_ceiling_for_prompt(ceiling)
    # 有用户方向时缩短 skill，减少注意力稀释
    feedback = (user_feedback or "").strip()
    skill_block = _load_skill_excerpt(max_chars=3500 if feedback else 6000)
    api_block = api_contracts_block(explanation=(explanation_text or "")[:3000])
    img_dir = build_img_dir_line(source_dir) if source_dir else ""
    log_tail = (trial_log or "")[-8000:]

    baseline = (analysis or {}).get("summary") or {}
    flags = list((analysis or {}).get("quality_flags") or [])
    baseline_line = (
        f"total={baseline.get('total_s')}s "
        f"black={baseline.get('black_s')}s "
        f"effective={baseline.get('effective_s')}s "
        f"reliable={baseline.get('metrics_reliable', True)} "
        f"flags={flags or []}"
        if baseline
        else "(无 baseline 指标)"
    )

    system = (
        "You optimize Minashigo FSM automation scripts.\n"
        "Goals (in order):\n"
        "1) Honor user optimization direction if present.\n"
        "2) Preserve correctness / task completion (do NOT trade reliability for speed).\n"
        "3) Reduce script-side waste measured by effective = total - black "
        "(use out-of-black match for script overhead; capture is parallel observer; "
        "ignore unreliable metrics).\n"
        "Do NOT shorten irreducible game loading / AUTO / battle waits.\n"
        "Surgical patches only: staged probe, black-aware polls, quiet matches, "
        "cooldowns, crop fallback, tighter post-UI sleeps.\n"
        "Never remove pseudo-record hooks if present; add if missing "
        "(DEBUG_PSEUDO_RECORD / enable_pseudo_record / finish_pseudo_record).\n"
        "Do not cross-wire task images (jjc must not click room/ta, etc).\n"
        "Allowed browser methods: "
        + ", ".join(sorted(ALLOWED_BROWSER_METHODS))
        + ".\n"
        f"IMG_DIR should be: {img_dir or '(from explanation)'}.\n\n"
        "## Skill reference\n"
        + skill_block
        + "\n\n"
        + api_block
    )

    feedback_block = (
        f"## User optimization direction (highest priority)\n{feedback}\n\n"
        if feedback
        else "## User optimization direction\n(none — optimize from metrics/logs)\n\n"
    )
    feas_block = f"## L1 feasibility\n{feas.get('user_message') or '(no trial log)'}\n\n"

    user = (
        feedback_block
        + feas_block
        + f"## Baseline metrics\n{baseline_line}\n\n"
        f"## Optimization ceiling (local assessment)\n{ceiling_block}\n\n"
        f"## Pseudo-record analysis\n{analysis_block}\n\n"
        f"## Script explanation (excerpt)\n{(explanation_text or '')[:5000]}\n\n"
        f"## Trial log (tail)\n{log_tail or '(empty)'}\n\n"
        "## Current Python script\n```python\n"
        f"{original}\n"
        "```\n\n"
        "## Task\n"
        "1. If user direction is present: address those points first.\n"
        "2. Keep or improve reliability (match success paths, popup loops like room_ok).\n"
        "3. Cut script-side waste only when metrics are reliable.\n"
        "4. Output optimized full script.\n"
        "5. In SUMMARY: user-request changes + reliability note + any effective wins.\n"
        "Respond format:\n<<<SUMMARY>>>\n...\n<<<CODE>>>\nfull python source\n"
    )

    _status("调用模型优化脚本…")
    _artifact("stage", "optimize|running|性能优化|分析完成，生成修订代码")
    raw, inp, out = await call_llm(
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        messages=[{"role": "user", "content": [{"type": "text", "text": user}]}],
        system_prompt=system,
        on_partial=on_partial,
        max_tokens=mt,
    )

    summary, code = _parse_optimize_response(raw)
    if not summary and "<<<SUMMARY>>>" not in raw.upper():
        code = strip_code_fences(raw)
        summary = "（模型未返回 SUMMARY 区块，已提取代码）"

    code = strip_code_fences(code).strip()
    if not code:
        raise RuntimeError("优化结果为空")

    code, patch_notes = apply_codegen_patches(
        code,
        source_dir=source_dir or "",
        plan=None,
        explanation=explanation_text or "",
    )
    val_errors = validate_generated_code(
        code,
        source_dir=source_dir or "",
        image_paths=[],
        explanation=explanation_text or "",
    )

    # 校验失败：自动再修一轮（确定性 patch 后仍失败时）
    if val_errors:
        _status(f"校验未过（{len(val_errors)}），自动修一轮…")
        fix_prompt = (
            "Fix ALL validation errors below. Return full Python script.\n"
            "Keep behavior; only fix structure/wiring/API issues.\n\n"
            "## Validation errors\n"
            + "\n".join(f"- {e}" for e in val_errors[:16])
            + "\n\n## Current code\n```python\n"
            + code
            + "\n```\n\n"
            "Respond:\n<<<SUMMARY>>>\nwhat you fixed\n<<<CODE>>>\nfull python\n"
        )
        raw2, i2, o2 = await call_llm(
            provider=provider,
            api_key=api_key,
            model=model,
            api_endpoint=api_endpoint,
            messages=[{"role": "user", "content": [{"type": "text", "text": fix_prompt}]}],
            system_prompt=(
                "You fix Minashigo script validation errors. "
                "Output full corrected Python only."
            ),
            on_partial=on_partial,
            max_tokens=mt,
        )
        inp += i2
        out += o2
        s2, c2 = _parse_optimize_response(raw2)
        c2 = strip_code_fences(c2 or raw2).strip()
        if c2:
            code = c2
            if s2:
                summary = (summary or "").rstrip() + "\n\n【自动修】\n" + s2
            code, more_patches = apply_codegen_patches(
                code,
                source_dir=source_dir or "",
                plan=None,
                explanation=explanation_text or "",
            )
            patch_notes = list(patch_notes or []) + list(more_patches or [])
            val_errors = validate_generated_code(
                code,
                source_dir=source_dir or "",
                image_paths=[],
                explanation=explanation_text or "",
            )

    if patch_notes:
        notes = "\n".join(f"- {n}" for n in patch_notes[:8])
        summary = (summary or "").rstrip() + f"\n\n【本地补丁】\n{notes}"

    if val_errors:
        summary = (summary or "").rstrip() + "\n\n【校验警告】\n" + "\n".join(
            f"- {e}" for e in val_errors[:8]
        )

    calibration_suggested = False
    try:
        from backend.script_generator.threshold_calibrate import should_offer_calibration
        if should_offer_calibration(user_feedback or "", trial_log or ""):
            calibration_suggested = True
            summary = (summary or "").rstrip() + (
                "\n\n【阈值标定】反馈/日志疑似匹配问题；"
                "可用 `threshold_calibrate.calibrate_image_threshold` 从 1.0 下降钉死"
                "（本次未自动执行）。"
            )
    except Exception:
        pass

    if feas.get("user_message"):
        summary = (summary or "").rstrip() + "\n\n【L1 可行性】\n" + feas["user_message"]

    ceiling_msg = (ceiling.get("user_message") or "").strip()
    if ceiling_msg:
        summary = (summary or "").rstrip() + "\n\n【优化边界评估】\n" + ceiling_msg

    _artifact("stage", f"optimize|done|优化完成|{summary[:400]}")

    meta = {
        "pseudo_record_dir": str(record_path) if record_path else "",
        "baseline": baseline,
        "ceiling": ceiling,
        "feasibility": feas,
        "validation_errors": val_errors,
        "trial_blocked": bool(val_errors),
        "script_hint": hint,
        "skipped_llm": False,
        "calibration_suggested": calibration_suggested,
    }
    return code, summary.strip(), inp, out, meta
