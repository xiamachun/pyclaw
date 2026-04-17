"""Web UI application for PyClaw control console."""

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, FastAPI
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)


def mount_webui(fastapi_app: FastAPI, static_dir: Optional[Path] = None) -> None:
    """Mount the web UI console to the FastAPI application.
    
    Args:
        fastapi_app: The FastAPI application instance.
        static_dir: Directory containing static files. If None, uses default.
    """
    if static_dir is None:
        static_dir = Path(__file__).parent / "static"
    
    static_dir = Path(static_dir)
    
    if static_dir.exists():
        # Mount static files
        from starlette.staticfiles import StaticFiles
        fastapi_app.mount("/ui/static", StaticFiles(directory=str(static_dir)), name="ui-static")
        logger.info("Mounted static files from: %s", static_dir)
    else:
        logger.warning("Static directory not found: %s", static_dir)
    
    # Create router for UI routes
    ui_router = APIRouter()
    
    @ui_router.get("/ui", response_class=HTMLResponse)
    async def serve_ui():
        """Serve the main UI page."""
        index_path = static_dir / "index.html"
        
        if index_path.exists():
            return HTMLResponse(
                content=index_path.read_text(),
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )
        else:
            return HTMLResponse(
                content="""
                <html>
                <head><title>PyClaw Control Console</title></head>
                <body>
                    <h1>PyClaw Control Console</h1>
                    <p>Static files not found. Please ensure the webui/static directory exists.</p>
                </body>
                </html>
                """
            )
    
    # Mount the router
    fastapi_app.include_router(ui_router)
    logger.info("Mounted Web UI routes")
