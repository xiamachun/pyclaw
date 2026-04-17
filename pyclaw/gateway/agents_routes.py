"""Agent list API routes for multi-agent support."""

import json
import logging
from typing import Optional, List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from pyclaw.config.paths import get_paths
from pyclaw.routing.resolver import RouteResolver

logger = logging.getLogger(__name__)

class AgentIdentityUpdate(BaseModel):
    """Agent identity update model."""
    name: Optional[str] = Field(None, description="Display name")
    theme: Optional[str] = Field(None, description="Role theme description")
    emoji: Optional[str] = Field(None, description="Role emoji")

class AgentUpdateRequest(BaseModel):
    """Agent update request model."""
    name: Optional[str] = Field(None, description="Human-readable agent name")
    system_prompt: Optional[str] = Field(None, description="System prompt")
    identity: Optional[AgentIdentityUpdate] = Field(None, description="Agent identity configuration")
    skills: Optional[List[str]] = Field(None, description="Available skills")

class AgentCreateRequest(BaseModel):
    """Agent creation request."""
    id: str = Field(..., description="Unique agent identifier")
    name: str = Field(..., description="Human-readable agent name")
    system_prompt: Optional[str] = Field(None, description="System prompt")
    identity: Optional[AgentIdentityUpdate] = Field(None, description="Agent identity")
    skills: Optional[List[str]] = Field(None, description="Available skills")
    default: bool = Field(False, description="Whether this is the default agent")

def create_agents_router(route_resolver: RouteResolver) -> APIRouter:
    """Create router for agent listing endpoints.

    Args:
        route_resolver: The route resolver instance.

    Returns:
        APIRouter with /v1/agents endpoints.
    """
    router = APIRouter(prefix="/v1/agents", tags=["agents"])

    @router.get("/")
    async def list_agents():
        """Return all configured agents with their identity info."""
        agents = route_resolver.list_agents()
        default_agent = route_resolver.get_default_agent()

        return {
            "agents": [
                {
                    "id": agent.id,
                    "name": agent.name,
                    "default": agent.default,
                    "emoji": agent.identity.emoji if agent.identity else "🤖",
                    "theme": agent.identity.theme if agent.identity else None,
                }
                for agent in agents
            ],
            "default_agent_id": default_agent.id,
        }

    @router.put("/{agent_id}")
    async def update_agent(agent_id: str, request: AgentUpdateRequest):
        """Update agent configuration in memory and persist to config.

        Args:
            agent_id: Agent identifier
            request: Update request containing fields to update

        Returns:
            Updated agent information

        Raises:
            HTTPException: If agent not found
        """
        agents = route_resolver.list_agents()
        target_agent = None

        for agent in agents:
            if agent.id == agent_id:
                target_agent = agent
                break

        if target_agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

        # Update fields in memory
        if request.name is not None:
            target_agent.name = request.name
            logger.info("Updated agent %s name to: %s", agent_id, request.name)

        if request.system_prompt is not None:
            target_agent.system_prompt = request.system_prompt
            logger.info("Updated agent %s system_prompt", agent_id)

        if request.identity is not None:
            if target_agent.identity is None:
                from pyclaw.config.schema import AgentIdentityConfig
                target_agent.identity = AgentIdentityConfig(
                    name=request.identity.name or target_agent.name,
                    theme=request.identity.theme or "helpful assistant",
                    emoji=request.identity.emoji or "🤖"
                )
            else:
                if request.identity.name is not None:
                    target_agent.identity.name = request.identity.name
                if request.identity.theme is not None:
                    target_agent.identity.theme = request.identity.theme
                if request.identity.emoji is not None:
                    target_agent.identity.emoji = request.identity.emoji
            logger.info("Updated agent %s identity", agent_id)

        if request.skills is not None:
            target_agent.skills = request.skills
            logger.info("Updated agent %s skills: %s", agent_id, request.skills)

        # Persist changes to config file
        _persist_agents_to_config(route_resolver)

        return {
            "id": target_agent.id,
            "name": target_agent.name,
            "system_prompt": target_agent.system_prompt,
            "identity": {
                "name": target_agent.identity.name if target_agent.identity else None,
                "theme": target_agent.identity.theme if target_agent.identity else None,
                "emoji": target_agent.identity.emoji if target_agent.identity else None,
            },
            "skills": target_agent.skills,
            "message": "Agent configuration updated and persisted",
        }

    @router.post("/")
    async def create_agent(request: AgentCreateRequest):
        """Create a new agent and persist to config."""
        # Check if agent ID already exists
        agents = route_resolver.list_agents()
        for agent in agents:
            if agent.id == request.id:
                raise HTTPException(status_code=409, detail=f"Agent {request.id} already exists")
        
        # Create new AgentEntry
        from pyclaw.config.schema import AgentEntry, AgentIdentityConfig
        identity = None
        if request.identity:
            identity = AgentIdentityConfig(
                name=request.identity.name or request.name,
                theme=request.identity.theme or "helpful assistant",
                emoji=request.identity.emoji or "🤖"
            )
        
        new_agent = AgentEntry(
            id=request.id,
            name=request.name,
            default=request.default,
            system_prompt=request.system_prompt,
            identity=identity,
            skills=request.skills or [],
        )
        
        # Add to resolver
        route_resolver.add_agent(new_agent)
        
        # Persist
        _persist_agents_to_config(route_resolver)
        
        return {
            "id": new_agent.id,
            "name": new_agent.name,
            "default": new_agent.default,
            "emoji": identity.emoji if identity else "🤖",
            "message": "Agent created and persisted",
        }

    @router.delete("/{agent_id}")
    async def delete_agent(agent_id: str):
        """Delete an agent and persist changes."""
        agents = route_resolver.list_agents()
        target = None
        for agent in agents:
            if agent.id == agent_id:
                target = agent
                break
        
        if target is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
        
        if getattr(target, "default", False):
            raise HTTPException(status_code=400, detail="Cannot delete the default agent")
        
        route_resolver.remove_agent(agent_id)
        _persist_agents_to_config(route_resolver)
        
        return {"message": f"Agent {agent_id} deleted"}

    return router

def _persist_agents_to_config(route_resolver: RouteResolver) -> None:
    """Persist current agent list to pyclaw.json config file."""
    paths = get_paths()
    config_path = paths.config_file
    
    try:
        with open(config_path, "r") as f:
            config_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        config_data = {}
    
    agents = route_resolver.list_agents()
    agents_list = []
    for agent in agents:
        agent_dict = {
            "id": agent.id,
            "name": agent.name,
            "default": getattr(agent, "default", False),
        }
        if agent.identity:
            agent_dict["identity"] = {
                "name": agent.identity.name,
                "theme": agent.identity.theme,
                "emoji": agent.identity.emoji,
            }
        if agent.system_prompt:
            agent_dict["systemPrompt"] = agent.system_prompt
        if agent.skills:
            agent_dict["skills"] = agent.skills
        if hasattr(agent, "model") and agent.model:
            # Preserve model config
            model_dict = {}
            if hasattr(agent.model, "primary") and agent.model.primary:
                model_dict["primary"] = agent.model.primary
            if model_dict:
                agent_dict["model"] = model_dict
        agents_list.append(agent_dict)
    
    if "agents" not in config_data:
        config_data["agents"] = {}
    config_data["agents"]["list"] = agents_list
    
    with open(config_path, "w") as f:
        json.dump(config_data, f, indent=2, ensure_ascii=False)
    
    logger.info("Persisted %d agents to %s", len(agents_list), config_path)