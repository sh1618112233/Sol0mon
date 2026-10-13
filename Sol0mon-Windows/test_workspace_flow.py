# test_workspace_flow.py

from workspace_orchestrator import WorkspaceOrchestrator

orchestrator = WorkspaceOrchestrator()

workspace_id = orchestrator.submit_workspace(
    prompt="Design Project Sol0mon for 10000 concurrent jobs"
)

print(f"Workspace: {workspace_id}")
