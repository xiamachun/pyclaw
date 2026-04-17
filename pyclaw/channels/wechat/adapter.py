"""
WeChat Work callback mode adapter

Receives messages using WeChat Work official callback mode.
Reference: WeChat Work official documentation: https://developer.work.weixin.qq.com/document/path/90930
"""

import asyncio
import json
import logging
import os
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable, Optional

import httpx
from pydantic import BaseModel, Field, SecretStr

from pyclaw.channels.base import ChannelAdapter, InboundMessage, MessageHandler, OutboundMessage
from pyclaw.channels.wechat.crypto import WeChatCrypto
from pyclaw.constants import DEFAULT_GATEWAY_URL

logger = logging.getLogger(__name__)


class WeChatWorkConfig(BaseModel):
    """WeChat Work configuration"""
    
    corp_id: str = Field(..., description="Enterprise ID")
    corp_secret: SecretStr = Field(..., description="App Secret")
    agent_id: int = Field(..., description="App AgentId")
    token: str = Field(..., description="Callback Token")
    encoding_aes_key: str = Field(..., description="Callback EncodingAESKey")
    gateway_url: str = Field(
        default=DEFAULT_GATEWAY_URL,
        description="Gateway URL"
    )
    gateway_token: Optional[str] = Field(None, description="Gateway authentication token")
    
    model_config = {"populate_by_name": True}


class WeChatWorkAdapter(ChannelAdapter):
    """WeChat Work callback mode adapter
    
    Receives messages using WeChat Work enterprise app callback mode,
    generates AI responses via Gateway's chatCompletions API.
    """
    
    _channel_id = "wechat_work"
    _GET_TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
    _SEND_MESSAGE_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"
    
    def __init__(self, config: WeChatWorkConfig) -> None:
        self.config = config
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0
        self._message_handler: Optional[MessageHandler] = None
        self._connected = False
        self._http_client: Optional[httpx.AsyncClient] = None
        self._crypto: Optional[WeChatCrypto] = None
        self._sessions: dict[str, dict] = {}  # user_id -> session context
    
    @property
    def channel_id(self) -> str:
        return self._channel_id
    
    async def connect(self) -> None:
        """Connect to WeChat Work"""
        self._http_client = httpx.AsyncClient(timeout=60.0)
        
        # Initialize encryption/decryption tool
        self._crypto = WeChatCrypto(
            token=self.config.token,
            encoding_aes_key=self.config.encoding_aes_key,
            corp_id=self.config.corp_id
        )
        
        # Refresh access token
        await self._refresh_access_token()
        
        self._connected = True
        logger.info("WeChat Work adapter connected")
    
    async def disconnect(self) -> None:
        """Disconnect"""
        self._connected = False
        
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        
        self._access_token = None
        self._crypto = None
        logger.info("WeChat Work adapter disconnected")
    
    async def send_message(self, target: str, message: OutboundMessage) -> None:
        """Send message to user
        
        Args:
            target: Target user ID (WeChat Work userid)
            message: Outbound message
        """
        if not self._connected or not self._http_client:
            raise RuntimeError("WeChat Work adapter is not connected")
        
        await self._ensure_token()
        
        url = f"{self._SEND_MESSAGE_URL}?access_token={self._access_token}"
        
        # WeChat Work text message format
        payload = {
            "touser": target,
            "msgtype": "text",
            "agentid": self.config.agent_id,
            "text": {
                "content": message.content
            },
            "safe": 0
        }
        
        try:
            response = await self._http_client.post(
                url,
                json=payload,
            )
            response.raise_for_status()
            result = response.json()
            
            if result.get("errcode") != 0:
                raise RuntimeError(f"Failed to send message: {result.get('errmsg')}")
            
            logger.debug(f"Message sent to {target}")
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            raise
    
    async def on_message(self, handler: MessageHandler) -> None:
        """Register message handler
        
        Args:
            handler: Message handling callback function
        """
        self._message_handler = handler
    
    async def health_check(self) -> bool:
        """Health check"""
        return self._connected and self._access_token is not None
    
    async def _refresh_access_token(self) -> None:
        """Refresh access token"""
        if not self._http_client:
            raise RuntimeError("HTTP client not initialized")
        
        params = {
            "corpid": self.config.corp_id,
            "corpsecret": self.config.corp_secret.get_secret_value(),
        }
        
        try:
            response = await self._http_client.get(
                self._GET_TOKEN_URL,
                params=params,
            )
            response.raise_for_status()
            result = response.json()
            
            if result.get("errcode") != 0:
                raise RuntimeError(f"Failed to get access_token: {result.get('errmsg')}")
            
            self._access_token = result.get("access_token")
            expires_in = result.get("expires_in", 7200)
            self._token_expires_at = time.time() + expires_in - 300  # Refresh 5 minutes early
            
            logger.info("WeChat Work access token refreshed")
        except Exception as e:
            logger.error(f"Failed to refresh access token: {e}")
            raise
    
    async def _ensure_token(self) -> None:
        """Ensure token is valid"""
        if time.time() >= self._token_expires_at:
            await self._refresh_access_token()
    
    async def handle_callback(
        self,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        body: str
    ) -> Optional[str]:
        """Handle WeChat Work callback message
        
        Args:
            msg_signature: Message signature
            timestamp: Timestamp
            nonce: Random string
            body: Callback request body (XML format)
            
        Returns:
            AI reply content (if immediate reply needed)
        """
        if not self._crypto:
            logger.warning("Crypto not initialized")
            return None
        
        try:
            # Decrypt message
            decrypted_msg = self._crypto.decrypt(body, msg_signature, timestamp, nonce)
            
            # Parse XML
            root = ET.fromstring(decrypted_msg)
            
            msg_type = root.findtext("MsgType")
            to_user_name = root.findtext("ToUserName")
            from_user_name = root.findtext("FromUserName")
            agent_id = root.findtext("AgentID")
            
            logger.info(f"Received message: type={msg_type}, from={from_user_name}, to={to_user_name}")
            
            # Only process text messages
            if msg_type != "text":
                logger.debug(f"Ignoring non-text message: {msg_type}")
                return None
            
            content = root.findtext("Content", "").strip()
            msg_id = root.findtext("MsgId", "")
            create_time = root.findtext("CreateTime", "")
            
            if not content or not from_user_name:
                return None
            
            logger.info(f"Received text message from {from_user_name}: {content[:50]}...")
            
            # Build inbound message
            inbound_msg = InboundMessage(
                channel_id=self._channel_id,
                peer_id=from_user_name,
                peer_name=from_user_name,  # WeChat Work may need additional user info
                content=content,
                raw_data={
                    "msg_id": msg_id,
                    "create_time": create_time,
                    "agent_id": agent_id,
                    "to_user_name": to_user_name,
                }
            )
            
            # Call message handler
            if self._message_handler:
                await self._message_handler(inbound_msg)
            
            # Call Gateway's chatCompletions API
            reply = await self._call_gateway_chat(from_user_name, content)
            
            if reply:
                # Send reply
                await self.send_message(from_user_name, OutboundMessage(content=reply))
                return reply
            
            return None
            
        except Exception as e:
            logger.error(f"Failed to handle callback: {e}", exc_info=True)
            return None
    
    async def verify_callback_url(
        self,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        echostr: str
    ) -> str:
        """Verify callback URL
        
        Args:
            msg_signature: Message signature
            timestamp: Timestamp
            nonce: Random string
            echostr: Encrypted random string
            
        Returns:
            Decrypted echostr (needs to be returned to WeChat Work as-is)
        """
        if not self._crypto:
            raise RuntimeError("Crypto not initialized")
        
        decrypted_echostr = self._crypto.verify_url(msg_signature, timestamp, nonce, echostr)
        logger.info("Callback URL verified successfully")
        return decrypted_echostr
    
    async def _call_gateway_chat(self, user_id: str, message: str) -> Optional[str]:
        """Call Gateway's chat completions API
        
        Args:
            user_id: User ID
            message: User message
            
        Returns:
            AI reply
        """
        if not self._http_client:
            return None
        
        url = f"{self.config.gateway_url}/v1/chat/completions"
        
        # Build request headers
        headers = {"Content-Type": "application/json"}
        if self.config.gateway_token:
            headers["Authorization"] = f"Bearer {self.config.gateway_token}"
        
        # Get or create session context
        session = self._sessions.setdefault(user_id, {"messages": []})
        
        # Add user message
        session["messages"].append({"role": "user", "content": message})
        
        # Limit history length
        if len(session["messages"]) > 20:
            session["messages"] = session["messages"][-20:]
        
        payload = {
            "model": "default",
            "messages": session["messages"],
            "stream": False,
        }
        
        try:
            response = await self._http_client.post(
                url,
                headers=headers,
                json=payload,
                timeout=120.0,
            )
            response.raise_for_status()
            result = response.json()
            
            reply = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            
            if reply:
                # Add assistant reply to history
                session["messages"].append({"role": "assistant", "content": reply})
            
            return reply
            
        except Exception as e:
            logger.error(f"Failed to call gateway: {e}")
            return f"Sorry, an error occurred while processing your request: {str(e)}"