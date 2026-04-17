"""Session storage implementation."""

import aiosqlite
from datetime import datetime, timedelta
from typing import Optional

from pyclaw.sessions.models import Session, SessionStatus, SessionType, TranscriptEntry


class SessionStore:
    """Session storage based on aiosqlite."""

    def __init__(self, db_path: str) -> None:
        """Initialize storage.

        Args:
            db_path: SQLite database file path
        """
        self.db_path = db_path
        self._connection: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        """Initialize database tables."""
        self._connection = await aiosqlite.connect(self.db_path)
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                session_type TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                channel TEXT,
                peer TEXT,
                metadata TEXT
            )
        """)
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS transcript (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                tool_name TEXT,
                tool_result TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions (session_id)
            )
        """)
        await self._connection.commit()

    async def save_session(self, session: Session) -> None:
        """Save session.

        Args:
            session: Session object
        """
        if not self._connection:
            raise RuntimeError("SessionStore not initialized")

        import json

        await self._connection.execute(
            """
            INSERT OR REPLACE INTO sessions 
            (session_id, agent_name, session_type, status, created_at, updated_at, channel, peer, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.session_id,
                session.agent_name,
                session.session_type.value,
                session.status.value,
                session.created_at.isoformat(),
                session.updated_at.isoformat(),
                session.channel,
                session.peer,
                json.dumps(session.metadata),
            ),
        )

        # Delete old transcript records
        await self._connection.execute(
            "DELETE FROM transcript WHERE session_id = ?",
            (session.session_id,),
        )

        # Insert new transcript records
        for entry in session.transcript:
            await self._connection.execute(
                """
                INSERT INTO transcript (session_id, role, content, timestamp, tool_name, tool_result)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    entry.role,
                    entry.content,
                    entry.timestamp.isoformat(),
                    entry.tool_name,
                    entry.tool_result,
                ),
            )

        await self._connection.commit()

    async def load_session(self, session_id: str) -> Optional[Session]:
        """Load session.

        Args:
            session_id: Session ID

        Returns:
            Session object, or None if not found
        """
        if not self._connection:
            raise RuntimeError("SessionStore not initialized")

        cursor = await self._connection.execute(
            "SELECT * FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()

        if not row:
            return None

        import json

        session = Session(
            session_id=row[0],
            agent_name=row[1],
            session_type=SessionType(row[2]),
            status=SessionStatus(row[3]),
            created_at=datetime.fromisoformat(row[4]),
            updated_at=datetime.fromisoformat(row[5]),
            channel=row[6],
            peer=row[7],
            metadata=json.loads(row[8]) if row[8] else {},
        )

        # Load transcript records
        cursor = await self._connection.execute(
            "SELECT role, content, timestamp, tool_name, tool_result FROM transcript WHERE session_id = ? ORDER BY timestamp",
            (session_id,),
        )
        rows = await cursor.fetchall()

        for row in rows:
            entry = TranscriptEntry(
                role=row[0],
                content=row[1],
                timestamp=datetime.fromisoformat(row[2]),
                tool_name=row[3],
                tool_result=row[4],
            )
            session.transcript.append(entry)

        return session

    async def list_sessions(
        self,
        agent_name: Optional[str] = None,
        status: Optional[SessionStatus] = None,
        limit: int = 100,
    ) -> list[Session]:
        """List sessions.

        Args:
            agent_name: Agent name filter
            status: Status filter
            limit: Maximum number to return

        Returns:
            List of sessions
        """
        if not self._connection:
            raise RuntimeError("SessionStore not initialized")

        query = "SELECT * FROM sessions"
        params: list = []

        conditions = []
        if agent_name:
            conditions.append("agent_name = ?")
            params.append(agent_name)
        if status:
            conditions.append("status = ?")
            params.append(status.value)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        cursor = await self._connection.execute(query, params)
        rows = await cursor.fetchall()

        import json

        sessions = []
        for row in rows:
            session = Session(
                session_id=row[0],
                agent_name=row[1],
                session_type=SessionType(row[2]),
                status=SessionStatus(row[3]),
                created_at=datetime.fromisoformat(row[4]),
                updated_at=datetime.fromisoformat(row[5]),
                channel=row[6],
                peer=row[7],
                metadata=json.loads(row[8]) if row[8] else {},
            )
            sessions.append(session)

        return sessions

    async def delete_session(self, session_id: str) -> None:
        """Delete session.

        Args:
            session_id: Session ID
        """
        if not self._connection:
            raise RuntimeError("SessionStore not initialized")

        await self._connection.execute(
            "DELETE FROM transcript WHERE session_id = ?",
            (session_id,),
        )
        await self._connection.execute(
            "DELETE FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        await self._connection.commit()

    async def cleanup_old_sessions(self, max_age_days: int = 30) -> int:
        """Clean up old sessions.

        Args:
            max_age_days: Maximum retention days

        Returns:
            Number of sessions deleted
        """
        if not self._connection:
            raise RuntimeError("SessionStore not initialized")

        cutoff_date = datetime.utcnow() - timedelta(days=max_age_days)

        cursor = await self._connection.execute(
            "SELECT session_id FROM sessions WHERE created_at < ?",
            (cutoff_date.isoformat(),),
        )
        rows = await cursor.fetchall()

        for row in rows:
            session_id = row[0]
            await self.delete_session(session_id)

        return len(rows)

    async def append_transcript(self, session_id: str, entry: TranscriptEntry) -> None:
        """Append transcript entry.

        Args:
            session_id: Session ID
            entry: Transcript entry
        """
        if not self._connection:
            raise RuntimeError("SessionStore not initialized")

        await self._connection.execute(
            """
            INSERT INTO transcript (session_id, role, content, timestamp, tool_name, tool_result)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                entry.role,
                entry.content,
                entry.timestamp.isoformat(),
                entry.tool_name,
                entry.tool_result,
            ),
        )

        await self._connection.execute(
            "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
            (datetime.utcnow().isoformat(), session_id),
        )

        await self._connection.commit()
