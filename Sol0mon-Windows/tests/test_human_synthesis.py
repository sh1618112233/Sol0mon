"""
test_human_synthesis.py - Human-Readable Synthesis V3 (all phases).

Verifies the 10 contracts from the task spec:

  1.  Human answer generated from verified decisions.
  2.  No model calls (deterministic; no LLM/browser/network import).
  3.  Architecture rendered correctly (grouped verified components).
  4.  Implementation roadmap rendered correctly (Phase 1/2/3, verified steps).
  5.  Risk section rendered (verified risks, never invented).
  6.  Recommended action respects the execution mode (selected_mode).
  7.  Appendix preserves verified decisions (+ tradeoffs / human decisions /
      confidence).
  8.  Persistence survives save/load (human_answer round-trips; synthesized_answer
      and decision_summary unchanged; schema migration idempotent + backward
      compatible).
  9.  CLI default prints human_answer (re-rendered with selected_mode); --audit
      prints synthesized_answer; --json prints the raw machine artifact.
  10. Debate #145 fixture produces a complete engineering report.

All offline: no network, no browser, no model. Uses a throwaway answer_memory
DB (temp path swap) and a fake pipeline_runner.execute for the CLI tests so
no debate/engine ever runs.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import answer_memory
import human_synthesis_renderer as hsr
import sol0mon_cli
from human_synthesis_renderer import HUMAN_HEADERS, render_human_answer


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _install_fake_pipeline_runner(execute_fn):
    """Inject a stub `pipeline_runner` module so the CLI's lazy
    `from pipeline_runner import execute` binds to the fake WITHOUT importing
    the real engine chain (debate_engine -> cerebras_client -> dotenv). Keeps
    the CLI tests fully hermetic. Returns the previously-registered module
    (or None) so the caller can restore it."""
    import types
    saved = sys.modules.get("pipeline_runner")
    mod = types.ModuleType("pipeline_runner")
    mod.execute = execute_fn
    sys.modules["pipeline_runner"] = mod
    return saved


def _restore_pipeline_runner(saved):
    if saved is None:
        sys.modules.pop("pipeline_runner", None)
    else:
        sys.modules["pipeline_runner"] = saved


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #

def _with_tmp_db():
    """Point answer_memory at a fresh temp DB. Returns path for cleanup."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    if os.path.exists(tmp.name):
        os.unlink(tmp.name)
    answer_memory.DB_FILE = tmp.name
    answer_memory.memory._initialized = False
    return tmp.name


def debate55_verification_result():
    """3 resolved architectural disputes (the audit-trigger shape)."""
    resolved = [
        ("orchestration_runtime",
         "Orchestration Runtime: LangGraph (Python) vs. JS/Wasm Agent Framework",
         "Resolved through a hybrid split: Edge-Native Coordination Plane + Python Execution Layer.", 98),
        ("primary_database",
         "Primary Database: SQLite (Server-side) vs. IndexedDB (Client-side)",
         "Resolved in favor of server-side authoritative storage; rejects IndexedDB.", 95),
        ("execution_environment",
         "Execution: Server-side Docker Workers vs. Client-side WebContainers",
         "Resolved in favor of Server-side Docker; rejects Client-side WebContainers.", 99),
    ]
    return {
        "resolved_claims": [
            {"normalized_key": nk, "dispute": title,
             "verification": {"status": "RESOLVED", "confidence": conf,
                              "finding": finding,
                              "requires_external_evidence": False}}
            for nk, title, finding, conf in resolved
        ],
        "equivalent_claims": [],
        "unresolved_claims": [],
        "verification_score": 100,
        "truncated_count": 0,
    }


def debate145_verification_result():
    """Debate #145 fixture — the verified decisions the audit rendered as an
    internal decision artifact instead of a concise engineering report:
    infrastructure / data / exchange / strategy recommendations across the
    four domains the Architecture Classification Fix recognises."""
    resolved = [
        ("infrastructure",
         "Infrastructure: hosting provider vs managed cloud",
         "Resolved in favor of Oracle VPS; rejects managed cloud.", 96),
        ("data_provider",
         "Data Provider: CoinGecko vs manual",
         "Resolved in favor of CoinGecko; rejects manual collection.", 94),
        ("exchange",
         "Exchange: direct adapter vs REST aggregator",
         "Resolved in favor of Exchange Adapter; rejects REST aggregator.", 92),
        ("algorithm",
         "Algorithmic Strategy: SMA vs EMA",
         "Resolved in favor of SMA Strategy; rejects EMA.", 90),
    ]
    return {
        "resolved_claims": [
            {"normalized_key": nk, "dispute": title,
             "verification": {"status": "RESOLVED", "confidence": conf,
                              "finding": finding,
                              "requires_external_evidence": False}}
            for nk, title, finding, conf in resolved
        ],
        "equivalent_claims": [],
        "unresolved_claims": [],
        "verification_score": 100,
        "truncated_count": 0,
    }


def _exec(decision):
    return {
        "execution_decision": decision,
        "execution_reason": "reason",
        "execution_confidence": 90,
        "execution_decision_source": "source",
    }


# --------------------------------------------------------------------------- #
# 1. Human answer generated from verified decisions                           #
# --------------------------------------------------------------------------- #

def test_human_answer_contains_all_six_sections_from_verified_decisions():
    """Contract 1: the human answer is generated from verified decisions and
    carries the six canonical sections in order."""
    vr = debate55_verification_result()
    answer = render_human_answer(
        "Design the lowest-cost platform", vr,
        "ignored audit prose", _exec("ARCHITECTURE_ONLY"),
    )
    positions = [answer.find(h) for h in HUMAN_HEADERS]
    assert all(p >= 0 for p in positions), (
        f"missing section(s): {[h for h, p in zip(HUMAN_HEADERS, positions) if p < 0]}")
    assert positions == sorted(positions), "canonical section order violated"
    # The verified decisions drive the Executive Summary: every dispute title
    # appears and the winner (the finding's first clause) is surfaced.
    for title, winner in [
        ("Orchestration Runtime: LangGraph (Python) vs. JS/Wasm Agent Framework",
         "Resolved through a hybrid split: Edge-Native Coordination Plane + Python Execution Layer"),
        ("Primary Database: SQLite (Server-side) vs. IndexedDB (Client-side)",
         "Resolved in favor of server-side authoritative storage"),
        ("Execution: Server-side Docker Workers vs. Client-side WebContainers",
         "Resolved in favor of Server-side Docker"),
    ]:
        assert title in answer
        assert winner in answer, f"winner not surfaced: {winner}"


# --------------------------------------------------------------------------- #
# 2. No model calls                                                           #
# --------------------------------------------------------------------------- #

def test_renderer_module_exposes_no_model_symbols():
    """Contract 2a: the renderer module exposes no generate/llm/chat symbols."""
    for forbidden in ("generate", "llm", "complete", "chat", "prompt"):
        assert not hasattr(hsr, forbidden), (
            f"human_synthesis_renderer unexpectedly exposes '{forbidden}'")


def test_importing_renderer_does_not_load_model_clients():
    """Contract 2b: importing human_synthesis_renderer alone must not load
    any LLM/browser/network client. Checked in a fresh subprocess so prior
    tests importing cerebras_client never mask an import chain."""
    script = (
        "import sys\n"
        "import human_synthesis_renderer as h\n"
        "for m in ('cerebras_client','ollama_client','openrouter_client',"
        "'groq_client','gpt_worker','gemini_worker','provider_router',"
        "'requests','httpx','urllib3'):\n"
        "    assert m not in sys.modules, f'unexpected import: {m}'\n"
        "assert not hasattr(h, 'generate')\n"
        "print('ok')\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_render_is_deterministic():
    """Contract 2c: same inputs -> byte-identical output (no hidden call)."""
    vr = debate55_verification_result()
    a = render_human_answer("q", vr, "s", _exec("ARCHITECTURE_ONLY"))
    b = render_human_answer("q", vr, "s", _exec("ARCHITECTURE_ONLY"))
    assert a == b
    # The audit synthesized_answer prose is NOT depended on — changing it
    # must not change the human answer (decision-model-driven, not prose).
    c = render_human_answer("q", vr, "completely different prose", _exec("ARCHITECTURE_ONLY"))
    assert a == c


# --------------------------------------------------------------------------- #
# 3. Architecture rendered correctly                                          #
# --------------------------------------------------------------------------- #

def test_architecture_rendered_correctly_grouped_verified_components():
    """Contract 3: Verified Architecture groups ONLY verified components (no
    fabricated component, no empty invented group). Section header is
    '# Verified Architecture'."""
    vr = debate145_verification_result()
    answer = render_human_answer("q", vr, "audit", _exec("ARCHITECTURE_ONLY"))
    arch_block = answer.split("# Implementation Roadmap")[0].split(
        "# Verified Architecture")[1]
    # The four verified components surface grouped under their categories.
    for group, name in [
        ("Infrastructure:", "Oracle VPS"),
        ("Data:", "CoinGecko"),
        ("Integration:", "Exchange Adapter"),
        ("Application:", "SMA Strategy"),
    ]:
        assert group in arch_block, f"missing group {group}"
        assert f"- {name}" in arch_block, f"missing component {name}"
    # No fabricated component / group.
    assert "QuantumBroom" not in arch_block
    assert "Security:" not in arch_block  # no verified security decision
    assert "Operations:" not in arch_block  # no verified operations decision


def test_empty_verification_renders_no_components():
    """No verified decisions -> the architecture section says so and invents
    nothing."""
    answer = render_human_answer("q", None, None, None)
    arch = answer.split("# Implementation Roadmap")[0].split(
        "# Verified Architecture")[1]
    assert "No verified architectural decisions recorded." in arch


# --------------------------------------------------------------------------- #
# 4. Implementation roadmap rendered correctly                                #
# --------------------------------------------------------------------------- #

def test_implementation_roadmap_rendered_correctly():
    """Contract 4: the roadmap groups verified implementation steps into
    Phase 1/2/3. Every task line is a verified component's step (no invention)."""
    vr = debate145_verification_result()
    answer = render_human_answer("q", vr, "audit", _exec("FULL_BUILD"),
                                 "FULL_BUILD")
    roadmap = answer.split("# Risks")[0].split("# Implementation Roadmap")[1]
    # Three phases, in order.
    for p in ("## Phase 1", "## Phase 2", "## Phase 3"):
        assert p in roadmap, f"missing {p}"
    ppos = [roadmap.find(f"## Phase {n}") for n in (1, 2, 3)]
    assert ppos == sorted(ppos), "phases out of order"
    # Foundation (data + strategy) -> Phase 1; integration -> Phase 2;
    # infrastructure (deploy) -> Phase 3. Matches the acceptance layout.
    assert "Create market data layer: CoinGecko" in roadmap
    assert "Create strategy module: SMA Strategy" in roadmap
    assert "Create exchange integration layer: Exchange Adapter" in roadmap
    assert "Create deployment configuration: Oracle VPS" in roadmap
    # Phase 1 holds the data + strategy steps; Phase 3 holds deployment.
    phase1 = roadmap.split("## Phase 2")[0]
    phase3 = roadmap.split("## Phase 3")[1]
    assert "CoinGecko" in phase1 and "SMA Strategy" in phase1
    assert "Oracle VPS" in phase3
    # No invented task.
    assert "QuantumBroom" not in roadmap
    assert "Monitoring" not in roadmap  # not a verified step — must not invent
    assert "Risk controls" not in roadmap  # not a verified step


def test_empty_roadmap_says_so():
    """No verified components -> the roadmap invents nothing."""
    answer = render_human_answer("q", None, None, None)
    roadmap = answer.split("# Risks")[0].split("# Implementation Roadmap")[1]
    assert "No implementation steps; nothing verified." in roadmap


# --------------------------------------------------------------------------- #
# 5. Risk section rendered                                                    #
# --------------------------------------------------------------------------- #

def test_risk_section_rendered_only_verified():
    """Contract 5: the Risks section renders only verified risks — never
    invented. With no risky verified component, it says so explicitly."""
    vr = debate145_verification_result()  # generic components carry no risk
    answer = render_human_answer("q", vr, "audit", _exec("ARCHITECTURE_ONLY"))
    risks = answer.split("# Recommended Next Action")[0].split("# Risks")[1]
    assert "No risks identified from the verified decision model." in risks
    assert "QuantumBroom" not in risks


# --------------------------------------------------------------------------- #
# 6. Recommended action respects execution mode                               #
# --------------------------------------------------------------------------- #

def test_recommended_action_respects_selected_mode():
    """Contract 6: the Recommended Next Action tracks selected_mode:
    RESEARCH_ONLY -> Stop; RESEARCH_ARCHITECTURE -> Produce architecture
    package; FULL_BUILD -> Continue to Build Spec."""
    vr = debate145_verification_result()
    # Explicit selected_mode drives the action.
    research = render_human_answer("q", vr, "a", _exec("RESEARCH_ONLY"),
                                    "RESEARCH_ONLY")
    assert "Stop — this outcome is research-only" in research
    arch = render_human_answer("q", vr, "a", _exec("ARCHITECTURE_ONLY"),
                               "RESEARCH_ARCHITECTURE")
    assert "Produce architecture package" in arch
    full = render_human_answer("q", vr, "a", _exec("IMPLEMENTATION_REQUIRED"),
                               "FULL_BUILD")
    assert "Continue to Build Spec" in full


def test_recommended_action_falls_back_to_execution_decision():
    """When selected_mode is None (the debate_engine call site), the action
    falls back to the execution_decision -> mode mapping."""
    vr = debate145_verification_result()
    # execution_decision drives the fallback when selected_mode is absent.
    assert "Stop — this outcome is research-only" in render_human_answer(
        "q", vr, "a", _exec("RESEARCH_ONLY"))
    assert "Produce architecture package" in render_human_answer(
        "q", vr, "a", _exec("ARCHITECTURE_ONLY"))
    assert "Continue to Build Spec" in render_human_answer(
        "q", vr, "a", _exec("IMPLEMENTATION_REQUIRED"))


def test_recommended_action_safety_gates_override_selected_mode():
    """A pending human decision or unresolved disputes block any next action
    regardless of the selected_mode — the human-escalation contract is never
    bypassed by the presentation layer."""
    vr = debate145_verification_result()
    human = render_human_answer(
        "q", vr, "a", _exec("STOP_HUMAN_REQUIRED"), "FULL_BUILD")
    assert "human decision is required" in human
    assert "Continue to Build Spec" not in human
    vr_unres = {
        "resolved_claims": vr["resolved_claims"],
        "equivalent_claims": [],
        "unresolved_claims": [{"normalized_key": "open", "dispute": "open"}],
        "verification_score": 70, "truncated_count": 0,
    }
    unresolved = render_human_answer(
        "q", vr_unres, "a", _exec("STOP_UNRESOLVED_DISPUTES"), "FULL_BUILD")
    assert "unresolved dispute" in unresolved
    assert "Continue to Build Spec" not in unresolved


# --------------------------------------------------------------------------- #
# 7. Appendix preserves verified decisions                                    #
# --------------------------------------------------------------------------- #

def test_appendix_preserves_verified_decisions_tradeoffs_confidence():
    """Contract 7: the Appendix carries Verified Decisions / Tradeoffs /
    Human Decisions / Confidence sub-sections, and the verified decisions are
    preserved verbatim (audit visibility)."""
    vr = debate145_verification_result()
    answer = render_human_answer("q", vr, "audit", _exec("ARCHITECTURE_ONLY"))
    appendix = answer.split("# Appendix")[1]
    for sub in ("## Verified Decisions", "## Verified Tradeoffs",
                "## Human Decisions Required", "## Confidence"):
        assert sub in appendix, f"missing appendix sub-section: {sub}"
    # Every verified decision is preserved in the appendix.
    for c in vr["resolved_claims"]:
        assert c["dispute"] in appendix
        conf = c["verification"]["confidence"]
        assert f"{conf}%" in appendix
    # Confidence summary carries the verification score + average.
    assert "Verification score: 100%" in appendix
    assert "Average: 93%" in appendix  # (96+94+92+90)/4


# --------------------------------------------------------------------------- #
# 8. Persistence survives save/load                                           #
# --------------------------------------------------------------------------- #

def test_human_answer_stored_and_retrievable():
    """Contract 8: save_debate(human_answer=...) persists and get_debate /
    get_human_answer return it."""
    db_path = _with_tmp_db()
    try:
        vr = debate55_verification_result()
        human = render_human_answer("q", vr, "audit", _exec("ARCHITECTURE_ONLY"))
        debate_id = answer_memory.memory.save_debate(
            question="q", gpt_final="g", gemini_final="m",
            synthesized_answer="audit-synthesized", consensus_score=95,
            conflict_severity="LOW", conflict_points=[],
            validated_disputes=[], verification_result=vr,
            human_answer=human,
        )
        row = answer_memory.memory.get_debate(debate_id)
        assert row["human_answer"] == human
        assert answer_memory.memory.get_human_answer(debate_id) == human
        # A missing debate returns None, not a KeyError.
        assert answer_memory.memory.get_human_answer(9999999) is None
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_human_answer_defaults_to_empty_for_legacy_callers():
    """Contract 8/backward-compat: save_debate without human_answer stores ''
    — existing callers never break."""
    db_path = _with_tmp_db()
    try:
        debate_id = answer_memory.memory.save_debate(
            question="q", gpt_final="g", gemini_final="m",
            synthesized_answer="a", consensus_score=90,
            conflict_severity="LOW", conflict_points=[],
            validated_disputes=[], verification_result={},
        )
        row = answer_memory.memory.get_debate(debate_id)
        assert row["human_answer"] == ""
        assert answer_memory.memory.get_human_answer(debate_id) == ""
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_schema_migration_idempotent():
    """Contract 8: running the migration repeatedly does not duplicate the
    column and does not raise."""
    db_path = _with_tmp_db()
    try:
        answer_memory.memory._initialize_database()
        answer_memory.memory._initialize_database()
        answer_memory.memory._initialize_database()
        import sqlite3
        conn = sqlite3.connect(answer_memory.DB_FILE)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(debates)").fetchall()]
        conn.close()
        assert "human_answer" in cols
        assert cols.count("human_answer") == 1, "column added more than once"
        for legacy in ("synthesized_answer", "resolved_decisions",
                       "decision_summary", "verification_result",
                       "workspace_id", "parent_answer_id"):
            assert legacy in cols, f"legacy column dropped: {legacy}"
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_schema_migration_backward_compatible_with_existing_rows():
    """Contract 8: a row inserted before the human_answer column reads back
    as an empty human answer; a NEW row still works after re-migration."""
    db_path = _with_tmp_db()
    try:
        old_id = answer_memory.memory.save_debate(
            question="legacy", gpt_final="g", gemini_final="m",
            synthesized_answer="old", consensus_score=80,
            conflict_severity="LOW", conflict_points=[],
            validated_disputes=[], verification_result={},
        )
        assert answer_memory.memory.get_human_answer(old_id) == ""
        answer_memory.memory._initialize_database()  # no-op re-migration
        assert answer_memory.memory.get_human_answer(old_id) == ""
        assert answer_memory.memory.get_debate(old_id)["human_answer"] == ""
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_existing_synthesized_answer_unchanged_when_human_answer_stored():
    """Contract 8: storing a human_answer does NOT alter synthesized_answer."""
    db_path = _with_tmp_db()
    try:
        vr = debate55_verification_result()
        synth = "AUDIT-ARTIFACT-DO-NOT-CHANGE"
        human = render_human_answer("q", vr, synth, _exec("ARCHITECTURE_ONLY"))
        debate_id = answer_memory.memory.save_debate(
            question="q", gpt_final="g", gemini_final="m",
            synthesized_answer=synth, consensus_score=95,
            conflict_severity="LOW", conflict_points=[],
            validated_disputes=[], verification_result=vr,
            human_answer=human,
        )
        row = answer_memory.memory.get_debate(debate_id)
        assert row["synthesized_answer"] == synth, "synthesized_answer was mutated"
        assert row["human_answer"] == human
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_existing_decision_summary_unchanged():
    """Contract 8: decision_summary is still derived from synthesized_answer
    (first 500 chars) exactly as before; adding human_answer changes nothing."""
    db_path = _with_tmp_db()
    try:
        long_synth = "S" * 600  # > 500 -> truncated to 500
        debate_id = answer_memory.memory.save_debate(
            question="q", gpt_final="g", gemini_final="m",
            synthesized_answer=long_synth, consensus_score=90,
            conflict_severity="LOW", conflict_points=[],
            validated_disputes=[], verification_result={},
            human_answer="human",
        )
        row = answer_memory.memory.get_debate(debate_id)
        assert row["decision_summary"] == long_synth[:500].strip()
        short_id = answer_memory.memory.save_debate(
            question="q2", gpt_final="g", gemini_final="m",
            synthesized_answer="short", consensus_score=90,
            conflict_severity="LOW", conflict_points=[],
            validated_disputes=[], verification_result={},
            human_answer="human2",
        )
        assert answer_memory.memory.get_debate(short_id)["decision_summary"] == "short"
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# --------------------------------------------------------------------------- #
# 9. CLI display                                                              #
# --------------------------------------------------------------------------- #

def _fake_execute(prompt):
    """A fake pipeline_runner.execute that returns a completed debate result
    carrying a verification_result + synthesized_answer (NO prebaked
    human_answer — the CLI re-renders it from the decision model so the
    Recommended Next Action reflects the user's selected mode)."""
    return {
        "status": "completed",
        "final_consensus_score": 95,
        "execution_decision": "ARCHITECTURE_ONLY",
        "execution_decision_source": "architecture_decisions",
        "execution_reason": "verified architecture decisions present",
        "synthesized_answer": "# Verified Decisions\nthe audit artifact\n# Architecture\n...",
        "verification_result": debate145_verification_result(),
    }


def _extract_json_payload(out: str) -> dict:
    """The CLI prints an optional preamble then ONE json.dumps(...) object.
    Locate the first '{' that begins a JSON object and parse to the matching
    '}'."""
    start = out.find("{")
    assert start != -1, f"no JSON object in CLI output:\n{out}"
    depth = 0
    for i in range(start, len(out)):
        if out[i] == "{":
            depth += 1
        elif out[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(out[start:i + 1])
    raise AssertionError(f"unbalanced JSON in CLI output:\n{out}")


def _run_cli(argv) -> tuple[int, dict | str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = sol0mon_cli.main(argv)
    out = buf.getvalue()
    # The CLI prints a preamble (Execution Decision / Execution Mode) before
    # the JSON payload; locate the first '{' and parse the JSON object.
    if "{" in out:
        return rc, _extract_json_payload(out)
    return rc, out


def test_cli_default_displays_human_answer():
    """Contract 9: default CLI shows a re-rendered human_answer (carrying the
    verified decisions and the Recommended Next Action), not synthesized_answer."""
    saved = _install_fake_pipeline_runner(_fake_execute)
    try:
        rc, payload = _run_cli(["debate", "design the platform"])
    finally:
        _restore_pipeline_runner(saved)
    assert rc == 0
    assert "human_answer" in payload
    assert "synthesized_answer" not in payload
    assert "# Executive Summary" in payload["human_answer"]
    assert "# Recommended Next Action" in payload["human_answer"]
    # The re-render used the fake's verification_result -> the verified
    # decisions surface in the displayed human_answer.
    assert "Oracle VPS" in payload["human_answer"]


def test_cli_audit_displays_synthesized_answer():
    """Contract 9: --audit shows synthesized_answer, not human_answer."""
    saved = _install_fake_pipeline_runner(_fake_execute)
    try:
        rc, payload = _run_cli(["debate", "design the platform", "--audit"])
    finally:
        _restore_pipeline_runner(saved)
    assert rc == 0
    assert "synthesized_answer" in payload
    assert "human_answer" not in payload
    assert "# Verified Decisions" in payload["synthesized_answer"]


def test_cli_json_displays_machine_artifact():
    """Contract 9: --json shows the raw machine artifact (the full debate
    result) exactly as existing automation consumed it — both
    synthesized_answer and verification_result present, no human_answer key."""
    saved = _install_fake_pipeline_runner(_fake_execute)
    try:
        rc, payload = _run_cli(["debate", "design the platform", "--json"])
    finally:
        _restore_pipeline_runner(saved)
    assert rc == 0
    assert "synthesized_answer" in payload
    assert "verification_result" in payload
    assert "execution_decision" in payload
    assert "human_answer" not in payload  # --json is the raw machine artifact


def test_cli_falls_back_to_synthesized_when_no_verification_result():
    """Default display falls back to the stored human_answer (or
    synthesized_answer) for legacy/failed results that carry no
    verification_result to re-render from (graceful degradation)."""
    def _fake_legacy(prompt):
        return {
            "status": "completed", "final_consensus_score": 95,
            "execution_decision": "ARCHITECTURE_ONLY",
            "execution_decision_source": "arch",
            "synthesized_answer": "# Verified Decisions\nfallback audit",
            "human_answer": "# Executive Summary\nstored legacy human answer",
        }
    saved = _install_fake_pipeline_runner(_fake_legacy)
    try:
        rc, payload = _run_cli(["debate", "design the platform"])
    finally:
        _restore_pipeline_runner(saved)
    assert rc == 0
    assert "human_answer" in payload
    assert "synthesized_answer" not in payload
    assert "stored legacy human answer" in payload["human_answer"]


# --------------------------------------------------------------------------- #
# No hallucinated decisions                                                   #
# --------------------------------------------------------------------------- #

def test_no_hallucinated_decisions():
    """Every decision in the human answer comes from the fixture. A fabricated
    option never appears; the Executive Summary's resolved-decision bullet
    count equals the number of verified disputes (no invention, no omission)."""
    vr = debate55_verification_result()
    answer = render_human_answer(
        "Design the platform", vr, "audit prose", _exec("ARCHITECTURE_ONLY"),
    )
    summary_block = answer.split("# Verified Architecture")[0]
    bullets = [ln for ln in summary_block.splitlines() if ln.startswith("- ")]
    assert len(bullets) == len(vr["resolved_claims"]), (
        f"expected {len(vr['resolved_claims'])} decision bullets, got {len(bullets)}")
    for c in vr["resolved_claims"]:
        assert c["dispute"] in answer
        assert hsr._first_sentence(c["verification"]["finding"]) in answer
    assert "QuantumBroom" not in answer
    assert "invented-option-xyz" not in answer
    # A rejected option is named (in the dispute title / finding) but never
    # presented as the thing selected after a winner marker.
    for rejected in ("indexeddb", "webcontainers"):
        assert rejected.lower() in answer.lower()  # named, faithful
        for marker in ("in favor of ", "selecting ", "adopting "):
            i = answer.lower().find(marker)
            while i != -1:
                window = answer.lower()[i: i + 60]
                assert rejected not in window, \
                    f"rejected option {rejected} selected after '{marker}'"
                i = answer.lower().find(marker, i + 1)


def test_empty_verification_result_does_not_invent():
    """No verified decisions -> the renderer says so explicitly in every
    relevant section and invents nothing, while still emitting all six."""
    answer = render_human_answer("q", None, None, None)
    for h in HUMAN_HEADERS:
        assert h in answer
    assert "No verified decisions were recorded." in answer
    assert "No verified architectural decisions recorded." in answer
    assert "No implementation steps; nothing verified." in answer
    assert "QuantumBroom" not in answer


# --------------------------------------------------------------------------- #
# 10. Debate #145 fixture produces a complete engineering report              #
# --------------------------------------------------------------------------- #

def test_debate_145_fixture_produces_complete_engineering_report():
    """Contract 10: the Debate #145 fixture produces all six sections with
    every verified decision reflected — the concise engineering report the
    user should have received instead of the internal decision artifact."""
    vr = debate145_verification_result()
    answer = render_human_answer(
        "Design the trading system architecture", vr,
        "audit artifact (internal decision artifact)",
        _exec("IMPLEMENTATION_REQUIRED"), "FULL_BUILD",
    )
    positions = [answer.find(h) for h in HUMAN_HEADERS]
    assert all(p >= 0 for p in positions), (
        f"missing header: {[h for h, p in zip(HUMAN_HEADERS, positions) if p < 0]}")
    assert positions == sorted(positions), "canonical order violated"
    for h in HUMAN_HEADERS:
        assert answer.count(h) == 1, f"header duplicated: {h}"
    # All four domains surface in the Executive Summary.
    summary = answer.split("# Verified Architecture")[0]
    for c in vr["resolved_claims"]:
        assert c["dispute"] in summary, \
            f"verified decision dropped: {c['dispute']}"
    # The audit artifact prose is never echoed as a decision.
    assert "audit artifact (internal decision artifact)" not in answer
    # The acceptance-criteria components appear in Verified Architecture.
    for name in ("Oracle VPS", "CoinGecko", "Exchange Adapter", "SMA Strategy"):
        assert f"- {name}" in answer
    # The Recommended Next Action reflects the selected mode (Full Build).
    assert "Continue to Build Spec" in answer


# --------------------------------------------------------------------------- #
# Pipeline integration sanity — debate_engine builds a human_answer           #
# --------------------------------------------------------------------------- #

def test_debate_engine_result_dict_carries_human_answer():
    """The primary synthesis path (debate_engine._synthesize_and_return)
    attaches human_answer to the returned result dict AND persists it.
    Verified without a live debate by invoking the method directly with
    fixture inputs. The persisted human_answer uses the selected_mode=None
    fallback (debate_engine cannot be modified to pass selected_mode), so the
    Recommended Next Action reflects the execution_decision -> mode mapping.

    Imports the engine lazily: its top-level import chain
    (debate_engine -> cerebras_client -> dotenv) is only available under the
    project venv, so the test is skipped on a bare interpreter."""
    try:
        from debate_engine import DebateEngine, ArchitectureMemory, GovernanceResult
    except Exception as e:  # pragma: no cover - import guard for bare interpreters
        print(f"[SKIP] debate_engine integration (deps unavailable): {e}")
        return

    db_path = _with_tmp_db()
    try:
        engine = DebateEngine.__new__(DebateEngine)
        vr = debate145_verification_result()
        arch_memory = ArchitectureMemory()
        gov = GovernanceResult()

        result = engine._synthesize_and_return(
            question="Design the trading system architecture",
            gpt_current="gpt", gemini_current="gem",
            disputes=[], verification_result=vr, score=99,
            status="completed", arch_memory=arch_memory, gov_result=gov,
        )
        # Both artifacts on the result dict.
        assert "synthesized_answer" in result
        assert "human_answer" in result
        assert result["synthesized_answer"] != result["human_answer"]
        # human_answer carries all six sections.
        for h in HUMAN_HEADERS:
            assert h in result["human_answer"]
        # synthesized_answer (audit) still carries its own audit headers.
        assert "# Verified Decisions" in result["synthesized_answer"]

        # Persistence: the stored row carries both.
        rows = answer_memory.memory.get_recent_debates(limit=1)
        assert rows, "debate was not persisted"
        stored = answer_memory.memory.get_debate(rows[0]["id"])
        assert stored["synthesized_answer"] == result["synthesized_answer"]
        assert stored["human_answer"] == result["human_answer"]
        assert answer_memory.memory.get_human_answer(rows[0]["id"]) == result["human_answer"]
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


if __name__ == "__main__":
    test_human_answer_contains_all_six_sections_from_verified_decisions()
    test_renderer_module_exposes_no_model_symbols()
    test_importing_renderer_does_not_load_model_clients()
    test_render_is_deterministic()
    test_architecture_rendered_correctly_grouped_verified_components()
    test_empty_verification_renders_no_components()
    test_implementation_roadmap_rendered_correctly()
    test_empty_roadmap_says_so()
    test_risk_section_rendered_only_verified()
    test_recommended_action_respects_selected_mode()
    test_recommended_action_falls_back_to_execution_decision()
    test_recommended_action_safety_gates_override_selected_mode()
    test_appendix_preserves_verified_decisions_tradeoffs_confidence()
    test_human_answer_stored_and_retrievable()
    test_human_answer_defaults_to_empty_for_legacy_callers()
    test_schema_migration_idempotent()
    test_schema_migration_backward_compatible_with_existing_rows()
    test_existing_synthesized_answer_unchanged_when_human_answer_stored()
    test_existing_decision_summary_unchanged()
    test_cli_default_displays_human_answer()
    test_cli_audit_displays_synthesized_answer()
    test_cli_json_displays_machine_artifact()
    test_cli_falls_back_to_synthesized_when_no_verification_result()
    test_no_hallucinated_decisions()
    test_empty_verification_result_does_not_invent()
    test_debate_145_fixture_produces_complete_engineering_report()
    test_debate_engine_result_dict_carries_human_answer()
    print("test_human_synthesis: ALL PASSED")
