"""
Configuration Schema for PyClaw

Aligned with OpenClaw configuration format for zero-cost migration.
Models use providers→models structure, Agents use defaults+list+bindings,
Secrets use providers with multiple sources (env/file/exec).
"""

import ipaddress
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, SecretStr

class ConfigError(Exception):
    """Exception raised for configuration errors."""
    pass

# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

class GatewayAuthConfig(BaseModel):
    """Authentication configuration for the gateway."""
    mode: str = Field(default="token", description="Authentication mode")
    token: SecretStr = Field(..., min_length=1, description="Authentication token")

    @field_validator('token')
    @classmethod
    def reject_empty_token(cls, v: SecretStr) -> SecretStr:
        token_value = v.get_secret_value()
        if not token_value or not token_value.strip():
            raise ValueError('Token cannot be empty or whitespace-only')
        return v


class GatewayHttpChatCompletionsConfig(BaseModel):
    """配置 /v1/chat/completions 端点 - 对齐 OpenClaw"""
    model_config = ConfigDict(populate_by_name=True)
    
    enabled: bool = Field(default=True, description="启用 chatCompletions 端点")
    max_body_bytes: int = Field(default=20 * 1024 * 1024, alias="maxBodyBytes", description="请求体大小限制")
    max_image_parts: int = Field(default=8, alias="maxImageParts", description="最大图片数")


class GatewayHttpEndpointsConfig(BaseModel):
    """Gateway HTTP 端点配置"""
    model_config = ConfigDict(populate_by_name=True)
    
    chat_completions: GatewayHttpChatCompletionsConfig = Field(
        default_factory=GatewayHttpChatCompletionsConfig,
        alias="chatCompletions",
        description="chatCompletions 端点配置"
    )


class GatewayHttpConfig(BaseModel):
    """Gateway HTTP 配置"""
    endpoints: GatewayHttpEndpointsConfig = Field(
        default_factory=GatewayHttpEndpointsConfig,
        description="HTTP 端点配置"
    )


class GatewayConfig(BaseModel):
    """Gateway server configuration."""
    host: str = Field(default="127.0.0.1", description="Gateway host address")
    port: int = Field(default=18789, description="Gateway port")
    auth: GatewayAuthConfig = Field(..., description="Authentication configuration")
    http: GatewayHttpConfig = Field(default_factory=GatewayHttpConfig, description="HTTP 配置")
    verbose: bool = Field(default=False, description="Enable verbose logging")

    @field_validator('host')
    @classmethod
    def validate_loopback_address(cls, v: str) -> str:
        try:
            addr = ipaddress.ip_address(v)
            if not addr.is_loopback:
                raise ValueError('Gateway host must be a loopback address (127.0.0.0/8 or ::1)')
        except ValueError as e:
            raise ValueError(f'Invalid IP address: {e}')
        return v

# ---------------------------------------------------------------------------
# LLM  (unified provider → models structure)
# ---------------------------------------------------------------------------

class LLMModelEntry(BaseModel):
    """A single model within an LLM provider."""
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., description="Model identifier, e.g. 'qwen3.5-plus'")
    max_tokens: int = Field(default=4096, alias="maxTokens", description="Maximum tokens to generate")
    temperature: float = Field(default=0.7, description="Generation temperature")
    context_window: int = Field(default=32768, alias="contextWindow", description="Context window size")

class LLMProviderEntry(BaseModel):
    """Configuration for a single LLM provider (e.g. local, nvidia)."""
    model_config = ConfigDict(populate_by_name=True)

    type: str = Field(
        default="openai_compatible",
        description="Provider protocol type: 'ollama' or 'openai_compatible'",
    )
    base_url: str = Field(
        default="",
        alias="baseUrl",
        description="API base URL (OpenAI-compatible endpoint)",
    )
    api_key: Optional[str] = Field(
        default=None,
        alias="apiKey",
        description="API key (not required for ollama)",
    )
    models: List[LLMModelEntry] = Field(
        default_factory=list,
        description="Available models for this provider",
    )
    timeout: float = Field(default=300.0, description="Request timeout in seconds")

class UnifiedLLMConfig(BaseModel):
    """Unified LLM configuration with named providers."""
    model_config = ConfigDict(populate_by_name=True)

    default: str = Field(
        default="local",
        description="Default provider name to use",
    )
    providers: Dict[str, LLMProviderEntry] = Field(
        default_factory=dict,
        description="Named LLM providers (e.g. local,nvidia)",
    )

    def get_local_base_url(self) -> str:
        """Return the base URL for the 'local' LLM provider.

        Falls back to DEFAULT_LOCAL_LLM_BASE_URL if the 'local' provider
        is not configured or has no base_url set.
        """
        from pyclaw.constants import DEFAULT_LOCAL_LLM_BASE_URL

        local_provider = self.providers.get("local")
        if local_provider and local_provider.base_url:
            return local_provider.base_url
        return DEFAULT_LOCAL_LLM_BASE_URL

# ---------------------------------------------------------------------------
# Channels  (DingTalk, WeChat, Feishu - Extensible)
# ---------------------------------------------------------------------------

ALLOWED_CHANNELS = frozenset({"dingtalk", "wechat", "feishu"})


class DingTalkConnectorConfig(BaseModel):
    """钉钉 Stream 模式配置 - 对齐 OpenClaw dingtalk-connector"""
    model_config = ConfigDict(populate_by_name=True)
    
    enabled: bool = Field(default=False, description="启用钉钉通道")
    client_id: str = Field(default="", alias="clientId", description="钉钉 Client ID (AppKey)")
    client_secret: SecretStr = Field(default=SecretStr(""), alias="clientSecret", description="钉钉 Client Secret (AppSecret)")
    gateway_token: Optional[str] = Field(default=None, alias="gatewayToken", description="Gateway 认证 token")
    gateway_password: Optional[str] = Field(default=None, alias="gatewayPassword", description="Gateway 认证密码（与 token 二选一）")
    session_timeout: int = Field(default=1800000, alias="sessionTimeout", description="会话超时(ms)，默认 30 分钟")


class WeChatConnectorConfig(BaseModel):
    """微信通道配置 (预留)"""
    model_config = ConfigDict(populate_by_name=True)
    
    enabled: bool = Field(default=False, description="启用微信通道")
    app_id: str = Field(default="", alias="appId", description="微信 AppID")
    app_secret: SecretStr = Field(default=SecretStr(""), alias="appSecret", description="微信 AppSecret")
    token: str = Field(default="", description="消息校验 Token")
    encoding_aes_key: str = Field(default="", alias="encodingAesKey", description="消息加解密密钥")


class FeishuConnectorConfig(BaseModel):
    """飞书通道配置 (预留)"""
    model_config = ConfigDict(populate_by_name=True)
    
    enabled: bool = Field(default=False, description="启用飞书通道")
    app_id: str = Field(default="", alias="appId", description="飞书 App ID")
    app_secret: SecretStr = Field(default=SecretStr(""), alias="appSecret", description="飞书 App Secret")
    verification_token: str = Field(default="", alias="verificationToken", description="事件订阅验证 token")
    encrypt_key: str = Field(default="", alias="encryptKey", description="事件加密密钥")


# 兼容旧版配置
class DingTalkChannelConfig(BaseModel):
    """Configuration for DingTalk channel (legacy format)."""
    enabled: bool = Field(default=False, description="Enable DingTalk channel")
    app_key: Optional[str] = Field(default=None, description="DingTalk app key")
    app_secret: Optional[SecretStr] = Field(default=None, description="DingTalk app secret")
    agent_id: Optional[str] = Field(default=None, description="DingTalk agent ID")
    dm_policy: str = Field(default="self_only", description="Direct message policy")


class ChannelsConfig(BaseModel):
    """Configuration for communication channels."""
    model_config = ConfigDict(populate_by_name=True)
    
    # OpenClaw 兼容格式 (推荐)
    dingtalk_connector: DingTalkConnectorConfig = Field(
        default_factory=DingTalkConnectorConfig,
        alias="dingtalk-connector",
        description="钉钉 Stream 模式配置"
    )
    wechat_connector: WeChatConnectorConfig = Field(
        default_factory=WeChatConnectorConfig,
        alias="wechat-connector",
        description="微信通道配置"
    )
    feishu_connector: FeishuConnectorConfig = Field(
        default_factory=FeishuConnectorConfig,
        alias="feishu-connector",
        description="飞书通道配置"
    )
    
    # 旧版格式 (兼容)
    dingtalk: DingTalkChannelConfig = Field(default_factory=DingTalkChannelConfig)

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

class SandboxConfig(BaseModel):
    """Sandbox configuration for code execution."""
    mode: str = Field(default="docker", description="Sandbox mode (docker, none)")
    docker_image: str = Field(default="python:3.11-slim", description="Docker image for sandbox")
    timeout_seconds: int = Field(default=300, description="Execution timeout in seconds")
    memory_limit_mb: int = Field(default=512, description="Memory limit in MB")
    network_disabled: bool = Field(default=True, description="Disable network in sandbox")

class SecurityConfig(BaseModel):
    """Security configuration."""
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    credential_redaction_enabled: bool = Field(default=True, description="Redact credentials in logs")
    allowed_channels: List[str] = Field(default_factory=lambda: ["dingtalk"], description="Allowed channels")
    plugin_allowlist_only: bool = Field(default=True, description="Only allow whitelisted plugins")
    browser_office_block_enabled: bool = Field(default=True, description="Block browser/office operations")
    mandatory_security_skill: bool = Field(default=True, description="Require security skill")
    data_ttl_days: int = Field(default=30, description="Data retention time in days")

    @field_validator('allowed_channels')
    @classmethod
    def validate_allowed_channels(cls, v: List[str]) -> List[str]:
        for channel in v:
            if channel not in ALLOWED_CHANNELS:
                raise ValueError(f'Channel "{channel}" is not allowed. Allowed channels: {ALLOWED_CHANNELS}')
        return v

# ---------------------------------------------------------------------------
# Agents  (aligned with OpenClaw defaults + list + bindings)
# ---------------------------------------------------------------------------

class AgentModelRef(BaseModel):
    """Model reference for an agent — supports primary + fallbacks."""
    primary: str = Field(..., description="Primary model id, e.g. 'openai/gpt-4'")
    fallbacks: List[str] = Field(default_factory=list, description="Fallback model ids")

class MemorySearchRemoteConfig(BaseModel):
    """Remote endpoint configuration for memory search."""
    model_config = ConfigDict(populate_by_name=True)

    base_url: str = Field(default="", alias="baseUrl", description="Remote base URL")
    api_key: str = Field(default="", alias="apiKey", description="Remote API key")
    batch: Dict[str, Any] = Field(
        default_factory=lambda: {"enabled": False, "concurrency": 2},
        description="Batch processing settings",
    )

class MemorySearchMMRConfig(BaseModel):
    """Maximal Marginal Relevance configuration."""
    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = Field(default=False, description="Enable MMR re-ranking")
    lambda_: float = Field(default=0.7, alias="lambda", description="MMR lambda parameter")

class MemorySearchTemporalDecayConfig(BaseModel):
    """Temporal decay configuration for memory search scoring."""
    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = Field(default=False, description="Enable temporal decay")
    half_life_days: int = Field(default=30, alias="halfLifeDays", description="Half-life in days")

class MemorySearchHybridConfig(BaseModel):
    """Hybrid search configuration combining vector and text search."""
    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = Field(default=True, description="Enable hybrid search")
    vector_weight: float = Field(default=0.7, alias="vectorWeight", description="Vector search weight")
    text_weight: float = Field(default=0.3, alias="textWeight", description="Text search weight")
    mmr: MemorySearchMMRConfig = Field(default_factory=MemorySearchMMRConfig, description="MMR settings")
    temporal_decay: MemorySearchTemporalDecayConfig = Field(
        default_factory=MemorySearchTemporalDecayConfig,
        alias="temporalDecay",
        description="Temporal decay settings",
    )

class MemorySearchQueryConfig(BaseModel):
    """Query-level configuration for memory search."""
    model_config = ConfigDict(populate_by_name=True)

    max_results: int = Field(default=6, alias="maxResults", description="Maximum search results")
    min_score: float = Field(default=0.15, alias="minScore", description="Minimum relevance score")
    hybrid: MemorySearchHybridConfig = Field(
        default_factory=MemorySearchHybridConfig, description="Hybrid search settings"
    )

class MemorySearchConfig(BaseModel):
    """Top-level memory search configuration — aligned with OpenClaw format."""
    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = Field(default=False, description="Enable memory search")
    provider: str = Field(default="openai", description="Embedding provider")
    model: str = Field(default="text-embedding-3-small", description="Embedding model")
    remote: MemorySearchRemoteConfig = Field(
        default_factory=MemorySearchRemoteConfig, description="Remote endpoint settings"
    )
    query: MemorySearchQueryConfig = Field(
        default_factory=MemorySearchQueryConfig, description="Query settings"
    )
    chunking: Dict[str, int] = Field(
        default_factory=lambda: {"tokens": 400, "overlap": 80},
        description="Chunking settings",
    )

class AgentDefaults(BaseModel):
    """Default settings applied to all agents unless overridden."""
    model_config = ConfigDict(populate_by_name=True)

    model: Optional[AgentModelRef] = Field(default=None, description="Default model reference")
    thinking_default: str = Field(default="medium", alias="thinkingDefault", description="Default thinking level")
    reasoning_default: str = Field(default="on", alias="reasoningDefault", description="Default reasoning mode")
    memory_search: MemorySearchConfig = Field(
        default_factory=MemorySearchConfig, alias="memorySearch", description="Memory search configuration"
    )

class AgentEntry(BaseModel):
    """Configuration for a single agent — aligned with OpenClaw format."""
    id: str = Field(..., description="Agent identifier")
    default: bool = Field(default=False, description="Whether this is the default agent")
    name: str = Field(..., description="Human-readable agent name")
    model: Optional[AgentModelRef] = Field(default=None, description="Model reference override")
    system_prompt: Optional[str] = Field(default=None, alias="systemPrompt", description="System prompt")
    skills: List[str] = Field(default_factory=list, description="Available skills")
    tools: List[str] = Field(default_factory=list, description="Available tools")
    sandbox_override: Optional[SandboxConfig] = Field(default=None, description="Override sandbox settings")
    max_turns: int = Field(default=50, alias="maxTurns", description="Maximum conversation turns")

    model_config = {"populate_by_name": True}

class BindingMatch(BaseModel):
    """Match criteria for a binding rule."""
    channel: Optional[str] = Field(default=None, description="Channel name to match")
    peer_id: Optional[str] = Field(default=None, alias="peerId", description="Peer ID to match (exact user)")
    peer_kind: Optional[str] = Field(default=None, alias="peerKind", description="Peer kind to match (private/group)")
    account_id: Optional[str] = Field(default=None, alias="accountId", description="Account ID to match")

    model_config = {"populate_by_name": True}

class Binding(BaseModel):
    """Routing binding — maps channels/accounts to agents."""
    type: str = Field(default="route", description="Binding type")
    agent_id: str = Field(..., alias="agentId", description="Target agent id")
    match: BindingMatch = Field(default_factory=BindingMatch, description="Match criteria")

    model_config = {"populate_by_name": True}

class AgentsConfig(BaseModel):
    """Configuration for agents — aligned with OpenClaw format."""
    defaults: AgentDefaults = Field(default_factory=AgentDefaults, description="Default agent settings")
    list: List[AgentEntry] = Field(default_factory=list, description="Agent definitions")

# ---------------------------------------------------------------------------
# Secrets  (aligned with OpenClaw providers multi-source)
# ---------------------------------------------------------------------------

class SecretProviderConfig(BaseModel):
    """Configuration for a single secret provider."""
    source: str = Field(..., description="Secret source type: env, file, exec")
    path: Optional[str] = Field(default=None, description="File path (for 'file' source)")
    mode: Optional[str] = Field(default=None, description="File mode: json, dotenv (for 'file' source)")
    command: Optional[str] = Field(default=None, description="Command to execute (for 'exec' source)")
    args: List[str] = Field(default_factory=list, description="Command arguments (for 'exec' source)")
    timeout_ms: int = Field(default=5000, alias="timeoutMs", description="Timeout in milliseconds")

    model_config = {"populate_by_name": True}

class SecretsConfig(BaseModel):
    """Secrets management — aligned with OpenClaw format."""
    providers: Dict[str, SecretProviderConfig] = Field(
        default_factory=lambda: {"default": SecretProviderConfig(source="env")},
        description="Secret providers",
    )

# ---------------------------------------------------------------------------
# Memory / Logging / Sessions / Cron
# ---------------------------------------------------------------------------

class MemoryConfig(BaseModel):
    """Memory configuration for conversation history.

    Accepts both flat fields (``embedding_model``) and the nested
    ``pyclaw.json`` format::

        "memory": {
          "embedding": { "provider": "openai", "model": "...", "baseUrl": "..." },
          "query": { "maxResults": 6, ... },
          "chunking": { "tokens": 400, "overlap": 80 }
        }

    A ``model_validator`` flattens the nested dict into the flat fields
    so that the rest of the codebase can use ``memory_config.embedding_model``
    etc. without caring about the JSON shape.
    """
    enabled: bool = Field(default=True, description="Enable memory")
    embedding_provider: str = Field(default="openai", description="Embedding provider")
    embedding_model: str = Field(default="text-embedding-ada-002", description="Embedding model")
    embedding_base_url: str = Field(default="", description="Base URL for embedding API")
    embedding_api_key: str = Field(default="", description="API key for embedding provider")
    embedding_dimensions: int = Field(default=1536, description="Embedding dimensions")
    search_top_k: int = Field(default=5, description="Top K results for search")
    bm25_weight: float = Field(default=0.5, ge=0.0, le=1.0, description="BM25 weight")
    vector_weight: float = Field(default=0.5, ge=0.0, le=1.0, description="Vector weight")

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode='before')
    @classmethod
    def flatten_nested_config(cls, data: Any) -> Any:
        """Flatten nested pyclaw.json memory config into flat fields."""
        if not isinstance(data, dict):
            return data

        # Flatten "embedding" sub-object
        embedding = data.pop("embedding", None)
        if isinstance(embedding, dict):
            if "provider" in embedding and "embedding_provider" not in data:
                data["embedding_provider"] = embedding["provider"]
            if "model" in embedding and "embedding_model" not in data:
                data["embedding_model"] = embedding["model"]
            if "baseUrl" in embedding and "embedding_base_url" not in data:
                data["embedding_base_url"] = embedding["baseUrl"]
            if "apiKey" in embedding and "embedding_api_key" not in data:
                data["embedding_api_key"] = embedding["apiKey"]
            if "dimensions" in embedding and "embedding_dimensions" not in data:
                data["embedding_dimensions"] = embedding["dimensions"]

        # Flatten "query" sub-object
        query = data.pop("query", None)
        if isinstance(query, dict):
            if "maxResults" in query and "search_top_k" not in data:
                data["search_top_k"] = query["maxResults"]
            hybrid = query.get("hybrid", {})
            if isinstance(hybrid, dict):
                if "vectorWeight" in hybrid and "vector_weight" not in data:
                    data["vector_weight"] = hybrid["vectorWeight"]
                if "textWeight" in hybrid and "bm25_weight" not in data:
                    data["bm25_weight"] = hybrid["textWeight"]

        # Ignore "chunking" for now (not used by MemoryConfig fields)
        data.pop("chunking", None)

        return data

class LoggingConfig(BaseModel):
    """Logging configuration."""
    level: str = Field(default="INFO", description="Log level")
    audit_enabled: bool = Field(default=True, description="Enable audit logging")
    redact_secrets: bool = Field(default=True, description="Redact secrets in logs")
    log_to_file: bool = Field(default=True, description="Log to file")
    max_log_size_mb: int = Field(default=10, description="Maximum log file size in MB")
    max_log_files: int = Field(default=5, description="Maximum number of log files")

class SessionsConfig(BaseModel):
    """Sessions configuration for conversation management."""
    enabled: bool = Field(default=True, description="Enable session management")
    db_path: str = Field(default="data/sessions.db", description="Path to sessions database")
    max_age_days: int = Field(default=30, description="Maximum session age in days")
    max_transcript_entries: int = Field(default=1000, description="Maximum transcript entries")

class CronConfig(BaseModel):
    """Cron job configuration."""
    data_cleanup_enabled: bool = Field(default=True, description="Enable data cleanup")
    data_cleanup_cron: str = Field(default="0 2 * * *", description="Cron schedule for cleanup")
    health_check_enabled: bool = Field(default=True, description="Enable health check")
    health_check_cron: str = Field(default="*/5 * * * *", description="Cron schedule for health check")

# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------

class PyClawConfig(BaseModel):
    """Main PyClaw configuration — aligned with OpenClaw format."""
    gateway: GatewayConfig = Field(..., description="Gateway configuration")
    llm: UnifiedLLMConfig = Field(default_factory=UnifiedLLMConfig, description="LLM provider configuration")
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig, description="Channels configuration")
    agents: AgentsConfig = Field(default_factory=AgentsConfig, description="Agents configuration")
    bindings: List[Binding] = Field(default_factory=list, description="Routing bindings")
    security: SecurityConfig = Field(default_factory=SecurityConfig, description="Security configuration")
    secrets: SecretsConfig = Field(default_factory=SecretsConfig, description="Secrets configuration")
    memory: MemoryConfig = Field(default_factory=MemoryConfig, description="Memory configuration")
    logging: LoggingConfig = Field(default_factory=LoggingConfig, description="Logging configuration")
    sessions: SessionsConfig = Field(default_factory=SessionsConfig, description="Sessions configuration")
    cron: CronConfig = Field(default_factory=CronConfig, description="Cron configuration")

    @model_validator(mode='after')
    def validate_config(self) -> 'PyClawConfig':
        """Cross-validate configuration dependencies."""
        # Collect all model ids from providers
        all_model_ids: set[str] = set()
        for provider_name, provider in self.llm.providers.items():
            for model_entry in provider.models:
                all_model_ids.add(model_entry.id)

        # Validate that at least one agent is marked as default
        default_agents = [agent for agent in self.agents.list if agent.default]
        if self.agents.list and not default_agents:
            raise ValueError('At least one agent must be marked as default')

        # Validate agent model references exist in providers
        for agent in self.agents.list:
            model_ref = agent.model or self.agents.defaults.model
            if model_ref and all_model_ids:
                if model_ref.primary not in all_model_ids:
                    raise ValueError(
                        f'Agent "{agent.id}" references unknown model "{model_ref.primary}". '
                        f'Available models: {sorted(all_model_ids)}'
                    )

        # Validate bindings reference existing agents
        agent_ids = {agent.id for agent in self.agents.list}
        for binding in self.bindings:
            if agent_ids and binding.agent_id not in agent_ids:
                raise ValueError(
                    f'Binding references unknown agent "{binding.agent_id}". '
                    f'Available agents: {sorted(agent_ids)}'
                )
            if binding.match.channel and binding.match.channel not in ALLOWED_CHANNELS:
                raise ValueError(
                    f'Binding references unknown channel "{binding.match.channel}"'
                )

        return self
