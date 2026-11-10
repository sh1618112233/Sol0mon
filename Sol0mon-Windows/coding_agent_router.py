"""
Project Sol0mon - coding agent router (Phase 1 stub).

Selects a coding agent for a build workspace. Phase 1 registers the
opencode agent only and performs no dispatch; later phases add Claude
Code, Codex, and Gemini CLI routes.
"""

from __future__ import annotations

from typing import Optional

from coding_agents.base_agent import BaseAgent
from coding_agents.opencode_agent import OpenCodeAgent

# Registered agents keyed by name. Extended in later phases.
AGENTS: dict[str, type[BaseAgent]] = {
    "opencode": OpenCodeAgent,
}


def get_agent(name: str, workspace_id: str) -> Optional[BaseAgent]:
    """Return an agent instance by name, or None if unregistered."""
    cls = AGENTS.get(name)
    if cls is None:
        return None
    return cls(workspace_id=workspace_id)


def available_agents() -> list[str]:
    """Names of agents registered with the router."""
    return list(AGENTS)
