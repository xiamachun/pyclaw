"""
Telegram Client

Standalone Telegram client using Long Polling to receive messages and forward to Gateway.
"""

import asyncio
import json
import logging
import os
import signal
import sys
from typing import Any

import httpx

from pyclaw.channels.telegram.adapter import TelegramAdapter, TelegramConfig
from pyclaw.constants import DEFAULT_GATEWAY_URL

# Configure logging
LOG_DIR = os.path.expanduser("~/.pyclaw")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "telegram_client.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class TelegramClient:
    """Telegram client"""

    def __init__(self, config: TelegramConfig) -> None:
        self.config = config
        self.adapter = TelegramAdapter(config)
        self._gateway_client: httpx.AsyncClient | None = None
        self._sessions: dict[str, dict] = {}
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """Start the client"""
        logger.info("Starting Telegram client...")

        # Connect adapter
        await self.adapter.connect()

        # Register message handler
        await self.adapter.on_message(self._handle_message)

        # Create Gateway client
        self._gateway_client = httpx.AsyncClient(timeout=60.0)

        logger.info("Telegram client started")

    async def stop(self) -> None:
        """Stop the client"""
        logger.info("Stopping Telegram client...")

        await self.adapter.disconnect()

        if self._gateway_client:
            await self._gateway_client.aclose()

        logger.info("Telegram client stopped")

    async def _handle_message(self, message: Any) -> None:
        """Handle message and forward to Gateway"""
        try:
            # Use shared Gateway client for routing resolution and API calls
            from pyclaw.channels.gateway_client import resolve_and_chat
            from pyclaw.channels.base import OutboundMessage
            
            assistant_message = await resolve_and_chat(
                channel="telegram",
                peer_id=message.peer_id,
                peer_kind="direct",
                message=message.content,
                gateway_url=self.config.gateway_url,
                gateway_token=self.config.gateway_token,
                http_client=self._gateway_client,
                sessions=self._sessions,
                thread_id=message.thread_id,
            )

            if assistant_message:
                # Send reply
                target = message.peer_id
                reply_to = message.raw_data.get("message_id")
                outbound = OutboundMessage(content=assistant_message, reply_to=reply_to)
                await self.adapter.send_message(target, outbound)
                logger.info(f"Replied to user {message.peer_name} ({message.peer_id})")

        except Exception as e:
            logger.error(f"Failed to process message: {e}", exc_info=True)


def load_config() -> TelegramConfig:
    """Load configuration"""
    config_file = os.path.expanduser("~/.pyclaw/pyclaw.json")

    if os.path.exists(config_file):
        with open(config_file, "r", encoding="utf-8") as f:
            config_data = json.load(f)
            telegram_config = config_data.get("telegram", {})
    else:
        telegram_config = {}

    # Environment variable override
    return TelegramConfig(
        bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", telegram_config.get("bot_token", "")),
        webhook_url=os.environ.get("TELEGRAM_WEBHOOK_URL", telegram_config.get("webhook_url")),
        gateway_url=telegram_config.get("gateway_url", DEFAULT_GATEWAY_URL),
        gateway_token=telegram_config.get("gateway_token")
    )


async def main() -> None:
    """Main function"""
    config = load_config()

    if not config.bot_token:
        logger.error("Telegram configuration incomplete, please set TELEGRAM_BOT_TOKEN")
        sys.exit(1)

    client = TelegramClient(config)

    # Start client
    await client.start()

    # Setup signal handlers
    def signal_handler(signum: int, frame: Any) -> None:
        logger.info(f"Received signal {signum}, shutting down...")
        client._shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Wait for shutdown signal
    await client._shutdown_event.wait()

    # Stop client
    await client.stop()


if __name__ == "__main__":
    asyncio.run(main())