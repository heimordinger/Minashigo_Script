"""Behaviour-level prohibitions for the visual agent.

This module is the *only* place where the runtime keeps a hand-written word
list, and the list is deliberately domain-neutral: it names classes of
behaviour that are unsafe in any game or web application (spending money,
handling credentials, destroying account state, granting access to third
parties) instead of naming buttons of one particular product.

Everything about how to reach the goal is left to the decision model and to
runtime evidence.  There is no route advice here, by design.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

PROHIBITIONS: dict[str, dict[str, Any]] = {
    "payment": {
        "label": "支付、充值或付费购买",
        "tokens": (
            "支付", "付款", "付费", "充值", "购买", "下单", "结算付款", "扣费",
            "订阅", "续费", "月卡", "礼包兑换码", "支付宝", "微信支付", "信用卡",
            "银行卡", "花呗",
            "pay", "payment", "purchase", "checkout", "subscribe", "billing",
            "credit card", "top up", "topup",
        ),
    },
    "credential": {
        "label": "凭证、验证码或高隐私信息",
        "tokens": (
            "密码", "验证码", "短信验证", "手机验证", "身份证", "实名认证",
            "人脸识别", "银行卡号", "安全码", "密钥", "授权码",
            "password", "passcode", "otp", "verification code", "2fa", "mfa",
            "ssn", "social security", "id number", "api key", "access token",
        ),
    },
    "destructive": {
        "label": "不可逆删除、注销或重置",
        "tokens": (
            "删除", "注销", "解绑", "清空", "重置账号", "封号", "退会", "关闭账号",
            "清除数据", "格式化", "卸载", "永久删除",
            "delete", "remove account", "deactivate", "unbind", "erase", "wipe",
            "reset account", "factory reset",
        ),
    },
    "external_grant": {
        "label": "对外授权、绑定或第三方登录",
        "tokens": (
            "绑定账号", "绑定手机", "绑定邮箱", "授权登录", "第三方登录", "关联账号",
            "oauth", "authorize", "grant access", "sign in with",
        ),
    },
}

# The action name decides whether a prohibition can apply at all: the model
# asking a question is never itself a prohibited action.
_ACTION_NAMES = {"click", "key", "type", "scroll"}

# Fields describing *what the action operates on*.  A hit here is decisive.
_PRIMARY_FIELDS = ("target", "text", "key")
# Fields that merely paraphrase the action.  They are ignored for a click the
# model declared as a dismissal, so closing a shop popup is not refused only
# because the popup mentions a price.
_SECONDARY_FIELDS = ("message", "expect")


def _match(text: str, token: str) -> bool:
    if not text:
        return False
    if token.isascii():
        return re.search(
            rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text
        ) is not None
    return token in text


def detect(text: object) -> list[dict[str, str]]:
    """Return every prohibition category matched in one piece of text."""
    value = str(text or "").lower()
    if not value:
        return []
    hits: list[dict[str, str]] = []
    for category, spec in PROHIBITIONS.items():
        for token in spec["tokens"]:
            if _match(value, str(token).lower()):
                hits.append(
                    {"category": category, "label": str(spec["label"]), "hit": str(token)}
                )
                break
    return hits


def detect_prohibited_categories(text: object) -> list[str]:
    """Category ids mentioned in the request itself, in declaration order."""
    return [hit["category"] for hit in detect(text)]


def describe_prohibitions(categories: Iterable[str]) -> str:
    """Human-readable label list for logs; empty input maps to an empty string."""
    wanted = [str(item) for item in categories or []]
    labels = [str(PROHIBITIONS[key]["label"]) for key in wanted if key in PROHIBITIONS]
    return "、".join(labels)


def check_action(action: dict[str, Any]) -> dict[str, str] | None:
    """Decide whether one concrete action must be refused before execution.

    Returns ``{category, label, hit, field}`` or ``None``.  The check is about
    the *object* of the action rather than incidental words on screen: reading a
    price list is allowed, buying is not.
    """
    if str(action.get("action") or "").lower() not in _ACTION_NAMES:
        return None
    for field in _PRIMARY_FIELDS:
        hits = detect(action.get(field))
        if hits:
            return {**hits[0], "field": field}
    if str(action.get("intent_type") or "").strip().lower() == "dismiss_overlay":
        return None
    for field in _SECONDARY_FIELDS:
        hits = detect(action.get(field))
        if hits:
            return {**hits[0], "field": field}
    return None

# ---------------------------------------------------------------------------
# Retreat detection
# ---------------------------------------------------------------------------
# Going back throws away whatever progress the current page represents, so the
# runtime refuses it while the previous action's frame is still transitioning.
# These are language-level words ("go back one level"), not product labels.
RETREAT_TOKENS = (
    "返回", "退出", "上一页", "上一步", "回到上一", "回到前",
    "back", "go back", "previous page",
)


def looks_like_retreat(action: dict[str, Any]) -> bool:
    """True when the action's own wording declares going back one level."""
    # Only the action itself: an `expect` may innocently predict "returns to the grid".
    for field in ("target", "message"):
        text = str(action.get(field) or "").lower()
        if not text:
            continue
        for token in RETREAT_TOKENS:
            if _match(text, token):
                return True
    return False