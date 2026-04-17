"""Agent-scoped Session Key construction.

Session Key format: agent:{agent_id}:{channel}:{peer_kind}:{peer_id}[:{thread_id}]
Ensures natural isolation of different Agent sessions while supporting threaded conversations.
"""


def build_session_key(
    agent_id: str,
    channel: str,
    peer_id: str,
    peer_kind: str = "direct",
    thread_id: str | None = None,
) -> str:
    """Build Agent-scoped Session Key.

    Args:
        agent_id: Agent role ID
        channel: Channel name
        peer_id: Peer user ID
        peer_kind: Conversation type (direct / group)
        thread_id: Thread ID, for threaded conversations in group chats

    Returns:
        Session Key string
    """
    if thread_id:
        return f"agent:{agent_id}:{channel}:{peer_kind}:{peer_id}:{thread_id}"
    return f"agent:{agent_id}:{channel}:{peer_kind}:{peer_id}"


def parse_session_key(session_key: str) -> dict[str, str]:
    """Parse Session Key and extract fields.

    Args:
        session_key: Session Key string

    Returns:
        Dictionary containing agent_id, channel, peer_kind, peer_id, thread_id
    """
    parts = session_key.split(":")
    result: dict[str, str] = {
        "agent_id": "",
        "channel": "",
        "peer_kind": "",
        "peer_id": "",
        "thread_id": "",
    }

    if len(parts) >= 2 and parts[0] == "agent":
        result["agent_id"] = parts[1]

    if len(parts) == 5:
        result["channel"] = parts[2]
        result["peer_kind"] = parts[3]
        result["peer_id"] = parts[4]
    elif len(parts) == 6:
        result["channel"] = parts[2]
        result["peer_kind"] = parts[3]
        result["peer_id"] = parts[4]
        result["thread_id"] = parts[5]

    return result