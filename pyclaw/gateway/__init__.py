"""Gateway core module.

Provides FastAPI server, authentication, WebSocket connection management, and API routing.
"""

from pyclaw.gateway.server import GatewayServer, create_gateway_app

__all__ = [
    "GatewayServer",
    "create_gateway_app",
]
