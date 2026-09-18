# -*- coding: utf-8 -*-
"""协作草稿落盘规则自测：统一目录、同名不覆盖、不进正式任务列表。"""
from __future__ import annotations

import types
from pathlib import Path

from core.path import SCRIPTS_PATH
from gui.widgets.script_gen_panel.mixin_collab import _draft_target_path


_CODE = "async def do_work(browser: UserBrowser | UserWindow):\n    pass\n"


def test_draft_target_path_keeps_same_content():
    root = SCRIPTS_PATH / "_collab_test_tmp"
    root.mkdir(parents=True, exist_ok=True)
    try:
        first = _draft_target_path(root, "同名", _CODE)
        assert first.name == "同名.py"
        first.write_text(_CODE, encoding="utf-8")
        # 同名同内容：复用，不堆重复
        assert _draft_target_path(root, "同名", _CODE) == first
        # 同名不同内容：追加 _2，不覆盖
        second = _draft_target_path(root, "同名", _CODE + "\n# 改过\n")
        assert second.name == "同名_2.py"
        assert second != first
    finally:
        for f in list(root.glob("*.py")):
            f.unlink()
        try:
            root.rmdir()
        except OSError:
            pass


def test_scan_process_tasks_skips_collab_drafts():
    import gui.facade_impl as facade_impl

    cls = next(
        v
        for v in vars(facade_impl).values()
        if isinstance(v, type) and hasattr(v, "scan_process_tasks")
    )
    draft_dir = SCRIPTS_PATH / "_collab"
    draft_dir.mkdir(parents=True, exist_ok=True)
    draft = draft_dir / "_tmp_task_list_check.py"
    draft.write_text(_CODE, encoding="utf-8")
    try:
        tasks = cls.scan_process_tasks(types.SimpleNamespace())
        assert not any(t.startswith("_collab/") for t in tasks)
        assert any(t.startswith("yys/") for t in tasks)
    finally:
        draft.unlink()
        try:
            draft_dir.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    test_draft_target_path_keeps_same_content()
    test_scan_process_tasks_skips_collab_drafts()
    print("ok")
