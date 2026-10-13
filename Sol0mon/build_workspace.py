"""
Project Sol0mon - Build Foundation Layer.

File-based workspace layout for the build phase. Each workspace is a
directory on disk holding the synthesized answer, implementation plan,
human decisions, and build state, plus a workspace.json manifest.

This layer is intentionally separate from workspace_manager.py (the
debate/consensus workspace backed by SQLite). The build workspace is a
flat-file manifest tied to a single build job.

Workspace layout (legacy single-stage — preserved byte-for-byte for all
existing tests / existing workspaces):

    workspaces/
        workspace_<id>/
            workspace.json
            synthesized_answer.md
            implementation_plan.md
            human_decisions.json
            build_state.json

Workspace layout (staged FULL_BUILD path — additive, opt-in via the
``human_slug`` argument to ``create_workspace`` and the staging planner):

    workspaces/
        <slug>_<YYYYMMDD>_<NNN>/          # human-readable; workspace_id stays canonical UUID
            workspace.json                # canonical workspace_id stored here
            project_plan.md
            architecture.md
            README.md
            manifest.json                  # post-build artifact index (manifest.py)
            build_plan.json                # staged plan (dependency-aware stages)
            stage_status.json              # per-stage lifecycle
            repair_history.json            # per-stage repair attempts
            test_results.json              # per-stage test results
            publication.json               # post-finalize publication summary
            build_state.json               # build-level lifecycle (legacy + lifecycle_stage)
            synthesized_answer.md
            answer.json
            decisions.json
            implementation_plan.md
            human_decisions.json
            execution_plan.json            # task DAG (execution_planner)
            src/ tests/ docs/ artifacts/ logs/ reports/ checkpoints/ stages/

The directory name exists for engineering usability; the ``workspace_id``
(UUID) stored inside ``workspace.json`` is the canonical identifier. The
two naming conventions coexist: ``_workspace_dir(workspace_id)`` resolves
the legacy UUID path first, then falls back to the slug-based index
(``workspaces/_index.json``). Older workspaces — UUID-only named — remain
fully valid; tests that pre-date this layer see byte-for-byte identical
behavior because ``create_workspace`` defaults to ``human_slug=None``.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Root for all build workspaces. Configurable via env so tests can
# point it at a tmp dir without touching real data.
WORKSPACE_ROOT = Path(os.environ.get("SOL0MON_BUILD_WORKSPACE_ROOT", "workspaces"))

BUILD_SPEC_VERSION = 1

# ponytail: index file registering slug-based directory names → workspace_ids.
# Legacy UUID-named dirs (``workspace_<uuid>``) DON'T appear here — they remain
# discoverable via ``_workspace_dir``'s legacy path. Backward-compat: a missing
# or corrupt index is treated as empty.
_WORKSPACE_INDEX = "_index.json"

# Files laid down at creation time -> default contents.
# human_decisions.json mirrors the Phase 2 human_decision_manager format:
#   {"pending": [], "resolved": []}
# answer.json / decisions.json are the authoritative build inputs (Fix: the
# decision model must persist in every workspace). Populated by
# build_pipeline.resurrect_answer; defaulted empty here so they always exist.
DEFAULT_FILES: dict[str, str] = {
    "synthesized_answer.md": "",
    "answer.json": "{}",
    "decisions.json": '{"verification_result": {}, "resolved_decisions": [], "verified_decisions": []}',
    "implementation_plan.md": "",
    "human_decisions.json": '{"pending": [], "resolved": []}',
    "build_state.json": "{}",
}

# Staged FULL_BUILD skeleton — additive files + dirs created ONLY when the
# pipeline enters the staged path (see ``initialize_full_build_layout``).
# Existing tests pin ``DEFAULT_FILES`` byte-for-byte, so these are NOT laid
# down by ``create_workspace`` unless the FULL_BUILD layout is opted into.
STAGED_LAYOUT_FILES: dict[str, str] = {
    "project_plan.md": "",
    "architecture.md": "",
    "README.md": "",
    "build_plan.json": '{"stages": [], "generated_at": null, "plan_id": ""}',
    "stage_status.json": '{"stages": {}, "updated_at": null}',
    "repair_history.json": '{"stages": {}}',
    "test_results.json": '{"stages": {}}',
    "publication.json": "{}",
}
STAGED_LAYOUT_DIRS: tuple[str, ...] = (
    "src", "tests", "docs", "artifacts", "logs", "reports",
    "checkpoints", "stages",
)


@dataclass
class BuildWorkspace:
    """In-memory manifest for a build workspace; mirrors workspace.json."""

    workspace_id: str
    source_answer_id: str | None = None
    source_question: str | None = None
    source_topic: str | None = None
    build_spec_version: int = BUILD_SPEC_VERSION
    status: str = "created"

    def to_dict(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "source_answer_id": self.source_answer_id,
            "source_question": self.source_question,
            "source_topic": self.source_topic,
            "build_spec_version": self.build_spec_version,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BuildWorkspace":
        return cls(
            workspace_id=data.get("workspace_id", ""),
            source_answer_id=data.get("source_answer_id"),
            source_question=data.get("source_question"),
            source_topic=data.get("source_topic"),
            build_spec_version=data.get("build_spec_version", BUILD_SPEC_VERSION),
            status=data.get("status", "created"),
        )

    @property
    def dir(self) -> Path:
        return _workspace_dir(self.workspace_id)

    @property
    def manifest_path(self) -> Path:
        return self.dir / "workspace.json"


def _workspace_dir(workspace_id: str) -> Path:
    """Resolve the on-disk directory for a workspace_id.

    Resolution order (backward-compat + new slug-based names):
      1. ``workspaces/workspace_<id>`` — the legacy default. Always returned
         when it exists on disk (covers every workspace created without
         ``human_slug``).
      2. Slug-based index lookup (``workspaces/_index.json``). Returns the
         registered slug directory name when present.
      3. The legacy path (1) is returned even when absent — so a caller in
         the middle of ``create_workspace`` (before the dir exists) still
         gets the path it is about to ``mkdir``.

    The split matters: a workspace created with a ``human_slug`` has a
    directory name that does NOT contain the workspace_id, so step (2) is
    the only way to find it. A workspace created WITHOUT a slug always hits
    step (1) and is unaffected by the index.
    """
    legacy = WORKSPACE_ROOT / f"workspace_{workspace_id}"
    if legacy.is_dir():
        return legacy
    idx = _read_index()
    name = idx.get(workspace_id)
    if name:
        slugged = WORKSPACE_ROOT / name
        if slugged.is_dir():
            return slugged
    return legacy


_SLUG_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    """Lowercase alphanumeric slug with underscores; falls back to ``workspace``
    when the input has no usable characters. Used both for directory names and
    for build_plan stage-file prefixes."""
    s = _SLUG_NORMALIZE_RE.sub("_", (text or "").lower()).strip("_")
    return s or "workspace"


def _next_daily_counter(slug: str, date_stamp: str) -> int:
    """Allocate the next 1-based counter for ``<slug>_<date>_<NNN>``. Scans the
    existing workspace dirs in WORKSPACE_ROOT for the (slug, date) pair. The
    race window (TOCTOU between the scan and the mkdir) is closed by
    ``create_workspace``'s ``mkdir(parents=True, exist_ok=False)`` — a
    collision raises, the caller retries with the next counter (ponytail:
    per-day counter scan is O(n) over existing dirs, fine for the volumes
    Sol0mon handles; a persistent counter file would be added when throughput
    becomes a concern)."""
    max_seen = 0
    prefix = f"{slug}_{date_stamp}_"
    if WORKSPACE_ROOT.is_dir():
        for p in WORKSPACE_ROOT.iterdir():
            if not p.is_dir():
                continue
            name = p.name
            if name.startswith(prefix):
                tail = name[len(prefix):]
                if tail.isdigit():
                    max_seen = max(max_seen, int(tail))
    return max_seen + 1


def _read_index() -> dict[str, str]:
    """Read ``workspaces/_index.json`` -> {workspace_id: dir_name}. Empty on
    absence / corruption (the index is a convenience for slug-based lookup,
    not a transactional store — losing it never orphans a workspace because
    ``workspace.json`` inside each dir carries the canonical id)."""
    p = WORKSPACE_ROOT / _WORKSPACE_INDEX
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8")) or {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_index_entry(workspace_id: str, dir_name: str) -> None:
    """Insert / replace a (workspace_id -> dir_name) entry in the index. Merge
    semantics: existing entries are preserved; the file is rewritten fully
    (atomic via the standard write_text pattern; small enough that the
    partial-write window is acceptable for a non-canonical index)."""
    idx = _read_index()
    idx[workspace_id] = dir_name
    p = WORKSPACE_ROOT / _WORKSPACE_INDEX
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(idx, indent=2, sort_keys=True), encoding="utf-8")


def create_workspace(
    workspace_id: str | None = None,
    source_answer_id: str | None = None,
    source_question: str | None = None,
    source_topic: str | None = None,
    human_slug: str | None = None,
) -> str:
    """Create a new build workspace on disk. Returns the workspace_id.

    ``human_slug`` (opt-in, None by default → ``workspace_<uuid>``):
        When provided, the directory is named ``<slug>_<YYYYMMDD>_<NNN>`` —
        human-readable. The canonical ``workspace_id`` (UUID) is stored
        inside ``workspace.json`` and registered in ``workspaces/_index.json``
        so ``_workspace_dir(id)`` can resolve it. Tests that pass
        ``human_slug=None`` (the default) see byte-for-byte identical
        directory naming to the legacy behavior (``workspace_<uuid>``).
    """
    workspace_id = workspace_id or str(uuid.uuid4())
    if human_slug:
        slug = _slugify(human_slug)[:80]  # cap length for filesystem friendliness
        date_stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        # retry loop on the rare collision (mkdir exist_ok=False below is the
        # final guard; the counter scan minimizes retries in the common case)
        for _ in range(8):
            n = _next_daily_counter(slug, date_stamp)
            dir_name = f"{slug}_{date_stamp}_{n:03d}"
            path = WORKSPACE_ROOT / dir_name
            try:
                path.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                continue
        else:  # exhausted retries; fall back to UUID-named (exceptional)
            path = WORKSPACE_ROOT / f"workspace_{workspace_id}"
            path.mkdir(parents=True, exist_ok=False)
            dir_name = path.name
            human_slug = None  # treat as if no slug was given
        if human_slug:
            _write_index_entry(workspace_id, dir_name)
    else:
        path = WORKSPACE_ROOT / f"workspace_{workspace_id}"
        # exist_ok=False is a data-integrity guard: fail loudly if the
        # workspace_id collides rather than silently clobbering a build.
        path.mkdir(parents=True, exist_ok=False)
    ws = BuildWorkspace(
        workspace_id=workspace_id,
        source_answer_id=source_answer_id,
        source_question=source_question,
        source_topic=source_topic,
    )
    for name, content in DEFAULT_FILES.items():
        (path / name).write_text(content, encoding="utf-8")
    # Stamp the canonical id + human-readable slug (if any) inside workspace.json
    # so a directory rename / move never orphans the workspace (the id lives
    # inside the dir, not in the path).
    save_workspace(ws)
    return workspace_id


def initialize_full_build_layout(workspace_id: str) -> bool:
    """Create the staged FULL_BUILD skeleton (extra files + dirs) on top of the
    legacy ``DEFAULT_FILES`` already laid down by ``create_workspace``.

    Idempotent: skips files / dirs that already exist. Returns False on I/O
    failure (caller does NOT block — the staging layer treats missing
    skeleton entries as "treat as legacy single-stage" rather than failing).

    The new layout files are ``STAGED_LAYOUT_FILES`` + ``STAGED_LAYOUT_DIRS``
    declared at the module top. They are EMPTY placeholders; the staging
    pipeline (build_planner → staged_executor → build_finalizer) populates
    them. Their existence at workspace root is what distinguishes a staged
    FULL_BUILD workspace from a legacy single-stage one — bidirectional
    backward-compat: an old workspace without these files is treated as a
    single-stage project (see ``stage_status.has_staged_layout``).
    """
    path = _workspace_dir(workspace_id)
    try:
        for sub in STAGED_LAYOUT_DIRS:
            (path / sub).mkdir(parents=True, exist_ok=True)
        for name, content in STAGED_LAYOUT_FILES.items():
            target = path / name
            if not target.is_file():
                target.write_text(content, encoding="utf-8")
        return True
    except OSError as e:
        print(f"[BUILD WORKSPACE LAYOUT INIT ERROR] {e}")
        return False


def load_workspace(workspace_id: str) -> BuildWorkspace | None:
    """Load a build workspace manifest from disk. None if missing/corrupt."""
    manifest = _workspace_dir(workspace_id) / "workspace.json"
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return BuildWorkspace.from_dict(data)


def save_workspace(ws: BuildWorkspace) -> bool:
    """Persist the workspace manifest to disk. Returns False on failure."""
    try:
        path = ws.manifest_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ws.to_dict(), indent=2), encoding="utf-8")
        return True
    except OSError as e:
        print(f"[BUILD WORKSPACE SAVE ERROR] {e}")
        return False


def list_build_workspaces() -> list[str]:
    """Return workspace_ids present under WORKSPACE_ROOT, newest-first by mtime.

    Covers both naming conventions:
      * legacy ``workspace_<uuid>`` directories — ids parsed from the dir
        name (no I/O beyond ``iterdir``).
      * slug-based ``<slug>_<YYYYMMDD>_<NNN>`` directories — ids resolved via
        ``workspaces/_index.json``. A slug dir missing from the index is
        skipped (the operator can re-register or fall back to looking at
        ``workspace.json`` inside).
    """
    if not WORKSPACE_ROOT.is_dir():
        return []
    out: list[str] = []
    seen: set[str] = set()
    # Legacy UUID-named dirs.
    for p in WORKSPACE_ROOT.iterdir():
        if not p.is_dir():
            continue
        name = p.name
        if name.startswith("workspace_"):
            wid = name[len("workspace_"):]
            if wid and wid not in seen:
                seen.add(wid)
                out.append(wid)
    # Slug-named dirs via the index (id -> dir_name).
    for wid, name in _read_index().items():
        if wid in seen:
            continue
        if (WORKSPACE_ROOT / name).is_dir():
            seen.add(wid)
            out.append(wid)
    out.sort(key=lambda wid: _workspace_dir(wid).stat().st_mtime, reverse=True)
    return out


def write_build_state(workspace_id: str, status: str, **extra) -> dict:
    """Merge `status` and any keyword fields into build_state.json. Preserves
    existing fields across updates and returns the new state dict.

    Audit fix #3: build_state.json used to be a dead `"{}"` placeholder. Now
    the pipeline orchestrator (build_pipeline.run_build_pipeline) and the
    resurrection stage (build_pipeline.resurrect_answer) call this at each
    stage, so build_state.json is the live pipeline-progress record.
    """
    path = _workspace_dir(workspace_id) / "build_state.json"
    state: dict = {}
    if path.is_file():
        try:
            state = json.loads(path.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            state = {}
    state["workspace_id"] = workspace_id
    state["status"] = status
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    state.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


if __name__ == "__main__":
    # Smoke self-check
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        WORKSPACE_ROOT = Path(tmp)  # ponytail: module-global swap for the self-check
        wid = create_workspace(source_question="Design project X")
        ws = load_workspace(wid)
        assert ws is not None and ws.status == "created"
        ws.source_answer_id = "ans-1"
        ws.status = "synthesizing"
        assert save_workspace(ws)
        ws2 = load_workspace(wid)
        assert ws2 is not None and ws2.source_answer_id == "ans-1"
        assert (ws.dir / "human_decisions.json").is_file()

        # Staged layout: human-readable naming + index + FULL_BUILD skeleton.
        from pathlib import Path as _P
        assert _workspace_dir(wid) == _P(tmp) / f"workspace_{wid}", \
            "legacy path is the default when no human_slug is given"

        wid2 = create_workspace(
            source_question="autonomous crypto trading platform",
            human_slug="Autonomous Crypto-Trading Platform!!",
        )
        ws2_dir = _workspace_dir(wid2)
        assert ws2_dir.name.startswith("autonomous_crypto_trading_platform_"), ws2_dir.name
        # Canonical UUID is preserved inside workspace.json.
        ws2b = load_workspace(wid2)
        assert ws2b is not None and ws2b.workspace_id == wid2
        # And the index can resolve it even if we forget the dir path.
        idx = _read_index()
        assert idx.get(wid2) == ws2_dir.name, (idx.get(wid2), ws2_dir.name)

        # init the staged layout
        assert initialize_full_build_layout(wid2) is True
        for sub in STAGED_LAYOUT_DIRS:
            assert (ws2_dir / sub).is_dir(), sub
        for name in STAGED_LAYOUT_FILES:
            assert (ws2_dir / name).is_file(), name
        # idempotent re-init does not raise.
        assert initialize_full_build_layout(wid2) is True

        # Both workspaces are listable.
        ids = list_build_workspaces()
        assert wid in ids and wid2 in ids, ids
        print("build_workspace smoke test PASSED")
