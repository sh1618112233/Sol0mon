"""
Project Sol0mon — Workspace Resume UI (lifecycle-aware, FS-authoritative).

Brings the existing canonical sources together for the
"Resume Recent" user experience. No new state, no duplicate metadata:

    workspace discovery       -> build_workspace.list_build_workspaces()  (FS)
                                  + build_workspace.load_workspace()       (FS)
    engineering lifecycle      -> lifecycle.current_lifecycle()             (FS)
    SQL metadata (optional)    -> workspace_mgr.load_workspace() (best-effort join)
    resume action by stage     -> resume.resume_build / resume_execution /
                                  resume_validation / WorkspaceDebateEngine

Two resume methods are exposed (spec):

    Method 1 (default): list the five most recent workspaces and let the
        user select by project number; Sol0mon resumes from that
        workspace's engineering lifecycle stage.
    Method 2 (advanced): the caller already knows the workspace_id; the
        resume action runs immediately without displaying the list.

The filesystem is the authoritative source for workspace discovery. The
SQL ``workspaces`` table is joined opportunistically (for timestamps or
additional metadata) but never required for a workspace to appear. This
closes the gap where valid FS workspaces vanished from the list when their
SQL row was missing — the SQLite registry is best-effort, not the source
of truth.

Display mapping is pure derivation from the canonical lifecycle enum —
``BuildLifecycle`` is the single source of truth for both the
"Engineering Lifecycle Stage" label and the "Status" label. The future
Research -> Architecture -> Implementation -> Validation -> Publication ->
Completed phased workflow is already representable: when phased debates
land, only ``RESUME_ACTIONS`` and ``ENGINEERING_STAGE_MAP`` gain entries;
the discovery / display / dispatch plumbing here does not.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from lifecycle import BuildLifecycle, current_lifecycle
from workspace_manager import workspace_mgr


# ---------------------------------------------------------------------------
# Engineering-stage label (canonical lifecycle -> user-facing label)
# ---------------------------------------------------------------------------
# Pure derivation. "Unknown" is the spec's "never guess" sentinel for
# states that do not unambiguously map to a stage (NEW / FAILED / BLOCKED /
# ABORTED). Adding new staged debates is a one-line edit here when they
# land; nothing else in this module changes.
ENGINEERING_STAGE_MAP: dict[BuildLifecycle, str] = {
    BuildLifecycle.NEW: "Research",
    BuildLifecycle.VERIFIED: "Research",
    BuildLifecycle.SPEC_READY: "Architecture",
    BuildLifecycle.EXECUTION_ALLOWED: "Architecture",
    BuildLifecycle.EXECUTING: "Implementation",
    BuildLifecycle.ARTIFACTS_VALIDATED: "Implementation",
    BuildLifecycle.REPAIRING: "Implementation",
    BuildLifecycle.TESTS_VALIDATED: "Validation",
    BuildLifecycle.PUBLISHED: "Publication",
    BuildLifecycle.COMPLETE: "Completed",
    BuildLifecycle.FAILED: "Failed",
    BuildLifecycle.BLOCKED: "Blocked",
    BuildLifecycle.ABORTED: "Aborted",
}


STATUS_LABEL_MAP: dict[BuildLifecycle, str] = {
    BuildLifecycle.NEW: "New",
    BuildLifecycle.VERIFIED: "Paused",
    BuildLifecycle.SPEC_READY: "Paused",
    BuildLifecycle.EXECUTION_ALLOWED: "Paused",
    BuildLifecycle.EXECUTING: "Active",
    BuildLifecycle.REPAIRING: "Active",
    BuildLifecycle.ARTIFACTS_VALIDATED: "Paused",
    BuildLifecycle.TESTS_VALIDATED: "Paused",
    BuildLifecycle.PUBLISHED: "Active",
    BuildLifecycle.COMPLETE: "Completed",
    BuildLifecycle.FAILED: "Failed",
    BuildLifecycle.BLOCKED: "Blocked",
    BuildLifecycle.ABORTED: "Aborted",
}


def engineering_stage_label(workspace_id: str) -> str:
    """The Engineering Lifecycle Stage label for a workspace, derived
    from the canonical lifecycle. Returns "Unknown" rather than guessing
    when the lifecycle does not unambiguously map to a stage."""
    if not workspace_id:
        return "Unknown"
    return ENGINEERING_STAGE_MAP.get(current_lifecycle(workspace_id), "Unknown")


def status_label(workspace_id: str) -> str:
    """Workflow status label, derived from the canonical lifecycle. Same
    source as ``engineering_stage_label`` — no parallel state."""
    if not workspace_id:
        return "Unknown"
    return STATUS_LABEL_MAP.get(current_lifecycle(workspace_id), "Unknown")


# ---------------------------------------------------------------------------
# Recent-workspaces assembly
# ---------------------------------------------------------------------------
def recent_workspaces(limit: int = 5, offset: int = 0) -> list[dict]:
    """Return up to ``limit`` most-recent workspaces assembled for display,
    starting at ``offset`` (the count of valid workspaces to skip).

    Filesystem-authoritative: enumerate ``workspaces/workspace_<id>/``
    directories (``list_build_workspaces``, already mtime-sorted DESC),
    load each ``workspace.json`` manifest, derive the lifecycle from the
    canonical ``build_state.json`` (``current_lifecycle``), and join SQL
    metadata (``workspace_mgr.load_workspace``) opportunistically — for the
    canonical ``consensus_score`` / ``updated_at`` / debate-side prompt —
    when a row exists, but never require SQL for a workspace to appear. This
    closes the gap where a valid FS workspace vanished from the list when its
    SQL row was missing (the SQLite registry is best-effort, not the source
    of truth).

    Older workspaces lacking newer metadata degrade gracefully:
        - no SQL row         -> topic from ``BuildWorkspace.source_question``,
                                timestamps from filesystem dir mtime
        - no lifecycle_stage -> ``lifecycle.current_lifecycle`` falls back to
                                mapping the legacy ``status`` string; if that
                                is unknown too, ``engineering_stage_label``
                                returns "Unknown" (never guesses)
        - no usable state at all -> the workspace is skipped (unreadable /
                                   invalid), not crashed on

    Paging: ``offset`` is the count of valid (non-skipped) workspaces to
    skip; item ``number`` is ``offset + 1, offset + 2, ...`` so numbering
    continues across pages when ``select_workspace`` walks pages via
    ``more``.

    No new state is created. The returned items keep the same shape as the
    previous SQL-only path so CLI / test callers keep working; the
    additional ``in_sql_registry`` flag is observational."""
    from build_workspace import list_build_workspaces, load_workspace

    items: list[dict] = []
    skipped: int = 0  # valid items passed over because of ``offset``
    for wid in list_build_workspaces():
        if len(items) >= limit:
            break
        # --- Canonical FS manifest — authoritative. Skip unreadable/invalid
        # workspaces rather than crashing the whole list on one bad row.
        try:
            bws = load_workspace(wid)
        except Exception:
            continue
        if bws is None:
            continue
        if skipped < offset:
            skipped += 1
            continue

        # --- SQL row — opportunistic metadata join. None when the
        # workspace has no row in database/workspaces.db; that's normal
        # for FS-only workspaces and the gap that previously hid them.
        sql_row = _safe_load_sql_workspace(wid)
        sql_dict = (sql_row.to_dict() if sql_row is not None else {})

        # Topic: prefer the question held in the FS manifest (the
        # resurrected answer's source question). Fall back to SQL's
        # original_prompt (the debate-side prompt may be richer than
        # the build's). Empty topic displays as "Unknown" downstream.
        topic = (bws.source_question
                 or bws.source_topic
                 or sql_dict.get("original_prompt", ""))

        # Engineering stage — canonical FS build_state.json (lifecycle
        # module's authority). Falls back to legacy status mapping when
        # lifecycle_stage is absent; "Unknown" when nothing maps.
        stage = engineering_stage_label(wid)
        status = status_label(wid)

        # Last updated — prefer the SQL row's updated_at (the debate
        # side bumps it on every save). Fall back to the FS dir mtime
        # when SQL is absent (the only available timestamp for a
        # SQL-less workspace). Both reduce to YYYY-MM-DD for display.
        updated_iso = (sql_dict.get("updated_at")
                       or _fs_mtime_iso(wid))

        items.append({
            "number": offset + len(items) + 1,
            "topic": topic,
            "lifecycle_stage": stage,
            "status": status,
            "last_updated": _date_only(updated_iso),
            "workspace_id": wid,
            # Carried for callers (CLI) that need the raw canonical values.
            "canonical_lifecycle": (current_lifecycle(wid).value if wid else ""),
            "raw_status": (bws.status or sql_dict.get("status", "")),
            "updated_at": updated_iso,
            # Observational flag — never required by the dispatch layer;
            # consumers that want to know "did this workspace have SQL
            # metadata?" can read it without re-querying.
            "in_sql_registry": sql_row is not None,
        })
    return items


def _safe_load_sql_workspace(workspace_id: str):
    """Best-effort SQL join. Returns a ``Workspace`` or None. Never raises:
    a corrupt SQL DB or missing row is normal for FS-only workspaces and
    must not break discovery."""
    if not workspace_id:
        return None
    try:
        return workspace_mgr.load_workspace(workspace_id)
    except Exception:
        return None


def _fs_mtime_iso(workspace_id: str) -> str:
    """ISO timestamp from the workspace directory's mtime. The only
    available timestamp when the SQL row is absent. Returns "" if the
    directory cannot be stated (corrupt / removed mid-listing)."""
    try:
        from build_workspace import _workspace_dir
        return datetime.fromtimestamp(
            _workspace_dir(workspace_id).stat().st_mtime,
            tz=timezone.utc,
        ).isoformat()
    except Exception:
        return ""


def _date_only(iso_ts: str) -> str:
    """Reduce an ISO timestamp to YYYY-MM-DD for display. Returns "" when
    the input is empty/unknown — callers display "Unknown" then."""
    if not iso_ts:
        return ""
    # Both "2026-07-21T..." and a bare "2026-07-21" reduce to the date.
    return iso_ts.split("T", 1)[0].split(" ", 1)[0] or ""


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------
_SEP = "-" * 50


def format_recent_workspaces(items: Iterable[dict]) -> str:
    """Render the recent-workspaces block exactly per the spec layout.

    The layout is intentionally line-broken (label on its own line, value
    on the next) so the column widths never depend on the topic length —
    diagnostics and pipeline logs both stay readable."""
    out = ["Recent Projects", "", _SEP, ""]
    items = list(items)
    if not items:
        out.append("No workspaces found.")
        out.append("")
        out.append(_SEP)
        return "\n".join(out)
    for it in items:
        out.append(f"{it['number']}.")
        out.append("")
        out.append("Topic")
        out.append("")
        out.append(it.get("topic") or "Unknown")
        out.append("")
        out.append("Lifecycle Stage")
        out.append("")
        out.append(it.get("lifecycle_stage") or "Unknown")
        out.append("")
        out.append("Status")
        out.append("")
        out.append(it.get("status") or "Unknown")
        out.append("")
        out.append("Last Updated")
        out.append("")
        out.append(it.get("last_updated") or "Unknown")
        out.append("")
        out.append("Workspace")
        out.append("")
        out.append(it.get("workspace_id") or "Unknown")
        out.append("")
        out.append(_SEP)
        out.append("")
    return "\n".join(out)


def select_workspace(prompt_fn: Callable[[str], str] | None = None,
                     limit: int = 5) -> str | None:
    """Display the recent-workspaces block and read a project number.

    The user may type ``more`` (case-insensitive) at the prompt to display
    the next page of up to ``limit`` workspaces. Numbering continues
    across pages (page 1: items 1..N; page 2: items N+1..2N; ...) so a
    selection by number is unique across everything already displayed —
    the caller does not need to know which page the number was on.

    ``prompt_fn`` defaults to ``input`` (the CLI's stdin); tests inject a
    fake. Returns the selected ``workspace_id`` or ``None`` if the user
    entered an empty / out-of-range / non-numeric choice (preserving the
    original one-shot bail behavior for backward compatibility with the
    non-``more`` inputs) or cancelled via EOF / Ctrl-C.

    When the user types ``more`` past the last page, an ``[END] No more
    workspaces.`` notice is printed and the prompt re-issues — the user
    can still select a number from any page already shown, or cancel with
    Enter. This never silently exits (the user's prior browsing is not
    thrown away); it also never silently advances (a clear message tells
    them nothing more is available)."""
    prompt_fn = prompt_fn or input
    all_items: list[dict] = []
    offset = 0
    while True:
        page = recent_workspaces(limit=limit, offset=offset)
        if page:
            print(format_recent_workspaces(page))
            all_items.extend(page)
        elif not all_items:
            # First call returned no workspaces. Show the empty block and
            # bail — there is nothing to select from.
            print(format_recent_workspaces([]))
            return None
        # else: past the last page. Keep all_items (already displayed)
        # on screen and re-prompt so the user can still pick from what
        # they've seen or cancel with Enter.
        try:
            raw = prompt_fn("Select Project Number (or type 'more' for next):\n> ")
        except (EOFError, KeyboardInterrupt):
            return None
        if raw is None:
            return None
        raw = raw.strip()
        if not raw:
            return None
        # 'more' (case-insensitive) -> next page.
        if raw.lower() == "more":
            if not page:
                # Already past the end: tell the user no more are
                # available, then re-prompt (their prior pages stay
                # visible on screen; they can still select or cancel).
                print("[END] No more workspaces.")
                continue
            offset += limit
            continue
        # Numeric selection — search across every item displayed so far
        # (which may span multiple pages if the user typed 'more').
        try:
            n = int(raw)
        except ValueError:
            print(f"[INVALID] Not a project number: {raw!r}")
            return None
        for it in all_items:
            if it["number"] == n:
                return it["workspace_id"]
        print(f"[INVALID] No project number {n}.")
        return None


# ---------------------------------------------------------------------------
# Resume dispatch
# ---------------------------------------------------------------------------
# Forward-compatible with the future phased workflow (Research -> Architecture
# -> Implementation -> Validation -> Publication -> Completed). Today every
# stage routes through the existing canonical resume entry points — the same
# ones ``sol0mon resume`` already trusted. When phased debates land, only
# this table gains entries (e.g. Research -> resume_research_debate); the
# discovery + display + selection plumbing above does not.
RESUME_ACTIONS: dict[str, Callable[..., dict]] = {
    # Research stage: today the "Research Debate" is the existing debate
    # flow (workspace_debate_engine.resume_workspace). We route to
    # resume_build so a workspace at VERIFIED automatically advances to
    # Architecture (spec generation) — and a follow-up debate can be
    # requested via run_followup_debate() below.
    "Research": lambda ws_id, **kw: _safe_resume_build(ws_id, **kw),
    "Architecture": lambda ws_id, **kw: _safe_resume_build(ws_id, **kw),
    "Implementation": lambda ws_id, **kw: _safe_resume_execution(ws_id, **kw),
    "Validation": lambda ws_id, **kw: _safe_resume_validation(ws_id, **kw),
    "Publication": lambda ws_id, **kw: _safe_resume_build(ws_id, **kw),
    # Completed: rerun the finalizer's reconciliation edge
    # (PUBLISHED -> COMPLETE -> PUBLISHED ...). The Build Finalizer is the
    # sole owner of that edge; resume_build routes through it.
    "Completed": lambda ws_id, **kw: _safe_resume_build(ws_id, **kw),
    "Failed": lambda ws_id, **kw: _safe_resume_build(ws_id, **kw),
    "Blocked": lambda ws_id, **kw: _safe_resume_build(ws_id, **kw),
    # Unknown: nothing to infer. Hand the caller back a structured
    # "I don't know" so the CLI can advise without a phantom resume.
    "Unknown": lambda ws_id, **kw: {
        "resumed": False, "reason": "unknown_lifecycle_stage",
        "lifecycle_stage": (current_lifecycle(ws_id).value if ws_id else ""),
        "workspace_id": ws_id,
    },
}


def execute_resume(workspace_id: str, *,
                    use_claude_code: bool = False,
                    follow_up: str | None = None) -> dict:
    """Resume a workspace from its current engineering lifecycle stage.

    The default dispatch (no ``follow_up``) routes to the canonical
    ``resume.resume_build`` / ``resume_execution`` / ``resume_validation``
    by stage — every entry point the existing CLI already trusted, just
    chosen by the user-facing lifecycle label instead of by hand.

    For the Research stage (a workspace whose debate is still alive or
    that the user wants to extend) the caller may pass ``follow_up`` with
    a follow-up question; this routes through
    ``workspace_orchestrator.follow_up_workspace`` — the existing debate
    resume API — so a completed project can be re-opened with a follow-up
    debate per the spec's Completed-stage "Continue with a follow-up
    debate" action. The other future Completed-stage actions (new phase
    / new implementation / new version) are deferred to the phased-debates
    work; the dispatch seam here is already in place for them."""
    if not workspace_id:
        return {"resumed": False, "reason": "no_workspace_id"}

    stage = engineering_stage_label(workspace_id)

    if follow_up:
        return _run_followup_debate(workspace_id, follow_up)

    action = RESUME_ACTIONS.get(stage)
    if action is None:
        return {"resumed": False, "reason": "no_resume_action_for_stage",
                "lifecycle_stage": stage, "workspace_id": workspace_id}
    return action(workspace_id, use_claude_code=use_claude_code)


def run_followup_debate(workspace_id: str, follow_up: str) -> dict:
    """Public seam for the Completed-stage "follow-up debate" action.

    Wraps ``workspace_orchestrator.follow_up_workspace`` — the existing
    debate-resume API — so a completed project can be re-entered with a
    new follow-up question without this module growing a debate engine
    of its own."""
    return _run_followup_debate(workspace_id, follow_up)


def _run_followup_debate(workspace_id: str, follow_up: str) -> dict:
    try:
        from workspace_orchestrator import follow_up_workspace
        return follow_up_workspace(workspace_id, follow_up)
    except Exception as e:  # pragma: no cover — defensive seam
        return {"resumed": False, "reason": "followup_error",
                "error": str(e), "workspace_id": workspace_id}


def _safe_resume_build(workspace_id: str, **kw) -> dict:
    from resume import resume_build
    return resume_build(workspace_id,
                        use_claude_code=kw.get("use_claude_code", False),
                        force=kw.get("force", False))


def _safe_resume_execution(workspace_id: str, **kw) -> dict:
    from resume import resume_execution
    return resume_execution(workspace_id,
                             use_claude_code=kw.get("use_claude_code", False))


def _safe_resume_validation(workspace_id: str, **kw) -> dict:
    from resume import resume_validation
    return resume_validation(workspace_id)


# ---------------------------------------------------------------------------
# Self-check (no pytest, no browser, no LLM)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    import tempfile
    from pathlib import Path

    import build_workspace as _bw
    import workspace_manager as _wm

    with tempfile.TemporaryDirectory() as tmp:
        _bw.WORKSPACE_ROOT = Path(tmp)
        _wm.DB_FILE = str(Path(tmp) / "ws_resume_ui.db")
        _wm.workspace_mgr._initialized = False

        # Seed three workspaces with distinct lifecycle stages so the
        # full display + selection + resume plumbing runs end-to-end.
        # wm.create + bw.create use the same uuid string — both layers
        # share the workspace_id, which is exactly what the spec needs.
        wids = []
        for topic, stage in [
            ("Autonomous Crypto Trading Platform",
             BuildLifecycle.VERIFIED),
            ("Distributed Debate Engine",
             BuildLifecycle.SPEC_READY),
            ("OpenCode Execution Pipeline",
             BuildLifecycle.COMPLETE),
        ]:
            for tgt in (BuildLifecycle.NEW,):
                # Create the SQL row first; create_workspace returns the id.
                wid = _wm.workspace_mgr.create_workspace(topic)
                # Mirror it into the FS so current_lifecycle finds it and
                # advance_lifecycle can persist build_state.json.
                _bw.create_workspace(workspace_id=wid, source_question=topic)
                # Walk the lifecycle forward to the target stage (legal only
                # along the canonical transition table).
                _bw.write_build_state(wid, "created")
                lifecycle_path = [
                    BuildLifecycle.NEW,
                    BuildLifecycle.VERIFIED,
                    BuildLifecycle.SPEC_READY,
                    BuildLifecycle.EXECUTION_ALLOWED,
                    BuildLifecycle.EXECUTING,
                    BuildLifecycle.ARTIFACTS_VALIDATED,
                    BuildLifecycle.TESTS_VALIDATED,
                    BuildLifecycle.COMPLETE,
                ]
                from lifecycle import advance_lifecycle
                seen = set()
                for tgt_step in lifecycle_path:
                    if tgt_step in seen:
                        continue
                    if tgt_step == stage:
                        advance_lifecycle(wid, tgt_step)
                        break
                    if tgt_step.value == "NEW":
                        seen.add(tgt_step)
                        continue
                    advance_lifecycle(wid, tgt_step)
                    seen.add(tgt_step)
                wids.append(wid)

        # 1. recent_workspaces returns <=5 rows, ordered most-recent-first
        # by the filesystem workspace directory mtime (DESC). The seed
        # order places the last-seeded row first because each
        # advance_lifecycle write bumps the directory mtime; lifecycle +
        # status labels are pure derivations from the canonical lifecycle
        # in each row's build_state.json.
        items = recent_workspaces(limit=5)
        assert len(items) == 3, items
        assert items[0]["number"] == 1
        assert items[1]["number"] == 2
        assert items[2]["number"] == 3
        # Most-recent-first (DESC on FS dir mtime): the third-seeded
        # workspace (OpenCode Execution Pipeline -> COMPLETE) appears
        # first; the first-seeded (Autonomous Crypto Trading Platform
        # -> VERIFIED) appears last.
        assert items[0]["topic"] == "OpenCode Execution Pipeline", items[0]
        assert items[1]["topic"] == "Distributed Debate Engine", items[1]
        assert items[2]["topic"] == "Autonomous Crypto Trading Platform", items[2]
        # Engineering stage labels are pure derivation from lifecycle.
        labels = {it["workspace_id"]: it["lifecycle_stage"] for it in items}
        assert labels[wids[0]] == "Research", labels
        assert labels[wids[1]] == "Architecture", labels
        assert labels[wids[2]] == "Completed", labels
        # Status labels likewise.
        statuses = {it["workspace_id"]: it["status"] for it in items}
        assert statuses[wids[0]] == "Paused", statuses
        assert statuses[wids[1]] == "Paused", statuses
        assert statuses[wids[2]] == "Completed", statuses
        # All three seeded workspaces have a SQL row (the seed wrote both
        # sides) — the opportunistic join found each.
        assert all(it["in_sql_registry"] for it in items), items

        # 2. format_recent_workspaces matches the spec's line layout.
        block = format_recent_workspaces(items)
        assert "Recent Projects" in block
        assert "1." in block and "2." in block and "3." in block
        # Each section carries the four labelled fields per the spec.
        for label in ("Topic", "Lifecycle Stage", "Status", "Last Updated",
                      "Workspace"):
            assert block.count(label) == 3, label
        # Workspace IDs all visible.
        for wid in wids:
            assert wid in block

        # 3. select_workspace reads + validates. Use an in-process fake.
        calls = []
        def fake_prompt(_msg):
            calls.append(_msg)
            return "2"
        picked = select_workspace(prompt_fn=fake_prompt, limit=5)
        assert picked == wids[1], picked
        assert calls, "prompt_fn was not invoked"
        # Out-of-range yields None and never raises.
        assert select_workspace(prompt_fn=lambda _m: "99", limit=5) is None
        # Non-numeric yields None.
        assert select_workspace(prompt_fn=lambda _m: "abc", limit=5) is None
        # Empty + Ctrl-D yield None.
        assert select_workspace(prompt_fn=lambda _m: "", limit=5) is None

        # 4. New workspaces are shown as the Research entry stage.
        fresh = _wm.workspace_mgr.create_workspace("Brand new")
        _bw.create_workspace(workspace_id=fresh, source_question="Brand new")
        # No build_state.json yet -> current_lifecycle returns NEW.
        assert engineering_stage_label(fresh) == "Research"
        assert status_label(fresh) == "New"
        # execute_resume on a fresh Research workspace returns a structured result.
        rep = execute_resume(fresh)
        assert rep["resumed"] is False
        assert rep["reason"] == "no_source_to_resume_from"

        # 5. execute_resume routes by engineering stage. The dispatch table
        # is the contract surface for the future phased workflow: today
        # every stage routes to a canonical resume entry point. Assert
        # the table holds the expected handler names (not the callables
        # themselves — the table is intentionally Lambda so the assertion
        # holds across imports) without invoking them, because actually
        # running resume_build on an unplanned SPEC_READY workspace would
        # mutate the lifecycle through the executor's "no spec found"
        # path — that flow is what test_resume.py covers, not this UI test.
        assert set(RESUME_ACTIONS.keys()) == {
            "Research", "Architecture", "Implementation",
            "Validation", "Publication", "Completed", "Failed", "Blocked", "Unknown",
        }, set(RESUME_ACTIONS.keys())
        # Completed stage re-enters the Build Finalizer's reconciliation
        # edge -> resume_build returns "already_complete" for wids[2]
        # (COMPLETE). No lifecycle mutation here — COMPLETE is terminal.
        rep_done = execute_resume(wids[2], use_claude_code=False)
        assert rep_done.get("reason") == "already_complete", rep_done

        # Validation stage (TESTS_VALIDATED) routes to resume_validation,
        # which is pure (no lifecycle mutation). Use a fresh workspace
        # advanced to TESTS_VALIDATED so the seeded lifecycle shapes above
        # are not perturbed.
        val_wid = _wm.workspace_mgr.create_workspace("Validation probe")
        _bw.create_workspace(workspace_id=val_wid,
                             source_question="Validation probe")
        _bw.write_build_state(val_wid, "created")
        from lifecycle import advance_lifecycle as _al
        _al(val_wid, BuildLifecycle.VERIFIED)
        _al(val_wid, BuildLifecycle.SPEC_READY)
        _al(val_wid, BuildLifecycle.EXECUTION_ALLOWED)
        _al(val_wid, BuildLifecycle.EXECUTING)
        _al(val_wid, BuildLifecycle.ARTIFACTS_VALIDATED)
        _al(val_wid, BuildLifecycle.TESTS_VALIDATED)
        assert engineering_stage_label(val_wid) == "Validation"
        rep_val = execute_resume(val_wid, use_claude_code=False)
        # resume_validation always returns resumed=True with a
        # decision_model report.
        assert rep_val.get("resumed") is True, rep_val
        assert "decision_model" in rep_val, rep_val

        # 6. No new metadata: the only files in the workspace dir are the
        # ones build_workspace + lifecycle wrote. There is no per-workspace
        # resume-state file introduced by this module.
        ws_dir = _bw._workspace_dir(wids[0])
        files = {p.name for p in ws_dir.iterdir()}
        assert "build_state.json" in files
        assert "workspace.json" in files
        # Specifically: no parallel resume_state / resume_ui_state file.
        assert not any("resume" in f.lower() for f in files), files

        # 7. FS-authoritative discovery (the refactor's core invariant):
        # a workspace with NO SQL row must STILL appear in recent_workspaces.
        # This is the regression that motivated the refactor — valid
        # filesystem workspaces were vanishing from Resume Recent whenever
        # their SQL row was missing. Create the FS manifest only; do NOT
        # touch the SQL DB at all.
        import uuid as _uuid
        fs_only_wid = str(_uuid.uuid4())
        _bw.create_workspace(workspace_id=fs_only_wid,
                              source_question="Filesystem-Only Probe")
        # Advance the FS lifecycle so the labels are deterministic.
        from lifecycle import advance_lifecycle as _al_fs
        _bw.write_build_state(fs_only_wid, "created")
        _al_fs(fs_only_wid, BuildLifecycle.VERIFIED)
        items_after = recent_workspaces(limit=20)
        fs_only_item = next(
            (it for it in items_after
             if it["workspace_id"] == fs_only_wid), None)
        assert fs_only_item is not None, (
            "FS-only workspace (no SQL row) failed to appear — the SQL-only "
            "regression has returned")
        assert fs_only_item["in_sql_registry"] is False, fs_only_item
        assert fs_only_item["topic"] == "Filesystem-Only Probe", fs_only_item
        assert fs_only_item["lifecycle_stage"] == "Research", fs_only_item
        # Last Updated derives from FS dir mtime (no SQL row): a real
        # YYYY-MM-DD date, not "" / "Unknown".
        assert fs_only_item["last_updated"], fs_only_item
        # The FS-only workspace appears at the TOP because it was just
        # touched (newest mtime under WORKSPACE_ROOT).
        assert items_after[0]["workspace_id"] == fs_only_wid, items_after[0]
        # The CLI-facing select_workspace() also discovers it — selecting
        # the FS-only workspace by its position works without SQL.
        # items_after[0] is the FS-only workspace; that's number 1.
        assert items_after[0]["number"] == 1, items_after[0]
        # Out-of-range still gives None even with mixed FS/SQL rows.
        assert select_workspace(prompt_fn=lambda _m: "999", limit=20) is None

        # 8. Unreadable / invalid workspaces are skipped, not crashed on.
        # Drop a directory that looks like a workspace but holds a corrupt
        # workspace.json. ``list_build_workspaces`` enumerates it by name
        # (its dir starts with "workspace_"), but load_workspace returns
        # None and recent_workspaces must skip it — never raise.
        import shutil as _sh
        bad_wid = str(_uuid.uuid4())
        bad_dir = _bw._workspace_dir(bad_wid)
        bad_dir.mkdir(parents=True, exist_ok=True)
        (bad_dir / "workspace.json").write_text("{not valid json",
                                                  encoding="utf-8")
        try:
            items_bad = recent_workspaces(limit=50)
            assert all(it["workspace_id"] != bad_wid for it in items_bad), (
                "corrupt workspace.json was not skipped", items_bad)
        finally:
            _sh.rmtree(bad_dir, ignore_errors=True)

        # 9. Older workspaces lacking newer metadata degrade gracefully:
        # no SQL row AND no lifecycle_stage in build_state.json — the
        # legacy ``status`` string alone drives the lifecycle via
        # lifecycle_stage_from_status. If even that is unknown, the
        # engineering label is "Unknown" (never guessed).
        legacy_wid = str(_uuid.uuid4())
        _bw.create_workspace(workspace_id=legacy_wid,
                              source_question="Legacy Workspace")
        # Simulate a pre-hardening build_state: only a legacy status string,
        # no lifecycle_stage field. ``executed`` maps to COMPLETE.
        _bw.write_build_state(legacy_wid, "executed", execution_success=True)
        # Build a state dict by hand without the lifecycle_stage field to
        # truly simulate pre-hardening state (write_build_state preserves
        # lifecycle_stage if present; we explicitly remove it here).
        import json as _json
        bs_path = _bw._workspace_dir(legacy_wid) / "build_state.json"
        bs = _json.loads(bs_path.read_text(encoding="utf-8"))
        bs.pop("lifecycle_stage", None)
        bs_path.write_text(_json.dumps(bs, indent=2), encoding="utf-8")
        items_legacy = recent_workspaces(limit=50)
        legacy_item = next(
            (it for it in items_legacy
             if it["workspace_id"] == legacy_wid), None)
        assert legacy_item is not None, "legacy workspace failed to appear"
        # ``executed`` -> lifecycle COMPLETE -> "Completed" stage.
        assert legacy_item["lifecycle_stage"] == "Completed", legacy_item
        assert legacy_item["in_sql_registry"] is False, legacy_item
        # And a truly empty legacy workspace (no status, no lifecycle):
        # current_lifecycle returns NEW, which is displayed as the Research entry stage.
        empty_wid = str(_uuid.uuid4())
        _bw.create_workspace(workspace_id=empty_wid,
                              source_question="Empty Legacy Workspace")
        # Remove any build_state.json so current_lifecycle returns NEW.
        bs_empty = _bw._workspace_dir(empty_wid) / "build_state.json"
        if bs_empty.is_file():
            bs_empty.unlink()
        items_empty = recent_workspaces(limit=50)
        empty_item = next(
            (it for it in items_empty
             if it["workspace_id"] == empty_wid), None)
        assert empty_item is not None, "empty legacy workspace failed to appear"
        assert empty_item["lifecycle_stage"] == "Research", empty_item
        assert empty_item["status"] == "New", empty_item

        # 10. 'more' paging: typing 'more' (case-insensitive) at the
        # selection prompt shows the next page of up to `limit` workspaces.
        # Numbering continues across pages (page N+1 starts at item
        # N*limit+1) so a numeric selection is unique across everything
        # the user has already seen — no need for them to remember which
        # page a number was on.
        #
        # Isolated, controlled-mtime env so the page composition is
        # deterministic — at this point in the self-check there are >5
        # workspaces already, but their mtime order is the by-product of
        # many writes and would make the test brittle. fresh tmp dir:
        import os as _os
        import time as _time
        import uuid as _uuid_pg
        with tempfile.TemporaryDirectory() as tmp_paging:
            _orig_root = _bw.WORKSPACE_ROOT
            _orig_db = _wm.DB_FILE
            _orig_init = _wm.workspace_mgr._initialized
            try:
                _bw.WORKSPACE_ROOT = Path(tmp_paging)
                _wm.DB_FILE = str(Path(tmp_paging) / "ws_paging.db")
                _wm.workspace_mgr._initialized = False
                wid_a = str(_uuid_pg.uuid4())
                wid_b = str(_uuid_pg.uuid4())
                wid_c = str(_uuid_pg.uuid4())
                # Create oldest-first so subsequent utime calls fully
                # control the mtime order independent of creation order.
                for wid in (wid_c, wid_b, wid_a):
                    _bw.create_workspace(workspace_id=wid,
                                          source_question=f"WS-{wid[:8]}")
                now = _time.time()
                # wid_a newest, wid_b middle, wid_c oldest. mtime-descending
                # discovery order will be: wid_a, wid_b, wid_c.
                for wid, age in ((wid_a, 0), (wid_b, 30), (wid_c, 60)):
                    _os.utime(_bw._workspace_dir(wid),
                              (now - age, now - age))

                # 10a. limit=2: page 1 = [wid_a, wid_b] (#1, #2);
                #      'more' -> page 2 = [wid_c] (#3); select '3' -> wid_c.
                inputs = iter(["more", "3"])

                def pager_prompt(_msg):
                    return next(inputs)
                picked = select_workspace(prompt_fn=pager_prompt, limit=2)
                assert picked == wid_c, picked

                # 10b. 'more' past the last page: with limit=3 all three
                #      fit on page 1, 'more' advances to an empty page 2
                #      which prints [END] and re-prompts; user can then
                #      still select from page 1 -> wid_a.
                inputs2 = iter(["more", "1"])

                def end_prompt(_msg):
                    return next(inputs2)
                picked2 = select_workspace(prompt_fn=end_prompt, limit=3)
                assert picked2 == wid_a, picked2

                # 10c. 'more' past end then empty input cancels cleanly
                #      (returns None, never infinite-loops).
                inputs3 = iter(["more", ""])

                def cancel_prompt(_msg):
                    return next(inputs3)
                assert select_workspace(prompt_fn=cancel_prompt,
                                         limit=3) is None

                # 10d. non-numeric, non-'more' input bails on first prompt
                #      (preserves the pre-paging one-shot contract).
                assert select_workspace(prompt_fn=lambda _m: "abc",
                                          limit=3) is None
                assert select_workspace(prompt_fn=lambda _m: "99",
                                          limit=3) is None
                assert select_workspace(prompt_fn=lambda _m: "",
                                          limit=3) is None

                # 10e. case-insensitive 'more' works (MORE / More).
                inputs4 = iter(["MORE", "2"])

                def more_ci_prompt(_msg):
                    return next(inputs4)
                picked4 = select_workspace(prompt_fn=more_ci_prompt,
                                            limit=1)
                # limit=1: page 1 = [wid_a] (#1); 'MORE' -> page 2 =
                # [wid_b] (#2); select '2' -> wid_b.
                assert picked4 == wid_b, picked4
            finally:
                _bw.WORKSPACE_ROOT = _orig_root
                _wm.DB_FILE = _orig_db
                _wm.workspace_mgr._initialized = _orig_init

    print("workspace_resume_ui smoke test PASSED")
