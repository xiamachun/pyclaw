"""Session export functionality."""

import logging
from datetime import datetime

from pyclaw.sessions.models import Session, TranscriptEntry
from pyclaw.sessions.store import SessionStore

logger = logging.getLogger(__name__)


async def export_session_markdown(session_id: str, store: SessionStore) -> str:
    """Export session to Markdown format.

    Args:
        session_id: Session ID
        store: Session storage instance

    Returns:
        Session content in Markdown format

    Raises:
        ValueError: Session not found
    """
    session = await store.load_session(session_id)
    if not session:
        raise ValueError(f"Session not found: {session_id}")

    lines = []

    # Title and metadata
    lines.append(f"# Session Export: {session_id}")
    lines.append("")
    lines.append(f"**Created at**: {session.created_at.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**Updated at**: {session.updated_at.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**Agent name**: {session.agent_name}")
    lines.append(f"**Session type**: {session.session_type.value}")
    lines.append(f"**Status**: {session.status.value}")
    
    if session.channel:
        lines.append(f"**Channel**: {session.channel}")
    if session.peer:
        lines.append(f"**Peer**: {session.peer}")
    
    lines.append("")
    lines.append("---")
    lines.append("")

    # Transcript
    lines.append("## Transcript")
    lines.append("")

    for entry in session.transcript:
        # Set different formats based on role
        if entry.role == "user":
            lines.append(f"### 👤 User")
        elif entry.role == "assistant":
            lines.append(f"### 🤖 AI Assistant")
        elif entry.role == "system":
            lines.append(f"### ⚙️ System")
        else:
            lines.append(f"### {entry.role}")

        lines.append("")
        lines.append(f"**Time**: {entry.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")
        lines.append(entry.content)
        lines.append("")

        # If there is a tool call record
        if entry.tool_name:
            lines.append(f"**Tool call**: `{entry.tool_name}`")
            if entry.tool_result:
                lines.append("")
                lines.append("```")
                lines.append(entry.tool_result)
                lines.append("```")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


async def export_session_json(session_id: str, store: SessionStore) -> dict:
    """Export session to JSON format.

    Args:
        session_id: Session ID
        store: Session storage instance

    Returns:
        Session data in JSON format

    Raises:
        ValueError: Session not found
    """
    session = await store.load_session(session_id)
    if not session:
        raise ValueError(f"Session not found: {session_id}")

    # Build message list
    messages = []
    for entry in session.transcript:
        message = {
            "role": entry.role,
            "content": entry.content,
            "timestamp": entry.timestamp.isoformat(),
        }
        
        if entry.tool_name:
            message["tool_call"] = {
                "name": entry.tool_name,
            }
            if entry.tool_result:
                message["tool_call"]["result"] = entry.tool_result
        
        messages.append(message)

    # Build complete JSON structure
    result = {
        "session_id": session.session_id,
        "agent_name": session.agent_name,
        "session_type": session.session_type.value,
        "status": session.status.value,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
        "channel": session.channel,
        "peer": session.peer,
        "metadata": session.metadata,
        "messages": messages,
    }

    return result