"""
Skill-to-Tool adapter.

Converts SkillDefinition objects into ToolDefinition objects so that
skills are exposed as first-class function tools to the LLM, instead
of being injected as large text blocks into the system prompt.

When a skill has a single primary script, the handler returns a
structured ``auto_execute`` JSON block that the AgentRuntime can
detect and execute directly, bypassing the LLM "thinking" step.
"""

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List

from pyclaw.agents.tools import ToolDefinition
from pyclaw.constants import SKILL_AUTO_EXECUTE_MARKER

logger = logging.getLogger(__name__)

# Characters not allowed in OpenAI function tool names
_TOOL_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")

# Global registry mapping tool_name → orchestration metadata.
# Populated during register_skill_tools() and read by AgentRuntime
# for the Intent Router feature.
_skill_orchestration_registry: Dict[str, Dict[str, Any]] = {}


def get_skill_orchestration_registry() -> Dict[str, Dict[str, Any]]:
    """Return the global skill orchestration registry.

    The registry maps tool names (e.g. ``skill_a-stock-analyzer``) to
    their orchestration metadata from SKILL.md frontmatter, including
    ``trigger_description``, ``chain_after``, ``role``, etc.

    Returns:
        A dict of ``{tool_name: orchestration_dict}``.
    """
    return _skill_orchestration_registry


def _sanitize_tool_name(raw_name: str) -> str:
    """Convert a skill name into a valid function-tool name.

    OpenAI function calling requires names matching ``^[a-zA-Z0-9_-]+$``.

    Args:
        raw_name: Original skill name (may contain spaces, dots, etc.)

    Returns:
        Sanitised name safe for use as a tool identifier.
    """
    sanitized = _TOOL_NAME_RE.sub("_", raw_name.strip())
    # Collapse consecutive underscores
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    return sanitized or "unnamed_skill"


def _find_primary_script(skill_dir: str, content: str) -> str | None:
    """Detect the primary executable script inside a skill directory.

    When the SKILL.md references one or more scripts, the **first**
    referenced script is treated as the primary entry point.  This is
    the script that will be auto-executed by the runtime when the LLM
    calls the skill tool.

    For skills with no script references at all, falls back to checking
    the ``scripts/`` directory for a single ``.py`` file.

    Args:
        skill_dir: Absolute path to the skill directory.
        content: Full SKILL.md content.

    Returns:
        Absolute path to the primary script, or *None* if none found.
    """
    scripts_dir = Path(skill_dir) / "scripts"

    # Collect unique scripts referenced in SKILL.md (preserving order)
    referenced: list[str] = []
    matches = re.findall(r"scripts/(\S+\.py)", content)
    for match in matches:
        candidate = scripts_dir / match
        if candidate.exists() and str(candidate) not in referenced:
            referenced.append(str(candidate))

    if referenced:
        # Use the first referenced script as the primary entry point.
        # Subsequent scripts (e.g. daily_stock_picker.py) are auxiliary
        # and can be invoked by the LLM via shell if needed.
        return referenced[0]

    # Fallback: check scripts/ directory directly
    if scripts_dir.is_dir():
        py_files = [
            f for f in sorted(scripts_dir.glob("*.py"))
            if not f.name.startswith("__")
        ]
        if len(py_files) == 1:
            return str(py_files[0])

    return None


def _detect_runner(content: str) -> str:
    """Detect the correct Python runner from SKILL.md metadata.

    Checks the metadata ``requires.bins`` list and usage examples
    to determine whether to use ``uv run`` or ``python3``.

    Args:
        content: Full SKILL.md content.

    Returns:
        ``"uv run"`` if the skill requires uv, otherwise ``"python3"``.
    """
    # Check metadata requires.bins for "uv"
    if re.search(r'"bins"\s*:\s*\[.*?"uv".*?\]', content):
        return "uv run"
    # Check usage examples for "uv run"
    if "uv run" in content:
        return "uv run"
    return "python3"


def _detect_argparse_query(content: str, script_path: str | None) -> bool:
    """Detect whether a skill script uses argparse with a --query flag.

    Scripts using argparse require named arguments (e.g. ``--query "text"``)
    rather than positional arguments.

    Args:
        content: Full SKILL.md content.
        script_path: Absolute path to the primary script, or None.

    Returns:
        True if the script expects ``--query`` as a named argument.
    """
    # Check SKILL.md for --query usage patterns
    if re.search(r"--query\s", content):
        return True

    # Check script source for argparse --query definition
    if script_path:
        try:
            script_source = Path(script_path).read_text(encoding="utf-8")
            if re.search(r"add_argument\s*\(\s*['\"]--query['\"]", script_source):
                return True
        except OSError:
            pass

    return False


def _detect_json_input(content: str, script_path: str | None) -> bool:
    """Detect whether a skill script expects JSON-formatted input.

    Checks the SKILL.md usage examples and the script source for
    patterns indicating JSON input is expected (e.g. ``json.loads``
    on ``sys.argv``).

    Args:
        content: Full SKILL.md content.
        script_path: Absolute path to the primary script, or None.

    Returns:
        True if the script expects a JSON string as its argument.
    """
    # Check SKILL.md for JSON usage patterns
    if re.search(r"<JSON>|'\{.*?\"query\"", content):
        return True

    # Check script source for json.loads(sys.argv patterns
    if script_path:
        try:
            script_source = Path(script_path).read_text(encoding="utf-8")
            if re.search(r"json\.loads\s*\(\s*(sys\.argv|query|args)", script_source):
                return True
        except OSError:
            pass

    return False


def _build_skill_handler(
    skill_dir: str,
    skill_content: str,
    orchestration: dict | None = None,
):
    """Build an async handler closure for a skill tool.

    When the skill has a single primary script, the handler returns a
    structured JSON payload with an ``auto_execute`` directive.  The
    AgentRuntime detects this marker and executes the shell command
    directly, bypassing the LLM reasoning step.

    For multi-script skills (or when no script is detected), the handler
    falls back to returning the full SKILL.md instructions so the LLM
    can construct the correct shell command itself.

    Args:
        skill_dir: Absolute path to the skill directory.
        skill_content: Full SKILL.md content.
        orchestration: Orchestration metadata from SKILL.md frontmatter.

    Returns:
        An async callable ``(args, session) -> str``.
    """
    orchestration = orchestration or {}
    # Replace relative script paths with absolute paths so the LLM
    # can copy-paste commands directly into the shell tool.
    resolved_content = skill_content.replace(
        "scripts/", f"{skill_dir}/scripts/"
    )

    primary_script = _find_primary_script(skill_dir, skill_content)
    runner = _detect_runner(skill_content)
    expects_json = _detect_json_input(skill_content, primary_script)
    uses_named_query = _detect_argparse_query(skill_content, primary_script)

    is_consumer = orchestration.get("role") == "consumer"
    related_skills = orchestration.get("related_skills", [])

    async def _handler(args: Dict[str, Any], session: Dict[str, Any]) -> str:
        query = args.get("query", "")
        context = args.get("context", "")

        # Guard: consumer skills with related_skills MUST receive
        # upstream data via the context parameter when the query
        # involves topics covered by related skills.
        # We reject the call outright so the LLM is forced to gather
        # data first.  For general topics (travel, education, etc.)
        # the LLM should pass context="" explicitly or any non-empty
        # string like "none" to bypass this guard.
        if is_consumer and related_skills and not context:
            skill_names = ", ".join(related_skills)
            return (
                f"STOP: You must gather data before calling this skill.\n\n"
                f"This skill accepts a 'context' parameter. You called it "
                f"without providing any context data.\n\n"
                f"IF the topic needs real-time data (stocks, market, news, "
                f"research):\n"
                f"  1. Call the relevant data skill: {skill_names}\n"
                f"  2. Execute its shell command and get the output\n"
                f"  3. Call this tool again with query AND context="
                f"(paste the FULL shell output)\n\n"
                f"IF the topic is general (travel, education, tutorial, "
                f"creative writing) and does NOT need external data:\n"
                f"  Call this tool again with context=\"general\" to proceed "
                f"directly.\n\n"
                f"Your original query was: {query}"
            )

        # Build a combined request that includes upstream data context
        # when this skill is called as part of an orchestration chain.
        context_block = ""
        if context:
            context_block = (
                f"\n\n--- DATA FROM PREVIOUS SKILL ---\n"
                f"{context}\n"
                f"--- END DATA ---\n\n"
                f"Use the above data to enrich your output.\n"
            )

        if primary_script:
            # Single-script skill: return structured auto-execute directive
            # so the runtime can bypass LLM and execute directly.
            full_query = f"{query}{context_block}" if context_block else query
            if expects_json:
                # Script expects a JSON string argument (e.g. baidu-search)
                json_arg = json.dumps({"query": full_query}, ensure_ascii=False)
                arg_escaped = json_arg.replace("'", "'\\''")
                command = f"{runner} {primary_script} '{arg_escaped}'"
            elif uses_named_query:
                # Script uses argparse with --query flag
                query_escaped = full_query.replace("'", "'\\''")
                command = f"{runner} {primary_script} --query '{query_escaped}'"
            else:
                query_escaped = full_query.replace("'", "'\\''")
                command = f"{runner} {primary_script} '{query_escaped}'"

            payload = {
                "auto_execute": {
                    "tool": "shell",
                    "command": command,
                },
                "user_request": full_query,
                "instructions": resolved_content,
            }
            return f"{SKILL_AUTO_EXECUTE_MARKER}\n{json.dumps(payload, ensure_ascii=False)}"

        # Multi-script or no-script skill: return full instructions
        # for the LLM to interpret and execute step by step.
        return (
            f"User request: {query}\n"
            f"{context_block}"
            f"Follow the instructions below. Use the `shell` tool to "
            f"execute the commands.\n\n"
            f"{resolved_content}"
        )

    return _handler


def register_skill_tools(
    tool_registry: Any,
    skills_base_dir: Path,
) -> int:
    """Scan the skills directory and register each skill as a function tool.

    Args:
        tool_registry: A ``ToolRegistry`` instance.
        skills_base_dir: Path to ``~/.pyclaw/workspace/skills/``.

    Returns:
        Number of skill tools registered.
    """
    if not skills_base_dir.exists():
        return 0

    from pyclaw.skills.parser import parse_skill_markdown

    registered = 0

    for skill_dir in sorted(skills_base_dir.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith("."):
            continue

        # Locate SKILL.md
        skill_file = None
        for marker in ("SKILL.md", "skill.md", "skills.md"):
            candidate = skill_dir / marker
            if candidate.exists():
                skill_file = candidate
                break
        if not skill_file:
            continue

        try:
            raw_content = skill_file.read_text(encoding="utf-8")
            skill_def = parse_skill_markdown(
                raw_content, directory_name=skill_dir.name
            )
        except Exception as parse_err:
            logger.debug("Skip skill %s: %s", skill_dir.name, parse_err)
            continue

        # Skip the security skill (it is always loaded separately)
        if skill_def.name == "security":
            continue

        tool_name = f"skill_{_sanitize_tool_name(skill_def.name)}"

        # Extract orchestration metadata early so it can be passed
        # to both the handler builder and the description builder.
        orchestration = skill_def.metadata.get("orchestration", {})

        handler = _build_skill_handler(
            skill_dir=str(skill_dir),
            skill_content=skill_def.content,
            orchestration=orchestration,
        )

        # Build an enhanced description that helps the LLM pick the
        # right skill tool instead of trying to do everything via shell.
        base_desc = (
            skill_def.description
            or f"Execute the '{skill_def.name}' skill."
        )
        enhanced_desc = (
            f"{base_desc} "
            f"IMPORTANT: Always prefer this tool over writing code yourself. "
            f"Call this tool first, then follow its instructions."
        )

        # Inject orchestration chain_hint into tool description so the
        # LLM knows when to combine this skill with others.
        chain_hint = (
            orchestration.get("chain_hint", "")
            if isinstance(orchestration, dict)
            else ""
        )
        if chain_hint:
            enhanced_desc = f"{enhanced_desc} ORCHESTRATION: {chain_hint}"

        # Build tool parameters; add a 'context' parameter for
        # consumer skills so the LLM can pass upstream data.
        is_consumer = (
            isinstance(orchestration, dict)
            and orchestration.get("role") == "consumer"
        )
        properties = {
            "query": {
                "type": "string",
                "description": "User request or topic for this skill.",
            },
        }
        if is_consumer:
            properties["context"] = {
                "type": "string",
                "description": (
                    "Full output from a previous data-gathering skill. "
                    "When chaining skills, paste the COMPLETE data output "
                    "here so this skill can use real data in its output."
                ),
            }

        tool_def = ToolDefinition(
            name=tool_name,
            description=enhanced_desc,
            parameters={
                "type": "object",
                "properties": properties,
                "required": ["query"],
            },
            handler=handler,
        )

        tool_registry.register(tool_def)
        registered += 1

        # Store orchestration metadata in the global registry so the
        # AgentRuntime Intent Router can access trigger_description
        # and chain_after without re-parsing SKILL.md files.
        if isinstance(orchestration, dict) and orchestration:
            _skill_orchestration_registry[tool_name] = orchestration
            logger.info(
                "Registered skill tool: %s (orchestration: role=%s)",
                tool_name,
                orchestration.get("role", "none"),
            )
        else:
            logger.info("Registered skill tool: %s", tool_name)

    return registered
