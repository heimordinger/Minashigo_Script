# -*- coding: utf-8 -*-
"""DeepSeek 把工具调用写进正文时，应解析成工具而不是显示给用户。"""
from backend.script_generator.agent import parse_dsml_tool_calls, strip_dsml_markup

BAR = "\uff5c"


def test_parse_doubled_dsml_invoke():
    raw = (
        f"<{BAR}{BAR}DSML{BAR}{BAR} calls>\n"
        f"<{BAR}{BAR}DSML{BAR}{BAR} invoke name=\"run_probe_script\">\n"
        f"<{BAR}{BAR}DSML{BAR}{BAR} parameter name=\"code\" string=\"true\">"
        "result = 1"
        f"</{BAR}{BAR}DSML{BAR}{BAR} parameter>\n"
        f"</{BAR}{BAR}DSML{BAR}{BAR} invoke>\n"
        f"</{BAR}{BAR}DSML{BAR}{BAR} calls>"
    )
    calls, cleaned = parse_dsml_tool_calls(raw)
    assert len(calls) == 1
    assert calls[0].function.name == "run_probe_script"
    assert "result = 1" in calls[0].function.arguments
    assert "DSML" not in cleaned
    assert "DSML" not in strip_dsml_markup(raw)


if __name__ == "__main__":
    test_parse_doubled_dsml_invoke()
    print("ok")
