#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验 Minashigo Android 脚本包（非业务：只查结构与引用）。

用法:
  python mobile/tools/validate_pack.py mobile/pack/examples/demo_pack
  python mobile/tools/validate_pack.py path/to/pack.zip
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

SUPPORTED = {"0.1"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "pack" / "schema"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_pack_dir(src: Path, tmp_root: Path | None = None) -> Path:
    if src.is_dir():
        return src
    if src.suffix.lower() != ".zip":
        raise FileNotFoundError(f"不是目录或 zip: {src}")
    if tmp_root is None:
        raise ValueError("zip 需要临时目录")
    with zipfile.ZipFile(src, "r") as zf:
        zf.extractall(tmp_root)
    # 若只有一层根目录则进入
    kids = [p for p in tmp_root.iterdir() if not p.name.startswith(".")]
    if len(kids) == 1 and kids[0].is_dir() and not (tmp_root / "manifest.json").exists():
        return kids[0]
    return tmp_root


def _collect_images(images_dir: Path) -> set[str]:
    if not images_dir.is_dir():
        return set()
    names: set[str] = set()
    for f in images_dir.rglob("*"):
        if f.is_file() and f.suffix.lower() in IMG_EXT:
            names.add(f.relative_to(images_dir).as_posix())
            names.add(f.name)
    return names


def _validate_schema_optional(data: dict, schema_path: Path, errors: list[str], label: str) -> None:
    try:
        import jsonschema  # type: ignore
    except ImportError:
        return
    try:
        schema = _load_json(schema_path)
        jsonschema.validate(data, schema)
    except Exception as e:
        errors.append(f"{label} schema: {e}")


def validate_pack(pack_dir: Path) -> list[str]:
    errors: list[str] = []
    warnings: list[str] = []

    man_path = pack_dir / "manifest.json"
    script_path = pack_dir / "script.json"
    images_dir = pack_dir / "images"

    if not man_path.is_file():
        errors.append("缺少 manifest.json")
        return errors
    if not script_path.is_file():
        errors.append("缺少 script.json")
        return errors

    try:
        manifest = _load_json(man_path)
    except Exception as e:
        return [f"manifest.json 无法解析: {e}"]
    try:
        script = _load_json(script_path)
    except Exception as e:
        return [f"script.json 无法解析: {e}"]

    if not isinstance(manifest, dict) or not isinstance(script, dict):
        return ["manifest/script 必须是 JSON object"]

    for key in ("schema_version", "id", "name", "version", "platform", "entry"):
        if key not in manifest:
            errors.append(f"manifest 缺少字段: {key}")
    sv = str(manifest.get("schema_version", ""))
    if sv and sv not in SUPPORTED:
        errors.append(f"不支持的 manifest.schema_version: {sv}")
    if manifest.get("platform") not in (None, "android"):
        errors.append(f"platform 必须为 android，当前: {manifest.get('platform')!r}")

    _validate_schema_optional(manifest, SCHEMA_DIR / "manifest.schema.json", errors, "manifest")
    _validate_schema_optional(script, SCHEMA_DIR / "script.schema.json", errors, "script")

    if str(script.get("schema_version", "")) not in SUPPORTED:
        errors.append(f"不支持的 script.schema_version: {script.get('schema_version')!r}")

    states = script.get("states")
    if not isinstance(states, dict) or not states:
        errors.append("script.states 必须为非空 object")
        return errors

    initial = script.get("initial")
    if initial not in states:
        errors.append(f"initial 状态不存在: {initial!r}")

    images = _collect_images(images_dir)
    if not images_dir.is_dir():
        warnings.append("缺少 images/ 目录")
    elif not images:
        warnings.append("images/ 下没有图片")

    def check_state_ref(name: str, ctx: str) -> None:
        if name and name not in states:
            errors.append(f"未知状态引用 {name!r} @ {ctx}")

    def check_image(name: str, ctx: str) -> None:
        if not name:
            errors.append(f"空 image @ {ctx}")
            return
        if name not in images and Path(name).name not in images:
            errors.append(f"缺少图片 {name!r} @ {ctx}")

    for sid, state in states.items():
        if not isinstance(state, dict):
            errors.append(f"states[{sid}] 必须是 object")
            continue
        on_miss = state.get("on_miss")
        if on_miss:
            check_state_ref(str(on_miss), f"states.{sid}.on_miss")
        for i, det in enumerate(state.get("detect") or []):
            if not isinstance(det, dict):
                errors.append(f"detect[{i}] 非法 @ {sid}")
                continue
            check_image(str(det.get("image") or ""), f"states.{sid}.detect[{i}]")
            check_state_ref(str(det.get("goto") or ""), f"states.{sid}.detect[{i}].goto")
        for i, act in enumerate(state.get("actions") or []):
            if not isinstance(act, dict):
                errors.append(f"actions[{i}] 非法 @ {sid}")
                continue
            t = act.get("type")
            if t not in ("click_image", "click_xy", "sleep", "goto", "log", "exit"):
                errors.append(f"未知 action.type {t!r} @ states.{sid}.actions[{i}]")
            if t == "click_image":
                check_image(str(act.get("image") or ""), f"states.{sid}.actions[{i}]")
                if act.get("appear_image"):
                    check_image(str(act["appear_image"]), f"states.{sid}.actions[{i}].appear_image")
            if t == "goto":
                check_state_ref(str(act.get("state") or ""), f"states.{sid}.actions[{i}].goto")
            if t == "click_xy":
                has_abs = "x" in act and "y" in act
                has_n = "nx" in act and "ny" in act
                if not has_abs and not has_n:
                    errors.append(f"click_xy 需要 x,y 或 nx,ny @ states.{sid}.actions[{i}]")

    # 把 warnings 也返回，前缀区分
    return [f"ERROR: {e}" for e in errors] + [f"WARN: {w}" for w in warnings]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="校验 Minashigo Android pack")
    ap.add_argument("path", type=Path, help="pack 目录或 zip")
    args = ap.parse_args(argv)
    src = args.path.resolve()
    if not src.exists():
        print(f"不存在: {src}", file=sys.stderr)
        return 2

    import tempfile

    try:
        if src.is_file() and src.suffix.lower() == ".zip":
            with tempfile.TemporaryDirectory() as td:
                pack = _resolve_pack_dir(src, Path(td))
                msgs = validate_pack(pack)
        else:
            msgs = validate_pack(_resolve_pack_dir(src))
    except Exception as e:
        print(f"失败: {e}", file=sys.stderr)
        return 2

    errs = [m for m in msgs if m.startswith("ERROR:")]
    warns = [m for m in msgs if m.startswith("WARN:")]
    for m in msgs:
        print(m)
    if errs:
        print(f"校验失败：{len(errs)} error, {len(warns)} warn")
        return 1
    print(f"校验通过：0 error, {len(warns)} warn — {src}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
