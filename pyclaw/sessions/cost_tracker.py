"""
Session cost tracking and reporting.

Tracks:
- Token usage per session
- API costs
- Daily/monthly aggregates
- Cost alerts
"""

import json
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, date, timedelta
from pathlib import Path
import aiosqlite

from pyclaw.sessions.models import SessionCost

logger = logging.getLogger(__name__)


# Model pricing (USD per 1K tokens)
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    # OpenAI models
    "gpt-4-turbo": {"input": 0.01, "output": 0.03},
    "gpt-4": {"input": 0.03, "output": 0.06},
    "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015},
    
    # Claude models
    "claude-3-opus": {"input": 0.015, "output": 0.075},
    "claude-3-sonnet": {"input": 0.003, "output": 0.015},
    "claude-3-haiku": {"input": 0.00025, "output": 0.00125},
    
    # Default for unknown models
    "default": {"input": 0.001, "output": 0.002},
}


def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """
    Calculate the cost for a model usage.
    
    Args:
        model: Model name
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
        
    Returns:
        Cost in USD
    """
    # Find pricing
    pricing = MODEL_PRICING.get(model)
    if not pricing:
        # Try to match by prefix
        for model_name, p in MODEL_PRICING.items():
            if model.startswith(model_name) or model_name in model.lower():
                pricing = p
                break
        
        if not pricing:
            pricing = MODEL_PRICING["default"]
    
    input_cost = (input_tokens / 1000) * pricing["input"]
    output_cost = (output_tokens / 1000) * pricing["output"]
    
    return input_cost + output_cost


class CostTracker:
    """
    Tracks and reports session costs.
    
    Features:
    - Per-session cost tracking
    - Daily/monthly aggregates
    - SQLite persistence
    - Cost alerts
    """
    
    def __init__(
        self,
        db_path: Optional[Path] = None,
        daily_budget_usd: Optional[float] = None,
    ):
        """
        Initialize the cost tracker.
        
        Args:
            db_path: Path to SQLite database
            daily_budget_usd: Optional daily budget limit
        """
        if db_path is None:
            from pyclaw.config.paths import get_paths as _get_paths
            db_path = _get_paths().costs_db
        
        self._db_path = db_path
        self._daily_budget = daily_budget_usd
        self._db: Optional[aiosqlite.Connection] = None
        
        # In-memory cache
        self._session_costs: Dict[str, SessionCost] = {}
    
    async def initialize(self) -> None:
        """Initialize the database."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._db = await aiosqlite.connect(str(self._db_path))
        
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                model TEXT,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cost_usd REAL NOT NULL,
                timestamp TEXT NOT NULL
            );
            
            CREATE INDEX IF NOT EXISTS idx_usage_session ON usage(session_id);
            CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON usage(timestamp);
            
            CREATE TABLE IF NOT EXISTS daily_totals (
                date TEXT PRIMARY KEY,
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0,
                total_cost_usd REAL DEFAULT 0,
                message_count INTEGER DEFAULT 0
            );
        """)
        
        await self._db.commit()
        logger.info("CostTracker initialized")
    
    async def record_usage(
        self,
        session_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> SessionCost:
        """
        Record usage for a session.
        
        Args:
            session_id: Session identifier
            model: Model name
            input_tokens: Input token count
            output_tokens: Output token count
            
        Returns:
            Updated SessionCost
        """
        cost_usd = calculate_cost(model, input_tokens, output_tokens)
        timestamp = datetime.now()
        
        # Update session cost
        if session_id not in self._session_costs:
            self._session_costs[session_id] = SessionCost(session_id=session_id)
        
        session_cost = self._session_costs[session_id]
        session_cost.add_usage(input_tokens, output_tokens, cost_usd)
        session_cost.model = model
        
        # Persist to database
        if self._db:
            await self._db.execute(
                """
                INSERT INTO usage (session_id, model, input_tokens, output_tokens, cost_usd, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, model, input_tokens, output_tokens, cost_usd, timestamp.isoformat())
            )
            
            # Update daily totals
            today = timestamp.date().isoformat()
            await self._db.execute(
                """
                INSERT INTO daily_totals (date, total_input_tokens, total_output_tokens, total_cost_usd, message_count)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(date) DO UPDATE SET
                    total_input_tokens = total_input_tokens + ?,
                    total_output_tokens = total_output_tokens + ?,
                    total_cost_usd = total_cost_usd + ?,
                    message_count = message_count + 1
                """,
                (today, input_tokens, output_tokens, cost_usd,
                 input_tokens, output_tokens, cost_usd)
            )
            
            await self._db.commit()
        
        logger.debug(
            f"Recorded usage: session={session_id}, tokens={input_tokens}+{output_tokens}, cost=${cost_usd:.4f}"
        )
        
        # Check budget
        if self._daily_budget:
            daily_cost = await self.get_daily_cost()
            if daily_cost >= self._daily_budget:
                logger.warning("Daily budget exceeded: $%.2f >= $%.2f", daily_cost, self._daily_budget)
        
        return session_cost
    
    async def get_session_cost(self, session_id: str) -> Optional[SessionCost]:
        """
        Get cost data for a session.
        
        Args:
            session_id: Session identifier
            
        Returns:
            SessionCost or None
        """
        # Check cache first
        if session_id in self._session_costs:
            return self._session_costs[session_id]
        
        # Load from database
        if not self._db:
            return None
        
        async with self._db.execute(
            """
            SELECT 
                COUNT(*) as message_count,
                SUM(input_tokens) as input_tokens,
                SUM(output_tokens) as output_tokens,
                SUM(cost_usd) as total_cost,
                MAX(model) as model,
                MAX(timestamp) as last_updated
            FROM usage
            WHERE session_id = ?
            """,
            (session_id,)
        ) as cursor:
            row = await cursor.fetchone()
            
            if row and row[0] > 0:
                cost = SessionCost(
                    session_id=session_id,
                    message_count=row[0],
                    input_tokens=row[1] or 0,
                    output_tokens=row[2] or 0,
                    total_tokens=(row[1] or 0) + (row[2] or 0),
                    total_cost_usd=row[3] or 0,
                    model=row[4],
                    last_updated=datetime.fromisoformat(row[5]) if row[5] else datetime.now(),
                )
                self._session_costs[session_id] = cost
                return cost
        
        return None
    
    async def get_daily_cost(self, target_date: Optional[date] = None) -> float:
        """
        Get total cost for a day.
        
        Args:
            target_date: Date to query (default: today)
            
        Returns:
            Total cost in USD
        """
        if not self._db:
            return 0.0
        
        if target_date is None:
            target_date = date.today()
        
        async with self._db.execute(
            "SELECT total_cost_usd FROM daily_totals WHERE date = ?",
            (target_date.isoformat(),)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0.0
    
    async def get_daily_stats(self, target_date: Optional[date] = None) -> Dict[str, Any]:
        """
        Get detailed stats for a day.
        
        Args:
            target_date: Date to query (default: today)
            
        Returns:
            Dict with daily statistics
        """
        if not self._db:
            return {}
        
        if target_date is None:
            target_date = date.today()
        
        async with self._db.execute(
            "SELECT * FROM daily_totals WHERE date = ?",
            (target_date.isoformat(),)
        ) as cursor:
            row = await cursor.fetchone()
            
            if row:
                return {
                    "date": row[0],
                    "total_input_tokens": row[1],
                    "total_output_tokens": row[2],
                    "total_cost_usd": row[3],
                    "message_count": row[4],
                }
            
            return {
                "date": target_date.isoformat(),
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cost_usd": 0,
                "message_count": 0,
            }
    
    async def get_monthly_cost(self, year: int, month: int) -> float:
        """
        Get total cost for a month.
        
        Args:
            year: Year
            month: Month (1-12)
            
        Returns:
            Total cost in USD
        """
        if not self._db:
            return 0.0
        
        month_start = f"{year:04d}-{month:02d}-01"
        if month == 12:
            month_end = f"{year + 1:04d}-01-01"
        else:
            month_end = f"{year:04d}-{month + 1:02d}-01"
        
        async with self._db.execute(
            """
            SELECT SUM(total_cost_usd) FROM daily_totals 
            WHERE date >= ? AND date < ?
            """,
            (month_start, month_end)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row and row[0] else 0.0
    
    async def get_cost_history(
        self,
        days: int = 30,
    ) -> List[Dict[str, Any]]:
        """
        Get cost history for recent days.
        
        Args:
            days: Number of days to include
            
        Returns:
            List of daily stats
        """
        if not self._db:
            return []
        
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        
        history = []
        async with self._db.execute(
            """
            SELECT * FROM daily_totals 
            WHERE date >= ?
            ORDER BY date DESC
            """,
            (cutoff,)
        ) as cursor:
            async for row in cursor:
                history.append({
                    "date": row[0],
                    "total_input_tokens": row[1],
                    "total_output_tokens": row[2],
                    "total_cost_usd": row[3],
                    "message_count": row[4],
                })
        
        return history
    
    async def get_top_sessions(
        self,
        limit: int = 10,
        days: int = 7,
    ) -> List[Dict[str, Any]]:
        """
        Get top sessions by cost.
        
        Args:
            limit: Maximum sessions to return
            days: Look back period
            
        Returns:
            List of session cost info
        """
        if not self._db:
            return []
        
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        
        sessions = []
        async with self._db.execute(
            """
            SELECT 
                session_id,
                SUM(input_tokens) as input_tokens,
                SUM(output_tokens) as output_tokens,
                SUM(cost_usd) as total_cost,
                COUNT(*) as message_count
            FROM usage
            WHERE timestamp >= ?
            GROUP BY session_id
            ORDER BY total_cost DESC
            LIMIT ?
            """,
            (cutoff, limit)
        ) as cursor:
            async for row in cursor:
                sessions.append({
                    "session_id": row[0],
                    "input_tokens": row[1],
                    "output_tokens": row[2],
                    "total_cost_usd": row[3],
                    "message_count": row[4],
                })
        
        return sessions
    
    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None


# Global instance
_tracker_instance: Optional[CostTracker] = None


async def get_cost_tracker(
    daily_budget_usd: Optional[float] = None,
) -> CostTracker:
    """Get the global CostTracker instance."""
    global _tracker_instance
    if _tracker_instance is None:
        _tracker_instance = CostTracker(daily_budget_usd=daily_budget_usd)
        await _tracker_instance.initialize()
    return _tracker_instance
