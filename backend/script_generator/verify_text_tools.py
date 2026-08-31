"""最小验证：文本伪工具循环（不调真实 API）。

用法（项目根）:
  set PYTHONPATH=.
  python -m backend.script_generator.verify_text_tools
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

_PROJECT = Path(__file__).resolve().parents[2]
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))


SAMPLE_CODE = '''
async def room_claim(browser):
    return True

async def do_work(browser):
    pass
'''


async def _run() -> None:
    from backend.script_generator.agent import _text_revise_tools_loop
    from backend.script_generator.revise_tools import (
        ReviseToolContext,
        parse_text_tool_calls,
        resolve_revise_tool_assist,
    )

    # 1) 解析协议
    raw_tool = (
        '<<<TOOL>>>\n{"name":"diagnose_log","arguments":{}}\n<<<END_TOOL>>>\n'
        '<<<TOOL>>>\n{"name":"get_unit","arguments":{"name":"room_claim"}}\n<<<END_TOOL>>>'
    )
    calls = parse_text_tool_calls(raw_tool)
    assert len(calls) == 2 and calls[0][0] == "diagnose_log", calls

    # 2) 千问辅助解析（复用识图 Key）
    assist = resolve_revise_tool_assist(
        vision_assist={
            "provider": "qwen",
            "api_key": "sk-test",
            "model": "qwen-vl-max",
        },
        defaults={
            "revise_tool_assist": {
                "enabled": True,
                "prefer_provider": "qwen",
                "prefer_model": "qwen3.5-flash",
                "reuse_vision_key": True,
            }
        },
    )
    assert assist and assist["model"] == "qwen3.5-flash", assist

    # 3) 模拟 DeepSeek：先 TOOL，再 FUNCS
    responses = [
        raw_tool,
        (
            "<<<SUMMARY>>>\n已查日志与 room_claim\n"
            "<<<FUNCS>>>\n### room_claim\n"
            "async def room_claim(browser):\n    return False\n"
            "<<<END>>>"
        ),
    ]
    idx = {"i": 0}

    async def fake_call_llm(**kwargs):
        i = idx["i"]
        idx["i"] += 1
        text = responses[min(i, len(responses) - 1)]
        return text, 10, 20

    ctx = ReviseToolContext(
        code=SAMPLE_CODE,
        feedback="房间假完成",
        trial_log="匹配无结果 room_ok\n仍见收取奖励",
    )
    with patch(
        "backend.script_generator.agent.call_llm",
        new=AsyncMock(side_effect=fake_call_llm),
    ):
        out, tin, tout = await _text_revise_tools_loop(
            provider="deepseek",
            api_key="x",
            model="deepseek-v4-flash",
            api_endpoint=None,
            messages=[{
                "role": "user",
                "content": [{"type": "text", "text": "revise room"}],
            }],
            system_prompt="test",
            max_tool_rounds=3,
            tool_ctx=ctx,
        )

    assert "<<<FUNCS>>>" in out, out[:200]
    assert "return False" in out
    assert "diagnose_log" in ctx.calls and "get_unit" in ctx.calls, ctx.calls
    assert idx["i"] == 2, f"expected 2 LLM rounds, got {idx['i']}"
    assert tin == 20 and tout == 40

    print("OK: text-tool loop")
    print("  calls:", ctx.calls)
    print("  llm_rounds:", idx["i"])
    print("  assist_model:", assist["model"])


def main() -> int:
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
