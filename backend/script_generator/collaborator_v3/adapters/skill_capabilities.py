"""把 ``skills/*/SKILL.md`` 归类成可管理的资源。

在此之前，这些 SKILL.md 是**孤儿文档**：全仓只有 ``optimize.py`` 硬编码读
``optimize-script-perf`` 一份，协作侧没有任何加载路径，模型根本看不见它们。
本模块把它们扫成 ``CapabilityRecord(kind="skill")``，于是：

* 和工具进**同一张能力表**，用同一套 ``capability.search`` / ``capability.get`` 查询；
* ``capability.search`` 只回摘要（名字 + 一句话用途），
  ``capability.get`` 才回完整正文——按需加载，不为 6 份文档付几十 KB。

版本号取文档内容哈希：改了文档就自动升版本，缓存不会拿到旧正文。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.path import PROJECT_ROOT

from ..domain.contracts import CapabilityRecord
from ..stores.sqlite_store import SqliteDomainStore

SKILLS_ROOT = PROJECT_ROOT / "backend" / "script_generator" / "skills"

#: 能力表里 skill 的 id 前缀（工具用 ``cap_v2_``）
SKILL_ID_PREFIX = "skill_"

_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


@dataclass(frozen=True)
class SkillDoc:
    """一份 SKILL.md 的解析结果。"""

    name: str
    description: str
    body: str
    path: str

    @property
    def content_hash(self) -> str:
        raw = f"{self.name}\n{self.description}\n{self.body}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:12]


def _unfold(value: str) -> str:
    """把 YAML 里 ``>-`` / ``|`` 的块折叠成单行文本。"""
    text = str(value or "").strip()
    if text in {">-", ">", "|-", "|"}:
        return ""
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """拆出 front matter 与正文。没有 front matter 时返回空字段表 + 原文。"""
    raw = str(text or "")
    match = _FRONT_MATTER.match(raw)
    if not match:
        return {}, raw.strip()
    block = match.group(1)
    body = raw[match.end():].strip()
    fields: dict[str, str] = {}
    lines = block.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        index += 1
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if value in {">-", ">", "|-", "|", ""}:
            # 块语法：把后续缩进行并进来
            collected: list[str] = []
            while index < len(lines):
                nxt = lines[index]
                if nxt.strip() and not nxt.startswith((" ", "\t")):
                    break
                collected.append(nxt.strip())
                index += 1
            merged = " ".join(x for x in collected if x)
            fields[key] = merged or _unfold(value)
            continue
        fields[key] = _unfold(value)
    return fields, body


def _split_paragraph(text: str) -> str:
    """取正文第一段（去掉标题行）当一句话用途。"""
    for block in re.split(r"\n\s*\n", str(text or "")):
        cleaned = " ".join(
            line.strip()
            for line in block.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ).strip()
        if cleaned:
            return cleaned
    return ""


def load_skill_doc(path: Path) -> SkillDoc | None:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    fields, body = parse_front_matter(text)
    folder = Path(path).parent.name
    name = str(fields.get("name") or folder).strip() or folder
    description = str(fields.get("description") or "").strip() or _split_paragraph(body)
    return SkillDoc(name=name, description=description, body=body, path=str(path))


def load_skill_docs(root: Path | None = None) -> list[SkillDoc]:
    """扫描 ``<root>/*/SKILL.md``。按名字排序，结果稳定。"""
    base = Path(root) if root is not None else SKILLS_ROOT
    if not base.is_dir():
        return []
    docs: list[SkillDoc] = []
    for path in sorted(base.glob("*/SKILL.md")):
        doc = load_skill_doc(path)
        if doc is not None:
            docs.append(doc)
    return sorted(docs, key=lambda item: item.name)


def capability_from_skill(doc: SkillDoc) -> CapabilityRecord:
    """Skill 是"怎么组合能力"的说明，**不持有执行权限**（§6.7）。"""
    return CapabilityRecord(
        id=f"{SKILL_ID_PREFIX}{doc.name}",
        kind="skill",
        name=doc.name,
        version=doc.content_hash,
        # 技能不接受参数：它是给人/模型读的说明书，不是可调用的函数
        input_schema={},
        output_schema={},
        permission_actions=(),  # 有权限会被 CapabilityRecord 拒绝
        cost_class="local",
        availability="available",
        scope="global",
        metadata={
            "description": doc.description,
            "body": doc.body,
            "path": doc.path,
            "source": "skill_md",
            "catalog_source": "skill_md",
        },
    )


def sync_skill_capabilities(
    store: SqliteDomainStore, root: Path | None = None
) -> list[CapabilityRecord]:
    """把本地 skill 文档同步进能力表，和工具放在一起管理。"""
    capabilities: list[CapabilityRecord] = []
    for doc in load_skill_docs(root):
        capability = capability_from_skill(doc)
        store.put_capability(capability)
        capabilities.append(capability)
    store.reconcile_capability_catalog(
        id_prefix=SKILL_ID_PREFIX,
        current={(item.id, item.version) for item in capabilities},
    )
    return capabilities


def skill_body(capability: CapabilityRecord) -> str:
    """取 skill 的完整正文（工具没有正文，返回空串）。"""
    if capability.kind != "skill":
        return ""
    return str(capability.metadata.get("body") or "")


def resource_catalog_lines(root: Path | None = None) -> list[str]:
    """人读的资源清单（V2 的 ``list_skills`` 兜底用）。"""
    return [
        f"· {doc.name}（skill）—— {doc.description[:80]}"
        for doc in load_skill_docs(root)
    ]
