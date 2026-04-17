"""Authentication middleware."""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from pyclaw.config.schema import PyClawConfig


async def verify_token(request: Request, config: PyClawConfig) -> bool:
    """Verify request token.

    Args:
        request: HTTP request
        config: Configuration object

    Returns:
        Whether verification passed
    """
    # Get token from header
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]
        if token == config.gateway.auth.token.get_secret_value():
            return True

    # Get token from query param
    token = request.query_params.get("token")
    if token and token == config.gateway.auth.token.get_secret_value():
        return True

    return False


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Token authentication middleware."""

    def __init__(self, app, config: PyClawConfig) -> None:
        """Initialize middleware.

        Args:
            app: ASGI application
            config: Configuration object
        """
        super().__init__(app)
        self.config = config

    async def dispatch(self, request: Request, call_next):
        """Process request.

        Args:
            request: HTTP request
            call_next: Next middleware or handler

        Returns:
            HTTP response
        """
        # Exclude endpoints that don't require authentication: health check, Web UI and its management APIs
        path = request.url.path.rstrip("/")
        if (
            path == "/api/health"
            or path.startswith("/ui")
            or path.startswith("/api/skills")
            or path.startswith("/api/cron")
            or path.startswith("/api/channels")
            or path.startswith("/api/config")
            or path.startswith("/api/usage")
            or path.startswith("/v1/agents")
        ):
            return await call_next(request)

        # Verify token
        if not await verify_token(request, self.config):
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized"},
            )

        return await call_next(request)