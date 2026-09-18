# -*- coding: utf-8 -*-
"""collab_form 协议自测。"""
from pathlib import Path

from backend.script_generator.collaborator.collab_form import (
    parse_collab_form,
    form_to_runtime_meta,
    normalize_form,
    plan_should_continue,
    logic_draft_to_graph,
    migrate_legacy_meta,
)


def _mk_session(with_img=True):
    """最小会话：带一张真实存在的素材图，用于素材引用 / IMG_DIR 归一路径。"""
    img = Path(__file__).resolve().parents[1] / "click_template.png"
    if with_img:
        assert img.is_file(), img
        assets = [{"id": "asset_1", "name": "挑战", "path": str(img)}]
    else:
        assets = []

    class _S:
        pass

    _S.assets = assets
    _S.frame_path = ""
    _S.artifacts = {}
    _S.logic = type("L", (), {"steps": []})()
    return _S(), img


def test_parse_new_form():
    raw = '''<<<COLLAB_FORM>>>
{"reply":"先匹配两张图","turn":"act","action":"match_and_describe","ask":null,"chips":[],"logic_draft":[{"label":"wait 挑战","asset":"[[a1]]"}],"focus_assets":["[[a1]]"],"script":null,"script_name":null}
<<<END>>>'''
    reply, form = parse_collab_form(raw)
    assert reply == "先匹配两张图"
    assert form["turn"] == "act"
    assert form["action"] == "match_and_describe"
    meta = form_to_runtime_meta(form)
    assert meta["match_assets"] is True
    assert meta["describe_frame"] is True
    g = logic_draft_to_graph(form["logic_draft"])
    assert len(g.steps) == 1


def test_legacy_end_json():
    raw = '''好的
<<<COLLAB_JSON>>>
{"turn":"ask","chips":["打开写文件权限"],"need_permission":"write_files","describe_frame":false,"match_assets":false}
<<<END_JSON>>>'''
    reply, form = parse_collab_form(raw)
    assert form["turn"] == "ask"
    assert form["ask"] == "write_files"
    meta = form_to_runtime_meta(form)
    assert meta["need_permission"] == "write_files"


def test_act_with_permission_deferred():
    f = normalize_form(
        {
            "reply": "先匹配再要权",
            "turn": "act",
            "action": "match_assets",
            "ask": "write_files",
        }
    )
    assert f["turn"] == "act"
    assert f["action"] == "match_assets"
    assert f.get("_ask_after_act") == "write_files"
    assert f.get("ask") is None


def test_script_delivery():
    f = normalize_form(
        {
            "reply": "脚本好了",
            "turn": "stop",
            "script": "def main():\n    pass\n",
            "script_name": "刷关.py",
            "logic_draft": [{"label": "click 挑战"}],
        }
    )
    meta = form_to_runtime_meta(f)
    assert "def main" in (meta.get("script") or "")
    assert meta.get("script_name") == "刷关.py"


def test_plan_not_done_continues():
    f = normalize_form(
        {
            "reply": "先对齐点击窗",
            "think": "窗可能绑错了",
            "plan": ["查点击窗"],
            "done_when": "点击窗一致",
            "task_done": False,
            "turn": "act",
        }
    )
    assert f["task_done"] is False
    assert f["plan"][0]["step"] == "查点击窗"
    meta = form_to_runtime_meta(f)
    assert meta["done_when"] == "点击窗一致"

    class _S:
        artifacts = {}
        generated_code = ""
        logic = type("L", (), {"steps": []})()
        frame_path = ""

    cont, gap = plan_should_continue(meta, _S())
    assert cont is True
    assert "窗口" in gap

    stopped = dict(meta)
    stopped["turn"] = "stop"
    cont, _gap = plan_should_continue(stopped, _S())
    assert cont is False


def test_plan_done_when_script_exists():
    f = normalize_form(
        {
            "reply": "写好了",
            "plan": ["写脚本"],
            "done_when": "已有 do_work",
            "task_done": True,
            "script": "async def do_work(browser):\n    pass\n",
        }
    )
    meta = form_to_runtime_meta(f)

    class _S:
        artifacts = {}
        generated_code = ""
        logic = type("L", (), {"steps": []})()
        frame_path = ""

    cont, _gap = plan_should_continue(meta, _S())
    assert cont is False


def test_permission_resume_steps():
    from backend.script_generator.collaborator.collab_form import permission_resume_steps

    class _S:
        artifacts = {
            "agent_plan": [{"step": "实测点击结算2并截图验证"}],
            "agent_think": "权限已给，实测点击",
            "last_window_inspect": {"ok": True, "resolved_differs": False},
        }
        messages = [{"role": "agent", "text": "直接戳一下结算2看动不动。"}]

    assert permission_resume_steps(_S(), "runtime_control") == ["click_probe"]
    assert permission_resume_steps(_S(), "write_files") == []

    class _Win:
        artifacts = {
            "agent_plan": [{"step": "改绑点击窗"}],
            "last_window_inspect": {"ok": True, "resolved_differs": True},
        }
        messages = []

    assert permission_resume_steps(_Win(), "runtime_control") == ["retarget"]


def test_user_wait_is_a_plan_pause():
    from backend.script_generator.collaborator.collab_form import (
        advance_plan_after_user,
        hold_for_user,
        plan_should_continue,
    )

    class _S:
        artifacts = {}
        generated_code = ""
        logic = type("L", (), {"steps": [1]})()
        frame_path = ""

    clicking = {
        "plan": [{"step": "实测点击结算2"}],
        "done_when": "等用户确认",
        "task_done": False,
        "turn": "act",
    }
    cont, gap = plan_should_continue(clicking, _S())
    assert cont is True
    assert "不要等用户" in gap

    clicking_stop = dict(clicking)
    clicking_stop["turn"] = "stop"
    cont, _gap = plan_should_continue(clicking_stop, _S())
    assert cont is False

    waiting = {
        "plan": [
            {"step": "等控制窗口授权"},
            {"step": "按探针结果改逻辑"},
        ],
        "done_when": "等用户确认",
        "task_done": False,
        "turn": "stop",
    }
    cont, _gap = plan_should_continue(waiting, _S())
    assert cont is False

    meta = {"plan": [{"step": "实测点击结算2"}], "chips": []}
    hold_for_user(meta, "runtime_control", "等控制窗口授权")
    assert meta["turn"] == "ask"
    assert meta["need_permission"] == "runtime_control"
    assert meta["task_done"] is False
    assert meta["plan"][0]["step"] == "等控制窗口授权"
    assert meta["plan"][1]["step"] == "实测点击结算2"

    left = advance_plan_after_user(meta["plan"], did_click=True)
    assert [x["step"] for x in left] == []


def test_form_ui_follows_plan():
    from backend.script_generator.collaborator.collab_form import form_ui_state

    waiting = form_ui_state(
        {
            "plan": [{"step": "把继续点击改到屏幕下方提示区"}],
            "task_done": False,
            "turn": "act",
            "chips": [],
        }
    )
    assert waiting["mode"] == "continue"
    assert waiting["cta_enabled"] is False
    assert waiting["chips"] == ["继续"]
    assert "屏幕下方" in waiting["activity"]

    ended = form_ui_state(
        {
            "plan": [{"step": "写入挑战点击修复版脚本"}],
            "task_done": False,
            "turn": "stop",
            "chips": [],
        }
    )
    assert ended["mode"] == "idle"
    assert ended["activity"] == ""
    assert ended["continue_step"] == ""

    session, _img = _mk_session()
    done = form_ui_state(
        {
            "plan": [],
            "task_done": True,
            "script": "async def do_work(browser):\n    pass\n",
        },
        session,
    )
    assert done["mode"] == "trial"
    assert done["cta_text"] == "去试运行"


def test_collab_script_kept_and_checked():
    from backend.script_generator.collaborator.collab_form import (
        coerce_runnable_script,
        collab_script_issues,
    )

    ok = (
        "async def do_work(browser: UserWindow):\n"
        "    await browser.wait_image('a.png')\n"
        "    await browser.click_image('a.png')\n"
    )
    session, _img = _mk_session()
    assert collab_script_issues(ok, session) == []
    assert "click_image" in coerce_runnable_script(ok, session)
    missing = collab_script_issues("print(1)\n")
    assert any("do_work" in item for item in missing)
    bad = collab_script_issues(
        "async def do_work(browser: UserWindow):\n"
        "    await browser.not_a_real_api()\n"
    )
    assert any("not_a_real_api" in item for item in bad)


def test_collab_script_normalize_delivers_runnable_code():
    """模型交回原始脚本（Any 标注 + [[素材id]] + 缺 import）要能被机械补齐。"""
    from backend.script_generator.collaborator.collab_form import (
        coerce_runnable_script,
        collab_script_issues,
    )

    session, img = _mk_session()
    raw = (
        "async def do_work(browser: Any):\n"
        "    ok = await browser.wait_image([[asset_1]], timeout=10)\n"
        "    await browser.click_image('[[asset_1]]', timeout=10)\n"
    )
    fixed = coerce_runnable_script(raw, session)
    assert "[[" not in fixed
    assert str(img).replace("\\", "/") in fixed
    assert "browser: UserBrowser | UserWindow" in fixed
    assert "from backend.automation.user_window import UserWindow" in fixed
    assert "IMG_DIR" in fixed
    assert collab_script_issues(fixed, session) == []


def test_collab_script_normalize_reports_unresolved_asset():
    from backend.script_generator.collaborator.collab_form import collab_script_issues

    session, _img = _mk_session()
    issues = collab_script_issues(
        "async def do_work(browser: UserWindow):\n"
        "    await browser.wait_image([[no_such_asset]])\n",
        session,
    )
    assert any("no_such_asset" in item for item in issues)


def test_collab_script_normalize_keeps_existing_img_dir():
    from backend.script_generator.collaborator.collab_form import normalize_collab_script

    session, _img = _mk_session()
    src = (
        "from pathlib import Path\n"
        "IMG_DIR = Path('C:/x')\n"
        "async def do_work(browser: UserWindow):\n"
        "    pass\n"
    )
    out, _notes = normalize_collab_script(src, session)
    assert out.count("IMG_DIR =") == 1
    assert "C:/x" in out


def test_collab_script_normalize_adds_missing_annotation():
    from backend.script_generator.collaborator.collab_form import normalize_collab_script

    session, _img = _mk_session()
    out, _notes = normalize_collab_script(
        "async def do_work(browser):\n    pass\n", session
    )
    assert "browser: UserBrowser | UserWindow" in out


if __name__ == "__main__":
    test_parse_new_form()
    test_legacy_end_json()
    test_act_with_permission_deferred()
    test_script_delivery()
    test_plan_not_done_continues()
    test_plan_done_when_script_exists()
    test_permission_resume_steps()
    test_user_wait_is_a_plan_pause()
    test_form_ui_follows_plan()
    test_collab_script_kept_and_checked()
    test_collab_script_normalize_delivers_runnable_code()
    test_collab_script_normalize_reports_unresolved_asset()
    test_collab_script_normalize_keeps_existing_img_dir()
    test_collab_script_normalize_adds_missing_annotation()
    print("ok")
