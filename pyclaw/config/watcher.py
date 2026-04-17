"""
Configuration Watcher for PyClaw

This module provides hot reload functionality for configuration files.
It monitors the configuration file for changes and triggers callbacks when changes are detected.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable, Optional

from pyclaw.config.loader import load_config
from pyclaw.config.paths import PyClawPaths
from pyclaw.config.schema import PyClawConfig

logger = logging.getLogger(__name__)

ReloadCallback = Callable[[PyClawConfig], Optional[Awaitable[None]]]


class ConfigWatcher:
    """Configuration file watcher.

    Monitors configuration file changes and triggers reload callbacks.
    """

    def __init__(
        self,
        config_path: Optional[Path] = None,
        check_interval: float = 5.0,
        on_reload: Optional[ReloadCallback] = None,
    ) -> None:
        """Initialize ConfigWatcher.

        Args:
            config_path: Path to the configuration file to monitor
            check_interval: Interval in seconds between checks (default: 5.0)
            on_reload: Callback function to invoke when configuration is reloaded
        """
        self.config_path = config_path or PyClawPaths().config_file
        self.check_interval = check_interval
        self.on_reload = on_reload
        self._running = False
        self._task: Optional[asyncio.Task[None]] = None
        self._last_mtime: Optional[float] = None
        self._current_config: Optional[PyClawConfig] = None

    async def _load_config(self) -> Optional[PyClawConfig]:
        """Load configuration from file.

        Returns:
            Loaded PyClawConfig or None if loading fails
        """
        try:
            config = load_config(config_path=str(self.config_path))
            return config
        except Exception as e:
            logger.error("Failed to load configuration from %s: %s", self.config_path, e, exc_info=True)
            return None

    async def _check_for_changes(self) -> None:
        """Check for configuration file changes and reload if necessary."""
        try:
            if not self.config_path.exists():
                logger.warning("Configuration file %s does not exist", self.config_path)
                return

            current_mtime = os.path.getmtime(self.config_path)

            if self._last_mtime is None:
                # First check, just record the modification time
                self._last_mtime = current_mtime
                return

            if current_mtime > self._last_mtime:
                logger.info(
                    "Configuration file changed: %s (mtime: %s)",
                    self.config_path,
                    current_mtime,
                )
                self._last_mtime = current_mtime

                # Attempt to reload configuration
                new_config = await self._load_config()
                if new_config is not None:
                    self._current_config = new_config
                    logger.info("Configuration reloaded successfully")
                    # Invoke callback if provided
                    if self.on_reload is not None:
                        try:
                            result = self.on_reload(new_config)
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception as e:
                            logger.error("Error in reload callback: %s", e, exc_info=True)
                else:
                    logger.warning("Configuration reload failed, keeping old configuration")
        except Exception as e:
            logger.error("Error checking for configuration changes: %s", e, exc_info=True)

    async def _watch_loop(self) -> None:
        """Main watch loop that periodically checks for changes."""
        while self._running:
            await self._check_for_changes()
            try:
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break

    async def start(self) -> None:
        """Start the configuration watcher."""
        if self._running:
            logger.warning("ConfigWatcher is already running")
            return

        logger.info("Starting ConfigWatcher for %s", self.config_path)
        self._running = True

        # Load initial configuration
        initial_config = await self._load_config()
        if initial_config is not None:
            self._current_config = initial_config
            logger.info("Initial configuration loaded successfully")

        # Start the watch loop
        self._task = asyncio.create_task(self._watch_loop())

    async def stop(self) -> None:
        """Stop the configuration watcher."""
        if not self._running:
            logger.warning("ConfigWatcher is not running")
            return

        logger.info("Stopping ConfigWatcher")
        self._running = False

        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def reload_now(self) -> Optional[PyClawConfig]:
        """Manually trigger configuration reload.

        Returns:
            Reloaded configuration object, or None if failed
        """
        logger.info("Manual configuration reload triggered")
        await self._check_for_changes()
        return self._current_config

    @property
    def current_config(self) -> Optional[PyClawConfig]:
        """Get current configuration.

        Returns:
            Current configuration object, or None if not loaded
        """
        return self._current_config

    @property
    def is_running(self) -> bool:
        """Check if watcher is running.

        Returns:
            True if running
        """
        return self._running