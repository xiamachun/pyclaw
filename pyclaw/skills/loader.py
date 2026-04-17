"""
Skill loader

Responsible for skill loading and management.
"""

from pathlib import Path
from typing import Any

from pyclaw.skills.parser import parse_skill_markdown
from pyclaw.skills.types import SkillDefinition

class SkillLoader:
    """Skill loader"""

    def __init__(self, skills_dirs: list[Path]) -> None:
        self.skills_dirs = skills_dirs
        self._skills: dict[str, SkillDefinition] = {}

    async def load_all(self) -> list[SkillDefinition]:
        """Load all skills

        Returns:
            List of skill definitions
        """
        all_skills = []

        for skills_dir in self.skills_dirs:
            if not skills_dir.exists():
                continue

            for skill_file in skills_dir.glob("*.md"):
                try:
                    skill = await self.load_skill(skill_file)
                    all_skills.append(skill)
                except Exception:
                    continue

        self._validate_security_skill(all_skills)

        return all_skills

    async def load_skill(self, path: Path) -> SkillDefinition:
        """Load single skill

        Args:
            path: Skill file path

        Returns:
            Skill definition
        """
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        skill = parse_skill_markdown(content)
        skill.file_path = str(path)

        self._skills[skill.name] = skill

        return skill

    def _parse_frontmatter(self, content: str) -> dict[str, Any]:
        """Parse YAML frontmatter of SKILL.md

        Args:
            content: Full content

        Returns:
            Frontmatter dictionary
        """
        from pyclaw.skills.parser import extract_frontmatter

        frontmatter, _ = extract_frontmatter(content)
        return frontmatter

    def _validate_security_skill(self, skills: list[SkillDefinition]) -> None:
        """Ensure security skill is loaded

        Args:
            skills: List of skills
        """
        skill_names = {skill.name for skill in skills}

        if "security" not in skill_names:
            raise RuntimeError("Security skill is required but not loaded")

    def get_skill(self, name: str) -> SkillDefinition | None:
        """Get skill

        Args:
            name: Skill name

        Returns:
            Skill definition or None
        """
        return self._skills.get(name)

    def list_all(self) -> list[SkillDefinition]:
        """List all skills

        Returns:
            List of skill definitions
        """
        return list(self._skills.values())

def resolve_agent_skills(
    all_skills: list[SkillDefinition],
    agent_skills_filter: list[str] | None,
) -> list[SkillDefinition]:
    """Filter skills based on agent's skill whitelist.

    Rules:
    - agent_skills_filter = None  → Return all skills (inherit default)
    - agent_skills_filter = []    → Return only security skill
    - agent_skills_filter = ["a"] → Return matching skills + security

    Args:
        all_skills: All loaded skills
        agent_skills_filter: Agent's skill whitelist

    Returns:
        Filtered skill list
    """
    if agent_skills_filter is None:
        return all_skills

    allowed_names = set(agent_skills_filter) | {"security"}
    return [skill for skill in all_skills if skill.name in allowed_names]