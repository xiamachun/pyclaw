"""
Skill parser

Parses skill file format.
"""

import re
from typing import Any

import yaml

from pyclaw.skills.types import SkillDefinition


def extract_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter and body content

    Args:
        content: Full content

    Returns:
        (frontmatter_dict, body_content)
    """
    frontmatter_pattern = r"^---\s*\n(.*?)\n---\s*\n(.*)$"
    match = re.match(frontmatter_pattern, content, re.DOTALL)

    if match:
        frontmatter_text = match.group(1)
        body = match.group(2).strip()

        try:
            frontmatter = yaml.safe_load(frontmatter_text) or {}
        except yaml.YAMLError:
            frontmatter = {}

        return frontmatter, body

    return {}, content


def parse_skill_markdown(content: str, directory_name: str | None = None) -> SkillDefinition:
    """Parse SKILL.md format

    Args:
        content: Markdown content
        directory_name: Skill directory name, used as fallback

    Returns:
        Skill definition
    """
    frontmatter, body = extract_frontmatter(content)

    name = frontmatter.get("name") or directory_name or "unknown"
    description = frontmatter.get("description", "")

    metadata = {}
    for key, value in frontmatter.items():
        if key not in ["name", "description"]:
            metadata[key] = value

    return SkillDefinition(
        name=name,
        description=description,
        content=body,
        metadata=metadata,
    )