"""
Project Sol0mon - Build Manifest (Final Architecture Hardening - Part 3).

A single immutable record that gathers *references* to every artifact a build
produced. The manifest IS the canonical description of a completed build —
``manifest.json`` lives at the workspace root beside ``build_state.json``,
``decisions.json``, ``implementation_plan.md``, ``execution_plan.json``.

The manifest CONTAINS REFERENCES, NOT DUPLICATE CONTENT. Each field holds an
artifact id (or None when the artifact has not yet been produced); the
``checksums`` map records each referenced artifact's SHA-256 so manifest
validation can detect drift (file on disk != the record) without re-deriving
the artifact.

Layout:

    manifest.json:
    {
      "workspace_id":        "...",
      "manifest_version":     1,
      "lifecycle":            "SPEC_READY",         # current_lifecycle at build time
      "decision_artifact":   "<artifact_id>" | null,
      "build_spec":           "<artifact_id>" | null,
      "execution_plan":       "<artifact_id>" | null,
      "implementation":       "<artifact_id>" | null,
      "validation":           "<artifact_id>" | null,
      "tests":                "<artifact_id>" | null,
      "checksums": {
        "decision_artifact": "<sha256>",
        "build_spec":         "<sha256>",
        "execution_plan":     "<sha256>",
        ...
      },
      "created_at":          "..."
    }

Ponytail: ONE small immutable dataclass. No "manifest builder" hierarchy, no
"manifest provider" ABC, no plugin framework. ``build_manifest_from_workspace``
is the only producer; ``store_manifest`` / ``load_manifest`` are the only I/O.
``with_section(...)`` is the only "mutation" — it returns a NEW manifest.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any


MANIFEST_VERSION = 1
MANIFEST_FILENAME = "manifest.json"

# Fixed namespace for deterministic manifest ids (uuid5 over workspace id).
_MANIFEST_NAMESPACE = uuid.UUID("c4d5e6f7-a8b9-4c0d-9abc-def012345680")


@dataclasses.dataclass(frozen=True)
class BuildManifest:
    """Immutable record of the artifacts a build has produced. References-only.

    The single authoritative description of a (possibly partial) completed
    build. ``None`` on a section means that artifact has not yet been produced
    for this build; the validator treats ``None`` + lifecycle < the section's
    home stage as a warning (expected gap), and ``None`` + lifecycle >= the
    section's home stage as an error (missing artifact for a stage that should
    have produced it).
    """

    workspace_id: str = ""
    manifest_version: int = MANIFEST_VERSION
    lifecycle: str = "NEW"
    # Provenance — the originating debate row this build was resurrected from.
    # Audit 6 (Metadata Consistency): the manifest is the canonical "what was
    # built" record; without ``source_answer_id`` it could not be linked back
    # to its debate except by reading ``workspace.json``. Direct-prompt builds
    # (no parent debate) carry an empty string here. ``to_dict`` /
    # ``from_dict`` round-trip the field, and the manifest id digest includes
    # it so a build re-published from the same debate is identifiable.
    source_answer_id: str = ""
    decision_artifact: str | None = None
    build_spec: str | None = None
    execution_plan: str | None = None
    implementation: str | None = None
    validation: str | None = None
    tests: str | None = None
    # checksums is a tuple-of-pairs (immutable) view of section->sha256.
    # Iteration order is insertion order (sorted section name).
    checksums: tuple[tuple[str, str], ...] = ()
    created_at: str = ""
    id: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            object.__setattr__(self, "created_at",
                               datetime.now(timezone.utc).isoformat())
        if not self.id:
            digest_input = (self.workspace_id + "|"
                            + self.source_answer_id + "|"
                            + self.lifecycle + "|" + "|".join(
                                f"{k}:{v or ''}" for k, v in self.checksums))
            object.__setattr__(self, "id", str(uuid.uuid5(
                _MANIFEST_NAMESPACE, digest_input)))

    # --- read-only helpers ---

    def checksum_of(self, section: str) -> str:
        """Lookup a section's recorded sha-256. Returns '' when the section
        isn't present (caller decides whether that's a warning or an error)."""
        for k, v in self.checksums:
            if k == section:
                return v
        return ""

    def section(self, name: str) -> str | None:
        return getattr(self, name, None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "manifest_version": self.manifest_version,
            "id": self.id,
            "created_at": self.created_at,
            "lifecycle": self.lifecycle,
            "source_answer_id": self.source_answer_id,
            "decision_artifact": self.decision_artifact,
            "build_spec": self.build_spec,
            "execution_plan": self.execution_plan,
            "implementation": self.implementation,
            "validation": self.validation,
            "tests": self.tests,
            "checksums": {k: v for k, v in self.checksums},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BuildManifest":
        d = d or {}
        cks = d.get("checksums") or {}
        if isinstance(cks, dict):
            cks_pairs = tuple(sorted(cks.items()))
        else:
            cks_pairs = tuple(cks)
        return cls(
            workspace_id=d.get("workspace_id", ""),
            manifest_version=d.get("manifest_version", MANIFEST_VERSION),
            lifecycle=d.get("lifecycle", "NEW"),
            source_answer_id=d.get("source_answer_id", ""),
            decision_artifact=d.get("decision_artifact"),
            build_spec=d.get("build_spec"),
            execution_plan=d.get("execution_plan"),
            implementation=d.get("implementation"),
            validation=d.get("validation"),
            tests=d.get("tests"),
            checksums=cks_pairs,
            created_at=d.get("created_at", ""),
            id=d.get("id", ""),
        )

    def with_section(self, section: str, artifact_id: str | None,
                     checksum: str = "") -> "BuildManifest":
        """Return a NEW manifest with the named section populated and its
        checksum recorded. Replacing an existing section overwrites the
        recorded checksum. Clearing (artifact_id=None) clears the checksum
        too. The original manifest is unchanged (immutability). The new
        manifest's ``id`` is re-derived over its current fields rather than
        inherited from the parent (the id is content-addressed)."""
        if section not in {"decision_artifact", "build_spec",
                           "execution_plan", "implementation",
                           "validation", "tests"}:
            raise ValueError(f"unknown manifest section: {section!r}")
        new_cks = [(k, v) for k, v in self.checksums if k != section]
        if artifact_id is not None and checksum:
            new_cks.append((section, checksum))
        new_cks.sort()
        return dataclasses.replace(
            self, **{section: artifact_id},
            checksums=tuple(new_cks),
            id="",  # force __post_init__ to re-derive over the new fields
        )

    def with_lifecycle(self, stage: str) -> "BuildManifest":
        return dataclasses.replace(self, lifecycle=stage, id="")


# --- Hashing helpers shared by the manifest producer + the validator ---

def _sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _sha256_json(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"),
                   default=str, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


# --- Per-workspace STORE / LOAD ---

def _manifest_path(workspace_id: str):
    from build_workspace import _workspace_dir
    return _workspace_dir(workspace_id) / MANIFEST_FILENAME


def store_manifest(workspace_id: str, manifest: BuildManifest) -> bool:
    """Write manifest.json to the workspace root. Returns False on I/O error."""
    try:
        p = _manifest_path(workspace_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(manifest.to_dict(), indent=2, default=str,
                                ensure_ascii=False), encoding="utf-8")
        return True
    except OSError:
        return False


def load_manifest(workspace_id: str) -> BuildManifest | None:
    """Read manifest.json for a workspace. None if absent/corrupt."""
    if not workspace_id:
        return None
    p = _manifest_path(workspace_id)
    if not p.is_file():
        return None
    try:
        return BuildManifest.from_dict(
            json.loads(p.read_text(encoding="utf-8")) or {})
    except (json.JSONDecodeError, OSError, ValueError):
        # ValueError -> unknown manifest section in from_dict indirectly
        return None


def refresh_manifest(workspace_id: str) -> bool:
    """Re-assemble and persist the manifest from the workspace's on-disk
    artifacts. The CANONICAL post-state-change manifest writer — the single
    authority consumed by both ``build_pipeline._persist_build_manifest``
    (end of run_build_pipeline, pre-finalizer) and ``resume._refresh_manifest``
    (after every lifecycle transition during resume).

    Audit 4 (Duplicate Concepts): prior to consolidation, two byte-identical
    helpers existed under different names (``_persist_build_manifest`` in
    build_pipeline, ``_refresh_manifest`` in resume) — one concept, two
    definitions. Both now delegate here. Failure is observational (returns
    False) so neither caller blocks on manifest persistence; the manifest
    is a tracking aid, not a pipeline dependency.

    Returns True on successful persist, False on a missing workspace /
    assembly error / I/O failure.
    """
    if not workspace_id:
        return False
    try:
        manifest = build_manifest_from_workspace(workspace_id)
        return store_manifest(workspace_id, manifest)
    except Exception:
        return False


# --- Producer: assemble a manifest from a workspace's on-disk artifacts ---

def build_manifest_from_workspace(workspace_id: str) -> BuildManifest:
    """Assemble a manifest referencing whatever artifacts currently exist on
    disk for the workspace. References only — no duplicate content. Missing
    artifacts are recorded as None + no checksum entry.

    Sources (single source of truth for each artifact):
      - decisions.json      -> decisions block artifact (verification_result +
                               verified_decisions), id derived from checksum
      - implementation_plan.md -> build_spec artifact, id derived from the
                                   sha256 of the markdown text
      - execution_plan.json -> ExecutionPlanArtifact (the envelope carries its
                               own id + checksum)
      - build_state.json     -> lifecycle stage (via lifecycle.current_lifecycle)
                               + implementation/validation/tests sections when
                               their fields are recorded (best-effort)
    """
    import lifecycle as _lc
    from build_workspace import _workspace_dir

    if not workspace_id:
        return BuildManifest()
    ws = _workspace_dir(workspace_id)

    # Provenance: read the originating debate id from workspace.json (the
    # filesystem workspace manifest, single source for this field). Audit 6
    # (Metadata Consistency): carry source_answer_id in the BuildManifest so
    # the canonical "what was built" record links back to the originating
    # debate without an extra file read. "" for direct-prompt builds.
    source_answer_id = ""
    workspace_json_path = ws / "workspace.json"
    if workspace_json_path.is_file():
        try:
            ws_data = json.loads(
                workspace_json_path.read_text(encoding="utf-8")) or {}
            sa = ws_data.get("source_answer_id")
            if sa is not None:
                source_answer_id = str(sa)
        except (json.JSONDecodeError, OSError):
            pass

    manifest = BuildManifest(workspace_id=workspace_id,
                             lifecycle=_lc.current_lifecycle(workspace_id).value,
                             source_answer_id=source_answer_id)

    # decisions.json -> decision block artifact (id derived over checksum).
    decisions_path = ws / "decisions.json"
    if decisions_path.is_file():
        try:
            decisions = json.loads(decisions_path.read_text(encoding="utf-8")) or {}
            dec_ck = _sha256_json(decisions)
            dec_id = str(uuid.uuid5(
                _MANIFEST_NAMESPACE,
                f"DecisionArtifact:{dec_ck}"))
            manifest = manifest.with_section("decision_artifact", dec_id, dec_ck)
        except (json.JSONDecodeError, OSError):
            pass

    # implementation_plan.md -> build_spec artifact.
    from build_spec_engine import load_implementation_spec
    spec_text = load_implementation_spec(workspace_id)
    if spec_text:
        spec_ck = _sha256_text(spec_text)
        spec_id = str(uuid.uuid5(
            _MANIFEST_NAMESPACE,
            f"BuildSpecArtifact:{spec_ck}"))
        manifest = manifest.with_section("build_spec", spec_id, spec_ck)

    # execution_plan.json -> ExecutionPlanArtifact. The persisted envelope
    # carries the authoritative id + checksum.
    from execution_planner import load_execution_plan, verify_execution_plan_checksum
    ep_env = load_execution_plan(workspace_id)
    if ep_env:
        # Trust the stored checksum IF it still matches the payload (drift
        # detection); otherwise recompute so the manifest records the on-disk
        # truth and validate_manifest flags the mismatch (the validator does
        # the same verification).
        ep_ck = ep_env.get("checksum", "")
        if not verify_execution_plan_checksum(ep_env):
            # Recompute from payload so the manifest carries what's actually on disk.
            ep_ck = _sha256_json(ep_env.get("payload", {}))
        manifest = manifest.with_section("execution_plan",
                                         ep_env.get("id", ""), ep_ck)

    # build_state.json -> implementation / validation / tests references (best
    # effort: only set when the workspace explicitly recorded a success /
    # failure). There is no separate persisted artifact for these today; the
    # manifest records a sentinel id so the existence check has something to
    # compare against.
    state_path = ws / "build_state.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            state = {}
        if state.get("execution_success") is True:
            impl_ck = _sha256_json({"execution_success": True,
                                     "repair_attempts": state.get("repair_attempts", 0),
                                     "status": state.get("status", "")})
            impl_id = str(uuid.uuid5(
                _MANIFEST_NAMESPACE, f"ImplementationArtifact:{impl_ck}"))
            manifest = manifest.with_section("implementation", impl_id, impl_ck)
            test_ck = _sha256_json({"test_runner": state.get("test_runner", ""),
                                     "passed": state.get("test_passed", 0),
                                     "failed": state.get("test_failed", 0)})
            test_id = str(uuid.uuid5(
                _MANIFEST_NAMESPACE, f"TestArtifact:{test_ck}"))
            manifest = manifest.with_section("tests", test_id, test_ck)
            # Validation is implied by tests passing + lifecycle COMPLETE.
            val_ck = _sha256_json({"validated": True,
                                    "lifecycle": manifest.lifecycle})
            val_id = str(uuid.uuid5(
                _MANIFEST_NAMESPACE, f"ValidationArtifact:{val_ck}"))
            manifest = manifest.with_section("validation", val_id, val_ck)

    return manifest


if __name__ == "__main__":
    # ponytail self-check: immutability, references-only, store/load round-trip,
    # with_section overwrites + clears, build_manifest_from_workspace.
    import tempfile
    from build_workspace import WORKSPACE_ROOT as _R0
    import build_workspace as _bw
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        _bw.WORKSPACE_ROOT = Path(tmp)
        wid = _bw.create_workspace(source_question="x")

        # Empty workspace -> manifest with lifecycle NEW. build_spec / execution_plan
        # are None (no plan / no real spec). decisions.json DOES exist with the
        # default empty shape from create_workspace.DEFAULT_FILES, so the
        # decision_artifact is non-null with a checksum over the empty record.
        m0 = build_manifest_from_workspace(wid)
        assert m0.workspace_id == wid
        assert m0.lifecycle == "NEW"
        assert m0.build_spec is None
        assert m0.execution_plan is None
        assert m0.decision_artifact is not None
        assert m0.checksum_of("build_spec") == ""
        assert m0.checksum_of("execution_plan") == ""

        # with_section returns NEW manifest; original intact (immutable).
        m1 = m0.with_section("build_spec", "art-1", "sha-a")
        assert m0.build_spec is None and m0.checksum_of("build_spec") == ""
        assert m1.build_spec == "art-1" and m1.checksum_of("build_spec") == "sha-a"

        # Replacing a section overwrites the checksum.
        m2 = m1.with_section("build_spec", "art-2", "sha-b")
        assert m2.build_spec == "art-2" and m2.checksum_of("build_spec") == "sha-b"

        # Clearing a section drops the checksum.
        m3 = m2.with_section("build_spec", None, "")
        assert m3.build_spec is None and m3.checksum_of("build_spec") == ""

        # Unknown section raises (no silent typos).
        raised = False
        try:
            m0.with_section("not_a_section", "x", "y")
        except ValueError:
            raised = True
        assert raised

        # with_lifecycle returns a new manifest.
        m4 = m1.with_lifecycle("SPEC_READY")
        assert m0.lifecycle == "NEW" and m4.lifecycle == "SPEC_READY"

        # store/load round-trip.
        assert store_manifest(wid, m1)
        loaded = load_manifest(wid)
        assert loaded is not None
        assert loaded.workspace_id == m1.workspace_id
        assert loaded.build_spec == m1.build_spec
        assert loaded.checksum_of("build_spec") == "sha-a"
        assert loaded.lifecycle == m1.lifecycle
        # nonexistent
        assert load_manifest("nope") is None

        # build_manifest_from_workspace picks up decision + spec + plan files.
        import json as _json
        (bw_dir := _bw._workspace_dir(wid) / "decisions.json").write_text(
            _json.dumps({"verified_decisions": [], "verification_result": {}}),
            encoding="utf-8")
        (bw_spec := _bw._workspace_dir(wid) / "implementation_plan.md").write_text(
            "# Architecture\n...\n# Components\n...\n# File Structure\n...\n"
            "# Dependencies\n...\n# Constraints\n...\n"
            "# Testing Requirements\n...\n# Acceptance Criteria\n...\n",
            encoding="utf-8")
        from execution_planner import plan, store_execution_plan
        store_execution_plan(wid, plan({"verification_result": {},
                                         "verified_decisions": []}))
        m5 = build_manifest_from_workspace(wid)
        assert m5.decision_artifact is not None
        assert m5.build_spec is not None
        assert m5.execution_plan is not None
        assert m5.checksum_of("decision_artifact")
        assert m5.checksum_of("build_spec")
        assert m5.checksum_of("execution_plan")
        # Roundtrip through disk preserves all references.
        assert store_manifest(wid, m5)
        m5r = load_manifest(wid)
        assert m5r is not None
        assert m5r.checksum_of("execution_plan") == m5.checksum_of("execution_plan")
        _bw.WORKSPACE_ROOT = _R0
    print("manifest smoke test PASSED")
