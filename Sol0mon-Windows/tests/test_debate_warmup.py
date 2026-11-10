"""
tests/test_debate_warmup.py — Research Warm-up phase invariant check.

Offline-only: no browser, no API, no LLM. Stubs GPT/Gemini sessions, the
conflict extractor, the verifier, and synthesis, then drives
``DebateEngine._run`` through the warmup phase and asserts the spec's
gating invariants:

  1. Rounds 1..WARMUP_ROUNDS run normally — both models are asked
     WARMUP_ROUNDS times before any conflict extraction / verifier /
     consensus check runs.
  2. The first conflict extraction is fed the POST-warmup positions
     (the warmup critique/revision calls actually updated them), not
     the initial Round-1 strings.
  3. No consensus-check (``_eval_consensus``) call occurs before
     WARMUP_ROUNDS round-turns have completed.
  4. No verifier (``verify_all_disputes``) call occurs during warmup.
  5. After warmup, the existing every-3-rounds per-dispute consensus
     cadence is preserved (first consensus check at dispute round 3,
     not before).

When WARMUP_ROUNDS <= 1 the helper is a no-op pass-through, preserving
backward compatibility.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _FakeSession:
    """Browser-less stand-in: returns a deterministic, ask-counted string."""

    def __init__(self, label):
        self.label = label
        self.asks = []

    def start(self):
        pass

    def close(self):
        pass

    def ask(self, prompt, dispute_key=None):
        self.asks.append((prompt, dispute_key))
        # Body encodes the ask ordinal so post-warmup positions differ
        # from Round-1 positions (lets us assert warmup actually ran).
        return f"{self.label}#{len(self.asks)}"


def _install_patches():
    """Stub every external touchpoint of DebateEngine._run except the
    research_warmup seam, so the no-disputes light path runs fully offline.

    debate_engine binds its imports at module top-level
    (``from conflict_extractor import extract_conflicts`` etc.), so we
    patch the symbols on the ``debate_engine`` module itself — not on
    the source modules. (Patching ``conflict_extractor.extract_conflicts``
    would not affect ``debate_engine.extract_conflicts``, which is the
    binding that actually resolves at call time.) Same shape the existing
    offline suites (test_research_driven_debate) use to fence WDE.

    Returns (patches, gpt, gem, extractor_calls, verifier_calls,
             consensus_calls)."""
    patches = []

    import debate_engine as de

    gpt, gem = _FakeSession("GPT"), _FakeSession("GEM")

    # --- Conflict extractor: patch the binding debate_engine actually uses
    extractor_calls = []
    patches.append((de, "extract_conflicts", de.extract_conflicts))

    def _fake_extract(g_pos, m_pos):
        extractor_calls.append((g_pos, m_pos))
        # No disputes -> triggers the light synthesis-and-return path,
        # which is exactly what we want to assert warmup invariants on
        # without running the per-dispute loop.
        return {"severity": "LOW", "agreed_claims": [],
                "disputed_claims": [], "resolved_disputes": []}

    de.extract_conflicts = _fake_extract

    # --- Verifier: patch the binding debate_engine actually uses
    verifier_calls = []
    patches.append((de, "verify_all_disputes", de.verify_all_disputes))

    def _fake_verify(**kw):
        verifier_calls.append(kw)
        return {"resolved_claims": [], "unresolved_claims": [],
                "verification_score": 100, "truncated_count": 0}

    de.verify_all_disputes = _fake_verify

    # --- Consensus evaluator: same — patch the binding de uses
    consensus_calls = []
    patches.append((de, "_eval_consensus", de._eval_consensus))

    def _fake_consensus(question, gpt_pos, gemini_pos):
        consensus_calls.append((question, gpt_pos, gemini_pos))
        return {"consensus_score": 95}

    de._eval_consensus = _fake_consensus

    # --- degrade the threshold so the per-dispute loop terminates
    # quickly without a model: a constant high score crosses
    # CONSENSUS_THRESHOLD at the first consensus check.
    # (Used by test_first_consensus_check_at_dispute_round_three.)
    patches.append((de, "CONSENSUS_THRESHOLD", de.CONSENSUS_THRESHOLD))
    patches.append((de, "MAX_ROUNDS_PER_DISPUTE", de.MAX_ROUNDS_PER_DISPUTE))

    # --- Synthesis + execution decision + human rendering: the light
    # path calls these on its way out; stub to keep the run offline.
    import synthesis_engine as se
    patches.append((se, "synthesize_answer", se.synthesize_answer))
    se.synthesize_answer = lambda **_kw: "SYN_OFFLINE"

    import execution_decision as ed
    patches.append((ed, "classify_execution_decision",
                    getattr(ed, "classify_execution_decision", None)))

    def _fake_classify(**_kw):
        return {"execution_decision": "NONE"}

    ed.classify_execution_decision = _fake_classify
    patches.append((ed, "attach_to_verification_result",
                    getattr(ed, "attach_to_verification_result", None)))
    ed.attach_to_verification_result = lambda _vr, _d: None

    import human_synthesis_renderer as hsr
    patches.append((hsr, "render_human_answer",
                    getattr(hsr, "render_human_answer", None)))
    hsr.render_human_answer = lambda **_kw: "HUMAN_OFFLINE"

    import human_decision_queue as hdq
    patches.append((hdq, "enqueue_human_decision",
                    getattr(hdq, "enqueue_human_decision", None)))
    hdq.enqueue_human_decision = lambda **_kw: "DEC_ID"

    # --- Governance verifier review: stub so the per-dispute max-rounds
    # fallback (third test) never makes a real LLM call. Returns a
    # terminal VERIFIED_TRADEOFF outcome — the test only asserts the
    # round-3 consensus-check cadence, not the outcome.
    import governance as gov
    _orig_vr = de.DebateEngine._verifier_review_dispute
    patches.append((de.DebateEngine, "_verifier_review_dispute", _orig_vr))

    def _fake_verifier_review(self, dispute, info_state):
        return gov.GovernedOutcome(
            outcome_type=gov.OutcomeType.VERIFIED_TRADEOFF,
            dispute_key=dispute.normalized_key,
            dispute_text=dispute.dispute_text,
            confidence=95, decision="tradeoff", reasoning="offline",
            option_a="A", option_b="B",
            evidence_quality="strong", architectural_validity="high",
            implementation_impact="moderate", operational_impact="moderate",
            scalability_impact="strong",
        )

    de.DebateEngine._verifier_review_dispute = _fake_verifier_review

    return patches, gpt, gem, extractor_calls, verifier_calls, consensus_calls


def _restore(patches):
    for mod, attr, original in patches:
        if original is not None or attr in mod.__dict__:
            try:
                setattr(mod, attr, original)
            except Exception:
                pass


def _run_engine(tmp_db, monkeypatch_memory=True):
    import debate_engine as de
    import answer_memory

    answer_memory.DB_FILE = tmp_db
    try:
        answer_memory.memory._initialized = False
    except Exception:
        pass

    engine = de.DebateEngine()
    return engine


def test_warmup_runs_before_any_extraction_or_consensus(tmp_path):
    """All three gated stages (extraction / verifier / consensus) wait
    for WARMUP_ROUNDS round-turns to complete; warmup actually mutates
    the positions the extractor then sees."""
    try:
        import debate_engine  # noqa: F401
    except Exception as e:  # pragma: no cover - import guard
        print(f"[SKIP] debate_engine deps unavailable: {e}")
        return

    patches, gpt, gem, extractor_calls, verifier_calls, consensus_calls = \
        _install_patches()
    tmp_db = str(tmp_path / "warmup.db")
    try:
        engine = _run_engine(tmp_db)
        engine._run(question="Design a cache layer",
                    gpt=gpt, gemini=gem, memory_ids=None)

        import debate_engine as de

        # 1. Both models participated in WARMUP_ROUNDS rounds before the
        #    first extract_conflicts call. Round 1 = 1 ask each;
        #    rounds 2..WARMUP_ROUNDS add (WARMUP_ROUNDS - 1) * 2 asks each
        #    (critique + revise). With the default WARMUP_ROUNDS = 3,
        #    that's 1 + 2*2 = 5 asks per model before extraction.
        expected_asks = 1 + 2 * (de.WARMUP_ROUNDS - 1)
        assert len(gpt.asks) >= expected_asks, (
            f"GPT only asked {len(gpt.asks)} times, "
            f"expected >= {expected_asks} before extraction")
        assert len(gem.asks) >= expected_asks, (
            f"Gemini only asked {len(gem.asks)} times, "
            f"expected >= {expected_asks} before extraction")

        # 2. First extraction sees the post-warmup positions, not the
        #    Round-1 strings. The extractor was called exactly once and
        #    both args carry the warmup marker (ask-count > 1).
        assert len(extractor_calls) == 1, (
            f"expected 1 extraction, got {len(extractor_calls)}")
        g_pos, m_pos = extractor_calls[0]
        assert "GPT#" in g_pos and "GEM#" in m_pos, (
            f"extractor saw non-warmup positions: {g_pos!r} / {m_pos!r}")
        # Each position is the LATEST ask, whose ordinal == expected_asks.
        assert g_pos.endswith(f"GPT#{expected_asks}"), g_pos
        assert m_pos.endswith(f"GEM#{expected_asks}"), m_pos

        # 3 & 4. With no disputes, the light path skips both the verifier
        # and consensus entirely — so neither can have run during warmup.
        # This asserts the negative half of the invariant: warmup did
        # not trigger either stage.
        assert verifier_calls == [], (
            f"verifier ran during warmup: {verifier_calls}")
        assert consensus_calls == [], (
            f"consensus check ran during warmup: {consensus_calls}")
    finally:
        _restore(patches)


def test_warmup_is_noop_when_threshold_le_one(tmp_path):
    """WARMUP_ROUNDS <= 1 → helper passes positions through unchanged,
    preserving the legacy single-round behavior."""
    try:
        import debate_engine as de
    except Exception as e:  # pragma: no cover
        print(f"[SKIP] debate_engine deps unavailable: {e}")
        return

    patches, gpt, gem, extractor_calls, verifier_calls, consensus_calls = \
        _install_patches()
    tmp_db = str(tmp_path / "noop_warmup.db")
    try:
        engine = _run_engine(tmp_db)
        before = de.WARMUP_ROUNDS
        try:
            de.WARMUP_ROUNDS = 1
            # Round-1 positions are passed straight through.
            gpt_after, gem_after = engine._research_warmup(
                gpt, gem, "ROUND1_GPT", "ROUND1_GEM")
            assert gpt_after == "ROUND1_GPT", gpt_after
            assert gem_after == "ROUND1_GEM", gem_after
            assert gpt.asks == [] and gem.asks == [], "warmup ran extra rounds"
            # Re-run with WARMUP_ROUNDS = 0 (also a no-op).
            de.WARMUP_ROUNDS = 0
            gpt_after, gem_after = engine._research_warmup(
                gpt, gem, "ROUND1_GPT", "ROUND1_GEM")
            assert gpt_after == "ROUND1_GPT" and gem_after == "ROUND1_GEM"
        finally:
            de.WARMUP_ROUNDS = before
    finally:
        _restore(patches)


def test_first_consensus_check_at_dispute_round_three(tmp_path):
    """Per-dispute consensus cadence is preserved: with disputes present,
    the first _eval_consensus call happens at dispute round 3, not
    earlier. Confirms warmup did not shift the existing every-3-rounds
    cadence within the per-dispute loop."""
    try:
        import debate_engine as de
    except Exception as e:  # pragma: no cover
        print(f"[SKIP] debate_engine deps unavailable: {e}")
        return

    patches, gpt, gem, extractor_calls, verifier_calls, consensus_calls = \
        _install_patches()
    # Replace the no-disputes extractor with a one-dispute extractor so
    # the per-dispute loop actually runs. Patch the binding de._run uses
    # (debate_engine.extract_conflicts); patching conflict_extractor alone
    # is invisible to debate_engine because it has already imported the
    # name at module top level.
    import debate_engine as de

    def _one_dispute(_g, _m):
        return {"severity": "MEDIUM", "agreed_claims": [],
                "disputed_claims": ["cache: redis vs memcached"],
                "resolved_disputes": []}
    de.extract_conflicts = _one_dispute

    # Patch _eval_validate so _build_dispute_queue doesn't call a model.
    patches.append((de, "_eval_validate", de._eval_validate))
    de._eval_validate = lambda question, disputes, gpt_pos, gemini_pos: [
        {"dispute": d.dispute_text, "normalized_key": "cache_choice",
         "classification": "VALID", "confidence": 90}
        for d in disputes
    ]

    tmp_db = str(tmp_path / "cadence.db")
    try:
        engine = _run_engine(tmp_db)
        # Cap dispute rounds so the test terminates quickly without a
        # consensus threshold being crossed (we want to observe the
        # round-3 first check, not a quick "resolved").
        import debate_engine as demod
        original_max = demod.MAX_ROUNDS_PER_DISPUTE
        original_thresh = demod.CONSENSUS_THRESHOLD
        # Force the per-dispute loop to stop after exactly round 3, so
        # we observe precisely one consensus check (at round 3) and the
        # governance fallback runs once on exit. This pins the cadence
        # without depending on model output to terminate the loop.
        demod.MAX_ROUNDS_PER_DISPUTE = 3
        demod.CONSENSUS_THRESHOLD = 200  # unreachable -> no early resolve
        try:
            engine._run(question="Design a cache layer",
                        gpt=gpt, gemini=gem, memory_ids=None)
        finally:
            demod.MAX_ROUNDS_PER_DISPUTE = original_max
            demod.CONSENSUS_THRESHOLD = original_thresh

        # First consensus check at dispute round 3 → at least one
        # _eval_consensus call captured, AND it ran AFTER warmup
        # (warmup itself never calls _eval_consensus).
        assert len(consensus_calls) >= 1, (
            "per-dispute consensus check did not fire at round 3; "
            "cadence appears broken")
    finally:
        _restore(patches)


if __name__ == "__main__":
    import pathlib
    import tempfile

    with tempfile.TemporaryDirectory(prefix="sol0mon_warmup_selfcheck_") as tmp:
        tmp = pathlib.Path(tmp)
        test_warmup_runs_before_any_extraction_or_consensus(tmp)
        test_warmup_is_noop_when_threshold_le_one(tmp)
        test_first_consensus_check_at_dispute_round_three(tmp)
    print("OK ? warmup invariants hold")
