"""
Agent runtime for executing agent loops with LLM and tool interactions.
"""

from typing import AsyncIterator, Optional, Any, Callable, Dict, List
from pydantic import BaseModel, Field
import json
import httpx
import logging

# Import constants
from pyclaw.constants import (
    ERROR_MESSAGE_PREFIXES,
    CONTEXT_CACHE_MAX_SIZE,
    CONTEXT_CACHE_TTL_SECONDS,
    SKILL_SHELL_TIMEOUT,
    HISTORY_CONTENT_MAX_CHARS,
    HTTP_TOOL_TIMEOUT_SECONDS,
    SHELL_TOOL_LONG_TIMEOUT_SECONDS,
    HTTP_TOOL_CONNECT_TIMEOUT_SECONDS,
)

try:
    from pyclaw.llm.internal_client import stream_chat_completions as _internal_stream
    _INTERNAL_AVAILABLE = True
except ImportError:
    _INTERNAL_AVAILABLE = False

# Import new feature modules
from pyclaw.agents.failover import ModelFailoverChain, FailoverStatus
from pyclaw.agents.context_cache import ContextCache


class AgentEvent(BaseModel):
    """Event emitted during agent execution."""
    
    event_type: str = Field(..., description="Type of event: text, tool_call, tool_result, error, done")
    content: Optional[str] = Field(None, description="Event content")
    tool_name: Optional[str] = Field(None, description="Name of tool being called")
    tool_args: Optional[Dict[str, Any]] = Field(None, description="Arguments for tool call")
    tool_result: Optional[str] = Field(None, description="Result from tool execution")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Extra metadata (e.g. observability metrics)")


class AgentRuntime:
    """Runtime for executing agent interactions with LLM and tools."""
    
    def __init__(
        self,
        config: Dict[str, Any],
        tool_registry: 'ToolRegistry',
        model_selector: 'ModelSelector',
        credential_redactor: Optional[Callable] = None,
        context_cache: Optional[ContextCache] = None,
        failover_chain: Optional[ModelFailoverChain] = None,
    ):
        """
        Initialize agent runtime.
        
        Args:
            config: Agent configuration dictionary
            tool_registry: Registry of available tools
            model_selector: Model selector for choosing LLM
            credential_redactor: Optional function to redact credentials from content
            context_cache: Optional context cache for reducing LLM calls
            failover_chain: Optional failover chain for model failover
        """
        self.config = config
        self.tool_registry = tool_registry
        self.model_selector = model_selector
        self.credential_redactor = credential_redactor or (lambda x: x)
        self.context_cache = context_cache or ContextCache(max_size=CONTEXT_CACHE_MAX_SIZE, ttl_seconds=CONTEXT_CACHE_TTL_SECONDS)
        self.failover_chain = failover_chain

    async def run(
        self,
        session: Dict[str, Any],
        message: str
    ) -> AsyncIterator[AgentEvent]:
        """
        Execute the full agent loop: LLM → tool calls → feed results back → repeat.

        The loop continues until the LLM produces a final text response
        (no more tool_calls) or the maximum iteration limit is reached.
        
        Features:
        - Context caching: Check cache before calling LLM for simple queries
        - Model failover: Automatically switch to backup model on errors
        """
        max_iterations = 25

        # Extract mode parameters if present (temperature, max_tokens)
        mode_params = session.get("mode_params", {})
        
        # Try to get response from cache (for simple queries only)
        session_id = session.get("session_id", "default")
        history = session.get("history", [])
        cached_entry = self.context_cache.get(session_id, history + [{"role": "user", "content": message}])
        if cached_entry and cached_entry.response:
            logging.info("Cache hit for message: %s", message[:50])
            yield AgentEvent(event_type="text", content=cached_entry.response)
            yield AgentEvent(event_type="done", content="From cache")
            return

        import time as _time
        session_start_time = _time.monotonic()
        # Observability: per-step timeline and aggregate counters
        step_timeline = []  # [{llm_ms, tool_ms, tool_name}]
        total_llm_ms = 0
        total_tool_ms = 0
        tool_chain = []  # ordered tool names
        tool_frequency: Dict[str, int] = {}
        slowest_tool_name = ""
        slowest_tool_ms = 0

        try:
            messages = self._build_messages(session, message)
            model_config = self.model_selector.select(session)
            
            # Apply mode parameters to override model_config defaults
            if mode_params:
                if "temperature" in mode_params:
                    model_config.temperature = mode_params["temperature"]
                if "max_tokens" in mode_params:
                    model_config.max_tokens = mode_params["max_tokens"]
                logging.info("Mode params applied: temperature=%s, max_tokens=%s", 
                            model_config.temperature, model_config.max_tokens)
            
            tools = self._get_tool_definitions(session)

            yield AgentEvent(event_type="text", content="Thinking...")

            for iteration in range(max_iterations):
                logging.info("AgentRuntime iteration %d/%d, messages=%d", iteration, max_iterations, len(messages))
                llm_response_content = ""
                llm_tool_calls = []
                stream_error = None

                llm_start = _time.monotonic()
                async for chunk in self._call_llm_stream(model_config, messages, tools):
                    if chunk["type"] == "delta":
                        yield AgentEvent(event_type="text_delta", content=chunk["content"])
                        llm_response_content += chunk["content"]
                    elif chunk["type"] == "tool_calls":
                        llm_tool_calls = chunk["tool_calls"]
                    elif chunk["type"] == "error":
                        stream_error = chunk["content"]
                    elif chunk["type"] == "done":
                        llm_response_content = chunk.get("content", llm_response_content)
                iter_llm_ms = int((_time.monotonic() - llm_start) * 1000)
                total_llm_ms += iter_llm_ms

                if stream_error:
                    yield AgentEvent(event_type="error", content=f"LLM error: {stream_error}")
                    return

                logging.info(
                    "AgentRuntime iter %d: text_len=%d, tool_calls=%d",
                    iteration, len(llm_response_content), len(llm_tool_calls),
                )

                if llm_tool_calls:
                    # Append the assistant message (with tool_calls) to conversation
                    assistant_msg: Dict[str, Any] = {"role": "assistant", "content": llm_response_content or ""}
                    assistant_msg["tool_calls"] = [
                        {
                            "id": f"call_{iteration}_{i}",
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for i, tc in enumerate(llm_tool_calls)
                    ]
                    messages.append(assistant_msg)

                    iter_tool_ms = 0
                    iter_tool_names = []
                    for i, tool_call in enumerate(llm_tool_calls):
                        tool_name = tool_call.function.name
                        try:
                            tool_args = json.loads(tool_call.function.arguments)
                        except json.JSONDecodeError:
                            tool_args = {}

                        yield AgentEvent(
                            event_type="tool_call",
                            tool_name=tool_name,
                            tool_args=tool_args,
                        )

                        tool_start = _time.monotonic()
                        result = await self._execute_tool(tool_name, tool_args, session)
                        tool_elapsed_ms = int((_time.monotonic() - tool_start) * 1000)

                        iter_tool_ms += tool_elapsed_ms
                        total_tool_ms += tool_elapsed_ms

                        # Smart skill detection: when shell/file_read commands
                        # reference a skill directory, use the skill name in
                        # observability instead of the raw tool name.
                        display_name = tool_name
                        if tool_name in ("shell", "file_read", "file_write"):
                            cmd_or_path = ""
                            if tool_name == "shell":
                                cmd_or_path = tool_args.get("command", "")
                            else:
                                cmd_or_path = tool_args.get("path", "")
                            if "skills/" in cmd_or_path:
                                import re as _re
                                skill_match = _re.search(r"skills/([^/]+)", cmd_or_path)
                                if skill_match:
                                    display_name = f"skill_{skill_match.group(1)}"

                        tool_chain.append(display_name)
                        iter_tool_names.append(display_name)
                        tool_frequency[display_name] = tool_frequency.get(display_name, 0) + 1
                        if tool_elapsed_ms > slowest_tool_ms:
                            slowest_tool_ms = tool_elapsed_ms
                            slowest_tool_name = tool_name

                        yield AgentEvent(
                            event_type="tool_result",
                            tool_name=tool_name,
                            tool_result=result,
                        )

                        # Feed tool result back into conversation for the LLM
                        messages.append({
                            "role": "tool",
                            "tool_call_id": f"call_{iteration}_{i}",
                            "content": result,
                        })

                    step_timeline.append({
                        "llm_ms": iter_llm_ms,
                        "tool_ms": iter_tool_ms,
                        "tool_name": " → ".join(iter_tool_names),
                    })

                    # Add a continuation hint so the LLM knows it should keep
                    # going if the original task is not fully done yet.
                    messages.append({
                        "role": "user",
                        "content": (
                            "[SYSTEM] The tool above has finished. "
                            "If the user's original request is NOT fully completed yet, "
                            "you MUST continue by calling the next required tool immediately. "
                            "Do NOT stop or say 'task complete' until every step is done "
                            "(e.g. install → write script → execute script → verify output). "
                            "If everything is truly done, reply with your final answer."
                        ),
                    })

                    # Loop back — let the LLM see the tool results and decide next step
                    continue

                # No tool calls — this is the final text response
                # Guard: if LLM returned nothing at all (e.g. rate-limited silently),
                # treat it as an error rather than silently completing the task.
                if not llm_response_content.strip():
                    yield AgentEvent(
                        event_type="error",
                        content="LLM returned empty response, possibly due to rate limiting (429) or network issues. Please try again later.",
                    )
                    return

                content = llm_response_content
                if self.credential_redactor:
                    content = self.credential_redactor(content)
                
                # Cache response for simple queries (when no tool calls were made)
                if iteration == 0:  # Only cache on first iteration (no tool calls)
                    cache_messages = history + [{"role": "user", "content": message}]
                    self.context_cache.put(session_id, cache_messages, response=content)
                    logging.info("Cached response for message: %s", message[:50])
                
                # Record final step (text response, no tool call)
                step_timeline.append({
                    "llm_ms": iter_llm_ms,
                    "tool_ms": 0,
                    "tool_name": "text response",
                })

                session_total_ms = int((_time.monotonic() - session_start_time) * 1000)
                observability = {
                    "session_id": session_id,
                    "tool_chain": tool_chain,
                    "tool_calls_count": len(tool_chain),
                    "total_tool_ms": total_tool_ms,
                    "total_llm_ms": total_llm_ms,
                    "session_ms": session_total_ms,
                    "tool_frequency": tool_frequency,
                    "slowest_tool_name": slowest_tool_name,
                    "slowest_tool_ms": slowest_tool_ms,
                    "step_timeline": step_timeline,
                }

                yield AgentEvent(event_type="text", content=content)
                yield AgentEvent(event_type="done", content="Response completed", metadata=observability)
                return

            # Exhausted max iterations
            session_total_ms = int((_time.monotonic() - session_start_time) * 1000)
            observability = {
                "session_id": session_id,
                "tool_chain": tool_chain,
                "tool_calls_count": len(tool_chain),
                "total_tool_ms": total_tool_ms,
                "total_llm_ms": total_llm_ms,
                "session_ms": session_total_ms,
                "tool_frequency": tool_frequency,
                "slowest_tool_name": slowest_tool_name,
                "slowest_tool_ms": slowest_tool_ms,
                "step_timeline": step_timeline,
            }
            yield AgentEvent(
                event_type="text",
                content="Reached maximum tool-call iterations. Here is what I have so far.",
            )
            yield AgentEvent(event_type="done", content="Max iterations reached", metadata=observability)

        except Exception as e:
            logging.error("AgentRuntime error: %s", e, exc_info=True)
            yield AgentEvent(
                event_type="error",
                content=f"Error during execution: {str(e)}",
            )
    
    async def _call_llm(
        self,
        model_config: Dict[str, Any],
        messages: List[Dict[str, str]],
        tools: List[Dict[str, Any]]
    ) -> Any:
        """
        Call LLM API with given messages and tools.
        
        Args:
            model_config: Model configuration
            messages: Message list for conversation
            tools: Available tool definitions
            
        Returns:
            LLM response object with .content and .tool_calls attributes
        """
        from pyclaw.config.loader import load_config as _load_config
        base_url = getattr(model_config, 'base_url', None) or _load_config().llm.get_local_base_url()
        url = f"{base_url}/chat/completions"
        
        request_data = {
            "model": model_config.name,
            "messages": messages,
            "max_tokens": getattr(model_config, 'max_tokens', 2048),
            "temperature": getattr(model_config, 'temperature', 0.7),
        }
        
        if tools:
            request_data["tools"] = tools
        
        class FunctionCall:
            def __init__(self, name: str, arguments: str):
                self.name = name
                self.arguments = arguments

        class ToolCall:
            def __init__(self, function: FunctionCall):
                self.function = function

        class LLMResponse:
            def __init__(self, content: str = "", tool_calls: List[Any] = None):
                self.content = content
                self.tool_calls = tool_calls or []
        
        try:
            async with httpx.AsyncClient(timeout=HTTP_TOOL_TIMEOUT_SECONDS) as client:
                response = await client.post(url, json=request_data)
                response.raise_for_status()
                data = response.json()
                
                choices = data.get("choices", [])
                if not choices:
                    return LLMResponse()
                
                message = choices[0].get("message", {})
                content = message.get("content", "")
                raw_tool_calls = message.get("tool_calls", [])

                parsed_tool_calls = []
                for raw_tc in raw_tool_calls:
                    func_data = raw_tc.get("function", {})
                    parsed_tool_calls.append(
                        ToolCall(FunctionCall(
                            name=func_data.get("name", ""),
                            arguments=func_data.get("arguments", "{}"),
                        ))
                    )
                
                return LLMResponse(content=content, tool_calls=parsed_tool_calls)
                
        except httpx.HTTPError as e:
            logging.error("HTTP error calling LLM API: %s", e, exc_info=True)
            return LLMResponse(content=f"Error: Failed to call LLM API - {str(e)}")
        except Exception as e:
            logging.error("Unexpected error calling LLM API: %s", e, exc_info=True)
            return LLMResponse(content=f"Error: Unexpected error - {str(e)}")
    
    async def _call_llm_stream(
        self,
        model_config,
        messages: List[Dict[str, str]],
        tools: List[Dict[str, Any]]
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Stream LLM response. Yields dicts with keys:
        - {"type": "delta", "content": "..."} for text chunks
        - {"type": "tool_calls", "tool_calls": [...]} when tool calls are detected
        - {"type": "done"} when streaming is complete

        Routes based on provider_type:
        - 'openai_compatible': Uses internal_client if available, otherwise direct httpx
        - 'ollama' or default: Uses ollama-compatible endpoint
        """
        provider = getattr(model_config, 'provider', 'ollama')
        provider_type = getattr(model_config, 'provider_type', 'ollama')

        # ── OpenAI-Compatible Channel (dashscope, nvidia, etc.) ─────────────
        if provider_type == 'openai_compatible' and _INTERNAL_AVAILABLE:
            model_name = getattr(model_config, 'name', '')
            api_key = getattr(model_config, 'api_key', None)
            base_url_val = getattr(model_config, 'base_url', None)
            timeout_val = getattr(model_config, 'timeout', 300.0)

            kwargs: Dict[str, Any] = dict(
                model=model_name,
                messages=messages,
                tools=tools or None,
                max_tokens=getattr(model_config, 'max_tokens', 4096),
                temperature=getattr(model_config, 'temperature', 0.7),
                timeout=timeout_val,
            )
            if api_key:
                kwargs['api_key'] = api_key
            if base_url_val:
                kwargs['base_url'] = base_url_val

            logging.info("Using OpenAI-compatible provider: %s, model=%s", provider, model_name)
            async for chunk in _internal_stream(**kwargs):
                yield chunk
            return

        # ── Local / Ollama Channel (original logic)────────────────────────────────────
        from pyclaw.config.loader import load_config as _load_config
        base_url = getattr(model_config, 'base_url', None) or _load_config().llm.get_local_base_url()
        url = f"{base_url}/chat/completions"

        request_data = {
            "model": model_config.name,
            "messages": messages,
            "max_tokens": getattr(model_config, 'max_tokens', 2048),
            "temperature": getattr(model_config, 'temperature', 0.7),
            "stream": True,
        }

        if tools:
            request_data["tools"] = tools

        logging.info("Local/Ollama stream request: url=%s, model=%s, messages=%d",
                     url, model_config.name, len(messages))

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(SHELL_TOOL_LONG_TIMEOUT_SECONDS, connect=HTTP_TOOL_CONNECT_TIMEOUT_SECONDS)) as client:
                async with client.stream("POST", url, json=request_data) as response:
                    if response.status_code != 200:
                        body = await response.aread()
                        error_body = body.decode("utf-8", errors="replace")
                        logging.error(
                            "Local/Ollama HTTP error: status=%d, model=%s, url=%s, body=%s",
                            response.status_code, model_config.name, url, error_body[:500],
                        )
                        yield {"type": "error", "content": f"Local model request failed HTTP {response.status_code}: {error_body[:200]}"}
                        return
                    response.raise_for_status()
                    collected_content = ""
                    collected_tool_calls = []
                    chunk_count = 0

                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        payload = line[6:].strip()
                        if payload == "[DONE]":
                            break

                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            logging.warning("Local/Ollama: failed to parse chunk: %s", payload[:200])
                            continue

                        choices = chunk.get("choices", [])
                        if not choices:
                            continue

                        chunk_count += 1
                        delta = choices[0].get("delta", {})

                        # Text content
                        if delta.get("content"):
                            collected_content += delta["content"]
                            yield {"type": "delta", "content": delta["content"]}

                        # Tool calls (accumulated across chunks)
                        if delta.get("tool_calls"):
                            for tc in delta["tool_calls"]:
                                idx = tc.get("index", 0)
                                while len(collected_tool_calls) <= idx:
                                    collected_tool_calls.append({"function": {"name": "", "arguments": ""}})
                                if tc.get("function", {}).get("name"):
                                    collected_tool_calls[idx]["function"]["name"] = tc["function"]["name"]
                                if tc.get("function", {}).get("arguments"):
                                    collected_tool_calls[idx]["function"]["arguments"] += tc["function"]["arguments"]

                    logging.info(
                        "Local/Ollama stream done: model=%s, chunks=%d, content_len=%d, tool_calls=%d",
                        model_config.name, chunk_count, len(collected_content), len(collected_tool_calls),
                    )

                    # After stream ends, yield tool calls if any
                    if collected_tool_calls:
                        # Build proper ToolCall objects
                        class FunctionCall:
                            def __init__(self, name, arguments):
                                self.name = name
                                self.arguments = arguments
                        class ToolCall:
                            def __init__(self, function):
                                self.function = function

                        parsed = [
                            ToolCall(FunctionCall(
                                name=tc["function"]["name"],
                                arguments=tc["function"]["arguments"],
                            ))
                            for tc in collected_tool_calls
                            if tc["function"]["name"]
                        ]
                        yield {"type": "tool_calls", "tool_calls": parsed}
                    else:
                        yield {"type": "done", "content": collected_content}

        except Exception as e:
            logging.error("Streaming LLM error: %s", e, exc_info=True)
            yield {"type": "error", "content": str(e)}
            return


    
    async def _execute_tool(
        self,
        tool_name: str,
        args: Dict[str, Any],
        session: Dict[str, Any]
    ) -> str:
        """Execute a tool with given arguments.

        When a skill tool returns the ``__AUTO_EXECUTE__`` marker followed
        by a JSON payload containing an ``auto_execute.command``, this
        method transparently executes the shell command and returns the
        real output instead of the raw JSON directive.  This saves one
        LLM round-trip and avoids weak-model parsing failures.

        Args:
            tool_name: Name of tool to execute.
            args: Arguments for the tool.
            session: Session context.

        Returns:
            Tool execution result as string.
        """
        from pyclaw.constants import SKILL_AUTO_EXECUTE_MARKER

        tool_def = self.tool_registry.get(tool_name)
        if not tool_def:
            return f"ERROR: Tool '{tool_name}' not found. Available tools: {', '.join(t.name for t in self.tool_registry.list_all())}"

        try:
            result = await tool_def.handler(args, session)
            result_str = str(result)

            # Detect auto_execute directive from skill handlers and
            # execute the shell command directly, returning real data.
            if result_str.startswith(SKILL_AUTO_EXECUTE_MARKER):
                result_str = await self._handle_auto_execute(
                    tool_name, result_str, session
                )

            return result_str
        except Exception as e:
            return (
                f"ERROR: Tool '{tool_name}' raised an exception:\n"
                f"{type(e).__name__}: {str(e)}\n"
                f"You MUST fix this error and retry the tool call."
            )

    async def _handle_auto_execute(
        self,
        tool_name: str,
        raw_result: str,
        session: Dict[str, Any],
    ) -> str:
        """Parse an ``__AUTO_EXECUTE__`` directive and run the command.

        Args:
            tool_name: The skill tool that produced the directive.
            raw_result: The full string starting with the marker.
            session: Session context.

        Returns:
            The shell command output (real data), or the original
            raw_result if parsing/execution fails.
        """
        from pyclaw.constants import SKILL_AUTO_EXECUTE_MARKER

        json_part = raw_result[len(SKILL_AUTO_EXECUTE_MARKER):].strip()
        try:
            payload = json.loads(json_part)
        except (json.JSONDecodeError, ValueError):
            logging.warning(
                "auto_execute: failed to parse JSON from %s, "
                "falling back to raw result",
                tool_name,
            )
            return raw_result

        auto_exec = payload.get("auto_execute", {})
        command = auto_exec.get("command", "")
        if not command:
            logging.warning(
                "auto_execute: no command found in payload from %s",
                tool_name,
            )
            return raw_result

        logging.info(
            "auto_execute: running shell command for %s: %.120s",
            tool_name, command,
        )

        # Execute via the shell tool already registered in tool_registry
        shell_tool = self.tool_registry.get("shell")
        if not shell_tool:
            logging.error("auto_execute: shell tool not found in registry")
            return raw_result

        try:
            shell_result = await shell_tool.handler(
                {"command": command, "timeout": SKILL_SHELL_TIMEOUT}, session
            )
            shell_output = str(shell_result)
            logging.info(
                "auto_execute: shell completed for %s, output_len=%d",
                tool_name, len(shell_output),
            )
            return shell_output
        except Exception as exc:
            logging.error(
                "auto_execute: shell execution failed for %s: %s",
                tool_name, exc, exc_info=True,
            )
            return (
                f"ERROR: auto_execute shell command failed:\n"
                f"{type(exc).__name__}: {str(exc)}\n"
                f"Command was: {command}"
            )
    
    def _build_messages(
        self,
        session: Dict[str, Any],
        new_message: str
    ) -> List[Dict[str, str]]:
        """
        Build message list from session history and new message.
        
        Args:
            session: Session context with history
            new_message: New user message
            
        Returns:
            List of message dictionaries
        """
        messages = []
        
        # Add system prompt
        agent_config = session.get('agent_config', {})
        skills = session.get('skills', [])
        system_prompt = self._apply_system_prompt(agent_config, skills)
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        
        # Add conversation history, skipping error/timeout messages
        # and truncating long assistant replies to prevent the LLM
        # from learning "answer without tools" or "parrot errors" patterns.
        history = session.get('history', [])

        # First pass: identify indices of error assistant messages
        # and their preceding user messages.
        skip_indices: set[int] = set()
        for idx, msg in enumerate(history):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                if any(content.startswith(p) for p in ERROR_MESSAGE_PREFIXES):
                    skip_indices.add(idx)
                    if idx > 0 and history[idx - 1].get("role") == "user":
                        skip_indices.add(idx - 1)

        # Second pass: build filtered history with truncation.
        for idx, msg in enumerate(history):
            if idx in skip_indices:
                continue
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "assistant" and len(content) > HISTORY_CONTENT_MAX_CHARS:
                content = content[:HISTORY_CONTENT_MAX_CHARS] + "\n... (truncated)"
            messages.append({"role": role, "content": content})
        
        # Add new message
        messages.append({"role": "user", "content": new_message})
        
        return messages
    
    def _apply_system_prompt(
        self,
        agent_config: Dict[str, Any],
        skills: List[Dict[str, Any]]
    ) -> str:
        """Build system prompt from agent config and skills.

        Args:
            agent_config: Agent configuration.
            skills: List of available skills.

        Returns:
            System prompt string.
        """
        base_prompt = agent_config.get('system_prompt', 'You are a helpful AI assistant.')

        if skills:
            skill_descriptions = "\n".join([
                f"- {skill.get('name', '')}: {skill.get('description', '')}"
                for skill in skills
            ])
            base_prompt += (
                "\n\nYou have access to the following skill tools:\n"
                f"{skill_descriptions}\n\n"
                "IMPORTANT: When the user's request can be fulfilled by a "
                "skill tool (e.g. stock analysis, web search, file operations), "
                "you MUST call the appropriate skill tool to get real-time data "
                "instead of answering from your own knowledge. Your training "
                "data is outdated — only tool results contain live information."
            )

        return base_prompt
    
    def _get_tool_definitions(self, session: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Get tool definitions available for the session.
        
        Args:
            session: Session context
            
        Returns:
            List of tool definition dictionaries
        """
        policy = session.get('security_policy', {})
        tools = self.tool_registry.list_for_session(session, policy)
        
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters
                }
            }
            for tool in tools
        ]
