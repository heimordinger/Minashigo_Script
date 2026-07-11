"""
Script Generator Agent
======================
调用 LLM API，根据用户提供的脚本解释和图片，自动生成自动化脚本。
"""

import base64
import json
import os
from pathlib import Path
from typing import Optional

from core.path import PROJECT_ROOT, IMG_PATH


# ═══════════════════════════════════════════════════════════════
# 配置 — 从 config.json 加载，支持热更新
# ═══════════════════════════════════════════════════════════════

_CONFIG_PATH = Path(__file__).parent / "config.json"
_SYSTEM_PROMPT_CACHE: str | None = None


def _load_config() -> dict:
    """加载配置 JSON"""
    return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))


def _build_system_prompt(source_dir: str = "") -> str:
    """从 config.json 动态构建 system prompt"""
    cfg = _load_config()
    defaults = cfg.get("defaults", {})
    th = defaults.get("threshold", 0.9)
    nav = defaults.get("nav_threshold", 0.8)
    icon_th = defaults.get("icon_threshold", 0.85)

    # 可用脚本列表
    scripts = cfg.get("available_scripts", [])
    script_lines = "\n".join(
        f'- **{s["module"]}**: `{s["name"]}` — {s["desc"]}'
        for s in scripts
    )
    scripts_block = f"以下脚本已存在，生成新脚本时应 import 使用：\n{script_lines}\n\n用法示例（注意用 scripts. 前缀）：\n"
    scripts_block += "```python\n"
    for s in scripts[:3]:
        name = s["name"].split("(")[0]
        scripts_block += f"from {s['module']} import {name}\n"
    scripts_block += "```"

    # 规则列表
    rules = cfg.get("rules", [])
    rules_block = "\n".join(f"{i+1}. {r}" for i, r in enumerate(rules))

    # IMG_DIR 路径
    if source_dir:
        try:
            from core.path import IMG_PATH
            rel = Path(source_dir).relative_to(IMG_PATH)
            parts = [f"'{p}'" for p in rel.parts]
            img_dir_line = "IMG_DIR = IMG_PATH / " + " / ".join(parts)
            src_line = f"图片文件夹路径: {source_dir}"
        except (ValueError, Exception):
            img_dir_line = f'IMG_DIR = Path(r"{source_dir}")'
            src_line = f"图片文件夹路径: {source_dir}"
    else:
        img_dir_line = "IMG_DIR = IMG_PATH / 'game' / 'script'"
        src_line = "（未指定）"

    # 填充模板
    template = cfg.get("system_prompt_template", "")
    prompt = template.replace("$THRESHOLD", str(th))
    prompt = prompt.replace("$NAV_THRESHOLD", str(nav))
    prompt = prompt.replace("$ICON_THRESHOLD", str(icon_th))
    prompt = prompt.replace("$AVAILABLE_SCRIPTS", scripts_block)
    prompt = prompt.replace("$SOURCE_DIR", src_line)
    prompt = prompt.replace("$IMG_DIR_LINE", img_dir_line)
    prompt = prompt.replace("$RULES", rules_block)

    return prompt


def _image_b64(image_path: Path, compress: bool = False, max_size: int = 800) -> tuple[str, str]:
    """读取图片为 base64，返回 (base64_data, media_type)。"""
    import cv2
    img = cv2.imread(str(image_path))
    if img is None:
        raise ValueError(f"无法读取图片: {image_path}")
    if compress:
        h, w = img.shape[:2]
        if max(h, w) > max_size:
            scale = max_size / max(h, w)
            new_w, new_h = int(w * scale), int(h * scale)
            img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
    _, buf = cv2.imencode(".png", img)
    data = base64.b64encode(bytes(buf)).decode("utf-8")
    ext = image_path.suffix.lower().lstrip(".")
    media_type = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "image/png")
    return data, media_type


def _build_messages(
    explanation_text: str,
    image_paths: list[Path],
    provider: str = "claude",
    send_images: bool = True,
    compress_images: bool = False,
) -> list[dict]:
    """构建消息列表，根据 provider 选择图片格式。"""
    content = [{"type": "text", "text": explanation_text}]
    if send_images and image_paths:
        content.append({"type": "text", "text": f"\n\n参考图片共 {len(image_paths)} 张，文件名对应脚本中的图片名："})
        for img_path in image_paths:
            try:
                b64data, media_type = _image_b64(img_path, compress=compress_images)
                if provider == "claude":
                    encoded = {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64data}}
                elif provider == "google":
                    encoded = {"inline_data": {"mime_type": media_type, "data": b64data}}
                else:  # openai / deepseek / groq
                    encoded = {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64data}"}}
                content.append(encoded)
                content.append({"type": "text", "text": f"  → {img_path.name}"})
            except Exception as e:
                content.append({"type": "text", "text": f"[图片加载失败: {img_path.name} - {e}]"})
    return [{"role": "user", "content": content}]


async def generate_script(
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    explanation_text: str,
    image_paths: list[Path],
    source_dir: str = "",
    send_images: bool = True,
    compress_images: bool = False,
    on_partial=None,
) -> tuple[str, int, int]:
    """
    调用 LLM 生成脚本。返回 (代码, 输入tokens, 输出tokens)。

    Args:
        provider: "claude" | "openai"
        api_key: API key
        model: 模型名
        api_endpoint: 自定义 API 端点（可选）
        explanation_text: 脚本解释内容
        image_paths: 关联的图片路径列表
        source_dir: 图片源文件夹路径，AI 应将其设为 img_dir
        send_images: 是否发送图片给 AI
        compress_images: 是否压缩图片（省 token）

    Returns:
        生成的 Python 脚本代码
    """
    prompt = _build_system_prompt(source_dir=source_dir)
    messages = _build_messages(explanation_text, image_paths, provider=provider, send_images=send_images, compress_images=compress_images)

    extra = {"on_partial": on_partial}
    if provider == "claude":
        raw, inp_tok, out_tok = await _call_claude(api_key, model, api_endpoint, messages, prompt, **extra)
    elif provider in ("openai", "deepseek", "groq"):
        raw, inp_tok, out_tok = await _call_openai(api_key, model, api_endpoint, messages, prompt, **extra)
    elif provider == "google":
        raw = await _call_gemini(api_key, model, api_endpoint, messages, prompt, **extra)
        inp_tok = out_tok = 0
    else:
        raise ValueError(f"不支持的 provider: {provider}")

    # 去掉 markdown 代码块标记
    raw = raw.strip()
    if raw.startswith("```python"):
        raw = raw[len("```python"):].strip()
    if raw.startswith("```"):
        raw = raw[len("```"):].strip()
    if raw.endswith("```"):
        raw = raw[:-len("```")].strip()

    # 语法校验
    try:
        compile(raw, "<generated>", "exec")
    except SyntaxError as e:
        raise RuntimeError(
            f"生成的脚本存在语法错误（{e.msg}，第 {e.lineno} 行），请点「生成脚本」重试"
        ) from e

    return raw, inp_tok, out_tok


_CFG = _load_config()
_DEFAULTS = _CFG.get("defaults", {})
API_TIMEOUT = _DEFAULTS.get("api_timeout", 300)
_MAX_TOKENS = _DEFAULTS.get("max_tokens", 16384)

async def _call_claude(
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    messages: list[dict],
    system_prompt: str = "",
    on_partial=None,
) -> tuple[str, int, int]:
    """调用 Anthropic Claude API，返回 (代码, 输入tokens, 输出tokens)"""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("需要安装 anthropic 包: pip install anthropic")

    client_kwargs = {"api_key": api_key, "timeout": API_TIMEOUT}
    if api_endpoint:
        client_kwargs["base_url"] = api_endpoint

    client = anthropic.AsyncAnthropic(**client_kwargs)

    if on_partial:
        text = ""
        async with client.messages.stream(
            model=model, max_tokens=_MAX_TOKENS, system=system_prompt, messages=messages,
        ) as stream:
            async for chunk in stream.text_stream:
                text += chunk
                on_partial(chunk)
        final = await stream.get_final_message()
        usage = final.usage
        if not text:
            raise RuntimeError("Claude 返回了空内容，请重试")
        return text, usage.input_tokens, usage.output_tokens
    else:
        response = await client.messages.create(
            model=model, max_tokens=_MAX_TOKENS, system=system_prompt, messages=messages,
        )
        text = response.content[0].text
        if not text:
            raise RuntimeError("Claude 返回了空内容，请重试")
        usage = response.usage
        return text, usage.input_tokens, usage.output_tokens


async def _call_openai(
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    messages: list[dict],
    system_prompt: str = "",
    on_partial=None,
) -> tuple[str, int, int]:
    """调用 OpenAI 兼容 API，支持流式输出"""
    try:
        from openai import AsyncOpenAI
    except ImportError:
        raise RuntimeError("需要安装 openai 包: pip install openai")

    from httpx import Timeout as HttpxTimeout
    client_kwargs = {"api_key": api_key, "timeout": HttpxTimeout(API_TIMEOUT)}
    if api_endpoint:
        client_kwargs["base_url"] = api_endpoint

    client = AsyncOpenAI(**client_kwargs)
    system_msg = [{"role": "system", "content": system_prompt}]

    if on_partial:
        text = ""
        inp_tok = out_tok = 0
        stream = await client.chat.completions.create(
            model=model, max_tokens=_MAX_TOKENS, messages=system_msg + messages,
            stream=True, stream_options={"include_usage": True},
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else ""
            if delta:
                text += delta
                on_partial(delta)
            if chunk.usage:
                inp_tok = chunk.usage.prompt_tokens or inp_tok
                out_tok = chunk.usage.completion_tokens or out_tok
        if not text:
            raise RuntimeError("API 返回了空内容，请重试")
        return text, inp_tok, out_tok
    else:
        response = await client.chat.completions.create(
            model=model, max_tokens=_MAX_TOKENS, messages=system_msg + messages,
        )
        text = response.choices[0].message.content or ""
        if not text:
            raise RuntimeError("API 返回了空内容，请重试")
        usage = response.usage
        inp = usage.prompt_tokens if usage else 0
        out = usage.completion_tokens if usage else 0
        return text, inp, out


async def _call_gemini(
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    messages: list[dict],
    system_prompt: str = "",
    on_partial=None,
) -> str:
    """调用 Google Gemini API"""
    try:
        import google.generativeai as genai
    except ImportError:
        raise RuntimeError("需要安装 google-generativeai 包: pip install google-generativeai")

    genai.configure(api_key=api_key)
    gemini_model = genai.GenerativeModel(model_name=model, system_instruction=system_prompt)

    # 提取用户文本
    user_text = ""
    for msg in messages:
        for part in msg["content"]:
            if part.get("type") == "text":
                user_text += part["text"] + "\n"

    response = await gemini_model.generate_content_async(user_text)
    return response.text
