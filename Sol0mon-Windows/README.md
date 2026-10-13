# Sol0mon for Windows

Sol0mon is a Python application for coordinating research, multi-model debate, verification, synthesis, and optional implementation work. It stores answers and workflow state in local workspaces.

## How It Works

Requests are planned and classified, research and architecture work is coordinated, disagreements are debated and checked, and the result is synthesized and persisted. Optional build and resume flows connect to the implementation pipeline. Outputs require human review; correctness is not guaranteed.

## Architecture

    CLI request
        ↓
    Workflow and project planning
        ↓
    Research and architecture → conflict extraction and debate
        ↓
    Verification and consensus → synthesis and workspace
                                      ↓
                             optional build or resume
## Main Components

- sol0mon_cli.py: command-line interface.
- pipeline_runner.py, workflow_orchestrator.py, and pipeline_scheduler.py: coordinate workflows.
- project_planner.py, research_planner.py, and debate_engine.py: plan and run work.
- provider_router.py and provider clients: call hosted or local model providers.
- verification_engine.py, consensus_engine.py, and synthesis_engine.py: verify, assess, and synthesize results.
- workspace_manager.py and related modules: persist and resume local work.

The Python modules are primarily at the repository root. coding_agents/ contains coding-agent adapters, docs/ contains engineering notes, and tests/ plus root-level test scripts contain tests.

## Requirements

- Python 3.12 (the version represented by the supplied environment; a clean Windows install has not been verified).
- Packages listed in requirements.txt.
- Credentials for any hosted providers used.
- Chrome with remote debugging enabled on 127.0.0.1:9222 for browser-backed GPT/Gemini sessions. Keep the authenticated profile outside this repository.
- An external coding-agent tool for optional build execution.

## Installation (PowerShell)

From this directory:

    py -3.12 -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    Copy-Item .env.example .env

If the Python launcher is unavailable, use a Python 3.12 executable to create the environment. Set only the provider keys needed in .env; never commit it.

## Browser Sessions

Start Chrome with the remote debugging port enabled. For a standard system installation, the command is:

    & "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="$env:LOCALAPPDATA\Sol0mon\ChromeProfile"

Adjust the Chrome executable path if needed. Authenticate with the required provider UI in that profile. The profile contains private session data and must stay outside the repository.

## Running

Use the environment's Python executable:

    .\.venv\Scripts\python.exe sol0mon_cli.py --help
    .\.venv\Scripts\python.exe sol0mon_cli.py debate "Describe the system you want to explore"
    .\.venv\Scripts\python.exe sol0mon_cli.py status

Debates may call external providers or browser sessions. Build and resume commands are listed by the CLI help.

## Testing

    .\.venv\Scripts\python.exe run_all_tests.py

Some tests need external tools or a running Chrome session. Report skips and failures as such.

## Contributing

See CONTRIBUTING.md. Keep changes focused, add tests for behavior changes, and report test results. Do not include credentials, browser profiles, logs, databases, or user workspace data.

## Security

See SECURITY.md. Do not report vulnerabilities in public issues.

## Roadmap

No public roadmap is maintained here. Engineering documents may describe plans rather than completed functionality.

## License

No license file or license decision is present. This candidate is not ready for public release until maintainers choose and add a license.
