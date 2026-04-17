# PyClaw

**Local-first personal AI assistant** — a Python reimplementation of [OpenClaw](https://github.com/openclaw/openclaw).

PyClaw runs on your own devices, connects to messaging channels you already use, executes tools (shell commands, file operations, browser automation), and maintains long-term memory — all locally.

## Features

- **Multi-Channel Support** — DingTalk, WeChat Work, WeChat (personal), Feishu, Slack, Telegram, and WebSocket
- **Skill System** — Markdown-based capability definitions (`SKILL.md`) for extensible workflows
- **Three-Layer Memory** — Session history + workspace prompts + vector knowledge base (hybrid BM25 + embeddings)
- **Tool Execution** — Shell commands, file read/write, web search, browser automation
- **Streaming Responses** — Real-time token streaming via WebSocket
- **Multi-Provider LLM** — Local (Ollama), DashScope, NVIDIA, and any OpenAI-compatible API
- **Task Scheduling** — Cron-based periodic tasks with APScheduler
- **Hot Reload** — Config file watcher with automatic reload on changes
- **Message Gating** — Command prefixes, @mention requirements, allow/deny lists per channel
- **DM Pairing** — Pairing-code based authorization for direct messages
- **Session Export** — Export conversations as Markdown or JSON
- **Usage Statistics** — Track sessions, messages, tokens, and costs
- **Security** — Token auth, loopback-only binding (`127.0.0.1`), configurable URL blocklist, credential redaction
- **Web UI** — Browser-based chat with agent management, usage stats, and settings

## Quick Start

### Option A: Local Mode (Ollama) — Recommended

No API key needed. Run a local LLM with [Ollama](https://ollama.ai):

```bash
# 1. Install Ollama and pull a model
ollama pull qwen3.5:9B

# 2. Clone and install PyClaw
git clone https://github.com/your-org/pyclaw.git
cd pyclaw
pip install -e .

# 3. Initialize config from sample
cp pyclaw.json.sample pyclaw.json
# Edit pyclaw.json — fill in your gateway token and model settings

# 4. Start the gateway
bash scripts/restart_pyclaw.sh
```

Open `http://127.0.0.1:18789` in your browser to start chatting.

> ⚠️ **Security Notice:** PyClaw binds to `127.0.0.1` (localhost only) by design. **Do not** expose the Gateway to the public internet via reverse proxy or port forwarding. Many API endpoints skip authentication for local convenience. If you need remote access, use SSH tunneling or a VPN.

### Option B: Remote API Mode

Use any OpenAI-compatible API endpoint:

```bash
# 1. Copy and edit config
cp pyclaw.json.sample pyclaw.json

# 2. Edit pyclaw.json — set your provider config:
#    - gateway.auth.token: your secret token
#    - models.providers: add your remote provider
#    - agents.defaults.model.primary: set to your model ID
```

### Configuration File (`pyclaw.json`)

PyClaw reads configuration from `pyclaw.json` in the **project root directory**. This file is `.gitignore`d to protect your credentials.

**Quick setup:**
```bash
cp pyclaw.json.sample pyclaw.json
```

**Required fields to fill in:**
- `gateway.auth.token` — Gateway authentication token (any string you choose)
- `llm.providers` — At least one LLM provider with base URL and models
- `llm.default` — Default provider name (e.g. `local`)

**Optional channel setup (DingTalk example):**
- `channels.dingtalk-connector.enabled` — Set to `true`
- `channels.dingtalk-connector.clientId` — Your DingTalk app key
- `channels.dingtalk-connector.clientSecret` — Your DingTalk app secret
- `channels.dingtalk-connector.gatewayToken` — Same as `gateway.auth.token`

> **Note:** All configuration is in `pyclaw.json`. No environment variables are needed for basic setup. The file `pyclaw.json.sample` contains all available options with placeholder values.

## Configuration

### `pyclaw.json` Structure

```jsonc
{
  "gateway": {
    "host": "127.0.0.1",       // Loopback only (security)
    "port": 18789,
    "auth": { "token": "..." } // Required
  },
  "llm": {
    "default": "local",        // Default provider name
    "providers": {
      "local": {
        "type": "ollama",
        "baseUrl": "http://localhost:11434/v1",
        "models": [
          { "id": "qwen3.5:9B", "maxTokens": 8192, "temperature": 0.7 }
        ]
      }
    }
  },
  "memory": {
    "embedding": {
      "provider": "openai",
      "model": "qwen3-embedding:latest",
      "baseUrl": "http://localhost:11434/v1"
    }
  },
  "channels": {
    "dingtalk-connector": { "enabled": false },
    "wechat-connector": { "enabled": false },
    "feishu-connector": { "enabled": false }
  },
  "agents": {
    "list": [
      { "id": "default", "default": true, "name": "Default Agent" }
    ]
  }
}
```

### Environment Variables (Optional Overrides)

Most configuration lives in `pyclaw.json`. These environment variables are only needed for advanced overrides:

| Variable | Description | Default |
|----------|-------------|---------|
| `PYCLAW_CONFIG_PATH` | Override config file path | `./pyclaw.json` |
| `PYCLAW_SECRET_KEY` | Encryption key for secret store | (auto-generated) |
| `PYCLAW_LOG_LEVEL` | Log level | `INFO` |

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        PyClaw System                            │
│                                                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐   │
│  │   CLI    │  │  Web UI  │  │ Channels │  │  Cron/Hooks  │   │
│  │ (click)  │  │(FastAPI) │  │(DingTalk │  │  (APScheduler│   │
│  │          │  │          │  │ WeChat   │  │             )│   │
│  │          │  │          │  │ Feishu)  │  │              │   │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └──────┬───────┘   │
│       └──────────────┴──────┬───────┴───────────────┘           │
│                             │                                   │
│                    ┌────────▼────────┐                          │
│                    │    Gateway      │                          │
│                    │  (WebSocket +   │                          │
│                    │   HTTP Server)  │                          │
│                    │  127.0.0.1:18789│                          │
│                    └────────┬────────┘                          │
│                             │                                   │
│       ┌─────────────────────┼─────────────────────┐            │
│       │                     │                     │            │
│  ┌────▼─────┐  ┌───────────▼──────────┐  ┌──────▼───────┐   │
│  │ Security │  │   Session Manager    │  │   Router     │   │
│  │  + Auth  │  │  (SQLite persist)    │  │  (Agent map) │   │
│  └──────────┘  └───────────┬──────────┘  └──────────────┘   │
│                             │                                   │
│                    ┌────────▼────────┐                          │
│                    │  Agent Runtime  │                          │
│                    │  (LLM + Tools)  │                          │
│                    └────────┬────────┘                          │
│                             │                                   │
│       ┌──────────┬──────────┼──────────┬──────────┐            │
│       │          │          │          │          │            │
│  ┌────▼───┐ ┌───▼────┐ ┌──▼───┐ ┌───▼────┐ ┌──▼─────┐     │
│  │ Memory │ │Process │ │Media │ │Browser │ │  TTS   │     │
│  │(Vector │ │Sandbox │ │Pipe  │ │(Play-  │ │(Edge/  │     │
│  │+BM25)  │ │(Docker)│ │line  │ │wright) │ │OpenAI) │     │
│  └────────┘ └────────┘ └──────┘ └────────┘ └────────┘     │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              Plugin & Skill System                        │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## Channel Setup

### DingTalk

1. Create a DingTalk bot application and get `clientId` / `clientSecret`
2. Configure in `pyclaw.json`:
   ```json
   {
     "channels": {
       "dingtalk-connector": {
         "enabled": true,
         "clientId": "your-client-id",
         "clientSecret": "your-client-secret"
       }
     }
   }
   ```

### WeChat Work (Enterprise)

Configure in `pyclaw.json`:
```json
{
  "channels": {
    "wechat-connector": {
      "enabled": true,
      "corpId": "your-corp-id",
      "agentId": "your-agent-id",
      "secret": "your-secret",
      "token": "your-token",
      "encodingAesKey": "your-encoding-aes-key"
    }
  }
}
```

### WeChat (Personal)

Uses `itchat-uos` for personal WeChat login via QR code scanning:

```bash
pip install itchat-uos
pyclaw wechat login
```

### Feishu

Configure in `pyclaw.json`:
```json
{
  "channels": {
    "feishu-connector": {
      "enabled": true,
      "appId": "your-app-id",
      "appSecret": "your-app-secret"
    }
  }
}
```

## Skill Development

Skills are Markdown files placed in `~/.pyclaw/workspace/skills/`:

```markdown
# my-skill

## Description
A custom skill that does something useful.

## Instructions
When the user asks about X, do Y using the shell tool.

## Scripts
```bash
#!/bin/bash
echo "Hello from my skill"
```​
```

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run with coverage
pytest --cov=pyclaw --cov-report=html

# Code formatting & linting
ruff format pyclaw/ tests/
ruff check pyclaw/ tests/

# Type checking
mypy pyclaw/

# Start in dev mode (verbose logging)
pyclaw gateway --port 18789 --verbose
```

## Project Structure

```
pyclaw/
├── pyclaw/
│   ├── agents/       # Agent runtime, model selection, tools
│   ├── channels/     # DingTalk, WeChat Work, WeChat (personal), Feishu, Telegram, Slack
│   │   └── gateway_client.py  # Shared route-resolve + Gateway call logic
│   ├── cli/          # Click-based CLI commands
│   ├── config/       # Pydantic config schema, loader, hot-reload watcher
│   ├── constants.py  # Project-wide constants (no magic numbers)
│   ├── cron/         # Scheduled tasks (APScheduler)
│   ├── gateway/      # FastAPI server, WebSocket, OpenAI-compat API
│   ├── hooks/        # Event hook system
│   ├── llm/          # LLM client (internal API, streaming)
│   ├── memory/       # Vector + BM25 hybrid search, embeddings
│   ├── plugins/      # Plugin system
│   ├── routing/      # Multi-agent routing (RouteResolver + Session Key)
│   ├── security/     # Auth, policies, audit, DM pairing
│   ├── sessions/     # Session management (SQLite) + export
│   ├── skills/       # Skill parser and loader
│   ├── tasks/        # Task registry
│   ├── webui/        # Web UI (HTML/CSS/JS)
│   └── infra/        # Shared infrastructure
├── tests/            # Test suite
├── skills/           # User skills directory
├── pyclaw.json.sample # Configuration template
└── LICENSE           # MIT License
```

## License

MIT — see [LICENSE](LICENSE) for details.
