"""
Skill type definitions

Defines core data models for the skill system.
"""

from typing import Any

from pydantic import BaseModel, Field


class SkillDefinition(BaseModel):
    """Skill definition"""

    name: str = Field(..., description="Skill name")
    description: str = Field(..., description="Skill description")
    content: str = Field(..., description="Skill content (Markdown format)")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Skill metadata")
    file_path: str | None = Field(None, description="Skill file path")
