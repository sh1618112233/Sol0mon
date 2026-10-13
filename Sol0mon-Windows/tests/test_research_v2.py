"""
tests/test_research_v2.py — Research V2: evidence available across the whole
reasoning process (dispute debate, retry debate, consistency audit) + resume
reuse + audit trail.

Verifies the 10 points required by the Research V2 spec:

  1.  evidence injected into dispute prompts
  2.  evidence injected into retry prompts
  3.  evidence injected into consistency audits
  4.  no duplicate research calls
  5.  resume reuses evidence (no re-research)
  6.  no model calls (pure helpers / planning / collection)
  7.  no browser calls (default research seam never invokes the browser when
      a fake research_fn is supplied; pure helpers never touch it)
  8.  relevant evidence only (database dispute -> database evidence, NOT
      execution / provider evidence)
  9.  workspace persistence of evidence + audit trail (incl. the new
      research_used / research_topics_used / research_reuse_count fields)
  10. deterministic output (pure transforms: same input -> same output)

All offline: no browser, no API, no model. The research_fn seam is injected as a
fake; GPT/Gemini/verify/synthesis/memory are stubbed. Pattern reused from
tests/test_research_driven_debate.py.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import research_planner as rp
import workspace_debate_engine as wde
from workspace_manager import Workspace, WorkspaceStatus, workspace_mgr
from debate_engine import Dispute, ArchitectureMemory, MAX_VERIFICATION_RETRIES


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSession:
    """Records every ask() prompt so a test can assert what entered a stage."""

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
    """In-memory workspace manager — returns the same ws each call."""

    def __init__(self, ws):
        self._ws = ws
        self.saves = 0

    def load_workspace(self, _id):
        return self._ws

    def save_workspace(self, _ws):
        self.saves += 1


class FakeMem:
    def save_debate(self, **_kw):
        return 4242


class FakeHQ:
    def get_pending_count(self, _id):
        return 0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _evidence_three():
    """Three orthogonal-topic evidence records with marker URLs/content so a
    test can assert which topic's evidence reached a given prompt."""
    return [
        {
            "topic": "database",
            "sources": [
                {"url": "http://m/database", "title": "DB",
                 "content": "unique database evidence body"}
            ],
            "summary": "database summary",
            "timestamp": "t-db",
        },
        {
            "topic": "execution",
            "sources": [
                {"url": "http://m/execution", "title": "EX",
                 "content": "unique execution evidence body"}
            ],
            "summary": "execution summary",
            "timestamp": "t-ex",
        },
        {
            "topic": "provider",
            "sources": [
                {"url": "http://m/provider", "title": "PR",
                 "content": "unique provider evidence body"}
            ],
            "summary": "provider summary",
            "timestamp": "t-pr",
        },
    ]


def _make_three_disputes():
    """Three disputes; each mentions exactly ONE of the three topics so the
    relevant-evidence selector picks exactly one per dispute."""
    return [
        Dispute(
            dispute_text="Choose the primary database: PostgreSQL vs MySQL",
            normalized_key="primary_database",
            gpt_source="g", gemini_source="m",
            priority="HIGH", priority_weight=5,
        ),
        Dispute(
            dispute_text="Pick an execution runtime: AWS Lambda vs ECS",
            normalized_key="execution_runtime",
            gpt_source="g", gemini_source="m",
            priority="MEDIUM", priority_weight=3,
        ),
        Dispute(
            dispute_text="Which cloud provider: AWS vs GCP for hosting",
            normalized_key="cloud_provider",
            gpt_source="g", gemini_source="m",
            priority="MEDIUM", priority_weight=3,
        ),
    ]


def _install_patches(captured: dict | None = None):
    """Patch every external touchpoint of resume_workspace() so it runs fully
    offline. Returns (patches, gpt, gem). Captures verify_all_disputes args if
    a ``captured`` dict is provided."""
    patches = []

    import conflict_extractor as ce
    patches.append((ce, "extract_conflicts", ce.extract_conflicts))
    ce.extract_conflicts = lambda _g, _m: {
        "severity": "MEDIUM",
        "agreed_claims": [],
        "disputed_claims": ["D-Database", "D-Execution", "D-Provider"],
        "resolved_disputes": [],
    }

    def _fake_verify_all_disputes(**kw):
        if captured is not None:
            captured["called"] = True
            captured["validated_disputes"] = list(kw.get("validated_disputes", []))
        return {
            "resolved_claims": [
                {"dispute": d.get("dispute", ""),
                 "normalized_key": d.get("normalized_key", ""),
                 "verification": {"status": "RESOLVED", "confidence": 95,
                                  "finding": "ok",
                                  "requires_external_evidence": False}}
                for d in kw.get("validated_disputes", [])
            ],
            "equivalent_claims": [],
            "unresolved_claims": [],
            "verification_score": 100,
            "truncated_count": 0,
        }

    patches.append((wde, "verify_all_disputes", wde.verify_all_disputes))
    wde.verify_all_disputes = _fake_verify_all_disputes

    import synthesis_engine as se
    patches.append((se, "synthesize_answer", se.synthesize_answer))
    se.synthesize_answer = lambda **_kw: "SYN_OFFLINE"

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


def _find_intro_for(asks, dispute_text):
    """Return the ask that contains the dispute_text (the constrained-dispute
    intro). None if not found."""
    for a in asks:
        if dispute_text in a:
            return a
    return None


# ---------------------------------------------------------------------------
# Shared driver: resume_workspace with 3 topic-orthogonal disputes
# ---------------------------------------------------------------------------


def _drive_resume(ws, follow_up="Compare options for our system"):
    """Run resume_workspace offline with 3 disputes, each pre-seeded to touch
    one topic. Returns (gpt, gem, engine)."""
    engine = wde.WorkspaceDebateEngine()
    engine._build_dispute_queue = lambda _raw, _g, _m: _make_three_disputes()
    engine._pre_debate_research_fn = lambda _topic: {"claim": _topic, "sources": []}

    def _debate(_q, dispute, _g, _m):
        dispute.consensus_score = 91
        return "resolved"

    engine._debate_dispute = _debate

    patches, gpt, gem = _install_patches()
    orig_wm = wde.workspace_mgr
    wde.workspace_mgr = FakeWM(ws)
    try:
        engine.resume_workspace(ws.workspace_id, follow_up)
    finally:
        _restore(patches, orig_wm)
    return gpt, gem, engine


# ---------------------------------------------------------------------------
# 1. evidence injected into dispute prompts
# ---------------------------------------------------------------------------


def test_1_evidence_injected_into_dispute_prompts() -> None:
    ws = Workspace(workspace_id="ws_v2_dispute", original_prompt="Design a system")
    ws.research_evidence = _evidence_three()
    ws.status = WorkspaceStatus.CONSENSUS_REACHED

    gpt, _gem, _engine = _drive_resume(ws)

    # The first ask is the initial-positions prompt (no research block for
    # this follow-up), so look at the dispute intros: the database dispute
    # intro must carry the database evidence block.
    db_intro = _find_intro_for(gpt.asks,
                               "Choose the primary database: PostgreSQL vs MySQL")
    assert db_intro is not None, "database dispute intro never asked"
    assert "<external_research>" in db_intro, "no research block in dispute intro"
    assert "http://m/database" in db_intro, "database evidence URL missing"
    assert "unique database evidence body" in db_intro, "database excerpt missing"
    # Gemini sees the same block too
    assert any("<external_research>" in a and "http://m/database" in a
               for a in _gem.asks), "research block missing from Gemini prompts"


# ---------------------------------------------------------------------------
# 2. evidence injected into retry prompts
# ---------------------------------------------------------------------------


def test_2_evidence_injected_into_retry_prompts() -> None:
    dispute = Dispute(
        dispute_text="Choose the primary database: PostgreSQL vs MySQL",
        normalized_key="primary_database",
        gpt_source="g", gemini_source="m",
        priority="HIGH", priority_weight=5,
    )
    dispute.status = "pending_verification"
    dispute.verification_retries = 0

    arch = ArchitectureMemory()
    rounds = {"n": 0}

    def _fake_verify(**_kw):
        rounds["n"] += 1
        if rounds["n"] == 1:
            return {
                "resolved_claims": [],
                "equivalent_claims": [],
                "unresolved_claims": [
                    {"normalized_key": "primary_database",
                     "verification": {"status": "UNRESOLVED", "confidence": 40,
                                      "finding": "unclear",
                                      "requires_external_evidence": True}}
                ],
                "verification_score": 40,
                "truncated_count": 0,
            }
        return {
            "resolved_claims": [
                {"normalized_key": "primary_database",
                 "verification": {"status": "RESOLVED", "confidence": 95,
                                  "finding": "ok",
                                  "requires_external_evidence": False}}
            ],
            "equivalent_claims": [],
            "unresolved_claims": [],
            "verification_score": 100,
            "truncated_count": 0,
        }

    def _debate(_q, d, _g, _m):
        d.consensus_score = 91
        return "resolved"

    engine = wde.WorkspaceDebateEngine()
    engine._debate_dispute = _debate

    patches = []
    patches.append((wde, "verify_all_disputes", wde.verify_all_disputes))
    wde.verify_all_disputes = _fake_verify
    try:
        gpt, gem = FakeSession("GPT"), FakeSession("GEM")
        engine._run_verification_ws(
            question="Q", disputes=[dispute],
            gpt_current="g", gemini_current="m",
            gpt=gpt, gemini=gem, arch_memory=arch,
            final_consensus_score=0, gov_result=None,
            prior_research_evidence=None,
            research_evidence=_evidence_three(),
        )
    finally:
        for mod, attr, original in patches:
            setattr(mod, attr, original)

    # A retry happened (round1 unresolved -> re-debate), and its prompt must
    # carry the relevant database evidence block.
    retry_intro = _find_intro_for(gpt.asks,
                                  "Choose the primary database: PostgreSQL vs MySQL")
    assert retry_intro is not None, "retry intro never asked"
    assert "<external_research>" in retry_intro, "no research block in retry intro"
    assert "http://m/database" in retry_intro, "database evidence missing from retry"
    assert rounds["n"] == 2, f"expected verify to run twice (round1+round2), got {rounds}"


# ---------------------------------------------------------------------------
# 3. evidence injected into consistency audits
# ---------------------------------------------------------------------------


def test_3_evidence_injected_into_consistency_audit() -> None:
    import debate_engine as de

    arch = ArchitectureMemory()
    d1 = Dispute("Use PostgreSQL as the primary database", "primary_database")
    d1.gpt_current = "PostgreSQL"
    arch.resolve(d1)
    d2 = Dispute("Use Redis for caching", "caching_layer")
    d2.gpt_current = "Redis"
    arch.resolve(d2)

    captured = {}

    def _fake_generate(prompt, module=None, **_kw):
        captured["prompt"] = prompt
        return "[]"  # no contradictions

    engine = wde.WorkspaceDebateEngine()
    _orig_gen = de.generate
    de.generate = _fake_generate
    try:
        new_disputes = engine._run_consistency_audit(
            arch, [d1, d2],
            research_context=rp.build_consistency_audit_context(_evidence_three()),
        )
    finally:
        de.generate = _orig_gen

    assert new_disputes == [], "audit should return no contradictions"
    assert "prompt" in captured, "consistency audit never called the model"
    prompt = captured["prompt"]
    assert "<external_research>" in prompt, "research block missing from audit prompt"
    assert "http://m/database" in prompt, "database evidence missing from audit"
    assert "unique database evidence body" in prompt, "evidence excerpt missing from audit"


# ---------------------------------------------------------------------------
# 4. no duplicate research calls
# ---------------------------------------------------------------------------


def test_4_no_duplicate_research_calls() -> None:
    calls: list = []

    def fake(topic):
        calls.append(topic)
        return {"claim": topic, "sources": [{"url": f"http://x/{topic}"}]}

    plan_a = rp.plan_research("Design a caching layer using Redis vs Memcached")
    plan_b = rp.plan_research("Design a storage layer using PostgreSQL and Redis")
    assert "redis" in [t.lower() for t in plan_a["research_topics"]]
    assert "redis" in [t.lower() for t in plan_b["research_topics"]]

    a = rp.collect_research(plan_a, fake, prior_evidence=None)
    n_after_a = len(calls)
    b = rp.collect_research(plan_b, fake, prior_evidence=a["evidence"])

    called_in_b = [c for c in calls[n_after_a:]]
    assert "redis" not in [c.lower() for c in called_in_b], \
        f"redis re-researched: {called_in_b}"
    assert "redis" in [t.lower() for t in b["reused_topics"]]


# ---------------------------------------------------------------------------
# 5. resume reuses evidence (no re-research)
# ---------------------------------------------------------------------------


def test_5_resume_reuses_evidence() -> None:
    ws = Workspace(workspace_id="ws_v2_reuse", original_prompt="Design a system")
    # Pre-populate topic-keyed evidence. The follow-up below plans research
    # only for 'postgresql', which is already cached -> the research seam must
    # NOT be invoked.
    ws.research_evidence = [{
        "topic": "postgresql",
        "sources": [{"url": "http://reuse/postgresql", "title": "PG",
                     "content": "reuse evidence for postgresql"}],
        "summary": "postgresql summary",
        "timestamp": "t-pg",
    }]
    ws.status = WorkspaceStatus.CONSENSUS_REACHED

    bomb = {"called": False}

    def _bomb_research(topic):
        bomb["called"] = True
        raise AssertionError(f"research seam should not be invoked, got: {topic}")

    engine = wde.WorkspaceDebateEngine()
    engine._build_dispute_queue = lambda _raw, _g, _m: [
        Dispute("PostgreSQL vs SQLite for the database", "db",
                "g", "m", priority="HIGH", priority_weight=5)
    ]
    engine._pre_debate_research_fn = _bomb_research
    engine._debate_dispute = lambda _q, d, _g, _m: (setattr(d, "consensus_score", 91) or "resolved")

    patches, gpt, gem = _install_patches()
    orig_wm = wde.workspace_mgr
    wde.workspace_mgr = FakeWM(ws)
    try:
        engine.resume_workspace(ws.workspace_id,
                                "Assume PostgreSQL cannot be used.")
    finally:
        _restore(patches, orig_wm)

    assert bomb["called"] is False, "research seam was invoked on resume (no reuse)"
    # Evidence is reused (not re-fetched) and still reaches the dispute prompt.
    intro = _find_intro_for(gpt.asks, "PostgreSQL vs SQLite for the database")
    assert intro is not None, "dispute intro never asked"
    assert "<external_research>" in intro, "reused evidence not injected on resume"
    assert "http://reuse/postgresql" in intro, "reused evidence URL missing"


# ---------------------------------------------------------------------------
# 6. no model calls (pure helpers / planning / collection)
# ---------------------------------------------------------------------------


def test_6_no_model_calls() -> None:
    import cerebras_client as cc
    hits = {"n": 0}

    def _bomb(**_kw):
        hits["n"] += 1
        raise AssertionError("model called during pure research setup")

    _orig = cc.generate
    cc.generate = _bomb
    try:
        # pure helpers
        ctx = rp.build_research_context(_evidence_three())
        assert ctx["source_counts"] == {"database": 1, "execution": 1, "provider": 1}
        sel = rp.select_relevant_evidence(_evidence_three(),
                                          "Choose the primary database", "db")
        assert [e["topic"] for e in sel] == ["database"]
        assert rp.format_research_for_dispute(_evidence_three(),
                                              "Choose the primary database", "db")
        assert rp.build_consistency_audit_context(_evidence_three())
        # planning + collection with a fake fn (no model, no default seam)
        plan = rp.plan_research("Design a queuing system using Kafka and Redis")
        assert plan["research_required"]
        collected = rp.collect_research(
            plan, lambda t: {"claim": t, "sources": []}, prior_evidence=None)
        rp.audit_record(plan, collected)
    finally:
        cc.generate = _orig
    assert hits["n"] == 0, "model was called during pure research setup"


# ---------------------------------------------------------------------------
# 7. no browser calls (default research seam never invoked when a fake fn is
#    supplied; pure helpers never touch it)
# ---------------------------------------------------------------------------


def test_7_no_browser_calls() -> None:
    browser = {"n": 0}

    def _bomb_browser(topic):
        browser["n"] += 1
        raise AssertionError(f"browser seam invoked for: {topic}")

    _orig_default = rp.default_research_fn
    rp.default_research_fn = _bomb_browser
    try:
        plan = rp.plan_research("Design a queuing system using Kafka and Redis")
        assert plan["research_required"]
        # explicit fake fn -> default (browser) seam never used
        out = rp.collect_research(plan, lambda t: {"claim": t, "sources": []},
                                  prior_evidence=None)
        assert browser["n"] == 0, "default browser seam was invoked"
        # pure helpers never touch the browser seam
        rp.build_research_context(out["evidence"])
        rp.select_relevant_evidence(out["evidence"], "kafka queue", "k")
        rp.format_research_for_dispute(out["evidence"], "kafka queue", "k")
        rp.build_consistency_audit_context(out["evidence"])
        assert browser["n"] == 0, "browser seam invoked by a pure helper"
    finally:
        rp.default_research_fn = _orig_default


# ---------------------------------------------------------------------------
# 8. relevant evidence only (database dispute -> database evidence only)
# ---------------------------------------------------------------------------


def test_8_relevant_evidence_only() -> None:
    ws = Workspace(workspace_id="ws_v2_relevant", original_prompt="Design a system")
    ws.research_evidence = _evidence_three()
    ws.status = WorkspaceStatus.CONSENSUS_REACHED

    gpt, _gem, _engine = _drive_resume(ws)

    cases = [
        ("Choose the primary database: PostgreSQL vs MySQL",
         "http://m/database", "database"),
        ("Pick an execution runtime: AWS Lambda vs ECS",
         "http://m/execution", "execution"),
        ("Which cloud provider: AWS vs GCP for hosting",
         "http://m/provider", "provider"),
    ]
    other_urls = {"http://m/database", "http://m/execution", "http://m/provider"}

    for dispute_text, want_url, _label in cases:
        intro = _find_intro_for(gpt.asks, dispute_text)
        assert intro is not None, f"intro missing for: {dispute_text}"
        assert want_url in intro, f"{want_url} missing from its dispute intro"
        # No OTHER topic's URL should be present in this dispute's intro.
        leaked = other_urls - {want_url}
        for bad in leaked:
            assert bad not in intro, \
                f"irrelevant evidence {bad} leaked into dispute: {dispute_text}"


# ---------------------------------------------------------------------------
# 9. workspace persistence of evidence + audit trail (incl. the new fields)
# ---------------------------------------------------------------------------


def test_9_workspace_persistence_audit_fields() -> None:
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
        block, _q = engine._run_pre_debate_research(ws, ws.original_prompt)
        assert "redis" in block.lower() and "postgresql" in block.lower(), block
        assert workspace_mgr.save_workspace(ws)

        ws2 = workspace_mgr.load_workspace(ws_id)
        assert ws2 is not None
        topics = {e["topic"].lower() for e in ws2.research_evidence}
        assert {"postgresql", "redis"} <= topics, ws2.research_evidence

        assert ws2.research_audit_trail, "audit trail not persisted"
        rec = ws2.research_audit_trail[-1]
        # existing fields still present
        assert rec["research_plan"]["research_required"] is True, rec
        assert set(rec["research_topics"]) == {"postgresql", "redis"}, rec
        assert rec["research_sources"], rec
        # V2 audit fields
        assert rec["research_used"] is True, rec
        assert set(t.lower() for t in rec["research_topics_used"]) == \
            {"postgresql", "redis"}, rec
        assert rec["research_reuse_count"] == 0, rec  # first run, no reuse
    finally:
        workspace_mgr.delete_workspace(ws_id)

    # Second run with prior evidence -> reuse_count > 0, research_used True.
    ws_id2 = workspace_mgr.create_workspace(
        "Design a system using PostgreSQL and Redis")
    try:
        ws = workspace_mgr.load_workspace(ws_id2)
        engine = wde.WorkspaceDebateEngine()
        engine._pre_debate_research_fn = lambda topic: {
            "claim": topic,
            "sources": [{"url": f"http://y/{topic}", "title": topic}]
        }
        engine._run_pre_debate_research(ws, ws.original_prompt)  # collect
        # Second pass: all topics cached -> reused, no new research.
        engine._pre_debate_research_fn = lambda topic: (
            AssertionError(f"research invoked for {topic}"))
        # Replace the seam with a bomb to prove reuse.
        bomb = {"n": 0}

        def _bomb(topic):
            bomb["n"] += 1
            raise AssertionError(f"research called on cached topic {topic}")
        engine._pre_debate_research_fn = _bomb
        engine._run_pre_debate_research(ws, ws.original_prompt)
        rec = ws.research_audit_trail[-1]
        assert rec["research_used"] is True, rec
        assert rec["research_reuse_count"] == 2, rec  # both topics reused
        assert bomb["n"] == 0, "cached topic re-researched"
    finally:
        workspace_mgr.delete_workspace(ws_id2)


# ---------------------------------------------------------------------------
# 10. deterministic output (pure transforms: same input -> same output)
# ---------------------------------------------------------------------------


def test_10_deterministic_output() -> None:
    ev = _evidence_three()
    assert rp.build_research_context(ev) == rp.build_research_context(ev)
    assert rp.select_relevant_evidence(ev, "Choose the database", "db") == \
        rp.select_relevant_evidence(ev, "Choose the database", "db")
    assert rp.format_research_for_dispute(ev, "Choose the database", "db") == \
        rp.format_research_for_dispute(ev, "Choose the database", "db")
    assert rp.build_consistency_audit_context(ev) == \
        rp.build_consistency_audit_context(ev)
    # Relevance is stable for any order-preserving repeat.
    ctx = rp.build_research_context(ev)
    assert ctx["evidence_by_topic"] == rp.build_research_context(ev)["evidence_by_topic"]
    assert ctx["source_counts"] == {"database": 1, "execution": 1, "provider": 1}
    assert ctx["citations"]["database"] == [
        {"url": "http://m/database", "title": "DB"}]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    test_1_evidence_injected_into_dispute_prompts()
    test_2_evidence_injected_into_retry_prompts()
    test_3_evidence_injected_into_consistency_audit()
    test_4_no_duplicate_research_calls()
    test_5_resume_reuses_evidence()
    test_6_no_model_calls()
    test_7_no_browser_calls()
    test_8_relevant_evidence_only()
    test_9_workspace_persistence_audit_fields()
    test_10_deterministic_output()
    print("test_research_v2: ALL PASSED (10 tests)")
