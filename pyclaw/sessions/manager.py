"""Session manager."""

from typing import Optional

from pyclaw.config.schema import PyClawConfig
from pyclaw.sessions.models import Session, SessionStatus, SessionType, TranscriptEntry
from pyclaw.sessions.store import SessionStore


class SessionManager:
    """Session manager."""

    def __init__(self, store: SessionStore, config: PyClawConfig) -> None:
        """Initialize session manager.

        Args:
            store: Session storage
            config: Configuration object
        """
        self.store = store
        self.config = config

    async def create_session(
        self,
        agent_name: str,
        session_type: SessionType,
        channel: Optional[str] = None,
        peer: Optional[str] = None,
    ) -> Session:
        """Create new session.

        Args:
            agent_name: Agent name
            session_type: Session type
            channel: Associated channel
            peer: Peer identifier

        Returns:
            Created session object
        """
        session = Session(
            agent_name=agent_name,
            session_type=session_type,
            channel=channel,
            peer=peer,
        )
        await self.store.save_session(session)
        return session

    async def get_session(self, session_id: str) -> Session:
        """Get session.

        Args:
            session_id: Session ID

        Returns:
            Session object

        Raises:
            ValueError: Session not found
        """
        session = await self.store.load_session(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")
        return session

    async def get_or_create_main_session(self, agent_name: str) -> Session:
        """Get or create main session.

        Args:
            agent_name: Agent name

        Returns:
            Main session object
        """
        sessions = await self.store.list_sessions(
            agent_name=agent_name,
            status=SessionStatus.ACTIVE,
            limit=1,
        )

        for session in sessions:
            if session.is_main:
                return session

        return await self.create_session(
            agent_name=agent_name,
            session_type=SessionType.MAIN,
        )

    async def close_session(self, session_id: str) -> None:
        """Close session.

        Args:
            session_id: Session ID
        """
        session = await self.get_session(session_id)
        session.status = SessionStatus.CLOSED
        await self.store.save_session(session)

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_name: Optional[str] = None,
        tool_result: Optional[str] = None,
    ) -> None:
        """Add message to session.

        Args:
            session_id: Session ID
            role: Role type
            content: Message content
            tool_name: Tool name
            tool_result: Tool execution result
        """
        session = await self.get_session(session_id)

        entry = TranscriptEntry(
            role=role,
            content=content,
            tool_name=tool_name,
            tool_result=tool_result,
        )

        session.transcript.append(entry)
        await self.store.append_transcript(session_id, entry)

    async def list_active_sessions(self) -> list[Session]:
        """List all active sessions.

        Returns:
            List of active sessions
        """
        return await self.store.list_sessions(status=SessionStatus.ACTIVE)
