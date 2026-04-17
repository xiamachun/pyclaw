"""Message routing resolver.

Routes messages to corresponding Agent roles based on channel, user, and other context.
Reference: OpenClaw resolve-route.ts matching priority design.
"""

import logging
from typing import Optional

from pyclaw.config.schema import AgentEntry, Binding, PyClawConfig

logger = logging.getLogger(__name__)

DEFAULT_AGENT_ID = "main"


class RouteResolver:
    """Routing resolver.

    Routes messages to corresponding Agent roles based on channel, user, and other context.

    Matching priority (from high to low):
    1. channel + peer_id exact match  (+10)
    2. channel + peer_kind match    (+5)
    3. channel + account_id match   (+3)
    4. channel match                (+1)
    5. Default Agent (default=True or first in list)
    """

    def __init__(self, config: PyClawConfig) -> None:
        self._config = config

    def resolve(
        self,
        channel: str,
        peer_id: Optional[str] = None,
        peer_kind: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> AgentEntry:
        """Resolve which Agent the message should be routed to.

        Args:
            channel: Channel name (dingtalk / wechat / feishu / web, etc.)
            peer_id: Peer user ID
            peer_kind: Conversation type (direct / group)
            account_id: Account ID

        Returns:
            Matched AgentEntry
        """
        best_binding: Optional[Binding] = None
        best_score = -1

        for binding in self._config.bindings:
            score = self._match_score(binding, channel, peer_id, peer_kind, account_id)
            if score > best_score:
                best_score = score
                best_binding = binding

        if best_binding is not None:
            agent = self.find_agent(best_binding.agent_id)
            if agent is not None:
                logger.info(
                    "Routed %s/%s -> agent '%s' (score=%d)",
                    channel,
                    peer_id or "*",
                    agent.id,
                    best_score,
                )
                return agent

        default_agent = self.get_default_agent()
        logger.info(
            "No binding matched for %s/%s, using default agent '%s'",
            channel,
            peer_id or "*",
            default_agent.id,
        )
        return default_agent

    def find_agent(self, agent_id: str) -> Optional[AgentEntry]:
        """Find Agent by ID.

        Args:
            agent_id: Agent ID

        Returns:
            Found AgentEntry, None if not exists
        """
        for agent in self._config.agents.list:
            if agent.id == agent_id:
                return agent
        return None

    def get_default_agent(self) -> AgentEntry:
        """Get default Agent.

        Priority: marked default=True -> first in list -> built-in fallback.

        Returns:
            Default AgentEntry
        """
        for agent in self._config.agents.list:
            if agent.default:
                return agent
        if self._config.agents.list:
            return self._config.agents.list[0]
        return AgentEntry(id=DEFAULT_AGENT_ID, default=True, name="PyClaw")

    def list_agents(self) -> list[AgentEntry]:
        """List all configured Agents.

        Returns:
            AgentEntry list
        """
        if self._config.agents.list:
            return list(self._config.agents.list)
        return [AgentEntry(id=DEFAULT_AGENT_ID, default=True, name="PyClaw")]

    def add_agent(self, agent: AgentEntry) -> None:
        """Add a new agent to the resolver."""
        self._config.agents.list.append(agent)
        logger.info("Added agent: %s", agent.id)

    def remove_agent(self, agent_id: str) -> None:
        """Remove an agent from the resolver."""
        self._config.agents.list = [a for a in self._config.agents.list if a.id != agent_id]
        logger.info("Removed agent: %s", agent_id)

    @staticmethod
    def _match_score(
        binding: Binding,
        channel: str,
        peer_id: Optional[str],
        peer_kind: Optional[str],
        account_id: Optional[str],
    ) -> int:
        """Calculate binding rule match score.

        Returns -1 for no match, higher positive numbers indicate higher priority.
        """
        match = binding.match

        # channel is the required base condition
        if match.channel and match.channel != channel:
            return -1

        score = 1  # channel match = 1 point

        # peer_id exact match (highest priority)
        if match.peer_id:
            if peer_id and match.peer_id == peer_id:
                score += 10
            else:
                return -1  # peer_id configured but doesn't match

        # peer_kind match
        if match.peer_kind:
            if peer_kind and match.peer_kind == peer_kind:
                score += 5
            else:
                return -1

        # account_id match
        if match.account_id:
            if account_id and match.account_id == account_id:
                score += 3
            else:
                return -1

        return score