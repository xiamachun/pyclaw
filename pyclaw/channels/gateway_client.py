"""
Gateway client shared module

Encapsulates repeated routing resolution, session management, and Gateway API call logic in channel adapters.
"""

import logging
from typing import Any

import httpx

from pyclaw.routing.resolver import RouteResolver
from pyclaw.config.loader import load_config
from pyclaw.routing.session_key import build_session_key

logger = logging.getLogger(__name__)


async def resolve_and_chat(
    channel: str,
    peer_id: str,
    peer_kind: str,
    message: str,
    gateway_url: str,
    gateway_token: str,
    http_client: httpx.AsyncClient,
    sessions: dict[str, Any],
    thread_id: str | None = None,
) -> str | None:
    """Resolve routing and call Gateway API.

    Args:
        channel: Channel identifier (dingtalk/feishu/telegram/slack)
        peer_id: Peer ID (user ID or group ID)
        peer_kind: Peer type (direct/group)
        message: User message content
        gateway_url: Gateway service URL
        gateway_token: Gateway authentication Token
        http_client: HTTP async client
        sessions: Session storage dictionary
        thread_id: Thread ID (optional, for threaded conversations in group chats)

    Returns:
        AI reply content, None on failure
    """
    # Determine target Agent through routing resolver
    resolved_agent_id: str | None = None
    try:
        route_config = load_config()
        resolver = RouteResolver(config=route_config)
        agent_entry = resolver.resolve(
            channel=channel, peer_id=peer_id, peer_kind=peer_kind,
        )
        resolved_agent_id = agent_entry.id
    except Exception as route_err:
        logger.debug("Route resolve skipped: %s", route_err)

    # Get or create session
    session_key = build_session_key(
        agent_id=resolved_agent_id or "main",
        channel=channel,
        peer_id=peer_id,
        thread_id=thread_id,
    )
    session = sessions.get(session_key, {"messages": []})

    # Add user message to session
    session["messages"].append({
        "role": "user",
        "content": message
    })

    # Call Gateway
    headers = {"Content-Type": "application/json"}
    if gateway_token:
        headers["Authorization"] = f"Bearer {gateway_token}"

    payload = {
        "model": "default",
        "messages": session["messages"],
        "stream": False
    }

    if resolved_agent_id:
        payload["agentId"] = resolved_agent_id

    try:
        response = await http_client.post(
            f"{gateway_url}/v1/chat/completions",
            json=payload,
            headers=headers
        )
        response.raise_for_status()
        result = response.json()

        # Extract reply
        assistant_message = result["choices"][0]["message"]["content"]
        
        # Save session
        sessions[session_key] = session
        
        return assistant_message
    except Exception as e:
        logger.error("Gateway API call failed: %s", e)
        return None