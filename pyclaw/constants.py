"""Project-wide constants for PyClaw."""

# ── Network ────────────────────────────────────────────────────────────
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18789

# ── Timeouts (seconds) ─────────────────────────────────────────────────
DEFAULT_TIMEOUT_SECONDS = 300
AGENT_TIMEOUT_SECONDS = 900

# ── LLM ────────────────────────────────────────────────────────────────
DEFAULT_LOCAL_LLM_BASE_URL = "http://localhost:11434/v1"
LLM_MAX_RETRIES = 3
LLM_RETRY_DELAY_SECONDS = 5.0
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 2048

# ── Memory ─────────────────────────────────────────────────────────────
DEFAULT_CHUNK_SIZE = 1600
DEFAULT_CHUNK_OVERLAP = 320
DEFAULT_MAX_FEATURES = 100
MEMORY_CONTENT_PREVIEW_LENGTH = 500

# ── Cache ──────────────────────────────────────────────────────────────
CONTEXT_CACHE_MAX_SIZE = 100
CONTEXT_CACHE_TTL_SECONDS = 3600

# ── HTTP Status Codes ──────────────────────────────────────────────────
HTTP_401_UNAUTHORIZED = 401
HTTP_429_TOO_MANY_REQUESTS = 429
HTTP_500_INTERNAL_SERVER_ERROR = 500
HTTP_503_SERVICE_UNAVAILABLE = 503

# ── Agent Runtime ──────────────────────────────────────────────────────
MAX_HISTORY_MESSAGES = 20
HISTORY_CONTENT_MAX_CHARS = 500
ERROR_MESSAGE_PREFIXES = (
    "\u23f3 Task execution timed out",
    "Sorry, an error occurred",
    "Sorry, I am temporarily unable to respond",
    "Error:",
)

# ── Skill Auto-Execution ───────────────────────────────────────────────
SKILL_AUTO_EXECUTE_MARKER = "__AUTO_EXECUTE__"
MAX_SKILL_CONTENT_LENGTH = 2000
SKILL_SEARCH_DEFAULT_LIMIT = 20
SKILL_LIST_DEFAULT_LIMIT = 50
SKILL_SEARCH_MAX_LIMIT = 100

# ── Shell ──────────────────────────────────────────────────────────────
DEFAULT_SHELL_TIMEOUT = 30
SKILL_SHELL_TIMEOUT = 120

# ── Channels ───────────────────────────────────────────────────────────
DINGTALK_CLIENT_TIMEOUT = 600
WECHAT_PERSONAL_LOGIN_TIMEOUT_SECONDS = 60

# ── Cron Scheduler ─────────────────────────────────────────────────────
# Grace period for missed cron jobs (e.g. after macOS sleep/wake).
# Jobs missed within this window will still execute once (coalesced).
CRON_MISFIRE_GRACE_TIME_SECONDS = 300
CRON_EXECUTOR_MAX_WORKERS = 10

# ── Gateway URL ────────────────────────────────────────────────────────
DEFAULT_GATEWAY_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"

# ── HTTP Client Timeouts ───────────────────────────────────────────────
HTTP_TOOL_TIMEOUT_SECONDS = 120.0
HTTP_TOOL_CONNECT_TIMEOUT_SECONDS = 30.0
SHELL_TOOL_LONG_TIMEOUT_SECONDS = 600.0
EMBEDDING_TIMEOUT_SECONDS = 120.0
EMBEDDING_CONNECT_TIMEOUT_SECONDS = 10.0
SKILLHUB_API_TIMEOUT_SECONDS = 15.0
SKILLHUB_INSTALL_TIMEOUT_SECONDS = 120
SKILLHUB_DOWNLOAD_TIMEOUT_SECONDS = 60.0