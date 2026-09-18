# -*- coding: utf-8 -*-
"""逻辑步骤秒数：界面中文，存盘仍是 timeout / sleep。"""
from backend.script_generator.logic_graph import (
    LogicStep,
    apply_step_param,
    display_action,
    param_for_ui,
    parse_step_params,
    step_line_for_model,
)
from backend.script_generator.collaborator.canvas import enrich_logic_steps_for_ui


def test_timeout_display_and_edit():
    label = "wait 「挑战」 timeout=60"
    params = parse_step_params(label)
    assert params["timeout"] == 60
    assert display_action(label) == "等待「挑战」"
    assert "timeout" not in display_action(label)
    ui = param_for_ui(params, label=label)
    assert ui["label"] == "最多等"
    assert ui["key"] == "timeout"
    assert ui["value"] == 60

    step = LogicStep(id="draft_1", label=label, params=dict(params))
    apply_step_param(step, "timeout", 90)
    assert step.params["timeout"] == 90
    assert "timeout=90" in step.label
    assert "最多等" not in step.label
    assert step_line_for_model(step) == step.label or "timeout=90" in step_line_for_model(step)


def test_sleep_display_and_edit():
    label = "sleep 2"
    params = parse_step_params(label)
    assert params["sleep"] == 2
    assert display_action(label) == "停顿"
    ui = param_for_ui(params, label=label)
    assert ui["key"] == "sleep"
    assert ui["label"] == "停"
    step = LogicStep(id="draft_2", label=label, params=dict(params))
    apply_step_param(step, "sleep", 5)
    assert step.params["sleep"] == 5
    assert step.label == "sleep 5"
    assert "停" not in step_line_for_model(step)


def test_enrich_keeps_english_label():
    cards = enrich_logic_steps_for_ui(
        [{"id": "draft_1", "label": "click 「金币」 timeout=30", "params": {}}],
        [],
    )
    assert cards[0]["display_label"] == "点击「金币」"
    assert cards[0]["param"]["key"] == "timeout"
    assert "timeout=30" in cards[0]["label"]
