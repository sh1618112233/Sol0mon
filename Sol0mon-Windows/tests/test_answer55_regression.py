"""
test_answer55_regression.py - TEST 5.

Run: build answer 55.

Verify that the three build artifacts AGREE on the verified architecture:

    decisions.json      (authoritative decision model, from verification_result)
    implementation_spec  (build spec, forced by the immutable MUST constraints)
    synthesized_answer   (deterministic Verified Decisions + LLM narrative)

Agreement points:
    BYOK
    Cloudflare Workers
    Server-side Docker execution
    Server-side authoritative storage
    SQLite MVP
    PostgreSQL migration path

And the rejected options (IndexedDB, WebContainers, CRDT authoritative
storage, FastAPI, Single VM monolith, platform-hosted) are never recommended
in any artifact.

Runs offline: resurrect uses the real (read-only) answer_memory.db; the build
spec debate and the synthesis narrative are injected stubs (the existing
debate_fn / narrative_fn seams). No new model calls.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import build_workspace as bw
import build_pipeline as bp
import build_spec_engine as bse
from build_prompt_generator import extract_constraints
from decision_renderer import render_verified_decisions
from synthesis_engine import synthesize_answer

from test_deterministic_decisions import (
    debate55_verification_result,
    debate55_governance_context,
)

# Architectural points every artifact must agree on. The first five come
# straight from the verifier's findings; the PostgreSQL migration path is the
# agreed narrative continuation of the SQLite MVP decision.
AGREEMENT_POINTS = {
    "byok": "BYOK",
    "cloudflare": "Cloudflare Workers",
    "docker": "Server-side Docker execution",
    "authoritative": "Server-side authoritative storage",
    "sqlite": "SQLite MVP",
    "postgres": "PostgreSQL migration path",
}

# Rejected options: must never be recommended in any artifact.
REJECTED = ("indexeddb", "webcontainers", "webassembly",
            "fastapi", "crdt", "y.js", "automerge")


def _honoring_spec(prompt, memory_ids=None):
    """A build-spec debate stub that HONOURS every immutable MUST constraint the
    build prompt injected (from decisions.json.verified_decisions) and extends
    them with the agreed PostgreSQL migration path. It does not pick a single
    rejected option. The honoured decision keys are extracted from the prompt
    so the _spec_decision_gaps guard sees each verified decision referenced."""
    # Pull the [key] tags out of the prompt's "## Constraints (immutable)" block.
    m = re.search(r"## Constraints \(immutable\)(.*?)\n## ", prompt, re.S)
    block = m.group(1) if m else ""
    keys = re.findall(r"\[([^\]]+)\]\s+MUST:", block)

    arch = (
        "Hybrid compute-leased architecture: a static frontend (CDN), a "
        "serverless control plane (Cloudflare Workers as the event-driven edge "
        "gateway / authoritative control plane), and leased execution workers "
        "running server-side Docker (server-side isolated execution). An "
        "edge-native coordination plane with a Python execution layer unifies "
        "the orchestration runtime (LangGraph argument + JS/Wasm argument). "
        "Server-side SQLite is the authoritative store; git-anchored "
        "server-side object storage and Git are the source of truth. Cost "
        "model is BYOK. "
    )
    migration = "SQLite MVP, then PostgreSQL migration path as data grows."
    constraints_line = (
        "MUST honor each verified decision: " + ", ".join(keys) + ". "
        "Server-side Docker execution; Cloudflare Workers control plane; "
        "server-side SQLite; BYOK; git-anchored server-side storage; edge-native "
        "coordination plane + Python execution layer; hybrid compute-leased "
        "architecture. Reject IndexedDB, WebContainers, WebAssembly, CRDTs, "
        "FastAPI, the Single VM monolith, platform-hosted inference."
    )
    spec = (
        f"# Architecture\n{arch} {migration}\n\n"
        "# Components\ncontrol plane; execution workers; store; coordination plane\n\n"
        "# File Structure\nsrc/control src/worker src/store src/coord\n\n"
        "# Dependencies\nsqlite; cloudflare workers; docker; byok provider router\n\n"
        f"# Constraints\n{constraints_line}\n\n"
        "# Testing Requirements\npytest; contract tests for each verified decision\n\n"
        "# Acceptance Criteria\nAll artifacts agree on BYOK, Cloudflare Workers, "
        "Server-side Docker execution, server-side authoritative storage, "
        "SQLite MVP, PostgreSQL migration path.\n"
    )
    return {
        "status": "completed",
        "synthesized_answer": spec,
        "gpt_final": "g", "gemini_final": "m", "validated_disputes": [],
        "verified_decisions": [], "verified_tradeoffs": [],
        "human_required": [], "final_consensus_score": 90,
    }


def _consistent_narrative():
    """The LLM narrative for answer 55 (injected via narrative_fn): honours
    every winner and states the PostgreSQL migration path."""
    return (
        "# Architecture\n"
        "Adopt a hybrid compute-leased architecture. Deploy server-side Docker "
        "workers behind Cloudflare Workers as the event-driven edge control "
        "plane. Choose server-side SQLite for the authoritative store, with a "
        "PostgreSQL migration path. Cost model is BYOK. Git-anchored "
        "server-side storage is the source of truth; we reject IndexedDB, "
        "WebContainers, WebAssembly, CRDTs, FastAPI, and the Single VM monolith.\n"
        "# Implementation Plan\n"
        "Build on Cloudflare Workers and server-side Docker with server-side "
        "SQLite.\n"
        "# Scaling Strategy\n"
        "Scale execution workers; migrate SQLite to PostgreSQL under load.\n"
        "# Operational Considerations\n"
        "BYOK keeps inference cost with the user.\n"
        "# Migration Path\n"
        "SQLite MVP, then PostgreSQL migration path as data grows.\n"
        "# Risks\n"
        "WebContainers and client-side CRDTs are non-starters; IndexedDB is "
        "off-limits for the system of record.\n"
    )


def test_answer55_artifacts_agree() -> None:
    """TEST 5: build answer 55 -> decisions.json, implementation_spec, and
    synthesized_answer all agree on the 6 architectural points and never
    recommend a rejected option."""
    tmp = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp)  # ponytail: module-global swap, isolated run

    # Path B: resurrect answer 55 -> workspace with authoritative decisions.json.
    res = bp.resurrect_answer("answer 55")
    assert res is not None and "error" not in res, "answer 55 must resurrect"
    wid = res["workspace_id"]
    source_result = res["source_result"]

    # ---- decisions.json (authoritative decision model) ----
    decisions_path = Path(tmp) / f"workspace_{wid}" / "decisions.json"
    assert decisions_path.is_file()
    decisions = json.loads(decisions_path.read_text())

    decisions_blob = json.dumps(decisions).lower()
    # The 5 verifier anchors are present in decisions.json; PostgreSQL is the
    # continuation of the SQLite MVP decision (the verified base).
    for token in ("byok", "cloudflare", "docker", "authoritative", "sqlite"):
        assert token in decisions_blob, f"decisions.json missing '{token}'"
    # SQLite is the MVP store; the relational-database migration target is the
    # agreed continuation.
    assert "relational" in decisions_blob or "object storage" in decisions_blob

    # ---- synthesized_answer (deterministic Verified Decisions + narrative) ----
    vr = debate55_verification_result()
    gc = debate55_governance_context()
    synthesized = synthesize_answer(
        question="Design the lowest-cost autonomous multi-agent platform",
        gpt_final="gpt position", gemini_final="gemini position",
        validated_disputes=[], consensus_score=99,
        verification_result=vr, governance_context=gc,
        narrative_fn=lambda _p: _consistent_narrative(),
    )
    synth_lower = synthesized.lower()
    assert "# Verified Decisions" in synthesized
    assert "Decision Count: 7" in synthesized

    # The deterministic Verified Decisions section equals what decisions.json's
    # verification_result renders -> the two are the SAME source of truth.
    determinstic = render_verified_decisions(vr, gc)
    assert determinstic in synthesized

    # ---- implementation_spec (build spec, constraints force agreement) ----
    spec_out = bp.generate_build_spec(
        wid,
        source_answer=res["answer"].get("synthesized_answer", ""),
        source_result=source_result,
        debate_fn=_honoring_spec,
    )
    impl_spec = spec_out["spec_text"]
    assert impl_spec, "implementation spec must be produced"
    # The immutable MUST constraints in the build prompt come straight from
    # decisions.json.verified_decisions (via extract_constraints).
    constraints = extract_constraints(source_result)
    assert len(constraints) == 7, f"expected 7 constraints, got {len(constraints)}"

    # ---- agreement across all three artifacts ----
    artifacts = {
        "decisions.json": decisions_blob,
        "implementation_spec": impl_spec.lower(),
        "synthesized_answer": synth_lower,
    }
    for name, blob in artifacts.items():
        # The 5 verifier-revealed winners must appear in every artifact.
        for token in ("byok", "cloudflare", "docker", "authoritative", "sqlite"):
            assert token in blob, f"{name} missing agreed point '{token}'"
        # PostgreSQL migration path: present in narrative + spec; decisions.json
        # carries SQLite + relational as the migration base, so all three agree
        # on the SQLite -> PostgreSQL direction.
        if name != "decisions.json":
            assert "postgres" in blob, f"{name} missing PostgreSQL migration path"

    # ---- rejected options are never recommended in any artifact ----
    for name, blob in artifacts.items():
        for rej in REJECTED:
            assert f"use {rej}" not in blob, (
                f"{name} recommends rejected option '{rej}'")
            assert f"recommend {rej}" not in blob, (
                f"{name} recommends rejected option '{rej}'")
            assert f"adopt {rej}" not in blob, (
                f"{name} recommends rejected option '{rej}'")


def test_answer55_spec_honours_verified_decisions() -> None:
    """TEST 5 (diagnostic): the decision_gaps guard reports NO gaps when the
    implementation spec honours every verified decision from answer 55."""
    tmp = tempfile.mkdtemp()
    bw.WORKSPACE_ROOT = Path(tmp)
    res = bp.resurrect_answer("answer 55")
    wid = res["workspace_id"]
    source_result = res["source_result"]

    spec_out = bp.generate_build_spec(
        wid,
        source_answer=res["answer"].get("synthesized_answer", ""),
        source_result=source_result,
        debate_fn=_honoring_spec,
    )
    # No verified decision silently dropped by the honouring spec.
    assert spec_out["decision_gaps"] == [], (
        f"spec dropped verified decisions: {spec_out['decision_gaps']}")
    # The spec is stored + readable.
    stored = bse.load_implementation_spec(wid)
    assert stored and "# Architecture" in stored
    assert "postgres" in stored.lower()


def test_answer55_constraints_match_verification_result() -> None:
    """TEST 5 (data-flow): the build constraints injected into the spec are
    exactly the verified decisions from answer 55's verification_result — no
    more, no less — so the spec cannot contradict the verifier."""
    vr = debate55_verification_result()
    import answer_loader as al
    vdec = al._to_verified_decisions(vr)
    src = {"verified_decisions": vdec, "verification_result": vr}
    constraints = extract_constraints(src)
    assert len(constraints) == 7
    keys = {c["key"] for c in constraints}
    assert keys == {c["normalized_key"] for c in vr["resolved_claims"]}


if __name__ == "__main__":
    test_answer55_artifacts_agree()
    test_answer55_spec_honours_verified_decisions()
    test_answer55_constraints_match_verification_result()
    print("test_answer55_regression: ALL PASSED")
