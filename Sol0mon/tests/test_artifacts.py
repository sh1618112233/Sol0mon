"""
test_artifacts.py - Phase B regression: the Artifact system.

Pins:
  - One class + kind discriminator: all 8 named artifact kinds exist & differ.
  - Immutability: frozen dataclass; with_validation returns a NEW artifact
    (version bumped); the original is unchanged.
  - Deterministic id + checksum over (kind, payload) — same payload -> same
    id; reorder dict keys -> same checksum (sort_keys); different payload ->
    different id.
  - Provenance: dependencies (tuple of upstream ids) are carried.
  - render_markdown: str payload -> string; dict/list -> JSON view.
  - ArtifactStore put/get/list/list_kind round-trip; version bump replaces the
    index entry; store respects WORKSPACE_ROOT (conftest isolation).
  - Artifact dependency integrity: a downstream artifact references the
    upstream id verbatim (resume + audit can walk the graph).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import artifacts as A
import build_workspace as bw


def _tmp_root():
    tmp = tempfile.mkdtemp(prefix="sol0mon_art_")
    bw.WORKSPACE_ROOT = Path(tmp)
    return tmp


def test_eight_named_kinds_exist_and_differ():
    factories = [
        A.verification_artifact({}, "x"),
        A.decision_artifact([], "x"),
        A.build_spec_artifact({}, "x"),
        A.execution_plan_artifact({}, "x"),
        A.implementation_artifact({}, "x"),
        A.validation_artifact({}, "x"),
        A.test_artifact({}, "x"),
        A.final_build_artifact({}, "x"),
    ]
    assert {f.kind for f in factories} == set(A.ArtifactKind)
    assert len({f.kind.value for f in factories}) == 8


def test_required_fields_present_on_every_artifact():
    a = A.build_spec_artifact({"spec_text": "x", "status": "completed"}, "bse",
                              dependencies=("upstream123",))
    for field in ("id", "version", "producer", "created_at", "checksum",
                  "dependencies", "validation_status", "lifecycle_stage",
                  "payload"):
        assert getattr(a, field) is not None or field != "created_at", field
    assert a.version == 1
    assert a.producer == "bse"
    assert a.dependencies == ("upstream123",)
    assert a.validation_status is A.ValidationStatus.UNVALIDATED
    # home lifecycle stage default for BUILD_SPEC is SPEC_READY (lifecycle.py)
    assert a.lifecycle_stage == "SPEC_READY"


def test_immutable_with_validation_returns_new_artifact():
    a = A.build_spec_artifact({"spec_text": "x"}, "bse")
    a_v = a.with_validation(A.ValidationStatus.PASSED, "rpt-1")
    assert a_v.validation_status is A.ValidationStatus.PASSED
    assert a_v.version == 2 and a_v.validation_report_id == "rpt-1"
    # Original intact.
    assert a.validation_status is A.ValidationStatus.UNVALIDATED and a.version == 1
    assert a.id == a_v.id  # validation is metadata, not content -> id stable


def test_frozen_outside_with_validation_rejects_assignment():
    a = A.build_spec_artifact({}, "x")
    raised = False
    try:
        a.version = 99  # type: ignore[misc]
    except Exception:
        raised = True
    assert raised, "frozen dataclass allowed direct assignment"


def test_deterministic_id_and_checksum():
    payload = {"spec_text": "abc", "status": "completed", "verified_decisions": []}
    a1 = A.build_spec_artifact(payload, "bse")
    a2 = A.build_spec_artifact(payload, "bse")
    assert a1.id == a2.id and a1.checksum == a2.checksum

    # Reordered dict keys -> same checksum (sort_keys canonicalization).
    reordered = {"verified_decisions": [], "spec_text": "abc", "status": "completed"}
    a3 = A.build_spec_artifact(reordered, "bse")
    assert a3.checksum == a1.checksum and a3.id == a1.id

    # Different payload -> different id.
    a4 = A.build_spec_artifact({"spec_text": "xyz", "status": "completed"}, "bse")
    assert a4.id != a1.id and a4.checksum != a1.checksum


def test_render_markdown_views():
    str_art = A.build_spec_artifact({"spec_text": "# Sections\\n..."}, "bse")
    # payload is the structured dict; markdown view of the dict is JSON. The
    # spec_text itself (a str) renders as the string when the payload IS str:
    txt_art = A.Artifact(kind=A.ArtifactKind.BUILD_SPEC, payload="# Heading", producer="x")
    assert txt_art.render_markdown() == "# Heading"
    # dict payload -> pretty JSON
    out = str_art.render_markdown()
    assert "spec_text" in out and "{" in out


def test_store_round_trip_and_index():
    _tmp_root()
    wid = bw.create_workspace()
    store = A.ArtifactStore(wid)
    a = A.build_spec_artifact({"spec_text": "x", "status": "completed"}, "bse")
    store.put(a)
    got = store.get(a.id)
    assert got is not None
    assert got.id == a.id and got.checksum == a.checksum
    assert got.payload["spec_text"] == "x"
    assert got.kind is A.ArtifactKind.BUILD_SPEC

    # list / list_kind
    assert any(x.id == a.id for x in store.list())
    assert any(x.id == a.id for x in store.list_kind(A.ArtifactKind.BUILD_SPEC))
    assert store.list_kind(A.ArtifactKind.DECISION) == []

    # Missing id -> None (never raises).
    assert store.get("does-not-exist") is None


def test_version_bump_replaces_index_entry_not_duplicates():
    _tmp_root()
    wid = bw.create_workspace()
    store = A.ArtifactStore(wid)
    a = A.build_spec_artifact({"spec_text": "x"}, "bse")
    store.put(a)
    store.put(a.with_validation(A.ValidationStatus.PASSED, "r"))  # version 2
    got = store.get(a.id)
    assert got is not None and got.version == 2
    # index.json has exactly one row per id (latest version).
    idx_path = store.root / "index.json"
    import json
    idx = json.loads(idx_path.read_text())
    rows = [e for e in idx if e["id"] == a.id]
    assert len(rows) == 1 and rows[0]["version"] == 2


def test_artifact_dependency_integrity():
    """A downstream artifact references upstream ids verbatim; the ids are
    content-addressed so identical upstream content yields the same id an
    audit can walk."""
    _tmp_root()
    wid = bw.create_workspace()
    store = A.ArtifactStore(wid)

    vr = {"verification_result_ok": True}
    upstream = A.verification_artifact(vr, "verifier")
    store.put(upstream)

    # Build a BUILD_SPEC that depends on the verification artifact.
    spec = A.build_spec_artifact({"spec_text": "...", "verified_decisions": []},
                                 "bse", dependencies=(upstream.id,))
    store.put(spec)
    assert spec.dependencies == (upstream.id,)
    # The id stored on the downstream matches the upstream retrieved id.
    got_up = store.get(upstream.id)
    got_down = store.get(spec.id)
    assert got_up.id in got_down.dependencies


def test_checksum_stable_for_string_payload():
    a = A.Artifact(kind=A.ArtifactKind.TEST, payload="hello", producer="t")
    b = A.Artifact(kind=A.ArtifactKind.TEST, payload="hello", producer="t")
    assert a.checksum == b.checksum and a.id == b.id
    c = A.Artifact(kind=A.ArtifactKind.TEST, payload="Hello", producer="t")
    assert c.checksum != a.checksum  # case-sensitive


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
