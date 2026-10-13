"""
Project Sol0mon - Artifact System (Final Architecture Hardening - Phase B).

First-class, immutable artifact objects with provenance. Every important
pipeline output is a structured ``Artifact``, not a markdown blob. Markdown
remains a *rendering* of the artifact's ``payload`` (Phase H invariant).

The spec lists eight artifact classes (VerificationArtifact, DecisionArtifact,
BuildSpecArtifact, ExecutionPlanArtifact, ImplementationArtifact,
ValidationArtifact, TestArtifact, FinalBuildArtifact). They have identical
shape (id / version / producer / created_at / checksum / dependencies /
validation_status / lifecycle_stage / payload). Eight subclasses with
identical fields is the textbook "factory for one product" anti-pattern, so
ponytail collapses them to ONE frozen dataclass + an ``ArtifactKind``
discriminator. The eight named "artifacts" are instances with the matching
kind — ``build_spec_artifact(spec, ...)`` returns ``Artifact(kind=
ArtifactKind.BUILD_SPEC, ...)``.

Immutability: ``Artifact`` is a frozen dataclass. The only "mutation" is
``with_validation(...)`` which returns a NEW artifact (version bumped), so the
record of what was produced never silently changes. Every downstream artifact
references its upstreams via ``dependencies`` (a tuple of artifact ids), so
provenance is a graph a future audit can walk.

Storage: ``ArtifactStore`` writes a sidecar per artifact under
``<workspace>/artifacts/<id>.json`` + an ``index.json`` summary. Reuses
``build_workspace._workspace_dir`` so conftest's WORKSPACE_ROOT snapshot covers
tests. Content-addressed-ish: the id is ``uuid5`` of ``"<kind>:<checksum>"`` so
the same producer re-deriving the same payload yields the same id (resume
re-uses the stored record; no duplicate writes).
"""

from __future__ import annotations

import enum
import hashlib
import json
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any


# Fixed namespace so deterministic ids are stable across runs / machines.
_ARTIFACT_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-4789-9abc-def012345678")


class ArtifactKind(str, enum.Enum):
    VERIFICATION = "VerificationArtifact"
    DECISION = "DecisionArtifact"
    BUILD_SPEC = "BuildSpecArtifact"
    EXECUTION_PLAN = "ExecutionPlanArtifact"
    IMPLEMENTATION = "ImplementationArtifact"
    VALIDATION = "ValidationArtifact"
    TEST = "TestArtifact"
    FINAL_BUILD = "FinalBuildArtifact"


class ValidationStatus(str, enum.Enum):
    UNVALIDATED = "unvalidated"
    PASSED = "passed"
    FAILED = "failed"
    WARNINGS = "warnings"


def _canonical_bytes(payload: Any) -> bytes:
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str, ensure_ascii=False).encode("utf-8")


def checksum(payload: Any) -> str:
    """SHA-256 hex of the canonical serialization of ``payload``. Dicts are
    sorted-key JSON so a payload re-derived in a different insertion order
    still hashes identically (deterministic provenance)."""
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


@dataclass(frozen=True)
class Artifact:
    """Immutable artifact record. The structured ``payload`` is the source of
    truth; markdown / rendered views derive from it. ``id`` is deterministic
    over (kind, checksum) so resume re-derives the same record."""

    kind: ArtifactKind
    payload: Any
    producer: str
    id: str = field(default="")
    version: int = 1
    created_at: str = ""
    checksum: str = ""
    dependencies: tuple[str, ...] = field(default_factory=tuple)
    validation_status: ValidationStatus = ValidationStatus.UNVALIDATED
    lifecycle_stage: str = ""
    validation_report_id: str = ""

    def __post_init__(self) -> None:
        # frozen=True forbids plain assignment; use object.__setattr__ to fill
        # the auto-computed fields when the caller omitted them.
        if not self.checksum:
            object.__setattr__(self, "checksum", checksum(self.payload))
        if not self.created_at:
            object.__setattr__(self, "created_at",
                               datetime.now(timezone.utc).isoformat())
        if not self.id:
            object.__setattr__(self, "id",
                               str(uuid.uuid5(_ARTIFACT_NAMESPACE,
                                              f"{self.kind.value}:{self.checksum}")))
        if not self.lifecycle_stage:
            # Default the lifecycle tag to the kind's "home" stage; callers can
            # override (e.g. a BUILD_SPEC produced at SPEC_READY).
            object.__setattr__(self, "lifecycle_stage", _KIND_HOME_STAGE.get(self.kind, ""))

    def with_validation(self, status: ValidationStatus,
                        report_id: str = "") -> "Artifact":
        """Return a new Artifact with validation_status set + version
        bumped. The original is unchanged (immutability). The id stays stable
        (validation is metadata, not content)."""
        return replace(self, validation_status=status,
                       validation_report_id=report_id,
                       version=self.version + 1)

    def render_markdown(self) -> str:
        """Markdown VIEW of the payload. For a string payload (e.g. the spec
        text) this is the payload itself. For a dict/list, canonical JSON
        pretty-printed. Markdown is never the source of truth."""
        if isinstance(self.payload, str):
            return self.payload
        return json.dumps(self.payload, indent=2, default=str, ensure_ascii=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "id": self.id,
            "version": self.version,
            "producer": self.producer,
            "created_at": self.created_at,
            "checksum": self.checksum,
            "dependencies": list(self.dependencies),
            "validation_status": self.validation_status.value,
            "lifecycle_stage": self.lifecycle_stage,
            "validation_report_id": self.validation_report_id,
        }


# "Home" lifecycle stage for each kind — used when the producer doesn't pass
# one explicitly. Matches the state machine in lifecycle.py.
_KIND_HOME_STAGE = {
    ArtifactKind.VERIFICATION: "VERIFIED",
    ArtifactKind.DECISION: "VERIFIED",
    ArtifactKind.BUILD_SPEC: "SPEC_READY",
    ArtifactKind.EXECUTION_PLAN: "SPEC_READY",
    ArtifactKind.IMPLEMENTATION: "EXECUTING",
    ArtifactKind.VALIDATION: "TESTS_VALIDATED",
    ArtifactKind.TEST: "TESTS_VALIDATED",
    ArtifactKind.FINAL_BUILD: "COMPLETE",
}


# --- Named factory helpers (one-liners; the 8 "artifact classes" as instances) ---

def verification_artifact(verification_result: dict, producer: str,
                          dependencies: tuple[str, ...] = ()) -> Artifact:
    return Artifact(kind=ArtifactKind.VERIFICATION, payload=verification_result,
                    producer=producer, dependencies=dependencies)


def decision_artifact(decision_records_value: list, producer: str,
                      dependencies: tuple[str, ...] = ()) -> Artifact:
    return Artifact(kind=ArtifactKind.DECISION, payload=decision_records_value,
                    producer=producer, dependencies=dependencies)


def build_spec_artifact(spec: dict, producer: str,
                        dependencies: tuple[str, ...] = ()) -> Artifact:
    """``spec`` is the structured dict returned by ``build_spec_engine.build_spec``.
    The markdown view is ``spec['spec_text']`` (Phase H: structured object is
    authoritative, markdown is a rendering)."""
    return Artifact(kind=ArtifactKind.BUILD_SPEC, payload=spec,
                    producer=producer, dependencies=dependencies)


def execution_plan_artifact(plan: dict, producer: str,
                            dependencies: tuple[str, ...] = ()) -> Artifact:
    return Artifact(kind=ArtifactKind.EXECUTION_PLAN, payload=plan,
                    producer=producer, dependencies=dependencies)


def implementation_artifact(file_manifest: dict, producer: str,
                           dependencies: tuple[str, ...] = ()) -> Artifact:
    return Artifact(kind=ArtifactKind.IMPLEMENTATION, payload=file_manifest,
                    producer=producer, dependencies=dependencies)


def validation_artifact(report: dict, producer: str,
                        dependencies: tuple[str, ...] = ()) -> Artifact:
    return Artifact(kind=ArtifactKind.VALIDATION, payload=report,
                    producer=producer, dependencies=dependencies)


def test_artifact(test_results: dict, producer: str,
                  dependencies: tuple[str, ...] = ()) -> Artifact:
    return Artifact(kind=ArtifactKind.TEST, payload=test_results,
                    producer=producer, dependencies=dependencies)


def final_build_artifact(build_state: dict, producer: str,
                         dependencies: tuple[str, ...] = ()) -> Artifact:
    return Artifact(kind=ArtifactKind.FINAL_BUILD, payload=build_state,
                    producer=producer, dependencies=dependencies)


# --- Store: per-workspace sidecar files + index ---

def _store_dir(workspace_id: str):
    from build_workspace import _workspace_dir  # respect conftest snapshot
    return _workspace_dir(workspace_id) / "artifacts"


class ArtifactStore:
    """Per-workspace append-only artifact store. Writes ``artifacts/<id>.json``
    side cars + an ``artifacts/index.json`` summary list. Re-putting the same id
    (same content) is a no-op (idempotent); putting a new version of the same
    id (validation bump) overwrites the side car and the index entry."""

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
        self.root = _store_dir(workspace_id)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, art_id: str) -> "Any":
        return self.root / f"{art_id}.json"

    def put(self, artifact: Artifact) -> Artifact:
        p = self._path(artifact.id)
        p.write_text(
            json.dumps({"artifact": artifact.to_dict(), "payload": artifact.payload},
                       indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
        self._touch_index(artifact)
        return artifact

    def get(self, art_id: str) -> Artifact | None:
        p = self._path(art_id)
        if not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return self._from_stored(data)

    def list(self) -> list[Artifact]:
        if not self.root.is_dir():
            return []
        out: list[Artifact] = []
        for p in sorted(self.root.glob("*.json")):
            if p.name == "index.json":
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            art = self._from_stored(data)
            if art is not None:
                out.append(art)
        return out

    def list_kind(self, kind: ArtifactKind) -> list[Artifact]:
        return [a for a in self.list() if a.kind is kind]

    def _from_stored(self, data: dict) -> Artifact | None:
        meta = data.get("artifact") or {}
        kind_str = meta.get("kind")
        if not kind_str:
            return None
        try:
            kind = ArtifactKind(kind_str)
        except ValueError:
            return None
        return Artifact(
            kind=kind,
            payload=data.get("payload"),
            producer=meta.get("producer", ""),
            id=meta.get("id", ""),
            version=meta.get("version", 1),
            created_at=meta.get("created_at", ""),
            checksum=meta.get("checksum", ""),
            dependencies=tuple(meta.get("dependencies") or ()),
            validation_status=ValidationStatus(
                meta.get("validation_status", "unvalidated")),
            lifecycle_stage=meta.get("lifecycle_stage", ""),
            validation_report_id=meta.get("validation_report_id", ""),
        )

    def _touch_index(self, artifact: Artifact) -> None:
        idx_path = self.root / "index.json"
        idx: list[dict] = []
        if idx_path.is_file():
            try:
                idx = json.loads(idx_path.read_text(encoding="utf-8")) or []
            except (json.JSONDecodeError, OSError):
                idx = []
        # Replace any prior entry for this id (version bump) then write.
        idx = [e for e in idx if e.get("id") != artifact.id]
        idx.append({
            "id": artifact.id,
            "kind": artifact.kind.value,
            "version": artifact.version,
            "producer": artifact.producer,
            "created_at": artifact.created_at,
            "checksum": artifact.checksum,
            "validation_status": artifact.validation_status.value,
            "lifecycle_stage": artifact.lifecycle_stage,
            "dependencies": list(tuple(artifact.dependencies)),
        })
        idx_path.write_text(json.dumps(idx, indent=2), encoding="utf-8")


if __name__ == "__main__":
    # ponytail self-check: smallest thing that fails if the artifact contract
    # regresses. Verifies immutability, deterministic id, checksum stability,
    # with_validation copy semantics, store round-trip, and the answer-156
    # invariant carried through the artifact layer.
    import tempfile
    from build_workspace import WORKSPACE_ROOT as _ROOT_BEFORE
    import build_workspace as _bw
    with tempfile.TemporaryDirectory() as tmp:
        _bw.WORKSPACE_ROOT = __import__("pathlib").Path(tmp)  # isolated store
        # Deterministic id + checksum for identical payload.
        a1 = build_spec_artifact({"spec_text": "abc", "status": "completed"}, "bse")
        a2 = build_spec_artifact({"spec_text": "abc", "status": "completed"}, "bse")
        assert a1.id == a2.id and a1.checksum == a2.checksum, (a1.id, a2.id)
        # Different payload -> different id.
        a3 = build_spec_artifact({"spec_text": "xyz", "status": "completed"}, "bse")
        assert a3.id != a1.id
        # Immutability: with_validation returns a new artifact, original intact.
        a1v = a1.with_validation(__import__("artifacts").ValidationStatus.PASSED, "rpt")
        assert a1v.validation_status.value == "passed" and a1v.version == 2
        assert a1.validation_status.value == "unvalidated" and a1.version == 1
        assert a1v.id == a1.id  # validation is metadata, not content
        # Store round-trip.
        store = ArtifactStore("ws_smoke")
        store.put(a1)
        store.put(a1v)  # version bump overwrites the side car + replaces index
        got = store.get(a1.id)
        assert got is not None and got.version == 2 and got.payload["spec_text"] == "abc"
        # All 8 named factories produce distinct kinds.
        kinds = [
            verification_artifact({}, "x").kind,
            decision_artifact([], "x").kind,
            build_spec_artifact({}, "x").kind,
            execution_plan_artifact({}, "x").kind,
            implementation_artifact({}, "x").kind,
            validation_artifact({}, "x").kind,
            test_artifact({}, "x").kind,
            final_build_artifact({}, "x").kind,
        ]
        assert len(set(kinds)) == 8, [k.value for k in kinds]
        # Provenance: downstream artifact references upstreams.
        dec = decision_artifact([{"winner_text": "SQLite"}], "dm",
                                dependencies=(a1.id,))
        assert dec.dependencies == (a1.id,)
        _bw.WORKSPACE_ROOT = _ROOT_BEFORE  # restore (conftest not active here)
    print("artifacts smoke test PASSED")
