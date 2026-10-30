"""Skills: reusable procedures in the open Agent Skills format (a folder with a SKILL.md file).

Only each skill's name and description go into the assistant's prompt. The full steps are loaded when the
assistant calls use_skill, so adding skills costs almost nothing per question. The same folders work in other
agents that read SKILL.md, such as Hermes Agent.

A skill may list the tools it cannot do without (metadata.requires_tools). The assistant cannot finish a
turn that used the skill until it has called them: a model that skips the web search would otherwise answer
from memory, and one that skips save_draft would claim a draft it never saved (both seen on 2026-10-30).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from duly.config import SKILLS_DIR


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path
    requires_tools: list[str] = field(default_factory=list)


def load_skills(folder: Path = SKILLS_DIR) -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    for path in sorted(folder.glob("*/SKILL.md")):
        raw = path.read_text(encoding="utf-8")
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", raw, flags=re.DOTALL)
        if not m:
            continue
        meta = yaml.safe_load(m.group(1)) or {}
        name = str(meta.get("name") or path.parent.name)
        requires = str((meta.get("metadata") or {}).get("requires_tools", "")).replace(",", " ").split()
        skills[name] = Skill(name, str(meta.get("description", "")), m.group(2).strip(), path, requires)
    return skills
