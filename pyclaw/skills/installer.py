"""
Skill Installer

Responsible for downloading and installing skills from the marketplace to the local workspace.
"""

import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from pyclaw.skills.marketplace import SkillMarketplaceClient, SkillMarketplaceConfig
from pyclaw.skills.parser import parse_skill_markdown
from pyclaw.skills.types import SkillDefinition


class SkillInstallResult(BaseModel):
    """Skill installation result"""

    ok: bool = Field(..., description="Success status")
    slug: str | None = Field(default=None, description="Skill identifier")
    version: str | None = Field(default=None, description="Installed version")
    target_dir: Path | None = Field(default=None, description="Installation target directory")
    message: str = Field(default="", description="Result message")
    error: str | None = Field(default=None, description="Error information")


class SkillOrigin(BaseModel):
    """Skill origin record (for tracking skills installed from marketplace)"""

    version: int = Field(default=1, description="Record format version")
    registry: str = Field(..., description="Registry URL")
    slug: str = Field(..., description="Skill identifier")
    installed_version: str = Field(..., description="Installed version")
    installed_at: int = Field(..., description="Installation timestamp")


class SkillLockEntry(BaseModel):
    """Skill lock file entry"""

    version: str = Field(..., description="Version number")
    installed_at: int = Field(..., description="Installation timestamp")


class SkillsLockfile(BaseModel):
    """Skill lock file"""

    version: int = Field(default=1, description="Lock file format version")
    skills: dict[str, SkillLockEntry] = Field(default_factory=dict, description="Installed skills")


class SkillInstaller:
    """Skill installer"""

    DOT_DIR = ".pyclawhub"
    SKILL_ORIGIN_FILE = "origin.json"
    LOCK_FILE = "lock.json"
    SKILL_MARKERS = ["SKILL.md", "skill.md", "skills.md", "SKILL.MD"]

    def __init__(
        self,
        workspace_dir: Path,
        marketplace_config: SkillMarketplaceConfig | None = None,
    ) -> None:
        self.workspace_dir = Path(workspace_dir)
        self.skills_dir = self.workspace_dir / "skills"
        self.marketplace = SkillMarketplaceClient(marketplace_config)

    def _resolve_skill_dir(self, slug: str) -> Path:
        """Resolve skill installation directory

        Args:
            slug: Skill identifier

        Returns:
            Skill directory path
        """
        # Security check: prevent path traversal
        if ".." in slug or "/" in slug or "\\" in slug:
            raise ValueError(f"Invalid skill slug: {slug}")

        return self.skills_dir / slug

    def _read_lockfile(self) -> SkillsLockfile:
        """Read lock file"""
        lock_path = self.workspace_dir / self.DOT_DIR / self.LOCK_FILE
        if not lock_path.exists():
            return SkillsLockfile()

        try:
            data = json.loads(lock_path.read_text(encoding="utf-8"))
            if data.get("version") == 1 and isinstance(data.get("skills"), dict):
                skills = {
                    k: SkillLockEntry(**v)
                    for k, v in data["skills"].items()
                }
                return SkillsLockfile(version=1, skills=skills)
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

        return SkillsLockfile()

    def _write_lockfile(self, lockfile: SkillsLockfile) -> None:
        """Write lock file"""
        lock_path = self.workspace_dir / self.DOT_DIR / self.LOCK_FILE
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "version": lockfile.version,
            "skills": {
                k: {"version": v.version, "installed_at": v.installed_at}
                for k, v in lockfile.skills.items()
            },
        }
        lock_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def _write_origin(self, skill_dir: Path, origin: SkillOrigin) -> None:
        """Write skill origin record"""
        origin_path = skill_dir / self.DOT_DIR / self.SKILL_ORIGIN_FILE
        origin_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "version": origin.version,
            "registry": origin.registry,
            "slug": origin.slug,
            "installedVersion": origin.installed_version,
            "installedAt": origin.installed_at,
        }
        origin_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def _read_origin(self, skill_dir: Path) -> SkillOrigin | None:
        """Read skill origin record"""
        origin_path = skill_dir / self.DOT_DIR / self.SKILL_ORIGIN_FILE
        if not origin_path.exists():
            return None

        try:
            data = json.loads(origin_path.read_text(encoding="utf-8"))
            if data.get("version") == 1:
                return SkillOrigin(
                    version=1,
                    registry=data["registry"],
                    slug=data["slug"],
                    installed_version=data["installedVersion"],
                    installed_at=data["installedAt"],
                )
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

        return None

    def _find_skill_root(self, extracted_dir: Path) -> Path:
        """Find skill root directory in extracted directory (directory containing SKILL.md)

        Args:
            extracted_dir: Extracted directory

        Returns:
            Skill root directory path

        Raises:
            ValueError: If SKILL.md is not found
        """
        # First check root directory
        for marker in self.SKILL_MARKERS:
            if (extracted_dir / marker).exists():
                return extracted_dir

        # Then check subdirectories (usually archives contain a top-level directory)
        for subdir in extracted_dir.iterdir():
            if subdir.is_dir():
                for marker in self.SKILL_MARKERS:
                    if (subdir / marker).exists():
                        return subdir

        raise ValueError("Downloaded archive is missing SKILL.md")

    def _extract_and_install(
        self,
        archive_path: Path,
        target_dir: Path,
        force: bool = False,
    ) -> Path:
        """Extract and install skill

        Args:
            archive_path: Archive path
            target_dir: Target directory
            force: Whether to force overwrite

        Returns:
            Installed skill directory
        """
        import tempfile

        # Create temporary extraction directory
        with tempfile.TemporaryDirectory(prefix="pyclaw-skill-extract-") as tmp_dir:
            tmp_path = Path(tmp_dir)

            # Extract
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(tmp_path)

            # Find skill root directory
            skill_root = self._find_skill_root(tmp_path)

            # If target directory exists, handle based on force parameter
            if target_dir.exists():
                if not force:
                    raise FileExistsError(f"Skill already exists at {target_dir}")
                shutil.rmtree(target_dir)

            # Copy to target directory
            target_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(skill_root, target_dir)

        return target_dir

    async def install(
        self,
        slug: str,
        version: str | None = None,
        force: bool = False,
    ) -> SkillInstallResult:
        """Install skill

        Args:
            slug: Skill identifier
            version: Specify version, default install latest
            force: Whether to force reinstall

        Returns:
            Installation result
        """
        try:
            # 1. Get skill details
            detail = await self.marketplace.get_skill_detail(slug)
            if not detail:
                return SkillInstallResult(
                    ok=False,
                    slug=slug,
                    error=f'Skill "{slug}" not found on marketplace.',
                )

            # 2. Determine version
            resolved_version = version or detail.latest_version
            if not resolved_version:
                return SkillInstallResult(
                    ok=False,
                    slug=slug,
                    error=f'Skill "{slug}" has no installable version.',
                )

            # 3. Check if already installed
            target_dir = self._resolve_skill_dir(slug)
            if target_dir.exists() and not force:
                return SkillInstallResult(
                    ok=False,
                    slug=slug,
                    error=f"Skill already exists at {target_dir}. Use force=True to reinstall.",
                )

            # 4. Download skill
            download_result = await self.marketplace.download_skill(
                slug=slug,
                version=resolved_version,
            )

            try:
                # 5. Extract and install
                installed_dir = self._extract_and_install(
                    download_result.archive_path,
                    target_dir,
                    force=force,
                )

                # 6. Verify installation (parse SKILL.md)
                skill = self._validate_skill_installation(installed_dir)

                # 7. Record origin
                installed_at = int(datetime.now().timestamp() * 1000)
                origin = SkillOrigin(
                    version=1,
                    registry=self.marketplace.config.base_url,
                    slug=slug,
                    installed_version=resolved_version,
                    installed_at=installed_at,
                )
                self._write_origin(installed_dir, origin)

                # 8. Update lock file
                lockfile = self._read_lockfile()
                lockfile.skills[slug] = SkillLockEntry(
                    version=resolved_version,
                    installed_at=installed_at,
                )
                self._write_lockfile(lockfile)

                return SkillInstallResult(
                    ok=True,
                    slug=slug,
                    version=resolved_version,
                    target_dir=installed_dir,
                    message=f"Installed {slug}@{resolved_version}",
                )

            finally:
                # Clean up downloaded archive
                if download_result.archive_path.exists():
                    download_result.archive_path.unlink()
                    # Try to clean up empty directory
                    try:
                        download_result.archive_path.parent.rmdir()
                    except OSError:
                        pass

        except Exception as e:
            return SkillInstallResult(
                ok=False,
                slug=slug,
                error=str(e),
            )

    def _validate_skill_installation(self, skill_dir: Path) -> SkillDefinition:
        """Validate skill installation

        Args:
            skill_dir: Skill directory

        Returns:
            Skill definition

        Raises:
            ValueError: If validation fails
        """
        # Find SKILL.md
        skill_file = None
        for marker in self.SKILL_MARKERS:
            candidate = skill_dir / marker
            if candidate.exists():
                skill_file = candidate
                break

        if not skill_file:
            raise ValueError("Installed skill is missing SKILL.md")

        # Parse skill file
        content = skill_file.read_text(encoding="utf-8")
        skill = parse_skill_markdown(content, directory_name=skill_dir.name)
        skill.file_path = str(skill_file)

        return skill

    async def update(
        self,
        slug: str | None = None,
        all_skills: bool = False,
    ) -> list[SkillInstallResult]:
        """Update skills

        Args:
            slug: Specify skill to update, if None then determined by all_skills
            all_skills: Whether to update all installed skills

        Returns:
            List of update results
        """
        if not slug and not all_skills:
            return [
                SkillInstallResult(
                    ok=False,
                    error='Update requires either "slug" or "all=True"',
                )
            ]

        lockfile = self._read_lockfile()

        # Determine skills to update
        if slug:
            if slug not in lockfile.skills:
                # Check if installed but not in lock file
                target_dir = self._resolve_skill_dir(slug)
                if not target_dir.exists():
                    return [
                        SkillInstallResult(
                            ok=False,
                            slug=slug,
                            error=f'Skill "{slug}" is not installed.',
                        )
                    ]
            slugs_to_update = [slug]
        else:
            slugs_to_update = list(lockfile.skills.keys())

        results: list[SkillInstallResult] = []

        for skill_slug in slugs_to_update:
            # Get current version
            current_entry = lockfile.skills.get(skill_slug)
            current_version = current_entry.version if current_entry else None

            # Get latest version
            detail = await self.marketplace.get_skill_detail(skill_slug)
            if not detail:
                results.append(
                    SkillInstallResult(
                        ok=False,
                        slug=skill_slug,
                        error=f'Skill "{skill_slug}" not found on marketplace.',
                    )
                )
                continue

            latest_version = detail.latest_version
            if not latest_version:
                results.append(
                    SkillInstallResult(
                        ok=False,
                        slug=skill_slug,
                        error=f'Skill "{skill_slug}" has no available version.',
                    )
                )
                continue

            # Check if update is needed
            if current_version == latest_version:
                results.append(
                    SkillInstallResult(
                        ok=True,
                        slug=skill_slug,
                        version=current_version,
                        message=f"Already up to date ({current_version})",
                    )
                )
                continue

            # Execute update (force install new version)
            result = await self.install(skill_slug, version=latest_version, force=True)
            results.append(result)

        return results

    async def uninstall(self, slug: str) -> SkillInstallResult:
        """Uninstall skill

        Args:
            slug: Skill identifier

        Returns:
            Uninstallation result
        """
        target_dir = self._resolve_skill_dir(slug)

        if not target_dir.exists():
            return SkillInstallResult(
                ok=False,
                slug=slug,
                error=f'Skill "{slug}" is not installed.',
            )

        try:
            # Delete skill directory
            shutil.rmtree(target_dir)

            # Update lock file
            lockfile = self._read_lockfile()
            if slug in lockfile.skills:
                del lockfile.skills[slug]
                self._write_lockfile(lockfile)

            return SkillInstallResult(
                ok=True,
                slug=slug,
                message=f"Uninstalled {slug}",
            )

        except Exception as e:
            return SkillInstallResult(
                ok=False,
                slug=slug,
                error=str(e),
            )

    def list_installed(self) -> list[dict[str, Any]]:
        """List installed skills

        Check both lock file and skills directory for skills (supports Skillhub installed skills).

        Returns:
            List of installed skills
        """
        lockfile = self._read_lockfile()
        results = []
        seen_slugs: set[str] = set()

        # 1. Read from lock file (ClawHub installed skills)
        for slug, entry in lockfile.skills.items():
            seen_slugs.add(slug)
            skill_dir = self._resolve_skill_dir(slug)
            origin = self._read_origin(skill_dir) if skill_dir.exists() else None

            info: dict[str, Any] = {
                "slug": slug,
                "version": entry.version,
                "installed_at": entry.installed_at,
            }

            if origin:
                info["registry"] = origin.registry

            # Try reading skill definition for more info
            for marker in self.SKILL_MARKERS:
                skill_file = skill_dir / marker
                if skill_file.exists():
                    try:
                        content = skill_file.read_text(encoding="utf-8")
                        skill = parse_skill_markdown(content, directory_name=skill_dir.name)
                        info["name"] = skill.name
                        info["description"] = skill.description
                    except Exception:
                        pass
                    break

            results.append(info)

        # 2. Scan skills directory for skills not in lock file (Skillhub installed)
        if self.skills_dir.exists():
            for subdir in self.skills_dir.iterdir():
                if not subdir.is_dir():
                    continue
                slug = subdir.name
                if slug in seen_slugs or slug.startswith("."):
                    continue

                # Check for SKILL.md or _meta.json
                has_skill = False
                for marker in self.SKILL_MARKERS:
                    if (subdir / marker).exists():
                        has_skill = True
                        break
                if not has_skill and not (subdir / "_meta.json").exists():
                    continue

                info = {
                    "slug": slug,
                    "version": "unknown",
                    "installed_at": int(subdir.stat().st_mtime),
                    "registry": "skillhub",
                }

                # Try reading version from _meta.json
                meta_file = subdir / "_meta.json"
                if meta_file.exists():
                    try:
                        meta = json.loads(meta_file.read_text(encoding="utf-8"))
                        info["version"] = meta.get("version", "unknown")
                    except Exception:
                        pass

                # Try reading SKILL.md for more info
                for marker in self.SKILL_MARKERS:
                    skill_file = subdir / marker
                    if skill_file.exists():
                        try:
                            content = skill_file.read_text(encoding="utf-8")
                            skill = parse_skill_markdown(content, directory_name=subdir.name)
                            info["name"] = skill.name
                            info["description"] = skill.description
                        except Exception:
                            pass
                        break

                results.append(info)

        return results

    async def close(self) -> None:
        """Close installer"""
        await self.marketplace.close()

    async def __aenter__(self) -> "SkillInstaller":
        """Async context manager entry"""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Async context manager exit"""
        await self.close()
