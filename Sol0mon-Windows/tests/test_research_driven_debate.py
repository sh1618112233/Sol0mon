"""
tests/test_research_driven_debate.py — Research-Driven Debate Planning V1.

Verifies the pre-debate research loop (research_planner + wiring into
WorkspaceDebateEngine) is fully offline-capable:

  1. research planner runs (deterministic, no LLM)
  2. evidence enters the debate (GPT/Gemini prompt)
  3. research is reused when prior evidence exists
  4. no duplicate research calls on overlap
  5. no model calls during planning/collection
  6. workspace persistence of evidence + audit trail
  7. follow-up reuse accross resume_workspace calls

All tests are offline: no browser, no API, no model. The research_fn seam is
injected as a fake; GPT/Gemini/verify/synthesis/memory are stubbed.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import research_planner as rp
import workspace_debate_engine as wde
from workspace_manager import Workspace, WorkspaceStatus, workspace_mgr


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSession:
    """Records every ask() prompt so a test can assert what entered the debate."""

    def __init__(self, label):
        self.label = label
        self.asks = []

    def start(self):
        pass

    def close(self):
        pass

    def ask(self, prompt, dispute_key=None):
        self.asks.append(prompt)
        return f"{self.label}:response"


class FakeWM:
    """In-memory workspace manager — returns the same ws object each call so
    research_evidence accumulates across multiple resume_workspace() calls."""

    def __init__(self, ws):
        self._ws = ws
        self.saves = 0

    def load_workspace(self, _id):
        return self._ws

    def save_workspace(self, _ws):
        self.saves += 1


class FakeMem:
    def save_debate(self, **_kw):
        return 9999


class FakeHQ:
    def get_pending_count(self, _id):
        return 0


def _install_light_patches():
    """Patch every external touchpoint of resume_workspace() EXCEPT the research
    seam, so the light (no-disputes) path runs fully offline. Returns
    (patches, gpt, gem) where gpt/gem record the prompts they were asked."""
    patches = []

    import conflict_extractor as ce
    patches.append((ce, "extract_conflicts", ce.extract_conflicts))
    ce.extract_conflicts = lambda _g, _m: {
        "severity": "LOW", "agreed_claims": [], "disputed_claims": [],
        "resolved_disputes": [],
    }

    import synthesis_engine as se
    patches.append((se, "synthesize_answer", se.synthesize_answer))
    se.synthesize_answer = lambda **_kw: "SYN_OFFLINE"

    patches.append((wde, "verify_all_disputes", wde.verify_all_disputes))
    wde.verify_all_disputes = lambda **_kw: {
        "resolved_claims": [], "unresolved_claims": [],
        "verification_score": 100, "truncated_count": 0,
    }

    patches.append((wde, "memory", wde.memory))
    wde.memory = FakeMem()

    patches.append((wde, "HumanDecisionQueue", wde.HumanDecisionQueue))
    wde.HumanDecisionQueue = FakeHQ

    import gpt_worker as gw
    import gemini_worker as gemw
    patches.append((gw, "GPTSession", gw.GPTSession))
    patches.append((gemw, "GeminiSession", gemw.GeminiSession))
    gpt, gem = FakeSession("GPT"), FakeSession("GEM")
    gw.GPTSession = lambda label="GPT": gpt
    gemw.GeminiSession = lambda label="GEM": gem

    return patches, gpt, gem


def _restore(patches, orig_wm):
    for mod, attr, original in patches:
        setattr(mod, attr, original)
    wde.workspace_mgr = orig_wm


# ---------------------------------------------------------------------------
# 1. research planner runs (deterministic, no LLM)
# ---------------------------------------------------------------------------


def test_1_planner_runs() -> None:
    plan = rp.plan_research(
        "Design a caching layer using Redis vs Memcached for high throughput")
    assert plan["research_required"] is True, plan
    assert plan["research_topics"], plan
    assert "redis" in [t.lower() for t in plan["research_topics"]], plan
    assert set(plan["research_priority"].keys()) == set(plan["research_topics"]), plan
    assert all(v in {"HIGH", "MEDIUM", "LOW"}
               for v in plan["research_priority"].values()), plan
    # trivially short question — no research
    assert rp.plan_research("hi")["research_required"] is False
    # non-technical question long enough — no research
    nontech = rp.plan_research("Tell me a short fun fact about anything at all today")
    assert nontech["research_required"] is False, nontech


# ---------------------------------------------------------------------------
# 2. evidence enters the debate prompt
# ---------------------------------------------------------------------------


def test_2_evidence_enters_debate() -> None:
    ws = Workspace(workspace_id="ws_ev", original_prompt="Design a cache")
    ws.status = WorkspaceStatus.CONSENSUS_REACHED
    engine = wde.WorkspaceDebateEngine()
    engine._pre_debate_research_fn = lambda topic: {
        "claim": topic,
        "sources": [{"url": f"http://marker.test/{topic}",
                     "title": f"MARKER_{topic.upper()}",
                     "content": f"unique evidence body for {topic}"}],
    }
    patches, gpt, gem = _install_light_patches()
    orig_wm = wde.workspace_mgr
    wde.workspace_mgr = FakeWM(ws)
    try:
        engine.resume_workspace(
            "ws_ev",
            "Design a caching layer using Redis vs Memcached for throughput")
    finally:
        _restore(patches, orig_wm)

    # The evidence block must have been prepended to the GPT prompt.
    assert gpt.asks, "GPT was never asked anything"
    prompt = gpt.asks[0]
    assert "<external_research>" in prompt, "research block missing from prompt"
    assert "http://marker.test/redis" in prompt, "evidence URL missing"
    assert "unique evidence body for redis" in prompt, "evidence excerpt missing"
    assert "Ground your reasoning" in prompt, "grounding instruction missing"
    # Gemini too
    assert any("<external_research>" in p for p in gem.asks), \
        "research block missing from Gemini prompt"


# ---------------------------------------------------------------------------
# 3. research is reused when prior evidence exists
# ---------------------------------------------------------------------------


def test_3_research_reused() -> None:
    plan = rp.plan_research("Compare Redis vs Memcached for caching")
    assert plan["research_required"]

    def fake(topic):
        return {"claim": topic, "sources": [{"url": f"http://x/{topic}", "title": topic}]}

    first = rp.collect_research(plan, fake, prior_evidence=None)
    assert set(first["researched_topics"]) == set(plan["research_topics"])
    assert first["reused_topics"] == []

    second = rp.collect_research(plan, fake, prior_evidence=first["evidence"])
    # topics are all already cached -> 0 researched, all reused
    assert second["researched_topics"] == []
    assert set(second["reused_topics"]) == set(plan["research_topics"]), second
    assert len(second["evidence"]) == len(plan["research_topics"])


# ---------------------------------------------------------------------------
# 4. no duplicate research calls on overlap
# ---------------------------------------------------------------------------


def test_4_no_duplicate_calls() -> None:
    calls: list = []

    def fake(topic):
        calls.append(topic)
        return {"claim": topic, "sources": [{"url": f"http://x/{topic}"}]}

    plan_a = rp.plan_research("Design a caching layer using Redis vs Memcached")
    plan_b = rp.plan_research("Design a storage layer using PostgreSQL and Redis")
    # both plans must include 'redis' so there is an overlap to test
    assert "redis" in [t.lower() for t in plan_a["research_topics"]]
    assert "redis" in [t.lower() for t in plan_b["research_topics"]]

    a = rp.collect_research(plan_a, fake, prior_evidence=None)
    n_after_a = len(calls)
    # redis already researched in A -> must NOT be called again in B
    b = rp.collect_research(plan_b, fake, prior_evidence=a["evidence"])

    called_in_b = [c for c in calls[n_after_a:]]
    assert "redis" not in called_in_b, f"redis re-researched: {called_in_b}"
    assert "redis" in b["reused_topics"], b
    # B's new topics got researched (no duplicate, exactly one call each)
    assert sorted(called_in_b) == sorted(b["researched_topics"])


# ---------------------------------------------------------------------------
# 5. no model calls during planning/collection
# ---------------------------------------------------------------------------


def test_5_no_model_calls() -> None:
    import cerebras_client as cc
    orig_gen = cc.generate
    bomb_calls = {"n": 0}

    def bomb(**_kw):
        bomb_calls["n"] += 1
        raise AssertionError("cerebras.generate was called during research setup")

    cc.generate = bomb
    try:
        plan = rp.plan_research("Design a queuing system using Kafka and Redis")
        assert plan["research_required"]
        collected = rp.collect_research(
            plan,
            research_fn=lambda topic: {"claim": topic, "sources": []},
            prior_evidence=None)
        block = rp.format_research_for_prompt(collected["evidence"])
        rp.audit_record(plan, collected)
        assert bomb_calls["n"] == 0, "model was called"
        # Also exercise the engine's pre-debate helper with a fake fn
        ws = Workspace(workspace_id="ws_nm", original_prompt="x")
        engine = wde.WorkspaceDebateEngine()
        engine._pre_debate_research_fn = lambda topic: {"claim": topic, "sources": []}
        engine._run_pre_debate_research(
            ws, "Design a queuing system using Kafka and Redis")
        assert bomb_calls["n"] == 0, "model was called during engine setup"
    finally:
        cc.generate = orig_gen


# ---------------------------------------------------------------------------
# 6. workspace persistence of evidence + audit trail
# ---------------------------------------------------------------------------


def test_6_workspace_persistence() -> None:
    ws_id = workspace_mgr.create_workspace(
        "Design a system using PostgreSQL and Redis")
    try:
        ws = workspace_mgr.load_workspace(ws_id)
        assert ws is not None
        engine = wde.WorkspaceDebateEngine()
        engine._pre_debate_research_fn = lambda topic: {
            "claim": topic,
            "sources": [{"url": f"http://x/{topic}", "title": topic,
                         "content": "ev"}],
        }
        block, q = engine._run_pre_debate_research(ws, ws.original_prompt)
        assert "redis" in block.lower() and "postgresql" in block.lower(), block
        assert workspace_mgr.save_workspace(ws)

        ws2 = workspace_mgr.load_workspace(ws_id)
        assert ws2 is not None
        topics = {e["topic"].lower() for e in ws2.research_evidence}
        assert {"postgresql", "redis"} <= topics, ws2.research_evidence
        assert ws2.research_audit_trail, "audit trail not persisted"
        rec = ws2.research_audit_trail[-1]
        assert rec["research_plan"]["research_required"] is True, rec
        assert set(rec["research_topics"]) == {"postgresql", "redis"}, rec
        assert rec["research_sources"], rec
        assert rec["research_reuse"]["researched_topics"], rec
    finally:
        workspace_mgr.delete_workspace(ws_id)


# ---------------------------------------------------------------------------
# 7. follow-up reuse across resume_workspace() calls
# ---------------------------------------------------------------------------


def test_7_follow_up_reuse() -> None:
    ws = Workspace(workspace_id="ws_fu", original_prompt="Design a system")
    ws.status = WorkspaceStatus.CONSENSUS_REACHED
    engine = wde.WorkspaceDebateEngine()

    calls: list = []

    def fake(topic):
        calls.append(topic)
        return {"claim": topic, "sources": [{"url": f"http://x/{topic}",
                "title": topic, "content": f"ev-{topic}"}]}
    engine._pre_debate_research_fn = fake

    patches, _gpt, _gem = _install_light_patches()
    orig_wm = wde.workspace_mgr
    wde.workspace_mgr = FakeWM(ws)
    try:
        fu1 = "Design a caching layer using Redis vs Memcached for throughput"
        fu2 = "Design a storage layer using PostgreSQL and Redis for durability"

        plan1 = rp.plan_research(fu1)
        plan2 = rp.plan_research(fu2)
        overlap = (set(t.lower() for t in plan1["research_topics"])
                   & set(t.lower() for t in plan2["research_topics"]))
        assert overlap, "test setup needs an overlap; got none"
        assert "redis" in overlap, overlap

        engine.resume_workspace("ws_fu", fu1)
        n_after_1 = len(calls)
        assert ws.research_audit_trail, "first run left no audit"
        rec1 = ws.research_audit_trail[-1]
        assert set(rec1["research_reuse"]["researched_topics"]) == \
            set(t.lower() for t in plan1["research_topics"]), rec1
        assert rec1["research_reuse"]["reused_topics"] == [], rec1

        ws.status = WorkspaceStatus.CONSENSUS_REACHED  # reset for next resume
        engine.resume_workspace("ws_fu", fu2)
        rec2 = ws.research_audit_trail[-1]
        calls_in_2 = calls[n_after_1:]
        # redis (overlap) must be reused, NOT re-called
        assert "redis" in rec2["research_reuse"]["reused_topics"], rec2
        assert "redis" not in [c.lower() for c in calls_in_2], \
            f"redis re-researched on follow-up: {calls_in_2}"
        # B's new topics each called exactly once
        assert sorted(c.lower() for c in calls_in_2) == \
            sorted(t.lower() for t in rec2["research_reuse"]["researched_topics"]), \
            (calls_in_2, rec2)

        # evidence accumulated across both runs (no topic lost)
        all_topics = {e["topic"].lower() for e in ws.research_evidence}
        assert (set(t.lower() for t in plan1["research_topics"])
                | set(t.lower() for t in plan2["research_topics"])) <= all_topics, \
            all_topics
        # two audit records, one per follow-up
        assert len(ws.research_audit_trail) == 2, len(ws.research_audit_trail)
    finally:
        _restore(patches, orig_wm)


if __name__ == "__main__":
    test_1_planner_runs()
    test_2_evidence_enters_debate()
    test_3_research_reused()
    test_4_no_duplicate_calls()
    test_5_no_model_calls()
    test_6_workspace_persistence()
    test_7_follow_up_reuse()
    print("test_research_driven_debate: ALL PASSED (7 tests)")
