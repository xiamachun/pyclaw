"""
Authentication profile management for LLM providers.

Supports multiple API keys per provider with:
- Automatic rotation on failure
- Cooldown period management
- Usage tracking
"""

from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from pydantic import BaseModel, SecretStr, Field
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class AuthProfile(BaseModel):
    """A single authentication profile for an LLM provider."""
    
    id: str = Field(..., description="Unique identifier for this profile")
    provider: str = Field(..., description="Provider name (openai, anthropic, etc.)")
    api_key: SecretStr = Field(..., description="API key or token")
    base_url: Optional[str] = Field(None, description="Custom base URL")
    
    # Metadata
    name: Optional[str] = Field(None, description="Human-readable name")
    priority: int = Field(0, description="Priority (higher = preferred)")
    
    # Status tracking
    cooldown_until: Optional[datetime] = Field(None, description="Cooldown expiry time")
    failure_count: int = Field(0, description="Consecutive failure count")
    last_used: Optional[datetime] = Field(None, description="Last successful use time")
    total_requests: int = Field(0, description="Total requests made")
    total_tokens: int = Field(0, description="Total tokens used")
    
    model_config = {"arbitrary_types_allowed": True}
    
    def is_available(self) -> bool:
        """Check if this profile is available (not in cooldown)."""
        if self.cooldown_until is None:
            return True
        return datetime.now() >= self.cooldown_until
    
    def get_remaining_cooldown(self) -> Optional[timedelta]:
        """Get remaining cooldown time, if any."""
        if self.cooldown_until is None:
            return None
        remaining = self.cooldown_until - datetime.now()
        if remaining.total_seconds() <= 0:
            return None
        return remaining


class AuthProfileManager:
    """
    Manages authentication profiles with rotation and cooldown.
    
    Features:
    - Multiple profiles per provider
    - Automatic rotation on failure
    - Cooldown period management
    - Persistence to disk
    """
    
    def __init__(
        self,
        store_path: Optional[Path] = None,
        default_cooldown_minutes: int = 5,
    ):
        """
        Initialize the auth profile manager.
        
        Args:
            store_path: Path to store profiles (default: ~/.pyclaw/auth_profiles.json)
            default_cooldown_minutes: Default cooldown duration in minutes
        """
        self._profiles: Dict[str, AuthProfile] = {}
        self._default_cooldown = timedelta(minutes=default_cooldown_minutes)
        
        if store_path is None:
            from pyclaw.config.paths import get_paths as _get_paths
            store_path = _get_paths().auth_profiles_file
        self._store_path = store_path
        
        # Load profiles from disk
        self._load_profiles()
    
    def _load_profiles(self) -> None:
        """Load profiles from disk."""
        if not self._store_path.exists():
            return
        
        try:
            data = json.loads(self._store_path.read_text())
            for profile_data in data.get("profiles", []):
                # Convert datetime strings back to datetime objects
                if profile_data.get("cooldown_until"):
                    profile_data["cooldown_until"] = datetime.fromisoformat(
                        profile_data["cooldown_until"]
                    )
                if profile_data.get("last_used"):
                    profile_data["last_used"] = datetime.fromisoformat(
                        profile_data["last_used"]
                    )
                
                # Convert api_key string to SecretStr
                if "api_key" in profile_data and isinstance(profile_data["api_key"], str):
                    profile_data["api_key"] = SecretStr(profile_data["api_key"])
                
                profile = AuthProfile(**profile_data)
                self._profiles[profile.id] = profile
            
            logger.info("Loaded %d auth profiles", len(self._profiles))
        except Exception as e:
            logger.error("Failed to load auth profiles: %s", e, exc_info=True)
    
    def _save_profiles(self) -> None:
        """Save profiles to disk."""
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            
            data = {"profiles": []}
            for profile in self._profiles.values():
                profile_dict = profile.model_dump()
                
                # Convert SecretStr to plain string for storage
                profile_dict["api_key"] = profile.api_key.get_secret_value()
                
                # Convert datetime to ISO string
                if profile_dict.get("cooldown_until"):
                    profile_dict["cooldown_until"] = profile_dict["cooldown_until"].isoformat()
                if profile_dict.get("last_used"):
                    profile_dict["last_used"] = profile_dict["last_used"].isoformat()
                
                data["profiles"].append(profile_dict)
            
            self._store_path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.error("Failed to save auth profiles: %s", e, exc_info=True)
    
    def add_profile(self, profile: AuthProfile) -> None:
        """
        Add or update an authentication profile.
        
        Args:
            profile: The profile to add
        """
        self._profiles[profile.id] = profile
        self._save_profiles()
        logger.info("Added auth profile: %s (%s)", profile.id, profile.provider)
    
    def remove_profile(self, profile_id: str) -> bool:
        """
        Remove an authentication profile.
        
        Args:
            profile_id: The profile ID to remove
            
        Returns:
            True if profile was removed
        """
        if profile_id in self._profiles:
            del self._profiles[profile_id]
            self._save_profiles()
            logger.info("Removed auth profile: %s", profile_id)
            return True
        return False
    
    def get_profile(self, profile_id: str) -> Optional[AuthProfile]:
        """
        Get a specific profile by ID.
        
        Args:
            profile_id: The profile ID
            
        Returns:
            The profile or None
        """
        return self._profiles.get(profile_id)
    
    def list_profiles(self, provider: Optional[str] = None) -> List[AuthProfile]:
        """
        List all profiles, optionally filtered by provider.
        
        Args:
            provider: Optional provider filter
            
        Returns:
            List of profiles
        """
        profiles = list(self._profiles.values())
        if provider:
            profiles = [p for p in profiles if p.provider == provider]
        return sorted(profiles, key=lambda p: -p.priority)
    
    def get_next_available(self, provider: str) -> Optional[AuthProfile]:
        """
        Get the next available profile for a provider.
        
        Respects cooldowns and returns the highest priority available profile.
        
        Args:
            provider: The provider name
            
        Returns:
            An available AuthProfile or None
        """
        profiles = self.list_profiles(provider)
        
        for profile in profiles:
            if profile.is_available():
                return profile
        
        # All profiles in cooldown, return the one with earliest expiry
        if profiles:
            return min(
                profiles,
                key=lambda p: p.cooldown_until or datetime.min
            )
        
        return None
    
    def mark_failure(
        self,
        profile_id: str,
        cooldown_minutes: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """
        Mark a profile as failed, setting cooldown.
        
        Args:
            profile_id: The profile ID
            cooldown_minutes: Custom cooldown duration
            error_message: Optional error message for logging
        """
        profile = self._profiles.get(profile_id)
        if not profile:
            return
        
        profile.failure_count += 1
        
        # Calculate cooldown with exponential backoff
        base_cooldown = timedelta(minutes=cooldown_minutes or 5)
        backoff_factor = min(profile.failure_count, 5)  # Cap at 5x
        cooldown = base_cooldown * backoff_factor
        
        profile.cooldown_until = datetime.now() + cooldown
        
        logger.warning(
            f"Auth profile {profile_id} marked as failed "
            f"(attempt {profile.failure_count}, cooldown {cooldown}): {error_message}"
        )
        
        self._save_profiles()
    
    def mark_success(self, profile_id: str, tokens_used: int = 0) -> None:
        """
        Mark a profile as successfully used.
        
        Args:
            profile_id: The profile ID
            tokens_used: Number of tokens used in the request
        """
        profile = self._profiles.get(profile_id)
        if not profile:
            return
        
        # Reset failure tracking
        profile.failure_count = 0
        profile.cooldown_until = None
        
        # Update usage tracking
        profile.last_used = datetime.now()
        profile.total_requests += 1
        profile.total_tokens += tokens_used
        
        self._save_profiles()
    
    def get_cooldown_status(self) -> Dict[str, Any]:
        """
        Get cooldown status for all profiles.
        
        Returns:
            Dict with profile ID -> status mapping
        """
        status = {}
        for profile in self._profiles.values():
            remaining = profile.get_remaining_cooldown()
            status[profile.id] = {
                "provider": profile.provider,
                "name": profile.name,
                "is_available": profile.is_available(),
                "cooldown_remaining": remaining.total_seconds() if remaining else 0,
                "failure_count": profile.failure_count,
                "total_requests": profile.total_requests,
            }
        return status
    
    def reset_all_cooldowns(self) -> None:
        """Reset all cooldowns (useful for testing or manual reset)."""
        for profile in self._profiles.values():
            profile.cooldown_until = None
            profile.failure_count = 0
        self._save_profiles()
        logger.info("All auth profile cooldowns reset")


# Global singleton instance
_manager_instance: Optional[AuthProfileManager] = None


def get_auth_profile_manager() -> AuthProfileManager:
    """Get the global AuthProfileManager instance."""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = AuthProfileManager()
    return _manager_instance
