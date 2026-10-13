"""
test_build_prompt_generator.py - Phase 4 tests.

Covers both modules (spec asks for one test file):
- build_prompt_generator: extract_decisions / extract_constraints /
  extract_tradeoffs / generate_build_prompt / validate_spec_sections
- build_spec_engine: build_spec (with a STUBBED debate_fn so no browser or
  network is touched; the real pipeline_runner.execute is the production
  default but never invoked here), store/load_implementation_spec.

The Build Prompt Debate reuses the existing debate methodology (GPT/Gemini/
Critique/Revision/Consensus). We do NOT create a new debate engine; we inject
a stub that returns the same result-dict shape the real engine returns.

Run: python3 test_build_prompt_generator.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import build_workspace as bw
import build_prompt_generator as bpg
import build_spec_engine as bse


# ---------------------------------------------------------------------------
# Canned debate_result dicts in the REAL engine shape
# (pipeline_runner.execute / DebateEngine.run return keys)
# ---------------------------------------------------------------------------

def _source_result():
    return {
        "synthesized_answer": "Use PostgreSQL with a Redis cache and a FastAPI gateway.",
        "gpt_final": "g", "gemini_final": "m",
        "validated_disputes": [
            {"outcome_type": "VERIFIED_DECISION", "normalized_key": "db",
             "governed_outcome": {"outcome_type": "VERIFIED_DECISION",
                                  "dispute_key": "db",
                                  "decision": "PostgreSQL as primary store",
                                  "reasoning": "ACID + relational"}},
            {"outcome_type": "VERIFIED_TRADEOFF", "normalized_key": "cache",
             "governed_outcome": {"outcome_type": "VERIFIED_TRADEOFF",
                                  "dispute_key": "cache",
                                  "option_a": "Redis", "option_b": "Memcached",
                                  "recommendation": "Redis",
                                  "reasoning": "persistence + types"}},
            {"outcome_type": "HUMAN_REQUIRED", "normalized_key": "deploy",
             "governed_outcome": {"outcome_type": "HUMAN_REQUIRED",
                                  "dispute_key": "deploy",
                                  "option_a": "K8s", "option_b": "Nomad",
                                  "recommendation": None,
                                  "reasoning": "ops preference"}},
        ],
        # Engine also buckets these at the top level:
        "verified_decisions": [
            {"outcome_type": "VERIFIED_DECISION", "dispute_key": "db",
             "decision": "PostgreSQL as primary store",
             "reasoning": "ACID + relational"},
        ],
        "verified_tradeoffs": [
            {"outcome_type": "VERIFIED_TRADEOFF", "dispute_key": "cache",
             "option_a": "Redis", "option_b": "Memcached",
             "recommendation": "Redis", "reasoning": "persistence + types"},
        ],
        "human_required": [
            {"outcome_type": "HUMAN_REQUIRED", "dispute_key": "deploy",
             "option_a": "K8s", "option_b": "Nomad"},
        ],
        "final_consensus_score": 82,
    }


def _good_spec_text() -> str:
    return (
        "# Architecture\nLayered service.\n\n"
        "# Components\nAPI, worker, DB.\n\n"
        "# File Structure\nsrc/api src/worker\n\n"
        "# Dependencies\nfastapi sqlalchemy redis\n\n"
        "# Constraints\nMUST use PostgreSQL.\n\n"
        "# Testing Requirements\npytest 90% cov\n\n"
        "# Acceptance Criteria\ntests green; one-command deploy\n"
    )


# ---------------------------------------------------------------------------
# build_prompt_generator tests
# ---------------------------------------------------------------------------

def test_extract_decisions_from_buckets() -> None:
    r = _source_result()
    d = bpg.extract_decisions(r)
    assert len(d) == 1
    assert d[0]["key"] == "db"
    assert d[0]["decision"] == "PostgreSQL as primary store"
    assert d[0]["reasoning"] == "ACID + relational"


def test_extract_constraints_are_verified_decisions_as_rules() -> None:
    r = _source_result()
    c = bpg.extract_constraints(r)
    assert len(c) == 1
    assert c[0]["key"] == "db"
    assert c[0]["constraint"] == "PostgreSQL as primary store"


def test_extract_tradeoffs() -> None:
    r = _source_result()
    t = bpg.extract_tradeoffs(r)
    assert len(t) == 1
    assert t[0]["option_a"] == "Redis"
    assert t[0]["option_b"] == "Memcached"
    assert t[0]["recommendation"] == "Redis"


def test_extract_falls_back_to_validated_disputes_when_buckets_absent() -> None:
    # Strip the top-level buckets; extractor should still find outcomes via
    # the validated_disputes[i].governed_outcome path.
    r = _source_result()
    r.pop("verified_decisions")
    r.pop("verified_tradeoffs")
    assert len(bpg.extract_decisions(r)) == 1
    assert len(bpg.extract_tradeoffs(r)) == 1


def test_extract_empty_result_safe() -> None:
    assert bpg.extract_decisions({}) == []
    assert bpg.extract_constraints({}) == []
    assert bpg.extract_tradeoffs({}) == []


def test_generate_build_prompt_contains_all_sections_and_data() -> None:
    r = _source_result()
    prompt = bpg.generate_build_prompt(
        r["synthesized_answer"],
        bpg.extract_decisions(r),
        bpg.extract_constraints(r),
        bpg.extract_tradeoffs(r),
    )
    low = prompt.lower()
    for s in bpg.SPEC_SECTIONS:
        assert s in low, f"section missing from prompt: {s}"
    assert "PostgreSQL as primary store" in prompt
    assert "Redis" in prompt and "Memcached" in prompt
    assert "immutable" in low  # constraint framing
    # prompt must instruct exactly the 7 sections in order
    last = -1
    for s in bpg.SPEC_SECTIONS:
        last = prompt.lower().find(s, last + 1)
        assert last != -1, f"section out of order / missing: {s}"


def test_validate_spec_sections() -> None:
    good = _good_spec_text()
    assert bpg.validate_spec_sections(good) == []
    missing = bpg.validate_spec_sections("# Architecture\n# Components\nonly these")
    assert set(missing) == {
        "file structure", "dependencies", "constraints",
        "testing requirements", "acceptance criteria",
    }


# ---------------------------------------------------------------------------
# build_spec_engine tests
# ---------------------------------------------------------------------------

def test_build_spec_with_stubbed_debate_fn() -> None:
    """The Build Prompt Debate reuses existing methodology: we inject a stub
    that returns the real engine's result shape, so no browser/network runs."""
    captured = {}

    def fake_debate(prompt, memory_ids=None):
        captured["prompt"] = prompt
        return {
            "status": "completed",
            "synthesized_answer": _good_spec_text(),
            "gpt_final": "pos-g", "gemini_final": "pos-m",
            "validated_disputes": [],
            "verified_decisions": [], "verified_tradeoffs": [],
            "human_required": [],
            "final_consensus_score": 88,
        }

    r = _source_result()
    out = bse.build_spec(
        r["synthesized_answer"], debate_fn=fake_debate, source_result=r,
    )
    assert out["status"] == "completed", out
    assert out["missing_sections"] == [], out["missing_sections"]
    assert out["spec_text"] == _good_spec_text()
    assert len(out["decisions"]) == 1
    assert len(out["constraints"]) == 1
    assert len(out["tradeoffs"]) == 1
    # The build prompt was actually passed to the reused debate methodology:
    assert "PostgreSQL as primary store" in captured["prompt"]
    assert "Redis" in captured["prompt"]
    # The consensus result is surfaced for callers that want GPT/Gemini/etc.
    assert out["debate_result"]["final_consensus_score"] == 88


def test_build_spec_marks_incomplete_when_section_missing() -> None:
    def partial_debate(prompt, memory_ids=None):
        return {"synthesized_answer": "# Architecture\n# Components\n",
                "status": "completed"}
    out = bse.build_spec("src answer", debate_fn=partial_debate, source_result={})
    assert out["status"] == "incomplete"
    assert out["missing_sections"]  # non-empty
    assert out["spec_text"]


def test_build_spec_no_source_result_still_works() -> None:
    def fake(prompt, memory_ids=None):
        return {"synthesized_answer": _good_spec_text(),
                "status": "completed"}
    out = bse.build_spec("just an answer", debate_fn=fake)
    assert out["status"] == "completed"
    assert out["decisions"] == [] and out["constraints"] == []
    assert out["tradeoffs"] == []


def test_store_and_load_implementation_spec() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        bw.WORKSPACE_ROOT = Path(tmp)
        wid = bw.create_workspace()
        assert bse.store_implementation_spec(wid, _good_spec_text())
        on_disk = (Path(tmp) / f"workspace_{wid}" / "implementation_plan.md").read_text()
        assert on_disk == _good_spec_text()
        assert bse.load_implementation_spec(wid) == _good_spec_text()
        assert bse.load_implementation_spec("no-such-ws") == ""


def test_full_phase4_flow_inline() -> None:
    """Success-criteria check: synthesized answer -> implementation spec,
    stored inside the workspace, no manual prompt writing, no new engine."""
    with tempfile.TemporaryDirectory() as tmp:
        bw.WORKSPACE_ROOT = Path(tmp)
        wid = bw.create_workspace(source_question="Design Sol0mon")

        def fake_debate(prompt, memory_ids=None):
            return {"synthesized_answer": _good_spec_text(),
                    "status": "completed",
                    "final_consensus_score": 80,
                    "gpt_final": "p", "gemini_final": "m"}

        out = bse.build_spec(
            "Use PostgreSQL with a Redis cache.",
            debate_fn=fake_debate, source_result=_source_result(),
        )
        assert out["status"] == "completed"
        assert bse.store_implementation_spec(wid, out["spec_text"])
        spec = bse.load_implementation_spec(wid)
        for s in bpg.SPEC_SECTIONS:
            assert s in spec.lower()
        assert "PostgreSQL" in spec or "postgresql" in spec.lower()


def test_does_not_create_new_debate_engine() -> None:
    """build_spec_engine must reuse pipeline_runner.execute, not define its
    own debate loop. We assert the module has no debate/critique/revision
    logic of its own; it only imports the existing runner lazily."""
    import inspect
    src = inspect.getsource(bse)
    # No private debate loop keywords defined here:
    for kw in ("def _debate", "def _critique", "def _revision",
               "def _consensus", "class DebateEngine"):
        assert kw not in src, f"build_spec_engine defines its own debate logic: {kw}"
    # It does reference the existing runner:
    assert "pipeline_runner" in src


def test_does_not_touch_protected_logic() -> None:
    protected = {
        "debate_engine", "governance", "verification_engine",
        "consensus_engine", "chat_session", "gpt_worker", "gemini_worker",
    }
    # build_prompt_generator is pure; check it imports nothing protected.
    imported_bpg = set(getattr(m, "__name__", "") for m in bpg.__dict__.values()
                       if hasattr(m, "__name__"))
    assert not (protected & imported_bpg), \
        f"build_prompt_generator pulls in protected modules: {protected & imported_bpg}"
    # build_spec_engine references pipeline_runner (allowed - reuse) but must
    # not import any protected module at module load.
    imported_bse = set(getattr(m, "__name__", "") for m in bse.__dict__.values()
                       if hasattr(m, "__name__"))
    assert not (protected & imported_bse), \
        f"build_spec_engine pulls in protected modules: {protected & imported_bse}"


if __name__ == "__main__":
    test_extract_decisions_from_buckets()
    test_extract_constraints_are_verified_decisions_as_rules()
    test_extract_tradeoffs()
    test_extract_falls_back_to_validated_disputes_when_buckets_absent()
    test_extract_empty_result_safe()
    test_generate_build_prompt_contains_all_sections_and_data()
    test_validate_spec_sections()
    test_build_spec_with_stubbed_debate_fn()
    test_build_spec_marks_incomplete_when_section_missing()
    test_build_spec_no_source_result_still_works()
    test_store_and_load_implementation_spec()
    test_full_phase4_flow_inline()
    test_does_not_create_new_debate_engine()
    test_does_not_touch_protected_logic()
    print("test_build_prompt_generator: ALL PASSED")
