#!/usr/bin/env python3
"""
WeChat Work client

Starts a FastAPI server to listen for WeChat Work callbacks,
receives messages and calls AI via Gateway's chatCompletions API to generate replies.

Architecture design:
- Message reception: FastAPI receives callbacks, responds quickly
- Message processing: Async processing, supports concurrency
- Session management: In-memory session history storage
"""

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Add project root to Python path
_project_root = Path(__file__).parent.parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, SecretStr

from pyclaw.channels.wechat.adapter import WeChatWorkAdapter, WeChatWorkConfig
from pyclaw.config.paths import get_paths
from pyclaw.constants import DEFAULT_GATEWAY_URL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
logger = logging.getLogger("wechat_client")

class WeChatWorkClientConfig(BaseModel):
    """WeChat Work client configuration"""
    
    corp_id: str = Field(..., description="Corp ID")
    corp_secret: SecretStr = Field(..., description="App Secret")
    agent_id: int = Field(..., description="App AgentId")
    token: str = Field(..., description="Callback Token")
    encoding_aes_key: str = Field(..., description="Callback EncodingAESKey")
    gateway_url: str = Field(
        default=DEFAULT_GATEWAY_URL,
        description="Gateway URL"
    )
    gateway_token: Optional[str] = Field(None, description="Gateway authentication token")
    host: str = Field(default="0.0.0.0", description="Listen host")
    port: int = Field(default=8765, description="Listen port")


class WeChatWorkClient:
    """WeChat Work client
    
    Starts a FastAPI server to listen for WeChat Work callbacks,
    receives messages and processes them via Gateway.
    """
    
    def __init__(self, config: WeChatWorkClientConfig):
        self.config = config
        self.adapter: Optional[WeChatWorkAdapter] = None
        self.app: Optional[FastAPI] = None
        self._server: Optional[any] = None
    
    async def start(self):
        """Start client"""
        # Create adapter configuration
        adapter_config = WeChatWorkConfig(
            corp_id=self.config.corp_id,
            corp_secret=self.config.corp_secret,
            agent_id=self.config.agent_id,
            token=self.config.token,
            encoding_aes_key=self.config.encoding_aes_key,
            gateway_url=DEFAULT_GATEWAY_URL,
            gateway_token=None,
        )
        
        # Create adapter
        self.adapter = WeChatWorkAdapter(adapter_config)
        
        # Connect adapter
        await self.adapter.connect()
        
        # Register message handler
        await self.adapter.on_message(self._handle_inbound_message)
        
        # Create FastAPI application
        self.app = FastAPI(title="WeChat Work Callback Server")
        
        # Register routes
        self._register_routes()
        
        logger.info(f"WeChat Work client started on {self.config.host}:{self.config.port}")
    
    async def stop(self):
        """Stop client"""
        if self.adapter:
            await self.adapter.disconnect()
        logger.info("WeChat Work client stopped")
    
    def _register_routes(self):
        """Register FastAPI routes"""
        
        @self.app.get("/wechat/callback")
        async def verify_callback(
            msg_signature: str,
            timestamp: str,
            nonce: str,
            echostr: str
        ):
            """Verify callback URL
            
            WeChat Work sends a GET request to verify when configuring callback URL for the first time.
            """
            logger.info("Received callback verification request")
            
            try:
                decrypted_echostr = await self.adapter.verify_callback_url(
                    msg_signature=msg_signature,
                    timestamp=timestamp,
                    nonce=nonce,
                    echostr=echostr
                )
                return PlainTextResponse(content=decrypted_echostr)
            except Exception as e:
                logger.error(f"Callback verification failed: {e}", exc_info=True)
                return PlainTextResponse(content="error", status_code=400)
        
        @self.app.post("/wechat/callback")
        async def handle_callback(request: Request):
            """Handle callback message
            
            WeChat Work sends a POST request when sending messages.
            """
            logger.info("Received callback message")
            
            # Get query parameters
            msg_signature = request.query_params.get("msg_signature", "")
            timestamp = request.query_params.get("timestamp", "")
            nonce = request.query_params.get("nonce", "")
            
            # Get request body
            body = await request.body()
            body_str = body.decode("utf-8")
            
            try:
                # Handle callback
                reply = await self.adapter.handle_callback(
                    msg_signature=msg_signature,
                    timestamp=timestamp,
                    nonce=nonce,
                    body=body_str
                )
                
                # Return success response
                return PlainTextResponse(content="success")
                
            except Exception as e:
                logger.error(f"Callback handling failed: {e}", exc_info=True)
                return PlainTextResponse(content="error", status_code=500)
        
        @self.app.get("/health")
        async def health_check():
            """Health check"""
            is_healthy = await self.adapter.health_check() if self.adapter else False
            return {"status": "healthy" if is_healthy else "unhealthy"}
    
    async def _handle_inbound_message(self, message):
        """Handle inbound message
        
        Args:
            message: InboundMessage object
        """
        logger.info(f"Processing inbound message from {message.peer_name}: {message.content[:50]}...")
        # Message processing logic is completed in adapter.handle_callback
        # This section is mainly for logging and extension points
    
    def run(self):
        """Run the server (synchronous interface)"""
        import uvicorn
        
        async def start_and_run():
            await self.start()
            config = uvicorn.Config(
                app=self.app,
                host=self.config.host,
                port=self.config.port,
                log_level="info"
            )
            server = uvicorn.Server(config)
            await server.serve()
        
        asyncio.run(start_and_run())


def load_config() -> WeChatWorkClientConfig:
    """Load configuration
    
    Prioritizes loading from a configuration file, falls back to environment variables.
    """
    paths = get_paths()
    
    # Try loading from configuration file
    if paths.wechat_config_file and paths.wechat_config_file.exists():
        import yaml
        with open(paths.wechat_config_file, 'r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f)
            if config_data:
                logger.info(f"Loaded config from {paths.wechat_config_file}")
                return WeChatWorkClientConfig(**config_data)
    
    # Load from environment variables
    logger.info("Loading config from environment variables")
    return WeChatWorkClientConfig(
        corp_id=os.environ.get("WECHAT_CORP_ID", ""),
        corp_secret=os.environ.get("WECHAT_CORP_SECRET", ""),
        agent_id=int(os.environ.get("WECHAT_AGENT_ID", "0")),
        token=os.environ.get("WECHAT_TOKEN", ""),
        encoding_aes_key=os.environ.get("WECHAT_ENCODING_AES_KEY", ""),
        gateway_url=DEFAULT_GATEWAY_URL,
        gateway_token=None,
        host=os.environ.get("WECHAT_HOST", "0.0.0.0"),
        port=int(os.environ.get("WECHAT_PORT", "8765")),
    )


def main():
    """Main function"""
    try:
        # Load configuration
        config = load_config()
        
        # Create and run the client
        client = WeChatWorkClient(config)
        client.run()
        
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()