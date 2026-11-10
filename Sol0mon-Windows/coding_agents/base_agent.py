"""
Project Sol0mon - coding agent base class (Phase 1 stub).

Defines the interface all build-phase coding agents implement. No
execution in Phase 1; subclasses override run() once wired to a real
backend (opencode CLI, Claude Code, Codex, Gemini CLI - later phases).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class AgentResult:
    """Placeholder result of an agent run. Populated in a later phase."""

    success: bool = False
    output: str = ""
    error: str = ""


class BaseAgent:
    """Base interface for coding agents. Subclasses implement run()."""

    name: str = "base"

    def __init__(self, workspace_id: str, **options: Any) -> None:
        self.workspace_id = workspace_id
        self.options = options

    def run(self, plan: str) -> AgentResult:
        raise NotImplementedError(
            f"{self.name} agent run() not implemented in Phase 1"
        )
