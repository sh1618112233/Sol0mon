"""
test_workspace_followup_verification.py - Regression test for the workspace
follow-up verification bug in workspace_debate_engine.py.

Root cause: the follow-up path in WorkspaceDebateEngine.resume_workspace()
called verify_all_disputes() with

    gpt_position="\\n\\n".join(... for d in pending)
    gemini_position="\\n\\n".join(... for d in pending)

  where `pending` was never defined, raising NameError the moment the
  follow-up path reached verification.

This test drives the real resume_workspace() end-to-end (offline, with no
model/browser/API calls) so that:

  1. The workspace follow-up path is exercised.
  2. At least one dispute has status == "pending_verification".
  3. verify_all_disputes() is reached (the buggy site).
  4. No NameError occurs.
  5. gpt_position and gemini_position are generated from the pending
     disputes only.
  6. validated_disputes is generated from the same pending list.
  7. Fully offline: no model calls, no browser calls, no API calls.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dataclasses import dataclass

import workspace_debate_engine as wde
from workspace_manager import Workspace, WorkspaceStatus
from debate_engine import Dispute


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSession:
    """Stand-in for GPTSession / GeminiSession — no browser, no network.

    `ask()` returns a deterministic string; `start()`/`close()` are no-ops.
    """

    def __init__(self, label: str):
        self.label = label
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    def ask(self, prompt, dispute_key=None):
        # Deterministic, side-effect-free response. The exact text does not
        # matter — _debate_dispute is mocked and overwrites per-dispute
        # positions inside resume_workspace().
        return f"{self.label}:response"


class FakeWorkspaceManager:
    """In-memory workspace manager — no SQLite, no disk."""

    def __init__(self, ws: Workspace):
        self._ws = ws
        self.save_calls = 0

    def load_workspace(self, workspace_id: str):
        assert workspace_id == self._ws.workspace_id, (
            f"unexpected workspace_id: {workspace_id!r}")
        return self._ws

    def save_workspace(self, _ws):
        self.save_calls += 1


class FakeMemory:
    """Stand-in for answer_memory.memory — no DB writes."""

    def __init__(self):
        self.save_calls = 0

    def save_debate(self, **_kwargs) -> int:
        self.save_calls += 1
        return 4242  # deterministic answer_id


class FakeHumanDecisionQueue:
    """Stand-in for HumanDecisionQueue.get_pending_count — no DB."""

    def __init__(self):
        self.created = True

    def get_pending_count(self, _workspace_id: str) -> int:
        return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_disputes() -> list:
    """Build three active disputes. After the (mocked) debate loop, two of
    them become pending_verification and one becomes deadlocked, so the
    fixed `pending` list contains exactly the first two — in order."""
    return [
        Dispute(
            dispute_text="Dispute Alpha: Postgres vs SQLite for metadata",
            normalized_key="alpha_db",
            gpt_source="g", gemini_source="m",
            priority="HIGH", priority_weight=5,
        ),
        Dispute(
            dispute_text="Dispute Beta: K8s vs Nomad for orchestration",
            normalized_key="beta_orch",
            gpt_source="g", gemini_source="m",
            priority="MEDIUM", priority_weight=3,
        ),
        Dispute(
            dispute_text="Dispute Gamma: Git vs CRDT for storage",
            normalized_key="gamma_store",
            gpt_source="g", gemini_source="m",
            priority="LOW", priority_weight=1,
        ),
    ]


# normalized_keys that the mocked _debate_dispute() will "resolve". Anything
# not in this set is returned as "deadlocked" and therefore excluded from the
# pending_verification set by the resume_workspace() loop.
_RESOLVED_KEYS = {"alpha_db", "beta_orch"}


def _stub_engine(engine: wde.WorkspaceDebateEngine) -> None:
    """Replace the LLM/browser-dependent methods on the instance with
    deterministic stubs so resume_workspace() runs fully offline."""
    engine._analyze_follow_up = lambda _follow_up, _ws: []
    engine._build_dispute_queue = lambda _raw, _g, _m: _make_disputes()

    def _debate_dispute(_question, dispute, _gpt, _gemini):
        # Deterministic per-dispute positions — these are what the fixed
        # `pending` comprehension joins into gpt_position / gemini_position.
        dispute.gpt_current = f"GPT::{dispute.normalized_key}"
        dispute.gemini_current = f"GEM::{dispute.normalized_key}"
        dispute.consensus_score = 91
        return "resolved" if dispute.normalized_key in _RESOLVED_KEYS \
            else "deadlocked"

    engine._debate_dispute = _debate_dispute


def _install_patches(captured: dict):
    """Patch every external touchpoint of resume_workspace(). Returns a
    list of (module, attr, original) tuples for restoration."""
    patches = []

    # verify_all_disputes: imported at module top into workspace_debate_engine.
    def _fake_verify_all_disputes(question, validated_disputes, gpt_position,
                                 gemini_position, use_external_research=False,
                                 **_kwargs):
        captured["called"] = True
        captured["question"] = question
        captured["validated_disputes"] = list(validated_disputes)
        captured["gpt_position"] = gpt_position
        captured["gemini_position"] = gemini_position
        captured["use_external_research"] = use_external_research
        # No unresolved claims -> all pending disputes get verified decisions
        # in the post-verification block, no governance escalation path taken.
        return {
            "resolved_claims": [
                {"dispute": d["dispute"], "normalized_key": d["normalized_key"],
                 "verification": {"status": "RESOLVED", "confidence": 95,
                                  "finding": "ok",
                                  "requires_external_evidence": False}}
                for d in validated_disputes
            ],
            "equivalent_claims": [],
            "unresolved_claims": [],
            "verification_score": 100,
            "truncated_count": 0,
        }

    patches.append((wde, "verify_all_disputes", wde.verify_all_disputes))
    wde.verify_all_disputes = _fake_verify_all_disputes

    # conflict_extractor.extract_conflicts: imported INSIDE resume_workspace
    # via `from conflict_extractor import extract_conflicts`. Patch the source.
    import conflict_extractor as ce
    patches.append((ce, "extract_conflicts", ce.extract_conflicts))
    ce.extract_conflicts = lambda _g, _m: {
        "severity": "MEDIUM",
        "agreed_claims": [],
        "disputed_claims": ["D-Alpha", "D-Beta", "D-Gamma"],
        "resolved_disputes": [],
    }

    # synthesis_engine.synthesize_answer: imported INSIDE resume_workspace.
    import synthesis_engine as se
    patches.append((se, "synthesize_answer", se.synthesize_answer))
    se.synthesize_answer = lambda **_kw: "SYNTHESIZED_ANSWER_OFFLINE"

    # GPTSession / GeminiSession: imported INSIDE resume_workspace via
    # `from gpt_worker import GPTSession` / `from gemini_worker import GeminiSession`.
    import gpt_worker as gw
    import gemini_worker as gemw
    patches.append((gw, "GPTSession", gw.GPTSession))
    patches.append((gemw, "GeminiSession", gemw.GeminiSession))
    gw.GPTSession = lambda label="GPT": FakeSession(label)
    gemw.GeminiSession = lambda label="GEM": FakeSession(label)

    # workspace_mgr: imported at module top.
    patches.append((wde, "workspace_mgr", wde.workspace_mgr))

    # memory: imported at module top.
    patches.append((wde, "memory", wde.memory))
    wde.memory = FakeMemory()

    # HumanDecisionQueue: imported at module top.
    patches.append((wde, "HumanDecisionQueue", wde.HumanDecisionQueue))
    wde.HumanDecisionQueue = FakeHumanDecisionQueue

    return patches


def _restore(patches, ws_holder: dict):
    for mod, attr, original in patches:
        setattr(mod, attr, original)
    # workspace_mgr restoration uses the original singleton captured above.
    wde.workspace_mgr = next(
        o for (m, _a, o) in patches if m is wde and _a == "workspace_mgr")


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_workspace_followup_verification_no_name_error() -> None:
    """Exercise resume_workspace() through the verification call and assert:

      - verify_all_disputes() is reached (no NameError),
      - gpt_position / gemini_position are the join of the pending disputes'
        current positions,
      - validated_disputes is built from the same pending list.
    """
    ws = Workspace(workspace_id="ws_test_followup", original_prompt="original")
    ws.status = WorkspaceStatus.CONSENSUS_REACHED
    ws_holder = {"ws": ws}

    captured: dict = {"called": False}
    orig_wm = wde.workspace_mgr
    patches = _install_patches(captured)
    # Set the fake workspace manager (original workspace_mgr is in patches for
    # restoration; the fake lives on wde during the test).
    wde.workspace_mgr = FakeWorkspaceManager(ws)

    engine = wde.WorkspaceDebateEngine()
    _stub_engine(engine)

    try:
        # The real follow-up path. Before the fix this raised NameError at the
        # `for d in pending` generator inside the verify_all_disputes() call.
        result = engine.resume_workspace("ws_test_followup", "follow-up Q?")
    finally:
        # Restore workspace_mgr first (so _restore's comprehension stays sane),
        # then restore everything else.
        wde.workspace_mgr = orig_wm
        for mod, attr, original in patches:
            if mod is wde and attr == "workspace_mgr":
                continue  # already restored above
            setattr(mod, attr, original)

    # ---- 3 + 4: verification reached, no NameError ----
    assert captured["called"] is True, (
        "verify_all_disputes() was never reached — "
        "resume_workspace() likely raised before/inside the verification call")

    # ---- 2 + 5 + 6: pending disputes drove the verification args ----
    # Expected pending set: alpha_db and beta_orch (resolved), gamma is
    # deadlocked and must NOT appear.
    vd = captured["validated_disputes"]
    keys_in_vd = [d["normalized_key"] for d in vd]
    assert keys_in_vd == ["alpha_db", "beta_orch"], (
        f"validated_disputes must be built from the pending list only; "
        f"got keys={keys_in_vd}")
    assert all(set(d.keys()) == {"dispute", "normalized_key"} for d in vd), (
        "each validated_disputes entry must carry exactly dispute + normalized_key")
    assert [d["dispute"] for d in vd] == [
        "Dispute Alpha: Postgres vs SQLite for metadata",
        "Dispute Beta: K8s vs Nomad for orchestration",
    ], "dispute text in validated_disputes must match the pending disputes"

    # gpt_position / gemini_position = join of pending disputes' current
    # positions, IN ORDER, excluding the deadlocked dispute.
    assert captured["gpt_position"] == "GPT::alpha_db\n\nGPT::beta_orch", (
        f"gpt_position must be generated from pending disputes only; "
        f"got {captured['gpt_position']!r}")
    assert captured["gemini_position"] == "GEM::alpha_db\n\nGEM::beta_orch", (
        f"gemini_position must be generated from pending disputes only; "
        f"got {captured['gemini_position']!r}")

    # ---- 5: use_external_research unchanged ----
    assert captured["use_external_research"] is False, (
        "use_external_research must remain False (no API/web research)")

    # ---- follow-up answer id / shape sanity ----
    assert result["status"] == "completed", f"got {result.get('status')!r}"
    assert result["answer_id"] == 4242, "memory.save_debate stub id leaked"


def test_pending_dispute_status_is_pending_verification() -> None:
    """Explicit regression guard: a dispute the mocked _debate_dispute
    resolves must land on status == 'pending_verification' inside
    resume_workspace(), which is the predicate the `pending` comprehension
    filters on. If this predicate ever changes, the bug fix silently stops
    selecting any disputes."""
    ws = Workspace(workspace_id="ws_test_status", original_prompt="original")
    ws.status = WorkspaceStatus.CONSENSUS_REACHED

    captured: dict = {"called": False}
    orig_wm = wde.workspace_mgr
    patches = _install_patches(captured)
    wde.workspace_mgr = FakeWorkspaceManager(ws)

    engine = wde.WorkspaceDebateEngine()
    _stub_engine(engine)

    # Capture the post-loop status of each dispute via the verify call args.
    try:
        engine.resume_workspace("ws_test_status", "follow-up Q?")
    finally:
        wde.workspace_mgr = orig_wm
        for mod, attr, original in patches:
            if mod is wde and attr == "workspace_mgr":
                continue
            setattr(mod, attr, original)

    assert captured["called"] is True
    # Only alpha_db and beta_orch were "resolved" by _debate_dispute; the
    # resume loop maps "resolved" -> "pending_verification". If a refactor
    # changes that mapping, validated_disputes would be empty here and the
    # assertion above would have already flagged it — this test makes the
    # contract explicit.
    assert [d["normalized_key"] for d in captured["validated_disputes"]] == [
        "alpha_db", "beta_orch"]


if __name__ == "__main__":
    test_workspace_followup_verification_no_name_error()
    test_pending_dispute_status_is_pending_verification()
    print("test_workspace_followup_verification: ALL PASSED")
