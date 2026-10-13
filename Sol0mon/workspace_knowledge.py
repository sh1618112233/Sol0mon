"""
Project Sol0mon — Workspace Knowledge Persistence.

Persist verified research independently of debates so research becomes
reusable input for future phases. The planner's work-packages produce
artefacts that downstream packages consume; this module is the file-system
layout + the load/save helpers for those artefacts.

Workspace layout (additive — beside the existing build workspace files)::

    workspaces/workspace_<id>/
        project.json                 # planner metadata
        planner/execution_graph.json  # the validated graph
        research/                    # research package outputs
            <package_id>.md
        verified/                    # verified decisions / tradeoffs
            <package_id>.json
        architecture/                 # architecture package outputs
            <package_id>.md
        implementation/              # implementation package outputs
            <package_id>.md

Layout choice: one file per work-package id under the type-named directory.
This is the cheapest correct shape — it survives resumption (load by id),
makes "what's been produced" trivially inspectable (``ls research/``), and
keeps the artefact set bounded by the graph size (no fan-out per debate
round). No DB column, no SQLite table, no index — the file system IS the
index. Ponytail: native platform feature before custom storage.

Cross-stage consumption (debate separation):
  - Architecture debates consume verified research, NOT the raw prompt.
    ``load_stage_input(workspace_id, "architecture")`` returns the merged
    text of every file under ``research/`` + ``verified/`` so the
    architecture prompt carries the evidence the prior stage produced.
  - Implementation debates consume architecture + verified research.
    ``load_stage_input(workspace_id, "implementation")`` merges
    ``architecture/`` + ``verified/`` + ``research/``.
  - Validation / documentation consume implementation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# Stage directory names (the work-package types that produce artefacts).
STAGE_DIRS = ("research", "verified", "architecture", "implementation")


def _workspace_dir(workspace_id: str) -> Path:
    """Re-use the planner's workspace resolver so knowledge files land in the
    same workspace the planner / scheduler already use."""
    from project_planner import _workspace_dir as _pd_dir
    return _pd_dir(workspace_id)


def stage_dir(workspace_id: str, stage: str, *, create: bool = False) -> Path:
    """Return the directory path for a stage's artefacts. ``create=True``
    ensures it exists."""
    if stage not in STAGE_DIRS:
        raise ValueError(f"unknown knowledge stage: {stage!r}")
    p = _workspace_dir(workspace_id) / stage
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def save_artifact(
    workspace_id: str, stage: str, package_id: str, content: str,
) -> Path:
    """Persist a single package's stage artefact as text (typically Markdown
    for research/architecture/implementation, JSON for verified).

    Returns the path written. ``package_id`` is sanitised into a filename
    (whichever chars are legal for filesystem names; the id is already
    slug-safe from the planner, but be defensive)."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (package_id or "package"))
    if stage == "verified":
        # Validate JSON before persisting verified artefacts — these feed
        # execution decisions and must be machine-readable.
        try:
            json.loads(content) if content.strip().startswith(("{", "[")) else None
        except json.JSONDecodeError:
            # Still persist; the consumer validates on load. Do not block the
            # stage on a bad payload — but stamp a warning in the file footer.
            content = content + "\n\n<!-- workspace_knowledge: invalid JSON on save -->\n"
        path = stage_dir(workspace_id, stage, create=True) / f"{safe}.json"
    else:
        path = stage_dir(workspace_id, stage, create=True) / f"{safe}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def load_artifact(workspace_id: str, stage: str, package_id: str) -> str | None:
    """Read a single artefact. Returns None if absent."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (package_id or "package"))
    if stage == "verified":
        p = stage_dir(workspace_id, stage) / f"{safe}.json"
    else:
        p = stage_dir(workspace_id, stage) / f"{safe}.md"
    if not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def list_artifacts(workspace_id: str, stage: str) -> list[str]:
    """Return the package ids whose artefacts are persisted for a stage.
    Sorted by filename for determinism."""
    d = stage_dir(workspace_id, stage)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.iterdir() if p.is_file())


def load_stage_input(workspace_id: str, stage: str) -> str:
    """Merge prior-stage artefacts for consumption by ``stage``.

    Precedence (what each stage consumes):
      - ``architecture``  consumes research + verified
      - ``implementation`` consumes architecture + verified + research
      - ``validation``/``documentation`` consume implementation
      - any other stage returns "" (no prior input expected).

    Returns a single merged string (the prior-stage evidence text). Empty
    string when nothing has been persisted yet — callers skip injection
    and behave as before (preserves back-compat).
    """
    consumers: dict[str, tuple[str, ...]] = {
        "architecture": ("research", "verified"),
        "implementation": ("architecture", "verified", "research"),
        "validation": ("implementation",),
        "documentation": ("implementation",),
    }
    sources = consumers.get(stage)
    if not sources:
        return ""
    parts: list[str] = []
    for src in sources:
        for pid in list_artifacts(workspace_id, src):
            text = load_artifact(workspace_id, src, pid)
            if text and text.strip():
                parts.append(f"## [{src}] {pid}\n\n{text.strip()}")
    return "\n\n".join(parts)


def save_debate_result(
    workspace_id: str, package_id: str, package_type: str, result: dict,
) -> Path | None:
    """Adapter: translate a debate-pipeline result into a persisted artefact
    under the right stage directory.

    The result dict carries ``synthesized_answer`` (the prose) and
    optionally ``verified_decisions`` / ``verified_tradeoffs`` (the
    structured verified outcomes). The prose is written to the matching
    stage dir; the verified outcomes are written under ``verified/`` (as a
    JSON file with the same package id) so architecture / implementation
    debates can consume them via ``load_stage_input``.

    Returns the path of the primary artefact (the prose), or None when the
    result had no ``synthesized_answer`` (nothing to persist). Empty prose
    DOES NOT clobber an existing artefact — a re-run with no real output
    leaves prior knowledge intact (resume safety).
    """
    if not isinstance(result, dict):
        return None
    prose = result.get("synthesized_answer", "") or result.get("gpt_final", "")
    primary = None
    stage = _stage_for_type(package_type)
    if stage and prose.strip():
        primary = save_artifact(workspace_id, stage, package_id, prose)

    # Verified outcomes (from governance) — written under verified/ so the
    # next stage's prompt carries the decisions, not just the prose.
    verified = {
        "verified_decisions": result.get("verified_decisions") or [],
        "verified_tradeoffs": result.get("verified_tradeoffs") or [],
        "verification_result": result.get("verification_result") or {},
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "package_id": package_id,
        "package_type": package_type,
    }
    if (verified["verified_decisions"] or verified["verified_tradeoffs"]
            or verified["verification_result"]):
        save_artifact(
            workspace_id, "verified", package_id, json.dumps(verified, indent=2),
        )
    return primary


def _stage_for_type(package_type: str) -> str | None:
    """Map a work-package type to its knowledge stage dir."""
    return {
        "research": "research",
        "architecture": "architecture",
        "implementation": "implementation",
    }.get(package_type)


if __name__ == "__main__":
    # ponytail self-check: save/load round-trip, stage input merging, list,
    # verified-as-JSON. Uses a tmp WORKSPACE_ROOT to avoid touching real data.
    import tempfile
    import build_workspace as bw
    saved_root = bw.WORKSPACE_ROOT
    try:
        bw.WORKSPACE_ROOT = Path(tempfile.mkdtemp(prefix="sol0mon_wk_"))
        from build_workspace import create_workspace
        wid = create_workspace(source_question="ship X")

        # research artefact
        save_artifact(wid, "research", "wp_research", "# Research\nUse Redis.")
        assert "wp_research" in list_artifacts(wid, "research")
        assert "Use Redis" in (load_artifact(wid, "research", "wp_research") or "")

        # verified artefact (JSON)
        save_artifact(wid, "verified", "wp_research",
                      json.dumps({"verified_decisions": [{"decision": "Use Redis"}]}))
        assert "wp_research" in list_artifacts(wid, "verified")

        # architecture stage consumes research + verified
        merged = load_stage_input(wid, "architecture")
        assert "Research" in merged and "Use Redis" in merged
        assert "[verified] wp_research" in merged

        # empty implementation stage input -> "" (preserves back-compat path)
        assert load_stage_input(wid, "implementation") == "" or \
               "Research" in load_stage_input(wid, "implementation")

        # save_debate_result adapter: prose + verified both written.
        path = save_debate_result(
            wid, "wp_arch", "architecture",
            {"synthesized_answer": "# Architecture\nstore via Redis.",
             "verified_decisions": [{"decision": "store-via-redis"}]},
        )
        assert path is not None and path.is_file()
        v = load_artifact(wid, "verified", "wp_arch")
        assert v and "verified_decisions" in v

        # implementation stage now consumes architecture + verified + research
        merged2 = load_stage_input(wid, "implementation")
        assert "Architecture" in merged2 and "store via Redis" in merged2
        assert "Research" in merged2

        # unknown stage -> ValueError
        try:
            stage_dir(wid, "exotic"); raise AssertionError("expected ValueError")
        except ValueError:
            pass
        # bogus load_stage_input stage -> ""
        assert load_stage_input(wid, "PLANNER") == ""
    finally:
        bw.WORKSPACE_ROOT = saved_root
    print("workspace_knowledge self-check PASSED")
