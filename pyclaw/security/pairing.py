"""
DM pairing management module.

Provides user pairing mechanism to prevent unauthorized access to DM messages.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from pydantic import SecretStr

logger = logging.getLogger(__name__)


class PairingManager:
    """Pairing manager, responsible for managing paired users."""

    def __init__(
        self,
        enabled: bool = False,
        code: Optional[SecretStr] = None,
        paired_users_file: str = "paired_users.json",
    ):
        """Initialize the pairing manager.

        Args:
            enabled: Whether pairing is enabled
            code: Pairing code
            paired_users_file: Path to paired users storage file
        """
        self.enabled = enabled
        self.code = code or SecretStr("")
        self.paired_users_file = paired_users_file
        self._paired_users: Dict[str, Dict[str, str]] = {}
        self._load_paired_users()

    def _get_file_path(self) -> Path:
        """Get the full path to the pairing file."""
        # Stored in ~/.pyclaw/paired_users.json
        pyclaw_dir = Path.home() / ".pyclaw"
        pyclaw_dir.mkdir(parents=True, exist_ok=True)
        return pyclaw_dir / self.paired_users_file

    def _load_paired_users(self) -> None:
        """Load paired users from file."""
        if not self.enabled:
            return

        file_path = self._get_file_path()
        if not file_path.exists():
            logger.debug("Paired users file not found: %s", file_path)
            return

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                self._paired_users = json.load(f)
            logger.info("Loaded %s paired users from %s", len(self._paired_users), file_path)
        except json.JSONDecodeError as e:
            logger.error("Failed to decode paired users file: %s", e, exc_info=True)
            self._paired_users = {}
        except Exception as e:
            logger.error("Failed to load paired users file: %s", e, exc_info=True)
            self._paired_users = {}

    def _save_paired_users(self) -> None:
        """Save paired users to file."""
        if not self.enabled:
            return

        file_path = self._get_file_path()
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(self._paired_users, f, indent=2, ensure_ascii=False)
            logger.debug("Saved %s paired users to %s", len(self._paired_users), file_path)
        except Exception as e:
            logger.error("Failed to save paired users file: %s", e, exc_info=True)

    def _make_key(self, channel: str, user_id: str) -> str:
        """Generate pairing key."""
        return f"{channel}:{user_id}"

    def is_paired(self, channel: str, user_id: str) -> bool:
        """Check if user is paired.

        Args:
            channel: Channel name (e.g., "dingtalk", "wechat")
            user_id: User ID

        Returns:
            True if user is paired, False otherwise
        """
        if not self.enabled:
            return True

        key = self._make_key(channel, user_id)
        is_paired = key in self._paired_users
        logger.debug("Check pairing for %s: %s", key, is_paired)
        return is_paired

    def try_pair(self, channel: str, user_id: str, code: str) -> bool:
        """Try to pair a user.

        Args:
            channel: Channel name
            user_id: User ID
            code: User-entered pairing code

        Returns:
            True if pairing successful, False otherwise
        """
        if not self.enabled:
            logger.debug("Pairing is disabled, auto-accept")
            return True

        key = self._make_key(channel, user_id)

        # Check if already paired
        if key in self._paired_users:
            logger.info("User %s is already paired", key)
            return True

        # Verify pairing code
        if code != self.code.get_secret_value():
            logger.warning("Invalid pairing code for %s", key)
            return False

        # Add pairing record
        self._paired_users[key] = {
            "channel": channel,
            "user_id": user_id,
            "paired_at": datetime.now().isoformat(),
        }
        self._save_paired_users()
        logger.info("Successfully paired user %s", key)
        return True

    def unpair(self, channel: str, user_id: str) -> None:
        """Unpair a user.

        Args:
            channel: Channel name
            user_id: User ID
        """
        if not self.enabled:
            return

        key = self._make_key(channel, user_id)
        if key in self._paired_users:
            del self._paired_users[key]
            self._save_paired_users()
            logger.info("Unpaired user %s", key)
        else:
            logger.debug("User %s was not paired", key)

    def get_paired_users(self) -> Dict[str, Dict[str, str]]:
        """Get all paired users.

        Returns:
            Dictionary of paired users
        """
        return self._paired_users.copy()