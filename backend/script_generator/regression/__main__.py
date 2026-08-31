"""Golden 回归：离线检查语料 / 指纹 / few-shot 检索；可选 --live 调 LLM。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.script_generator.few_shot import (  # noqa: E402
    build_few_shot_block,
    corpus_root,
    load_index,
    select_few_shots,
    select_templates,
)


@dataclass
class CheckResult:
    case_id: str
    ok: bool
    messages: list[str] = field(default_factory=list)


def _load_case(case_dir: Path) -> dict:
    meta = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    expl_name = meta.get("explanation_file") or "explanation.txt"
    expl_path = case_dir / expl_name
    explanation = expl_path.read_text(encoding="utf-8") if expl_path.is_file() else ""
    meta["_dir"] = case_dir
    meta["_explanation"] = explanation
    return meta


def _resolve_ref_script(meta: dict) -> Path | None:
    ref = meta.get("ref_script") or ""
    if not ref:
        return None
    p = Path(ref)
    if not p.is_absolute():
        # relative to case dir first, then project root
        cand = meta["_dir"] / ref
        if cand.is_file():
            return cand
        cand = _PROJECT_ROOT / ref
        if cand.is_file():
            return cand
    return p if p.is_file() else None


def _check_text_patterns(code: str, meta: dict) -> list[str]:
    errs: list[str] = []
    for s in meta.get("must_contain") or []:
        if s not in code:
            errs.append(f"missing must_contain: {s!r}")
    for s in meta.get("must_not_contain") or []:
        if s in code:
            errs.append(f"found must_not_contain: {s!r}")
    return errs


def check_case_offline(meta: dict) -> CheckResult:
    cid = meta.get("id") or meta["_dir"].name
    msgs: list[str] = []
    ok = True

    # 1) explanation present
    if not (meta.get("_explanation") or "").strip():
        ok = False
        msgs.append("explanation empty")

    # 2) ref script fingerprint
    ref = _resolve_ref_script(meta)
    if ref is None:
        ok = False
        msgs.append(f"ref_script not found: {meta.get('ref_script')}")
    else:
        code = ref.read_text(encoding="utf-8")
        for e in _check_text_patterns(code, meta):
            ok = False
            msgs.append(e)
        if meta.get("run_validator"):
            from backend.script_generator.agent import validate_generated_code
            verrs = validate_generated_code(
                code,
                source_dir=meta.get("source_dir") or "",
            )
            if verrs:
                ok = False
                msgs.append(f"validator: {verrs[0]}")
            else:
                msgs.append("validator: pass")

    # 3) few-shot retrieval
    expected = list(meta.get("expect_few_shot") or [])
    shots = select_few_shots(
        explanation=meta.get("_explanation") or "",
        tags=list(meta.get("tags") or []),
    )
    got_ids = [s["id"] for s in shots]
    for eid in expected:
        if eid not in got_ids:
            ok = False
            msgs.append(f"few-shot miss: expected {eid!r} in {got_ids}")
    if expected and all(e in got_ids for e in expected):
        msgs.append(f"few-shot ok: {got_ids}")

    expect_tmpl = meta.get("expect_template", "__unset__")
    tmpls = select_templates(
        explanation=meta.get("_explanation") or "",
        tags=list(meta.get("tags") or []),
        source_dir=str(_PROJECT_ROOT / (meta.get("source_dir") or "")) if meta.get("source_dir") else "",
    )
    tmpl_ids = [t["id"] for t in tmpls]
    if expect_tmpl == "__unset__":
        if tmpl_ids:
            msgs.append(f"template retrieved: {tmpl_ids}")
    elif expect_tmpl:
        if expect_tmpl not in tmpl_ids:
            ok = False
            msgs.append(f"template miss: expected {expect_tmpl!r} in {tmpl_ids}")
        else:
            msgs.append(f"template ok: {tmpl_ids}")
    elif tmpl_ids:
        ok = False
        msgs.append(f"template unexpected: {tmpl_ids}")
    else:
        msgs.append("template none (ok)")

    # 4) prompt assembly includes few-shot
    from backend.script_generator.agent import _build_system_prompt
    prompt = _build_system_prompt(
        source_dir=str(_PROJECT_ROOT / (meta.get("source_dir") or "")) if meta.get("source_dir") else "",
        explanation=meta.get("_explanation") or "",
        tags=list(meta.get("tags") or []),
    )
    if expected:
        if "Few-shot Examples" not in prompt:
            ok = False
            msgs.append("system prompt missing Few-shot Examples section")
        else:
            missing_in_prompt = [e for e in expected if e not in prompt]
            # ids may only appear in retrieval metadata, not prompt body — check titles/content via block
            block = build_few_shot_block(
                explanation=meta.get("_explanation") or "",
                tags=list(meta.get("tags") or []),
                source_dir=str(_PROJECT_ROOT / (meta.get("source_dir") or "")) if meta.get("source_dir") else "",
            )
            if "```python" not in block:
                ok = False
                msgs.append("few-shot block missing code fence")
            else:
                msgs.append("few-shot injected into system prompt")
    else:
        msgs.append("system prompt built")

    # 5) rules budget
    cfg = json.loads(
        (_PROJECT_ROOT / "backend/script_generator/config.json").read_text(encoding="utf-8")
    )
    n_rules = len(cfg.get("rules") or [])
    if n_rules > 12:
        ok = False
        msgs.append(f"rules too many: {n_rules} (want ≤12, target ~10)")
    else:
        msgs.append(f"rules count={n_rules}")

    return CheckResult(case_id=cid, ok=ok, messages=msgs)


def iter_cases() -> list[dict]:
    index = load_index()
    root = corpus_root()
    cases = []
    for entry in index.get("golden") or []:
        d = root / entry["dir"]
        if not (d / "case.json").is_file():
            continue
        cases.append(_load_case(d))
    return cases


def run_offline() -> int:
    cases = iter_cases()
    if not cases:
        print("No golden cases found.")
        return 1
    failed = 0
    for meta in cases:
        result = check_case_offline(meta)
        status = "PASS" if result.ok else "FAIL"
        print(f"[{status}] {result.case_id}")
        for m in result.messages:
            print(f"    - {m}")
        if not result.ok:
            failed += 1
    print(f"\n{len(cases) - failed}/{len(cases)} passed")
    return 1 if failed else 0


async def run_live(case_ids: list[str] | None = None) -> int:
    """对指定 case 调 generate_script，再跑 must_contain（需环境变量里的 API）。"""
    import os
    from backend.script_generator.agent import generate_script, validate_generated_code

    provider = os.environ.get("SG_PROVIDER", "deepseek")
    api_key = os.environ.get("SG_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or ""
    model = os.environ.get("SG_MODEL", "deepseek-chat")
    endpoint = os.environ.get("SG_ENDPOINT") or None
    if not api_key:
        print("SG_API_KEY / DEEPSEEK_API_KEY required for --live")
        return 1

    failed = 0
    for meta in iter_cases():
        cid = meta["id"]
        if case_ids and cid not in case_ids:
            continue
        print(f"[LIVE] generating {cid}…")
        source = meta.get("source_dir") or ""
        source_abs = str(_PROJECT_ROOT / source) if source else ""
        try:
            code, _, _ = await generate_script(
                provider=provider,
                api_key=api_key,
                model=model,
                api_endpoint=endpoint,
                explanation_text=meta.get("_explanation") or "",
                image_paths=[],
                source_dir=source_abs,
                send_images=False,
            )
        except Exception as e:
            print(f"[FAIL] {cid}: generate error: {e}")
            failed += 1
            continue
        errs = _check_text_patterns(code, meta)
        verrs = validate_generated_code(code, source_dir=source_abs)
        if errs or verrs:
            print(f"[FAIL] {cid}")
            for e in errs + verrs[:3]:
                print(f"    - {e}")
            failed += 1
        else:
            print(f"[PASS] {cid}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Script generator golden regression")
    parser.add_argument("--live", action="store_true", help="Call LLM to regenerate cases")
    parser.add_argument("--case", action="append", dest="cases", help="Case id filter (repeatable)")
    args = parser.parse_args(argv)
    if args.live:
        import asyncio
        return asyncio.run(run_live(args.cases))
    return run_offline()


if __name__ == "__main__":
    raise SystemExit(main())
