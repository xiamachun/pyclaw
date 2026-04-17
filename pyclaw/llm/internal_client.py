"""
Internal LLM Client — calls large models via OpenAI-compatible interface, supports streaming output.
"""

import asyncio
import os
import json
import logging
import time
from typing import AsyncIterator, Dict, Any, List, Optional

import httpx

from pyclaw.constants import DEFAULT_MAX_TOKENS, LLM_MAX_RETRIES, LLM_RETRY_DELAY_SECONDS

logger = logging.getLogger(__name__)

# Global debug flag, can be dynamically toggled via set_debug_logging()
_debug_enabled: bool = False

def set_debug_logging(enabled: bool) -> None:
    """Dynamically enable/disable Internal LLM verbose debug logging."""
    global _debug_enabled
    _debug_enabled = enabled
    level = logging.DEBUG if enabled else logging.INFO
    logger.setLevel(level)
    logger.info("Internal LLM debug logging %s", "ENABLED" if enabled else "DISABLED")

def _dbg(msg: str, *args: Any) -> None:
    """Log messages only in debug mode."""
    if _debug_enabled:
        logger.debug(msg, *args)

INTERNAL_BASE_URL = os.environ.get("INTERNAL_BASE_URL", "")

# Supported models list. Override via INTERNAL_SUPPORTED_MODELS env var (comma-separated).
# Example: INTERNAL_SUPPORTED_MODELS=gpt-4o,claude-sonnet-4,qwen-plus-latest
SUPPORTED_MODELS = [
    "gpt-4o",
    "claude-sonnet-4",
    "qwen-plus-latest",
    "qwen3-max",
    "qwen3.5-plus",
    "gemini-2.5-pro",
    "kimi-k2.5",
    "qwen3.5:9B",
]

# Fallback models for rate limiting
RATE_LIMIT_FALLBACK_MODELS = [
    "qwen3.5-plus",
    "qwen-plus-latest",
    "qwen3-max",
    "gpt-4o",
]

def get_internal_api_key() -> str:
    """Read Internal LLM API Key from environment variables."""
    api_key = os.getenv("INTERNAL_API_KEY")
    if not api_key:
        raise ValueError(
            "Internal LLM API Key not configured! Please set environment variable:\n"
            "  export INTERNAL_API_KEY='your-api-key'"
        )
    return api_key

async def _do_stream_once(
    model: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    max_tokens: int,
    temperature: float,
    resolved_api_key: str,
    base_url: str,
    timeout: float,
) -> AsyncIterator[Dict[str, Any]]:
    """
    Internal single streaming request without retry logic.
    When encountering 429, yield {"type": "rate_limit", "content": ...} for upper layer handling.
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {resolved_api_key}",
        "Content-Type": "application/json",
    }
    request_body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    if tools:
        request_body["tools"] = tools

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, json=request_body, headers=headers) as response:
            if response.status_code == 429:
                body = await response.aread()
                yield {"type": "rate_limit", "content": body.decode("utf-8", errors="replace")}
                return

            if response.status_code >= 400:
                body = await response.aread()
                error_text = body.decode("utf-8", errors="replace")
                logger.error(
                    "LLM API error %d: %s", response.status_code, error_text[:500]
                )
                raise httpx.HTTPStatusError(
                    f"HTTP {response.status_code}: {error_text[:200]}",
                    request=response.request,
                    response=response,
                )

            collected_content = ""
            collected_tool_calls: List[Dict[str, Any]] = []

            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if payload == "[DONE]":
                    break

                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                choices = chunk.get("choices", [])
                if not choices:
                    continue

                delta = choices[0].get("delta", {})

                text_chunk = delta.get("content")
                if text_chunk:
                    collected_content += text_chunk
                    yield {"type": "delta", "content": text_chunk}

                raw_tool_calls = delta.get("tool_calls")
                if raw_tool_calls:
                    for tc in raw_tool_calls:
                        idx = tc.get("index", 0)
                        while len(collected_tool_calls) <= idx:
                            collected_tool_calls.append({"function": {"name": "", "arguments": ""}})
                        func = tc.get("function", {})
                        if func.get("name"):
                            collected_tool_calls[idx]["function"]["name"] = func["name"]
                        if func.get("arguments"):
                            collected_tool_calls[idx]["function"]["arguments"] += func["arguments"]

            if collected_tool_calls:

                class _FunctionCall:
                    def __init__(self, name: str, arguments: str):
                        self.name = name
                        self.arguments = arguments

                class _ToolCall:
                    def __init__(self, function: _FunctionCall):
                        self.function = function

                parsed_tool_calls = [
                    _ToolCall(_FunctionCall(
                        name=tc["function"]["name"],
                        arguments=tc["function"]["arguments"],
                    ))
                    for tc in collected_tool_calls
                    if tc["function"]["name"]
                ]
                yield {"type": "tool_calls", "tool_calls": parsed_tool_calls}
            else:
                yield {"type": "done", "content": collected_content}

async def stream_chat_completions(
    model: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.7,
    api_key: Optional[str] = None,
    base_url: str = INTERNAL_BASE_URL,
    timeout: float = 300.0,
    max_retries: int = LLM_MAX_RETRIES,
    retry_delay: float = LLM_RETRY_DELAY_SECONDS,
) -> AsyncIterator[Dict[str, Any]]:
    """
    Perform streaming conversation via OpenAI-compatible interface, yield format matches runtime._call_llm_stream:
      {"type": "delta", "content": "..."}
      {"type": "tool_calls", "tool_calls": [...]}
      {"type": "done", "content": "..."}
      {"type": "error", "content": "..."}

    Automatically retry with exponential backoff when encountering 429 rate limit, and switch models in RATE_LIMIT_FALLBACK_MODELS order.

    Args:
        model: Preferred model name, e.g. "qwen3.5-plus"
        messages: OpenAI format message list
        tools: Tool definition list (optional)
        max_tokens: Maximum generation tokens
        temperature: Generation temperature
        api_key: API Key, read from environment if not provided
        base_url: API base URL
        timeout: Request timeout (seconds), default 300s
        max_retries: Maximum retry count when encountering 429
        retry_delay: Initial retry wait seconds (exponential backoff)
    """
    resolved_api_key = api_key or get_internal_api_key()

    # Candidate model list: preferred model first, then supplement in fallback order
    candidate_models = [model] + [m for m in RATE_LIMIT_FALLBACK_MODELS if m != model]

    logger.info(
        "Internal LLM stream request: model=%s, messages=%d, max_tokens=%d, temperature=%.2f, base_url=%s",
        model, len(messages), max_tokens, temperature, base_url,
    )
    _dbg("Internal LLM request body preview: first_msg_role=%s, tools_count=%d",
         messages[0].get("role", "?") if messages else "none",
         len(tools) if tools else 0)

    last_error: Optional[str] = None

    for attempt in range(max_retries):
        current_model = candidate_models[min(attempt, len(candidate_models) - 1)]

        if attempt > 0:
            wait_seconds = retry_delay * (2 ** (attempt - 1))
            logger.warning(
                "Internal LLM 429 rate limit, retry #%d, switching model=%s, waiting %.1fs",
                attempt, current_model, wait_seconds,
            )
            await asyncio.sleep(wait_seconds)

        attempt_start = time.monotonic()
        _dbg("Internal LLM attempt %d/%d: model=%s", attempt + 1, max_retries, current_model)

        try:
            got_rate_limit = False
            chunk_count = 0
            first_chunk_time: Optional[float] = None

            async for chunk in _do_stream_once(
                model=current_model,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
                resolved_api_key=resolved_api_key,
                base_url=base_url,
                timeout=timeout,
            ):
                if chunk["type"] == "rate_limit":
                    got_rate_limit = True
                    last_error = f"429 rate limit (model={current_model}): {chunk['content']}"
                    logger.warning("Internal LLM 429: %s", last_error[:300])
                    break

                if chunk["type"] == "delta":
                    chunk_count += 1
                    if first_chunk_time is None:
                        first_chunk_time = time.monotonic() - attempt_start
                        logger.info(
                            "Internal LLM first token: model=%s, ttft=%.2fs",
                            current_model, first_chunk_time,
                        )


                elif chunk["type"] == "tool_calls":
                    tool_names = [tc.function.name for tc in chunk.get("tool_calls", [])]
                    logger.info("Internal LLM tool_calls: model=%s, tools=%s", current_model, tool_names)

                elif chunk["type"] == "done":
                    elapsed = time.monotonic() - attempt_start
                    logger.info(
                        "Internal LLM stream done: model=%s, chunks=%d, total_time=%.2fs, content_len=%d",
                        current_model, chunk_count, elapsed, len(chunk.get("content", "")),
                    )

                elif chunk["type"] == "error":
                    logger.error("Internal LLM stream error chunk: model=%s, error=%s", current_model, chunk.get("content", ""))

                yield chunk

            if not got_rate_limit:
                if first_chunk_time is None:
                    # Stream ended without receiving any delta (empty response)
                    elapsed = time.monotonic() - attempt_start
                    logger.warning(
                        "Internal LLM empty response: model=%s, elapsed=%.2fs (no delta chunks received)",
                        current_model, elapsed,
                    )
                return  # Success, exit retry loop

        except httpx.HTTPStatusError as exc:
            elapsed = time.monotonic() - attempt_start
            last_error = f"Internal LLM HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            logger.error("Internal LLM HTTP error: status=%d, elapsed=%.2fs, body=%s",
                         exc.response.status_code, elapsed, exc.response.text[:200])
            if exc.response.status_code != 429:
                yield {"type": "error", "content": last_error}
                return
        except httpx.TimeoutException as exc:
            elapsed = time.monotonic() - attempt_start
            logger.error("Internal LLM timeout: model=%s, elapsed=%.2fs, error=%s",
                         current_model, elapsed, exc)
            yield {"type": "error", "content": f"Internal LLM request timeout ({elapsed:.1f}s): {exc}"}
            return
        except Exception as exc:
            elapsed = time.monotonic() - attempt_start
            logger.error("Internal LLM stream error: model=%s, elapsed=%.2fs, error=%s",
                         current_model, elapsed, exc, exc_info=True)
            yield {"type": "error", "content": str(exc)}
            return

    # All retries failed
    logger.error("Internal LLM all retries exhausted: max_retries=%d, last_error=%s", max_retries, last_error)
    yield {
        "type": "error",
        "content": f"Internal LLM still rate limited after multiple retries, please try again later. Last error: {last_error}",
    }