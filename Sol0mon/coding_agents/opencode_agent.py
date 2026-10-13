"""
Project Sol0mon - OpenCode coding agent (subprocess backend).

Wraps the opencode CLI for build-phase code generation. Execution is
non-interactive: a single `opencode run --auto --dir <working_dir>` subprocess
per plan, returning the existing AgentResult contract {success, output, error}.

Audit D3: replaces the Phase-1 stub body with a real subprocess adapter.
"""

from __future__ import annotations

import shutil
import subprocess

from coding_agents.base_agent import AgentResult, BaseAgent

# Hard ceiling on a single opencode run.
OPENCODE_TIMEOUT_SECONDS = 600


class OpenCodeAgent(BaseAgent):
    """Real backend that drives the opencode CLI via subprocess."""

    name = "opencode"

    def run(self, plan: str, working_dir: str | None = None,
            timeout: int | None = None) -> AgentResult:
        if not plan or not plan.strip():
            return AgentResult(success=False, error="empty plan")
        if shutil.which("opencode") is None:
            return AgentResult(success=False,
                               error="opencode CLI not found in PATH")
        cmd = ["opencode", "run", plan, "--auto"]
        if working_dir:
            cmd += ["--dir", working_dir]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout or OPENCODE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return AgentResult(
                success=False,
                error=f"opencode timed out ({timeout or OPENCODE_TIMEOUT_SECONDS}s)",
            )
        except Exception as e:
            return AgentResult(success=False, error=f"opencode CLI error: {e}")
        if proc.returncode == 0:
            return AgentResult(success=True, output=proc.stdout.strip())
        tail = (proc.stderr or proc.stdout or "")[:500]
        return AgentResult(success=False,
                           error=f"opencode exit={proc.returncode}: {tail}")


if __name__ == "__main__":
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        r = OpenCodeAgent(workspace_id="smoke").run(
            "Create a file named hello.txt containing exactly the text hello "
            "in the current directory. Then stop. Do not create anything else.",
            working_dir=tmp,
        )
        assert r.success, f"smoke failed: {r.error}"
        assert (Path(tmp) / "hello.txt").is_file()
        print("opencode_agent smoke test PASSED")
