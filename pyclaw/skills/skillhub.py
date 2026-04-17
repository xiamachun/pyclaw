"""
Skillhub Integration

Supports two modes:
1. Direct HTTP access to Skillhub COS index (recommended, more stable)
2. Through skillhub CLI tool

Skillhub is a domestic accelerated skill marketplace, more stable than ClawHub.
"""

import asyncio
import json
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from pyclaw.constants import (
    SKILLHUB_API_TIMEOUT_SECONDS,
    SKILLHUB_INSTALL_TIMEOUT_SECONDS,
    SKILLHUB_DOWNLOAD_TIMEOUT_SECONDS,
)

# Skillhub COS base URL
SKILLHUB_INDEX_URL = "https://skillhub-1388575217.cos.ap-guangzhou.myqcloud.com/skills.json"
SKILLHUB_DOWNLOAD_TEMPLATE = "https://skillhub-1388575217.cos.ap-guangzhou.myqcloud.com/skills/{slug}.zip"


# Simple cache
class _Cache:
    def __init__(self, ttl: int = 600):
        self.ttl = ttl
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        if key in self._data:
            ts, val = self._data[key]
            if time.time() - ts < self.ttl:
                return val
            del self._data[key]
        return None

    def set(self, key: str, val: Any) -> None:
        self._data[key] = (time.time(), val)


_cache = _Cache(ttl=600)  # 10 minute cache


def _fix_flat_install(skills_dir: Path, target_dir: Path, slug: str) -> None:
    """Fix flat extraction issue: if files are extracted directly to skills_dir instead of subdirectory, move to correct location"""
    meta_file = skills_dir / "_meta.json"
    skill_md = skills_dir / "SKILL.md"

    # Check if files are flattened to skills_dir
    if meta_file.exists() and not target_dir.exists():
        target_dir.mkdir(parents=True, exist_ok=True)
        # Move all non-directory files and scripts directory to subdirectory
        for item in list(skills_dir.iterdir()):
            if item.name == slug or item == target_dir:
                continue
            dest = target_dir / item.name
            shutil.move(str(item), str(dest))


class SkillhubSearchResult(BaseModel):
    """Skillhub search result"""

    slug: str = Field(..., description="Skill identifier")
    display_name: str = Field(..., description="Display name")
    summary: str | None = Field(default=None, description="Skill summary")
    version: str | None = Field(default=None, description="Latest version")


class SkillhubInstallResult(BaseModel):
    """Skillhub installation result"""

    ok: bool = Field(..., description="Success status")
    slug: str | None = Field(default=None, description="Skill identifier")
    message: str = Field(default="", description="Result message")
    error: str | None = Field(default=None, description="Error message")


def is_skillhub_available() -> bool:
    """Check if Skillhub is available (CLI installed or COS index reachable)"""
    # Can directly access COS index with httpx
    return True


async def _fetch_index() -> list[dict[str, Any]]:
    """Fetch Skillhub skill index (with cache)

    Returns:
        List of skills
    """
    cached = _cache.get("index")
    if cached is not None:
        return cached

    async with httpx.AsyncClient(timeout=SKILLHUB_API_TIMEOUT_SECONDS, verify=False) as client:
        response = await client.get(SKILLHUB_INDEX_URL)
        response.raise_for_status()

    data = response.json()
    skills = data.get("skills", [])
    _cache.set("index", skills)
    return skills


async def skillhub_search(query: str) -> list[SkillhubSearchResult]:
    """Search Skillhub skills

    Args:
        query: Search keyword

    Returns:
        List of skill search results
    """
    skills = await _fetch_index()

    # Local fuzzy matching
    query_lower = query.strip().lower()
    if query_lower == "*" or not query_lower:
        # Return all skills (sorted by rank)
        matched = skills
    else:
        matched = []
        keywords = query_lower.split()
        for s in skills:
            text = f"{s.get('slug', '')} {s.get('name', '')} {s.get('description', '')}".lower()
            if all(kw in text for kw in keywords):
                matched.append(s)

    return [
        SkillhubSearchResult(
            slug=s["slug"],
            display_name=s.get("name", s["slug"]),
            summary=s.get("description"),
            version=s.get("version"),
        )
        for s in matched
    ]


async def skillhub_install(
    slug: str,
    workspace_dir: Path | None = None,
) -> SkillhubInstallResult:
    """Install Skillhub skill

    Download zip from COS and extract to workspace skills directory.

    Args:
        slug: Skill identifier
        workspace_dir: Workspace directory

    Returns:
        Installation result
    """
    if workspace_dir is None:
        from pyclaw.config.paths import get_paths as _get_paths
        workspace_dir = _get_paths().workspace_dir

    skills_dir = workspace_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    target_dir = skills_dir / slug

    # First try skillhub CLI (it may have local cache)
    if shutil.which("skillhub"):
        try:
            proc = await asyncio.create_subprocess_exec(
                "skillhub", "install", slug,
                "--dir", str(skills_dir),
                "--force",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=SKILLHUB_INSTALL_TIMEOUT_SECONDS)
            if proc.returncode == 0:
                # After CLI installation, check if extracted directly to skills_dir (no subdirectory)
                _fix_flat_install(skills_dir, target_dir, slug)
                return SkillhubInstallResult(
                    ok=True,
                    slug=slug,
                    message=f"Installed {slug} via Skillhub CLI",
                )
        except Exception:
            pass  # CLI failed, fallback to direct download

    # Direct download from COS
    download_url = SKILLHUB_DOWNLOAD_TEMPLATE.format(slug=slug)

    try:
        async with httpx.AsyncClient(timeout=SKILLHUB_DOWNLOAD_TIMEOUT_SECONDS, verify=False) as client:
            response = await client.get(download_url)
            response.raise_for_status()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return SkillhubInstallResult(
                ok=False,
                slug=slug,
                error=f"Skill '{slug}' not found in Skillhub",
            )
        return SkillhubInstallResult(
            ok=False,
            slug=slug,
            error=f"Download failed: HTTP {e.response.status_code}",
        )
    except Exception as e:
        return SkillhubInstallResult(
            ok=False,
            slug=slug,
            error=f"Download failed: {e}",
        )

    # Save and extract zip to subdirectory
    import tempfile
    zip_path = Path(tempfile.mktemp(suffix=".zip"))
    tmp_extract = Path(tempfile.mkdtemp(prefix="skillhub-"))
    try:
        zip_path.write_bytes(response.content)

        # Extract to temporary directory
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp_extract)

        # Check extraction result: flat files or single subdirectory
        items = list(tmp_extract.iterdir())
        if len(items) == 1 and items[0].is_dir():
            # Zip has single top-level directory, use directly
            src_dir = items[0]
        else:
            # Zip files are flattened, entire temp directory is content
            src_dir = tmp_extract

        # Clean old directory and move
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(src_dir, target_dir)
    finally:
        zip_path.unlink(missing_ok=True)
        shutil.rmtree(tmp_extract, ignore_errors=True)

    return SkillhubInstallResult(
        ok=True,
        slug=slug,
        message=f"Installed {slug} via Skillhub COS",
    )


async def skillhub_list() -> list[SkillhubSearchResult]:
    """List all available skills in Skillhub"""
    return await skillhub_search("*")


class SkillhubClient:
    """
    Skillhub client wrapper class

    Prioritize getting skill list from COS index, try CLI first during installation,
    fallback to direct download on failure.
    """

    def __init__(self, workspace_dir: Path | None = None):
        self.workspace_dir = workspace_dir

    @property
    def available(self) -> bool:
        """Always available (direct HTTP access to COS)"""
        return True

    async def search(self, query: str = "*") -> list[SkillhubSearchResult]:
        """Search skills"""
        return await skillhub_search(query)

    async def install(self, slug: str) -> SkillhubInstallResult:
        """Install skill"""
        return await skillhub_install(slug, self.workspace_dir)

    async def list_skills(self) -> list[SkillhubSearchResult]:
        """List available skills"""
        return await skillhub_list()