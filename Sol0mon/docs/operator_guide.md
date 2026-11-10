# Operator Guide

## Runtime and installation

Use Python 3.12. From `Sol0mon/`, create a virtual environment and install the
pinned dependencies:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows, use `py -3.12 -m venv .venv` and activate
`.venv\Scripts\Activate.ps1`. Chrome or Chromium is required for the GPT and
Gemini browser sessions. The application connects to Chrome DevTools at
`127.0.0.1:9222`; launch Chrome with a dedicated profile outside the checkout
and sign in interactively. The debugging port must remain loopback-only and
must not be exposed by a firewall, proxy, or public listener.
The browser workflow is authenticated and interactive; it is not a headless,
unattended provider integration.

Verification-provider variables are listed in `.env.example`. The current
browser debate uses the signed-in websites; no GPT or Gemini API credentials
are used. Keep `.env`, browser profiles, cookies, and provider credentials out
of Git and logs.

## Persistent state

By default, application data is stored below the repository's `data/`
directory, independent of the caller's current working directory. Configure
`SOLOMON_DATA_DIR` to place persistent state on storage that remains available
across restarts.
Individual database overrides are `ANSWER_MEMORY_DB`, `WORKSPACE_DB`,
`HUMAN_DECISION_DB`, and `JOB_DB`; workspace and human decision databases
must resolve to the same path. `SOL0MON_BUILD_WORKSPACE_ROOT` configures build
workspace artifacts. Relative override paths are resolved from the repository
root.

Existing databases in their historical repository locations are used in
place. If multiple possible legacy databases are found, startup fails with
their paths rather than creating a new empty store. Copy data only while the
application is stopped, and set the overrides to the copied destinations.
No automatic migration deletes or rewrites source databases.

SQLite uses WAL mode. Back up while Sol0mon is stopped, or use SQLite's
online backup API; do not copy only the main database file while WAL writers
are active. Store backups separately and periodically test
restoration into a disposable directory with `PRAGMA integrity_check`.
Retain the database together with the build workspace directory because
workspace rows refer to filesystem artifacts.

The current `debates` schema stores the final GPT and Gemini position text in
`gpt_final` and `gemini_final`. A separate memory policy document says raw
transcripts should not be retained. The implementation and policy therefore
disagree about retention. This guide does not remove historical content or
change retention; an owner decision is required before any destructive
cleanup or retention migration.

## Human decisions and resumption

Inspect decisions for a workspace:

```bash
python sol0mon_cli.py human list WORKSPACE_ID
```

Resolve a pending item by its workspace-scoped ID and an exact recorded
option:

```bash
python sol0mon_cli.py human resolve WORKSPACE_ID DECISION_ID "Option A"
python sol0mon_cli.py resume WORKSPACE_ID
```

The chosen option is recorded as a human decision and added to that workspace's
active constraints. It is not recorded as an independently verified fact.
Invalid options, blank choices, and IDs from another workspace are rejected.
If a pending item has no explicit options, it cannot be resolved through this
command and requires a corrected governance prompt/outcome.

`python sol0mon_cli.py resume --help` describes workspace selection and
follow-up behavior. After an interrupted process, inspect the workspace status
and artifacts before retrying. Do not manually edit SQLite rows while the
application is running.

## Tests

From the repository root:

```bash
python -m pytest
```

This runs the offline suite and excludes tests marked `live`. Live tests must
be explicitly enabled and require local preparation:

```bash
SOLOMON_RUN_LIVE_TESTS=1 python -m pytest -m live
```

On PowerShell, set `$env:SOLOMON_RUN_LIVE_TESTS = "1"` before the command.
Offline tests must not use personal database files, signed-in browser
profiles, or provider credentials.

## Operating a local or hosted instance

The current CLI uses authenticated browser sessions and may prompt for
execution choices. Run it in a supervised interactive session; it has not
been validated as an unattended service.

- Keep the dedicated browser profile and application data private and outside
  source control.
- Restrict the browser debugging endpoint to loopback (`127.0.0.1:9222`).
- Configure only the verification provider you intend to use, and protect its
  environment file with operating-system permissions.
- Put `SOLOMON_DATA_DIR` and `SOL0MON_BUILD_WORKSPACE_ROOT` on storage that
  persists across restarts if the checkout is temporary.
- Back up databases while Sol0mon is stopped, or use SQLite's online backup
  API. Test restoration in a disposable directory and run
  `PRAGMA integrity_check` before resuming work.
- After an interruption, inspect workspace status and artifacts before
  resuming. Avoid concurrent processes writing to the same workspace.

The browser-provider workflow, hosted operation, and backup restoration have
not been independently validated here. Offline tests do not establish live
provider or deployment readiness.

## AWS validation preparation (Linux/server version)

This checklist is generic preparation for an operator-managed AWS instance;
it contains no owner account, host, network, or deployment configuration.

- [ ] Start from a clean Linux checkout and install Python 3.12.
- [ ] Create a virtual environment and install `Sol0mon/requirements.txt`.
- [ ] Copy `.env.example` to `.env`; configure only the chosen verifier and
      confirm secrets are protected from source control and logs.
- [ ] Configure durable `SOLOMON_DATA_DIR` and
      `SOL0MON_BUILD_WORKSPACE_ROOT` paths, then verify startup logs show the
      intended locations.
- [ ] Install compatible Chromium/Chrome, create a dedicated profile, sign in
      to the required websites, and confirm DevTools is loopback-only.
- [ ] Run the offline test suite and CLI help before any live calls.
- [ ] In a non-production workspace, validate a controlled debate, follow-up
      correction, human resolution, implementation gate, build, restart, and
      resume using the intended providers.
- [ ] Back up SQLite consistently and workspace artifacts together; restore
      into a separate path and verify `PRAGMA integrity_check` before use.

AWS validation remains pending until these steps are performed on the
operator's own instance. Windows deployment and testing remain separate.
