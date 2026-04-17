"""
DingTalk channel routes

Handles DingTalk bot callback messages and Stream connections.
"""

import json
import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, Response, Header

logger = logging.getLogger(__name__)


def create_dingtalk_router(config: Any, agent_runtime: Any) -> APIRouter:
    """Create DingTalk router
    
    Args:
        config: PyClaw configuration
        agent_runtime: Agent runtime
        
    Returns:
        FastAPI router
    """
    router = APIRouter(prefix="/api/dingtalk", tags=["dingtalk"])
    
    # Initialize DingTalk adapter (lazy import)
    _adapter = None
    
    def get_adapter():
        nonlocal _adapter
        if _adapter is not None:
            return _adapter
        
        # Check if DingTalk is enabled
        dingtalk_config = getattr(config.channels, 'dingtalk_connector', None)
        if not dingtalk_config or not dingtalk_config.enabled:
            return None
        
        from pyclaw.channels.dingtalk.stream_adapter import DingTalkStreamConfig, DingTalkStreamAdapter
        
        # Get gateway token
        gateway_token = dingtalk_config.gateway_token
        if not gateway_token:
            auth = config.gateway.auth
            if hasattr(auth, 'token'):
                token = auth.token
                gateway_token = token.get_secret_value() if hasattr(token, 'get_secret_value') else str(token)
        
        stream_config = DingTalkStreamConfig(
            client_id=dingtalk_config.client_id,
            client_secret=dingtalk_config.client_secret.get_secret_value() if hasattr(dingtalk_config.client_secret, 'get_secret_value') else str(dingtalk_config.client_secret),
            gateway_token=gateway_token,
            gateway_url=f"http://{config.gateway.host}:{config.gateway.port}",
            session_timeout=dingtalk_config.session_timeout,
        )
        
        _adapter = DingTalkStreamAdapter(stream_config)
        return _adapter
    
    @router.post("/callback")
    async def dingtalk_callback(request: Request):
        """Handle DingTalk callback message
        
        After DingTalk bot is configured with Stream mode, messages will be pushed to this endpoint.
        """
        adapter = get_adapter()
        if not adapter:
            raise HTTPException(status_code=503, detail="DingTalk channel not configured")
        
        try:
            body = await request.json()
            logger.info("DingTalk callback received: %s", json.dumps(body, ensure_ascii=False)[:200])
            
            # Process message
            reply = await adapter.handle_callback(body)
            
            return {"success": True, "reply": reply}
            
        except Exception as e:
            logger.error("DingTalk callback error: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.get("/status")
    async def dingtalk_status():
        """Check DingTalk channel status"""
        adapter = get_adapter()
        if not adapter:
            return {
                "enabled": False,
                "connected": False,
                "message": "DingTalk channel not configured"
            }
        
        health = await adapter.health_check()
        return {
            "enabled": True,
            "connected": health,
            "message": "OK" if health else "Not connected"
        }
    
    @router.post("/connect")
    async def dingtalk_connect():
        """Manually connect to DingTalk Stream"""
        adapter = get_adapter()
        if not adapter:
            raise HTTPException(status_code=503, detail="DingTalk channel not configured")
        
        try:
            await adapter.connect()
            return {"success": True, "message": "Connected to DingTalk Stream"}
        except Exception as e:
            logger.error("DingTalk connect error: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.post("/disconnect")
    async def dingtalk_disconnect():
        """Disconnect DingTalk Stream connection"""
        adapter = get_adapter()
        if not adapter:
            raise HTTPException(status_code=503, detail="DingTalk channel not configured")
        
        try:
            await adapter.disconnect()
            return {"success": True, "message": "Disconnected from DingTalk Stream"}
        except Exception as e:
            logger.error("DingTalk disconnect error: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.post("/send")
    async def dingtalk_send(
        request: Request,
        user_id: Optional[str] = None,
        content: Optional[str] = None,
    ):
        """Send message to DingTalk
        
        Supports two methods:
        1. Query parameters: ?user_id=xxx&content=xxx
        2. JSON Body: {"message": "xxx"} - for scheduled task broadcast
        
        Args:
            user_id: User ID
            content: Message content
        """
        # Try to get message from JSON body
        message_from_body = None
        try:
            body = await request.json()
            message_from_body = body.get("message")
        except (ValueError, KeyError):
            pass
        
        final_message = content or message_from_body
        if not final_message:
            raise HTTPException(status_code=400, detail="Missing message content")
        
        # Use Stream client to send message to last active user
        try:
            from pyclaw.channels.dingtalk.stream_client import send_cron_message
            success = await send_cron_message(final_message)
            if success:
                return {"success": True, "message": f"Message broadcast sent"}
            else:
                return {"success": False, "message": "No active sessions"}
        except Exception as e:
            logger.error("DingTalk send error: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))
    
    return router