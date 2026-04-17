"""
Feishu client

Independent Feishu client that listens for event callbacks and forwards to Gateway.
"""

import asyncio
import json
import logging
import os
import signal
import sys
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
import uvicorn

from pyclaw.channels.feishu.adapter import FeishuAdapter, FeishuConfig
from pyclaw.constants import DEFAULT_GATEWAY_URL

# Configure logging
LOG_DIR = os.path.expanduser("~/.pyclaw")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "feishu_client.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class FeishuClient:
    """Feishu client"""

    def __init__(self, config: FeishuConfig) -> None:
        self.config = config
        self.adapter = FeishuAdapter(config)
        self.app = FastAPI()
        self._setup_routes()
        self._gateway_client: httpx.AsyncClient | None = None
        self._sessions: dict[str, dict] = {}
        self._shutdown_event = asyncio.Event()

    def _setup_routes(self) -> None:
        """Set up routes."""

        @self.app.post("/")
        async def handle_callback(request: Request) -> Response:
            """Handle Feishu event callback."""
            timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
            nonce = request.headers.get("X-Lark-Request-Nonce", "")
            signature = request.headers.get("X-Lark-Signature", "")
            body = await request.body()
            body_str = body.decode("utf-8")

            # Verify signature
            if not self.adapter.verify_signature(timestamp, nonce, body_str, signature):
                logger.warning("Feishu event signature verification failed")
                return Response(status_code=401)

            event_data = json.loads(body_str)
            logger.info(f"Received Feishu event: {event_data.get('type')}")

            # Process event
            result = await self.adapter.handle_event(event_data)

            # URL verification challenge
            if result:
                return JSONResponse(content={"challenge": result})

            return Response(status_code=200)

        @self.app.get("/health")
        async def health_check() -> dict[str, Any]:
            """Health check."""
            return {"status": "healthy", "connected": await self.adapter.health_check()}

    async def start(self) -> None:
        """Start client."""
        logger.info("Starting Feishu client...")

        # Connect adapter
        await self.adapter.connect()

        # Register message handler
        await self.adapter.on_message(self._handle_message)

        # Create Gateway client
        self._gateway_client = httpx.AsyncClient(timeout=60.0)

        logger.info("Feishu client started")

    async def stop(self) -> None:
        """Stop client."""
        logger.info("Stopping Feishu client...")

        await self.adapter.disconnect()

        if self._gateway_client:
            await self._gateway_client.aclose()

        logger.info("Feishu client stopped")

    async def _handle_message(self, message: Any) -> None:
        """Handle message and forward to Gateway"""
        try:
            # Use shared Gateway client for route resolution and API calls
            from pyclaw.channels.gateway_client import resolve_and_chat
            from pyclaw.channels.base import OutboundMessage
            
            assistant_message = await resolve_and_chat(
                channel="feishu",
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
                outbound = OutboundMessage(content=assistant_message)
                await self.adapter.send_message(message.peer_id, outbound)
                logger.info(f"Replied to user {message.peer_id}")

        except Exception as e:
            logger.error(f"Failed to handle message: {e}", exc_info=True)


def load_config() -> FeishuConfig:
    """Load configuration."""
    config_file = os.path.expanduser("~/.pyclaw/pyclaw.json")

    if os.path.exists(config_file):
        with open(config_file, "r", encoding="utf-8") as f:
            config_data = json.load(f)
            feishu_config = config_data.get("feishu", {})
    else:
        feishu_config = {}

    # Override with environment variables
    return FeishuConfig(
        app_id=os.environ.get("FEISHU_APP_ID", feishu_config.get("app_id", "")),
        app_secret=os.environ.get("FEISHU_APP_SECRET", feishu_config.get("app_secret", "")),
        verification_token=os.environ.get(
            "FEISHU_VERIFICATION_TOKEN",
            feishu_config.get("verification_token", "")
        ),
        encrypt_key=os.environ.get("FEISHU_ENCRYPT_KEY", feishu_config.get("encrypt_key", "")),
        gateway_url=feishu_config.get("gateway_url", DEFAULT_GATEWAY_URL),
        gateway_token=feishu_config.get("gateway_token")
    )


async def main() -> None:
    """Main function."""
    config = load_config()

    if not config.app_id or not config.app_secret:
        logger.error("Feishu configuration incomplete, please set FEISHU_APP_ID and FEISHU_APP_SECRET")
        sys.exit(1)

    client = FeishuClient(config)

    # Start client
    await client.start()

    # Set up signal handling
    def signal_handler(signum: int, frame: Any) -> None:
        logger.info(f"Received signal {signum}, preparing to shut down...")
        client._shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start FastAPI server
    host = "0.0.0.0"
    port = int(os.environ.get("FEISHU_PORT", "8080"))

    config = uvicorn.Config(
        app=client.app,
        host=host,
        port=port,
        log_config=None
    )
    server = uvicorn.Server(config)

    # Run server
    task = asyncio.create_task(server.serve())

    # Wait for shutdown signal
    await client._shutdown_event.wait()

    # Stop server
    server.should_exit = True
    await task

    # Stop client
    await client.stop()


if __name__ == "__main__":
    asyncio.run(main())