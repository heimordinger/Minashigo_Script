"""基线脚本绑定与最小修改约束（设计 §6/§15）。

用户说"只替换资源"或"局部修复"时，模型很容易顺手重写控制流。本模块做两件事：

1. **绑定基线**：把"改动前的脚本"固定下来（内容 hash + 策略），后续候选都要跟它比；
2. **AST/语义 diff + 变更政策**：算出改了什么，并按政策判定是否越界。

只做**静态可判**的部分；语义等价性判断不了的地方不假装能判（留给试运行与人工）。
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable

#: 变更政策（用户需求边界，设计 §15.7）。
POLICY_ASSET_ONLY = "asset_replacement_only"
POLICY_LOCAL_REPAIR = "local_repair"
POLICY_FREE = "free"

POLICIES: tuple[str, ...] = (POLICY_ASSET_ONLY, POLICY_LOCAL_REPAIR, POLICY_FREE)

#: 控制流 AST 节点（这些改动会改变执行路径，不只是"换资源/补一行"）。
CONTROL_FLOW_NODES: tuple[type, ...] = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.With,
    ast.AsyncWith,
    ast.Match if hasattr(ast, "Match") else ast.If,
    ast.Break,
    ast.Continue,
    ast.Return,
    ast.Raise,
)


def script_sha256(code: str) -> str:
    return "sha256:" + hashlib.sha256((code or "").strip().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BaselineBinding:
    """基线脚本绑定：改动前的那一份 + 本次允许的政策。"""

    code: str
    sha256: str
    policy: str = POLICY_FREE
    source: str = ""

    @classmethod
    def of(cls, code: str, *, policy: str = POLICY_FREE, source: str = "") -> "BaselineBinding":
        text = code or ""
        return cls(
            code=text,
            sha256=script_sha256(text),
            policy=policy if policy in POLICIES else POLICY_FREE,
            source=str(source or ""),
        )

    def matches(self, code: str) -> bool:
        return script_sha256(code) == self.sha256


@dataclass
class ScriptDiff:
    """两份脚本的结构化差异（静态可判的部分）。"""

    function_names_before: tuple[str, ...] = ()
    function_names_after: tuple[str, ...] = ()
    functions_added: tuple[str, ...] = ()
    functions_removed: tuple[str, ...] = ()
    changed_functions: tuple[str, ...] = ()
    control_flow_changed: tuple[str, ...] = ()
    string_literals_added: tuple[str, ...] = ()
    string_literals_removed: tuple[str, ...] = ()
    imports_added: tuple[str, ...] = ()
    imports_removed: tuple[str, ...] = ()
    calls_added: tuple[str, ...] = ()
    calls_removed: tuple[str, ...] = ()
    parse_error: str = ""

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.functions_added,
                self.functions_removed,
                self.changed_functions,
                self.control_flow_changed,
                self.string_literals_added,
                self.string_literals_removed,
                self.imports_added,
                self.imports_removed,
                self.calls_added,
                self.calls_removed,
            )
        )

    @property
    def touches_control_flow(self) -> bool:
        return bool(self.control_flow_changed or self.functions_added or self.functions_removed)

    def summary(self) -> dict[str, Any]:
        return {
            "functions_added": list(self.functions_added),
            "functions_removed": list(self.functions_removed),
            "changed_functions": list(self.changed_functions),
            "control_flow_changed": list(self.control_flow_changed),
            "string_literals_added": list(self.string_literals_added)[:20],
            "string_literals_removed": list(self.string_literals_removed)[:20],
            "imports_added": list(self.imports_added),
            "imports_removed": list(self.imports_removed),
            "calls_added": list(self.calls_added)[:20],
            "calls_removed": list(self.calls_removed)[:20],
            "touches_control_flow": self.touches_control_flow,
            "parse_error": self.parse_error,
        }


def _parse(code: str) -> ast.AST | None:
    try:
        return ast.parse(code or "")
    except SyntaxError:
        return None


def _function_bodies(tree: ast.AST) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = ast.dump(node)
    return out


def _control_flow_signature(tree: ast.AST) -> dict[str, list[str]]:
    """每个函数内的控制流形状（只看节点类型序列，不看字面量）。"""
    out: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        kinds = [
            type(child).__name__
            for child in ast.walk(node)
            if isinstance(child, CONTROL_FLOW_NODES)
        ]
        out[node.name] = kinds
    return out


def _strings(tree: ast.AST) -> set[str]:
    return {
        str(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _imports(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            out.update("{}.{}".format(base, alias.name) for alias in node.names)
    return out


def _calls(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            out.add(func.attr)
        elif isinstance(func, ast.Name):
            out.add(func.id)
    return out


def diff_scripts(baseline: str, candidate: str) -> ScriptDiff:
    """算基线 → 候选的 AST/语义差异。"""
    before = _parse(baseline)
    after = _parse(candidate)
    if before is None or after is None:
        return ScriptDiff(parse_error="baseline or candidate is not valid Python")

    fb, fa = _function_bodies(before), _function_bodies(after)
    cf_before, cf_after = _control_flow_signature(before), _control_flow_signature(after)

    control_changes: list[str] = []
    changed: list[str] = []
    for name, body in fa.items():
        if name not in fb:
            continue
        if body != fb[name]:
            changed.append(name)
        if cf_after.get(name) != cf_before.get(name):
            control_changes.append(name)
    for name in fb:
        if name not in fa:
            control_changes.append(name)  # 整个函数被删掉，当然是控制流变化

    strings_before, strings_after = _strings(before), _strings(after)
    imports_before, imports_after = _imports(before), _imports(after)
    calls_before, calls_after = _calls(before), _calls(after)

    return ScriptDiff(
        function_names_before=tuple(sorted(fb)),
        function_names_after=tuple(sorted(fa)),
        functions_added=tuple(sorted(set(fa) - set(fb))),
        functions_removed=tuple(sorted(set(fb) - set(fa))),
        changed_functions=tuple(sorted(set(changed))),
        control_flow_changed=tuple(sorted(set(control_changes))),
        string_literals_added=tuple(sorted(strings_after - strings_before)),
        string_literals_removed=tuple(sorted(strings_before - strings_after)),
        imports_added=tuple(sorted(imports_after - imports_before)),
        imports_removed=tuple(sorted(imports_before - imports_after)),
        calls_added=tuple(sorted(calls_after - calls_before)),
        calls_removed=tuple(sorted(calls_before - calls_after)),
    )


@dataclass(frozen=True)
class PolicyVerdict:
    """政策判定结果。``ok=False`` 时**不得提交**（或必须补证据说明）。"""

    ok: bool
    policy: str
    violations: tuple[str, ...] = ()
    requires_justification: bool = False
    repair_hint: str = ""
    diff: ScriptDiff | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "policy": self.policy,
            "violations": list(self.violations),
            "requires_justification": self.requires_justification,
            "repair_hint": self.repair_hint,
            "diff": self.diff.summary() if self.diff is not None else None,
        }


def enforce_change_policy(
    baseline: BaselineBinding | None,
    candidate: str,
    *,
    justification: str = "",
) -> PolicyVerdict:
    """按基线绑定的政策判定候选是否越界（设计 §15.1/§15.6）。

    - ``asset_replacement_only``：只允许改字面量/调用参数，**控制流一变就拒**；
    - ``local_repair``：允许改函数体，但不允许新增/删除函数，也不允许改控制流形状
      （除非给了 ``justification`` 证据说明）；
    - ``free``：不限制结构，只汇报 diff。
    """
    if baseline is None:
        return PolicyVerdict(
            ok=True,
            policy=POLICY_FREE,
            repair_hint="没有基线绑定：本次不做最小修改约束（如实记录）",
        )
    diff = diff_scripts(baseline.code, candidate)
    if diff.parse_error:
        return PolicyVerdict(
            ok=False,
            policy=baseline.policy,
            violations=(diff.parse_error,),
            repair_hint="先让候选脚本能被 ast.parse 解析",
            diff=diff,
        )
    if baseline.policy == POLICY_FREE:
        return PolicyVerdict(ok=True, policy=baseline.policy, diff=diff)

    violations: list[str] = []
    if baseline.policy == POLICY_ASSET_ONLY:
        if diff.control_flow_changed:
            violations.append(
                "控制流发生了变化：{}".format(", ".join(diff.control_flow_changed))
            )
        if diff.functions_added:
            violations.append("新增了函数：{}".format(", ".join(diff.functions_added)))
        if diff.functions_removed:
            violations.append("删除了函数：{}".format(", ".join(diff.functions_removed)))
        if diff.calls_added:
            violations.append("新增了调用：{}".format(", ".join(diff.calls_added)))
        if diff.calls_removed:
            violations.append("删除了调用：{}".format(", ".join(diff.calls_removed)))
    elif baseline.policy == POLICY_LOCAL_REPAIR:
        if diff.functions_added:
            violations.append("新增了函数：{}".format(", ".join(diff.functions_added)))
        if diff.functions_removed:
            violations.append("删除了函数：{}".format(", ".join(diff.functions_removed)))
        if diff.control_flow_changed and not str(justification or "").strip():
            violations.append(
                "控制流发生了变化且未给说明：{}".format(", ".join(diff.control_flow_changed))
            )

    if not violations:
        return PolicyVerdict(ok=True, policy=baseline.policy, diff=diff)
    return PolicyVerdict(
        ok=False,
        policy=baseline.policy,
        violations=tuple(violations),
        requires_justification=baseline.policy == POLICY_LOCAL_REPAIR,
        repair_hint=(
            "政策={}：只做被要求的最小改动。若确实必须改结构，请提供证据说明"
            "（为什么非改不可），或让用户确认放宽政策".format(baseline.policy)
        ),
        diff=diff,
    )


def changed_string_literals(baseline: str, candidate: str) -> tuple[list[str], list[str]]:
    """便捷视图：只关心"换了哪些字面量"（资源替换场景最常用）。"""
    diff = diff_scripts(baseline, candidate)
    return list(diff.string_literals_added), list(diff.string_literals_removed)


def tuning_deltas(baseline: str, candidate: str) -> list[dict[str, Any]]:
    """基线 → 候选里**参数取值**的变化（设计 §6：每次调整都要能说清改了什么）。

    只按**关键字名**收集（``threshold`` / ``timeout`` / ``retries`` …，取自
    ``tuning.PARAM_RANGES``），比较每个参数的取值集合；不追踪具体调用点，
    因此同名参数在多处出现时会合并成一个变化项 —— 这是刻意的保守近似，
    够用来回答"这次交付是否动过调参"。
    """
    from . import tuning as _tuning

    known = set(_tuning.PARAM_RANGES)

    def collect(code: str) -> dict[str, set[float]]:
        tree = _parse(code)
        out: dict[str, set[float]] = {}
        if tree is None:
            return out

        def numeric(node: ast.AST) -> float | None:
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                return float(node.value)
            if isinstance(node, ast.UnaryOp) and isinstance(node.operand, ast.Constant):
                if isinstance(node.operand.value, (int, float)):
                    return -float(node.operand.value)
            return None

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if not kw.arg or kw.arg not in known:
                        continue
                    value = numeric(kw.value)
                    if value is not None:
                        out.setdefault(kw.arg, set()).add(value)
            elif isinstance(node, ast.Dict):
                # 本项目脚本常把参数集中写在 CFG = {'threshold': 0.8} 里；
                # 只看调用关键字会漏掉最常见的写法。
                for key, value in zip(node.keys, node.values):
                    if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                        continue
                    if key.value not in known:
                        continue
                    number = numeric(value)
                    if number is not None:
                        out.setdefault(key.value, set()).add(number)
        return out

    before, after = collect(baseline), collect(candidate)
    out: list[dict[str, Any]] = []
    for name in sorted(set(before) | set(after)):
        b, a = before.get(name, set()), after.get(name, set())
        if b == a:
            continue
        out.append(
            {
                "parameter": name,
                "before": sorted(b) or None,
                "after": sorted(a) or None,
                "direction": (
                    "lowered" if a and b and min(a) < min(b) else ("raised" if a and b else "changed")
                ),
            }
        )
    return out


def normalize_policy(value: Any) -> str:
    key = str(value or "").strip().lower()
    if key in POLICIES:
        return key
    aliases = {
        "asset": POLICY_ASSET_ONLY,
        "asset_only": POLICY_ASSET_ONLY,
        "只替换资源": POLICY_ASSET_ONLY,
        "换素材": POLICY_ASSET_ONLY,
        "repair": POLICY_LOCAL_REPAIR,
        "局部修复": POLICY_LOCAL_REPAIR,
        "修复": POLICY_LOCAL_REPAIR,
    }
    return aliases.get(key, POLICY_FREE)


def policy_names() -> Iterable[str]:
    return POLICIES
