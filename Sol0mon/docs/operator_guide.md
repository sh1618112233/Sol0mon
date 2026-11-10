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
must not be exposed by a firewall, security group, proxy, or public listener.
The browser workflow is authenticated and interactive; it is not a headless,
unattended provider integration.

Verification-provider variables are listed in `.env.example`. The current
browser debate uses the signed-in websites; no GPT or Gemini API credentials
are used. Keep `.env`, browser profiles, cookies, and provider credentials out
of Git and logs.

## Persistent state

By default, application data is stored below the repository's `data/`
directory, independent of the caller's current working directory. Configure
`SOLOMON_DATA_DIR` to place persistent state on an attached durable volume.
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
are active. Store backups on a separate durable volume and periodically test
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

## AWS deployment checklist

No systemd unit is provided: the current CLI can prompt for execution mode,
requires signed-in browser sessions, and does not expose a fully unattended
service lifecycle. Operate it from a supervised interactive session until
those requirements change.

- [ ] Provision Python 3.12 and install `Sol0mon/requirements.txt` in a
      virtual environment from a clean checkout.
- [ ] Install compatible Chrome/Chromium and verify Playwright's CDP
      connection against `127.0.0.1:9222`.
- [ ] Launch a dedicated browser profile on the instance; never copy personal
      cookies or a workstation profile.
- [ ] Bind remote debugging only to loopback; verify port 9222 is absent from
      public listeners and blocked by the instance security group.
- [ ] Authenticate to ChatGPT and Gemini interactively and plan for session
      expiry and manual reauthentication.
- [ ] Configure required verification-provider credentials via a protected
      environment file or secret manager; restrict file permissions.
- [ ] Set `SOLOMON_DATA_DIR` and `SOL0MON_BUILD_WORKSPACE_ROOT` to durable
      attached storage with adequate free space.
- [ ] Confirm the resolved DB paths and workspace location before processing
      real work. Keep all stores on the intended persistent volume.
- [ ] Establish stopped-process backups for the SQLite databases and
      workspaces, store copies separately, and test restore plus
      `PRAGMA integrity_check`.
- [ ] Run the offline suite, then validate browser connectivity, a complete
      debate, follow-up correction, human resolution, build execution, restart
      and resume, and backup recovery in a non-production workspace.
- [ ] Monitor process output, provider/browser failures, disk capacity, and
      database health. Stop cleanly before copying databases or upgrading.

AWS deployment, browser connectivity, backup restoration, and a complete
workflow have not been validated by this repository change. Local tests do
not establish AWS deployment readiness.
