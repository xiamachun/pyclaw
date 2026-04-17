"""
Skill marketplace client

Interacts with remote skill marketplace (ClawHub) to provide skill search, detail retrieval, and download functionality.
"""

import os
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field


# Simple in-memory cache
class SimpleCache:
    """Simple in-memory cache to reduce API request frequency"""

    def __init__(self, ttl_seconds: int = 300):
        self.ttl = ttl_seconds
        self._cache: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        if key in self._cache:
            timestamp, value = self._cache[key]
            if time.time() - timestamp < self.ttl:
                return value
            del self._cache[key]
        return None

    def set(self, key: str, value: Any) -> None:
        self._cache[key] = (time.time(), value)

    def clear(self) -> None:
        self._cache.clear()


# Global cache instance (5 minute TTL)
_cache = SimpleCache(ttl_seconds=300)


class SkillMarketplaceConfig(BaseModel):
    """Skill marketplace configuration"""

    base_url: str = Field(default="https://clawhub.ai", description="Marketplace base URL")
    token: str | None = Field(default=None, description="Authentication token")
    timeout: int = Field(default=30, description="Request timeout (seconds)")


class SkillSearchResult(BaseModel):
    """Skill search result"""

    score: float = Field(..., description="Search match score")
    slug: str = Field(..., description="Skill identifier")
    display_name: str = Field(..., description="Display name")
    summary: str | None = Field(default=None, description="Skill summary")
    version: str | None = Field(default=None, description="Latest version")
    updated_at: int | None = Field(default=None, description="Update timestamp")


class SkillDetail(BaseModel):
    """Skill detail"""

    slug: str = Field(..., description="Skill identifier")
    display_name: str = Field(..., description="Display name")
    summary: str | None = Field(default=None, description="Skill summary")
    tags: dict[str, str] = Field(default_factory=dict, description="Tags")
    created_at: int = Field(..., description="Creation timestamp")
    updated_at: int = Field(..., description="Update timestamp")
    latest_version: str | None = Field(default=None, description="Latest version")
    owner: dict[str, Any] | None = Field(default=None, description="Owner information")
    metadata: dict[str, Any] | None = Field(default=None, description="Metadata")


class SkillDownloadResult(BaseModel):
    """Skill download result"""

    archive_path: Path = Field(..., description="Downloaded archive path")
    integrity: str = Field(..., description="SHA256 integrity check")


class SkillMarketplaceClient:
    """Skill marketplace client"""

    def __init__(self, config: SkillMarketplaceConfig | None = None) -> None:
        self.config = config or SkillMarketplaceConfig()
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client"""
        if self._client is None or self._client.is_closed:
            headers: dict[str, str] = {}
            token = self._resolve_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url,
                headers=headers,
                timeout=self.config.timeout,
            )
        return self._client

    def _resolve_token(self) -> str | None:
        """Resolve authentication token"""
        # 1. Prefer token from config
        if self.config.token:
            return self.config.token

        # 2. Read from environment variables
        return (
            os.environ.get("PYCLAW_CLAWHUB_TOKEN", "").strip()
            or os.environ.get("CLAWHUB_TOKEN", "").strip()
            or os.environ.get("CLAWHUB_AUTH_TOKEN", "").strip()
        ) or None

    async def search_skills(
        self,
        query: str = "*",
        limit: int = 20,
    ) -> list[SkillSearchResult]:
        """Search skills

        Args:
            query: Search keyword, default is * for all
            limit: Return result count limit

        Returns:
            List of skill search results
        """
        # Check cache
        cache_key = f"search:{query}:{limit}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

        client = await self._get_client()
        params = {"q": query.strip() or "*", "limit": str(limit)}

        try:
            response = await client.get("/api/v1/search", params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                raise Exception("Rate limited, please try again later") from e
            raise

        data = response.json()
        results = data.get("results", [])

        result_list = [
            SkillSearchResult(
                score=r.get("score", 0.0),
                slug=r["slug"],
                display_name=r["displayName"],
                summary=r.get("summary"),
                version=r.get("version"),
                updated_at=r.get("updatedAt"),
            )
            for r in results
        ]

        # Store in cache
        _cache.set(cache_key, result_list)
        return result_list

    async def get_skill_detail(self, slug: str) -> SkillDetail | None:
        """Get skill detail

        Args:
            slug: Skill identifier

        Returns:
            Skill detail, returns None if not exists

        Raises:
            Exception: When encountering rate limit or other errors
        """
        # Check cache
        cache_key = f"detail:{slug}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

        client = await self._get_client()

        try:
            response = await client.get(f"/api/v1/skills/{slug}")
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            if e.response.status_code == 429:
                raise Exception("Rate limited, please try again later") from e
            raise

        data = response.json()
        skill_data = data.get("skill")
        if not skill_data:
            return None

        latest = data.get("latestVersion", {})

        detail = SkillDetail(
            slug=skill_data["slug"],
            display_name=skill_data["displayName"],
            summary=skill_data.get("summary"),
            tags=skill_data.get("tags", {}),
            created_at=skill_data["createdAt"],
            updated_at=skill_data["updatedAt"],
            latest_version=latest.get("version") if latest else None,
            owner=data.get("owner"),
            metadata=data.get("metadata"),
        )

        # Store in cache
        _cache.set(cache_key, detail)
        return detail

    async def download_skill(
        self,
        slug: str,
        version: str | None = None,
        output_dir: Path | None = None,
    ) -> SkillDownloadResult:
        """Download skill archive

        Args:
            slug: Skill identifier
            version: Specify version, default download latest
            output_dir: Output directory, default uses system temp directory

        Returns:
            Download result containing archive path and integrity check

        Raises:
            Exception: When download fails or rate limited
        """
        client = await self._get_client()

        params: dict[str, str] = {"slug": slug}
        if version:
            params["version"] = version

        try:
            response = await client.get("/api/v1/download", params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                raise Exception("Rate limited, please try again later") from e
            raise Exception(f"Download failed: {e}") from e

        # Calculate integrity checksum
        content = response.content
        import hashlib

        integrity = f"sha256-{hashlib.sha256(content).digest().hex()}"

        # Determine output path
        if output_dir is None:
            import tempfile

            output_dir = Path(tempfile.gettempdir()) / "pyclaw-skills"

        output_dir.mkdir(parents=True, exist_ok=True)
        archive_path = output_dir / f"{slug}.zip"

        # Write to file
        archive_path.write_bytes(content)

        return SkillDownloadResult(
            archive_path=archive_path,
            integrity=integrity,
        )

    async def list_skills(
        self,
        limit: int = 50,
    ) -> list[SkillSearchResult]:
        """List available skills

        Args:
            limit: Return result count limit

        Returns:
            List of skills
        """
        # Check cache
        cache_key = f"list:{limit}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

        client = await self._get_client()
        params = {"limit": str(limit)}

        try:
            response = await client.get("/api/v1/skills", params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                raise Exception("Rate limited, please try again later") from e
            raise

        data = response.json()
        items = data.get("items", [])

        result_list = [
            SkillSearchResult(
                score=0.0,  # List API has no score
                slug=item["slug"],
                display_name=item["displayName"],
                summary=item.get("summary"),
                version=item.get("latestVersion", {}).get("version")
                if item.get("latestVersion")
                else None,
                updated_at=item.get("updatedAt"),
            )
            for item in items
        ]

        # Store in cache
        _cache.set(cache_key, result_list)
        return result_list

    async def close(self) -> None:
        """Close client connection"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "SkillMarketplaceClient":
        """Async context manager entry"""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Async context manager exit"""
        await self.close()