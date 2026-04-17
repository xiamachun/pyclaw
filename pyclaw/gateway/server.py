"""Gateway 服务器。"""

import logging
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from pyclaw.config.schema import PyClawConfig
from pyclaw.constants import CONTEXT_CACHE_MAX_SIZE, CONTEXT_CACHE_TTL_SECONDS
from pyclaw.gateway.auth import TokenAuthMiddleware
from pyclaw.gateway.routes import (
    create_channels_router,
    create_config_router,
    create_cron_router,
    create_health_router,
    create_sessions_router,
    create_skills_router,
)
from pyclaw.gateway.openai_compat import create_chat_completions_router
from pyclaw.gateway.dingtalk_routes import create_dingtalk_router
from pyclaw.gateway.routes_extended import (
    create_tasks_router,
    create_approvals_router,
    create_costs_router,
    create_hooks_router,
    create_context_cache_router,
    create_failover_router,
)
from pyclaw.gateway.websocket import WebSocketManager, handle_websocket
from pyclaw.sessions.manager import SessionManager
from pyclaw.sessions.store import SessionStore
from pyclaw.memory.store import MemoryStore
from pyclaw.memory.manager import MemoryManager
from pyclaw.memory.embeddings import OpenAIEmbeddingProvider, LocalEmbeddingProvider
from pyclaw.agents.workspace import ensure_workspace, load_workspace_files, build_workspace_system_prompt
from pyclaw.config.paths import get_paths as _get_paths

logger = logging.getLogger(__name__)

def _configure_logging() -> None:
    """Configure logging so that all pyclaw modules write to gateway.log."""
    log_path = str(_get_paths().gateway_log)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )

    pyclaw_logger = logging.getLogger("pyclaw")
    pyclaw_logger.setLevel(logging.DEBUG)
    if not pyclaw_logger.handlers:
        pyclaw_logger.addHandler(file_handler)

def create_gateway_app(config: PyClawConfig) -> FastAPI:
    """Create Gateway FastAPI application.

    Args:
        config: Configuration object

    Returns:
        FastAPI application instance
    """
    _configure_logging()
    app = FastAPI(title="PyClaw Gateway", version="0.1.0")

    # Add CORS middleware (only localhost)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:*", "http://127.0.0.1:*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Add authentication middleware
    app.add_middleware(TokenAuthMiddleware, config=config)

    # Initialize session storage and manager (expand ~ path)
    db_path = os.path.expanduser(config.sessions.db_path)
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    session_store = SessionStore(db_path=db_path)
    session_manager = SessionManager(store=session_store, config=config)

    # --- Third-layer memory system initialization ---
    # Second layer: Ensure workspace directory structure exists
    _paths = _get_paths()
    workspace_dir = str(_paths.workspace_dir)
    ensure_workspace(workspace_dir)

    # Third layer: Initialize MemoryStore + EmbeddingProvider + MemoryManager
    # Use config.memory for memory configuration (new structure)
    memory_config = config.memory
    memory_db_path = str(_paths.memory_db)
    os.makedirs(os.path.dirname(memory_db_path), exist_ok=True)
    memory_store = MemoryStore(db_path=memory_db_path)

    # Determine embedding provider based on memory configuration
    # Priority: pyclaw.json config > environment variable fallback > local TF-IDF
    if memory_config.enabled:
        embed_api_key = memory_config.embedding_api_key or os.environ.get("OPENAI_API_KEY", "")
        embed_base_url = memory_config.embedding_base_url or os.environ.get("OPENAI_BASE_URL", "")

        if embed_api_key or embed_base_url:
            embedding_provider = OpenAIEmbeddingProvider(
                api_key=embed_api_key or "ollama",
                model=memory_config.embedding_model,
                base_url=embed_base_url or None,
            )
        else:
            embedding_provider = LocalEmbeddingProvider()
    else:
        embedding_provider = LocalEmbeddingProvider()

    memory_manager = MemoryManager(
        store=memory_store,
        config={
            "bm25_weight": memory_config.bm25_weight,
            "vector_weight": memory_config.vector_weight,
        },
        embedding_provider=embedding_provider,
    )

    # Initialize WebSocket manager
    ws_manager = WebSocketManager()

    # Initialize Agent Runtime
    from pyclaw.agents.tools import ToolRegistry, register_builtin_tools
    from pyclaw.agents.models import ModelSelector
    from pyclaw.agents.runtime import AgentRuntime

    tool_registry = ToolRegistry()
    register_builtin_tools(tool_registry)

    # Register skill tools from ~/.pyclaw/workspace/skills/
    from pyclaw.skills.tool_adapter import register_skill_tools
    skills_count = register_skill_tools(tool_registry, _paths.workspace_skills_dir)
    logging.info("Registered %d skill tools from %s", skills_count, _paths.workspace_skills_dir)

    # Build models config dict for ModelSelector
    # ModelSelector expects {"models": {name: {provider, base_url, ...}}, "default_model": name}
    inner_models = {}
    first_model_name = None
    default_provider_name = config.llm.default

    for provider_name, provider_entry in config.llm.providers.items():
        for model_entry in provider_entry.models:
            inner_models[model_entry.id] = {
                "provider": provider_name,
                "provider_type": provider_entry.type,
                "base_url": provider_entry.base_url,
                "api_key": provider_entry.api_key,
                "max_tokens": model_entry.max_tokens,
                "temperature": model_entry.temperature,
                "timeout": provider_entry.timeout,
            }
            if first_model_name is None:
                first_model_name = model_entry.id

    # Use first model from default provider as default
    default_provider = config.llm.providers.get(default_provider_name)
    if default_provider and default_provider.models:
        default_model_name = default_provider.models[0].id
    else:
        default_model_name = first_model_name

    logging.info("LLM providers: %s, default=%s/%s", list(config.llm.providers.keys()), default_provider_name, default_model_name)

    models_dict = {"models": inner_models}
    if default_model_name:
        models_dict["default_model"] = default_model_name

    model_selector = ModelSelector(models_config=models_dict)
    agent_runtime = AgentRuntime(
        config={"workspace": str(_get_paths().workspace_dir)},
        tool_registry=tool_registry,
        model_selector=model_selector,
    )

    # Store to app state for use in routes
    app.state.session_manager = session_manager
    app.state.ws_manager = ws_manager
    app.state.config = config
    app.state.agent_runtime = agent_runtime
    app.state.memory_store = memory_store
    app.state.memory_manager = memory_manager
    app.state.embedding_provider = embedding_provider
    app.state.workspace_dir = workspace_dir
    app.state.memory_config = memory_config

    # Global model preference (updated by WebSocket set_model, read by HTTP API)
    app.state.global_model_pref = {
        "model": default_model_name,
        "platform": default_provider_name,
    }

    # Register routes
    app.include_router(create_health_router())
    app.include_router(create_config_router(config, app))
    app.include_router(create_sessions_router(session_manager))
    app.include_router(create_channels_router())
    app.include_router(create_skills_router(config))
    app.include_router(create_cron_router())
    
    # Register OpenAI compatible chat completions endpoints
    if config.gateway.http.endpoints.chat_completions.enabled:
        app.include_router(create_chat_completions_router(config, agent_runtime))
        logger.info("Chat completions endpoint enabled at /v1/chat/completions")
    
    # Register DingTalk routes
    if hasattr(config.channels, 'dingtalk_connector') and config.channels.dingtalk_connector.enabled:
        app.include_router(create_dingtalk_router(config, agent_runtime))
        logger.info("DingTalk channel routes enabled at /api/dingtalk")

    # Register extended feature routes
    app.include_router(create_tasks_router())
    app.include_router(create_approvals_router())
    app.include_router(create_costs_router())
    app.include_router(create_hooks_router())
    app.include_router(create_context_cache_router())
    app.include_router(create_failover_router())
    logger.info("Extended feature routes enabled (tasks, approvals, costs, hooks, cache, failover)")

    # Mount Web UI
    from pyclaw.webui.app import mount_webui
    mount_webui(app)

    # Register WebSocket endpoint
    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket endpoint."""
        await handle_websocket(
            websocket=websocket,
            session_manager=session_manager,
            ws_manager=ws_manager,
            agent_runtime=agent_runtime,
            config=config,
        )

    # Startup event
    @app.on_event("startup")
    async def startup_event():
        """Application startup event."""
        logger.info("Initializing PyClaw Gateway...")
        await session_store.initialize()
        logger.info("Session store initialized")

        # Initialize Memory Store (SQLite)
        await memory_store.initialize()
        logger.info("Memory store initialized")

        # Initialize task registry
        from pyclaw.tasks.registry import TaskRegistry
        task_db_path = str(_get_paths().tasks_db)
        os.makedirs(os.path.dirname(task_db_path), exist_ok=True)
        task_registry = TaskRegistry(db_path=task_db_path)
        await task_registry.initialize()
        app.state.task_registry = task_registry
        logger.info("Task registry initialized")

        # Initialize hook registry
        from pyclaw.hooks.handlers import HookRegistry
        hook_registry = HookRegistry()
        app.state.hook_registry = hook_registry
        logger.info("Hook registry initialized")

        # Initialize approval manager
        from pyclaw.infra.approvals import ApprovalManager
        approval_db_path = str(_get_paths().approvals_db)
        approval_manager = ApprovalManager(db_path=approval_db_path)
        await approval_manager.initialize()
        app.state.approval_manager = approval_manager
        logger.info("Approval manager initialized")

        # Initialize cost tracker
        from pyclaw.sessions.cost_tracker import CostTracker
        cost_tracker = CostTracker()
        app.state.cost_tracker = cost_tracker
        logger.info("Cost tracker initialized")

        # Initialize context cache
        from pyclaw.agents.context_cache import ContextCache
        context_cache = ContextCache(max_size=CONTEXT_CACHE_MAX_SIZE, ttl_seconds=CONTEXT_CACHE_TTL_SECONDS)
        app.state.context_cache = context_cache
        logger.info("Context cache initialized")

        # Start cron scheduler
        from pyclaw.cron.scheduler import get_scheduler
        cron_scheduler = get_scheduler()
        await cron_scheduler.start()
        app.state.cron_scheduler = cron_scheduler
        logger.info("Cron scheduler started")

        # Index memory directory (third-layer memory)
        if memory_config.enabled:
            from pyclaw.memory.indexer import index_memory_directory
            memory_dir = os.path.join(workspace_dir, "memory")
            try:
                stats = await index_memory_directory(
                    memory_dir=memory_dir,
                    memory_store=memory_store,
                    embedding_provider=embedding_provider,
                    model_name=memory_config.embedding_model,
                    chunking={"tokens": 400, "overlap": 80},
                )
                logger.info("Memory indexing complete: %s", stats)
            except Exception as index_error:
                logger.warning("Memory indexing failed (non-fatal): %s", index_error)

        logger.info("PyClaw Gateway started successfully")

    # Shutdown event
    @app.on_event("shutdown")
    async def shutdown_event():
        """Application shutdown event."""
        logger.info("Shutting down PyClaw Gateway...")
        # Stop cron scheduler
        if hasattr(app.state, 'cron_scheduler'):
            await app.state.cron_scheduler.stop()
            logger.info("Cron scheduler stopped")
        # Close task registry
        if hasattr(app.state, 'task_registry'):
            await app.state.task_registry.close()
            logger.info("Task registry closed")
        # Close approval manager
        if hasattr(app.state, 'approval_manager'):
            await app.state.approval_manager.close()
            logger.info("Approval manager closed")
        # Close Memory Store
        await memory_store.close()
        # Close Embedding Provider
        if hasattr(embedding_provider, "close"):
            await embedding_provider.close()
        logger.info("PyClaw Gateway shut down")

    return app


def create_app() -> FastAPI:
    """Zero-argument factory for uvicorn --factory.

    Loads configuration from the default pyclaw.json (or env overrides)
    and returns a fully configured FastAPI application.
    """
    from pyclaw.config.loader import load_config

    config = load_config()
    return create_gateway_app(config)


class GatewayServer:
    """Gateway server."""

    def __init__(self, config: PyClawConfig) -> None:
        """Initialize the server.

        Args:
            config: Configuration object
        """
        self.config = config
        self.app = create_gateway_app(config)

    async def start(self) -> None:
        """Start the server."""
        import uvicorn

        logger.info("Starting Gateway server on %s:%s", self.config.gateway.host, self.config.gateway.port)

        config = uvicorn.Config(
            app=self.app,
            host=self.config.gateway.host,
            port=self.config.gateway.port,
            log_level="info",
        )
        server = uvicorn.Server(config)
        await server.serve()

    async def stop(self) -> None:
        """Stop the server."""
        logger.info("Stopping Gateway server...")
        # uvicorn shutdown logic is handled in the shutdown event