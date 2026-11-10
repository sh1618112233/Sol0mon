import json
import re

from cerebras_client import generate
from decision_renderer import (
    render_verified_decisions,
    render_verified_tradeoffs,
    render_human_required,
    validate_synthesis_consistency,
    contradiction_warning,
    render_architecture,
    render_implementation_plan,
    render_migration_path,
    render_risks,
)


def extract_json(text):
    if not text:
        return None

    text = text.strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    start_idx = text.find("{")

    if start_idx == -1:
        return None

    bracket_count = 0

    for i in range(start_idx, len(text)):
        if text[i] == "{":
            bracket_count += 1
        elif text[i] == "}":
            bracket_count -= 1

            if bracket_count == 0:
                try:
                    return json.loads(text[start_idx:i + 1])
                except Exception:
                    break

    return None


def build_synthesis_prompt(
    question,
    gpt_final,
    gemini_final,
    validated_disputes,
    consensus_score,
    verification_result=None,
    governance_context=None,
    deterministic_context=None,
):
    dispute_text = ""

    if validated_disputes:
        for idx, dispute in enumerate(validated_disputes, start=1):
            if isinstance(dispute, dict):
                dispute_text += (
                    f"\n{idx}. "
                    f"{dispute.get('dispute', '')}"
                )
            else:
                dispute_text += (
                    f"\n{idx}. {str(dispute)}"
                )
    else:
        dispute_text = "\nNone"

    # H9: Build verification context from verification_result
    verification_text = ""
    if verification_result:
        resolved = verification_result.get("resolved_claims", [])
        equivalent = verification_result.get("equivalent_claims", [])
        unresolved = verification_result.get("unresolved_claims", [])
        verification_score = verification_result.get("verification_score", 100)

        verification_text = f"""
VERIFICATION RESULTS

Verification Score: {verification_score}%

Resolved Disputes ({len(resolved)}):
"""
        for r in resolved:
            if isinstance(r, dict):
                v = r.get("verification", {}) if isinstance(r.get("verification"), dict) else {}
                # FIX 3: include winner + confidence + rationale (finding), not
                # just the dispute title. The finding states which option won and
                # which was rejected; without it the synthesis LLM demotes RESOLVED
                # disputes to "any combination valid" tradeoffs.
                finding = v.get("finding", "(no finding recorded)")
                confidence = v.get("confidence", "?")
                verification_text += (
                    f"  - [RESOLVED] {r.get('normalized_key', '')}: {r.get('dispute', '')}\n"
                    f"    Winner: {finding}\n"
                    f"    Confidence: {confidence}%\n"
                    f"    Rationale: {finding}\n"
                )

        verification_text += f"\nEquivalent/Artificial Disputes ({len(equivalent)}):\n"
        for e in equivalent:
            if isinstance(e, dict):
                verification_text += f"  - [EQUIVALENT] {e.get('normalized_key', '')}: {e.get('dispute', '')}\n"

        verification_text += f"\nUNRESOLVED Disputes ({len(unresolved)}) — must remain visible in output:\n"
        for u in unresolved:
            if isinstance(u, dict):
                v = u.get("verification", {}) if isinstance(u.get("verification"), dict) else {}
                verification_text += (
                    f"  - [UNRESOLVED] {u.get('normalized_key', '')}: {u.get('dispute', '')}\n"
                    f"    Finding: {v.get('finding', '(no finding)')}\n"
                )

    # Build governance context section
    governance_text = ""
    if governance_context:
        verified_decisions = governance_context.get("verified_decisions", [])
        verified_tradeoffs = governance_context.get("verified_tradeoffs", [])
        human_required = governance_context.get("human_required", [])

        if verified_decisions or verified_tradeoffs or human_required:
            governance_text = "\nGOVERNANCE OUTCOMES\n\n"

            if verified_decisions:
                governance_text += f"Verified Decisions ({len(verified_decisions)}):\n"
                for vd in verified_decisions:
                    if isinstance(vd, dict):
                        governance_text += (
                            f"  - [DECISION] {vd.get('dispute_text', '')}\n"
                            f"    Decision: {vd.get('decision', 'No decision recorded')}\n"
                            f"    Confidence: {vd.get('confidence', '?')}%\n"
                            f"    Reasoning: {vd.get('reasoning', '')[:300]}\n"
                        )

            if verified_tradeoffs:
                governance_text += f"\nVerified Tradeoffs ({len(verified_tradeoffs)}):\n"
                for vt in verified_tradeoffs:
                    if isinstance(vt, dict):
                        governance_text += (
                            f"  - [TRADEOFF] {vt.get('dispute_text', '')}\n"
                            f"    Option A: {vt.get('option_a', 'N/A')}\n"
                            f"    Option B: {vt.get('option_b', 'N/A')}\n"
                            f"    Recommendation: {vt.get('recommendation', 'No recommendation')}\n"
                            f"    Confidence: {vt.get('confidence', '?')}%\n"
                            f"    Reasoning: {vt.get('reasoning', '')[:300]}\n"
                        )

            if human_required:
                governance_text += f"\nHuman Decisions Required ({len(human_required)}):\n"
                for hr in human_required:
                    if isinstance(hr, dict):
                        governance_text += (
                            "Human Decisions Required\n"
                            f"Decision ID: {hr.get('decision_id', 'Not recorded')}\n"
                            f"Dispute: {hr.get('dispute_text') or hr.get('question', '')}\n"
                            f"Reason: {hr.get('reasoning', '')[:500]}\n"
                            "Options:\n"
                            f"  - {hr.get('option_a') or (hr.get('options') or ['N/A', 'N/A'])[0]}\n"
                            f"  - {hr.get('option_b') or (hr.get('options') or ['N/A', 'N/A'])[1]}\n"
                            f"Recommendation: {hr.get('recommendation') or 'Review the queued decision and choose the option to unblock implementation.'}\n"
                        )

    deterministic_section = ""
    if deterministic_context:
        deterministic_section = f"""
DETERMINISTIC SECTIONS (already generated verbatim from the verification result)

The Verified Decisions, Verified Tradeoffs, and Human Decisions Required
sections below have been generated deterministically by the verifier. They
are the source of truth. You MUST NOT regenerate, restate, paraphrase,
contradict, or override them. Treat every winner as settled fact and every
rejected option as off-limits.

{deterministic_context}
"""

    return f"""
You are a Research Synthesis Engine.

Your responsibility is to produce the best final answer.

You are NOT a summarizer.

Rules:

1. Use both positions.
2. Preserve all validated facts.
3. Prefer information supported by both models.
4. Do NOT artificially force agreement.
5. If unresolved disputes remain, explicitly describe them as open questions.
6. If one position is clearly superior, adopt it.
7. Produce a complete final answer.
8. Remove repetition.
9. Preserve important implementation details.
10. Do not attribute decisions to GPT or Gemini; describe the architecture itself.
11. Use the VERIFICATION RESULTS to guide your output: resolved items are settled decisions; unresolved items are open questions.
12. The consensus score is informational only. Do not weight it above the verifier's findings.
13. The GOVERNANCE OUTCOMES represent final determinations on deadlocked disputes — they are settled fact.
14. TRADEOFF outcomes mean BOTH options are valid — preserve both in your narrative. Do not force a choice between them.
15. HUMAN REQUIRED outcomes need human input — do not resolve them in the narrative.
16. The DETERMINISTIC SECTIONS are authoritative and pre-rendered. Never contradict a verified winner. Never recommend a rejected option.
17. Dispute-centric architecture must state: consensus is measured by dispute resolution; resolved disputes are removed from active debate; only unresolved disputes continue; consensus is not document similarity; tradeoffs are preserved; consensus is never forced.
18. Qwen Responsibilities must state: verifier only; not a debate participant; validates claims and evidence; performs governance review; does not contribute debate positions.
19. Workspace Capabilities must state: persistent workspaces, persistent follow-ups, debate history retention, resume workspace, re-debate only affected disputes, preserve prior verified decisions.
20. Autonomous Executor must state: read repository, create files, modify files, delete files when required, execute commands, run tests, verify outcomes, generate implementation reports, generate PR-style summaries.

QUESTION

{question}

CONSENSUS SCORE (informational only)

{consensus_score}%

{verification_text}

{governance_text}
{deterministic_section}

VALIDATED DISPUTES

{dispute_text}

GPT FINAL POSITION

==================================================
{gpt_final}
==================================================

GEMINI FINAL POSITION

==================================================
{gemini_final}
==================================================

Return ONLY the NARRATIVE portions of the final synthesized answer.
The Verified Decisions, Verified Tradeoffs, and Human Decisions Required
sections are already rendered for you verbatim above — DO NOT regenerate them.

Your answer MUST contain ONLY these narrative sections, in this order:
1. Architecture
2. Implementation Plan
3. Scaling Strategy
4. Operational Considerations
5. Migration Path
6. Risks

Every verified winner above is an immutable constraint on your narrative.
Never recommend, adopt, or present as valid any option the verifier rejected.
"""


def _render_architecture_sections(verification_result, governance_context):
    return "\n\n".join([
        render_architecture(verification_result, governance_context),
        render_implementation_plan(verification_result, governance_context),
        render_migration_path(verification_result, governance_context),
        render_risks(verification_result, governance_context),
    ])


def _render_deterministic_sections(verification_result, governance_context):
    """Render the three verifier-authoritative sections directly from the
    decision model: Verified Decisions (RESOLVED claims), Verified Tradeoffs
    (VERIFIED_TRADEOFF outcomes), Human Decisions Required (HUMAN_REQUIRED
    outcomes). No LLM. The verifier is the source of truth."""
    return "\n\n".join([
        render_verified_decisions(verification_result, governance_context),
        render_verified_tradeoffs(verification_result, governance_context),
        render_human_required(verification_result, governance_context),
    ])


def synthesize_answer(
    question,
    gpt_final,
    gemini_final,
    validated_disputes,
    consensus_score,
    verification_result=None,
    governance_context=None,
    disputes=None,
    narrative_fn=None,
):
    """Synthesize the final answer.

    The three decision sections (Verified Decisions, Verified Tradeoffs, Human
    Decisions Required) AND the four narrative sections (Architecture,
    Implementation Plan, Migration Path, Risks) are generated
    DETERMINISTICALLY from verification_result + governance_context by
    decision_renderer — NO LLM. The verifier is the source of truth and the
    final answer is fully reproducible from the decision model.

    `narrative_fn` is an injectable SEAM (mirrors debate_fn / executor_fn
    elsewhere) so OFFLINE TESTS can supply a fixed narrative and exercise the
    FIX 6 consistency guard. Production leaves it None and obtains the
    deterministic narrative directly from decision_renderer — no generate()
    call, no Cerebras tokens, byte-for-byte reproducible.

    FIX 6 — when a seam narrative is supplied and it contradicts the verifier
    (a rejected option recommended or a winner negated) a contradiction
    warning is appended. The deterministic sections stand regardless: the
    verifier wins.
    """
    # 1. Three verifier-authoritative sections, rendered straight from the
    #    verification_result / governance_context — no LLM.
    deterministic = _render_deterministic_sections(
        verification_result, governance_context
    )

    # 2. Build the narrative portion. The DEFAULT path renders the four
    #    narrative sections (Architecture, Implementation Plan, Migration
    #    Path, Risks) deterministically — no generate(), no model tokens, no
    #    narrative divergence from the verified decisions. `narrative_fn`
    #    remains an offline-test seam (the same shape as the debate_fn /
    #    executor_fn seams used elsewhere) so existing tests that supplied a
    #    fixed narrative keep working unchanged.
    if narrative_fn is not None:
        try:
            prompt = build_synthesis_prompt(
                question=question,
                gpt_final=gpt_final,
                gemini_final=gemini_final,
                validated_disputes=validated_disputes,
                consensus_score=consensus_score,
                verification_result=verification_result,
                governance_context=governance_context,
                deterministic_context=deterministic,
            )
            narrative = narrative_fn(prompt) or ""
        except Exception as e:
            print(f"[SYNTHESIS ERROR] narrative_fn failed: {e}")
            narrative = ""
        # ponytail: keep the historical longer-position fallback for the seam
        # path so a test seam returning empty does not blank the answer. The
        # production (deterministic) path never falls through here.
        if not narrative:
            narrative = (
                gpt_final
                if len(gpt_final) >= len(gemini_final)
                else gemini_final
            )
    else:
        # PRODUCTION: fully deterministic — no LLM, no credits, reproducible.
        narrative = _render_architecture_sections(
            verification_result, governance_context
        )
    narrative = (narrative or "").strip()

    # 3. Combine: three verifier-authoritative sections, then the narrative
    #    (either the four deterministic narrative sections or the supplied
    #    seam narrative).
    answer = deterministic.rstrip() + "\n\n" + narrative

    # 4. FIX 6: surface any narrative contradiction with the verifier. The
    #    deterministic narrative is consistent by construction; this guard
    #    is the safety net for a seam narrative or a future LLM path.
    contradictions = validate_synthesis_consistency(
        answer, verification_result, governance_context
    )
    if contradictions:
        warning = contradiction_warning(contradictions)
        if warning:
            print("\n" + "=" * 50)
            print(warning)
            print("=" * 50)
            answer = answer.rstrip() + "\n\n" + warning

    return answer


if __name__ == "__main__":

    sample_question = (
        "Design a planet scale AI consensus platform"
    )

    sample_gpt = """
Use Kubernetes, Kafka, PostgreSQL,
TEE-backed execution,
and multi-agent consensus.
"""

    sample_gemini = """
Use Kubernetes, Kafka, PostgreSQL,
TEE-backed execution,
and decentralized edge validation.
"""

    sample_disputes = [
        {
            "dispute":
            "Edge validation strategy differs."
        }
    ]

    answer = synthesize_answer(
        question=sample_question,
        gpt_final=sample_gpt,
        gemini_final=sample_gemini,
        validated_disputes=sample_disputes,
        consensus_score=95,
        narrative_fn=lambda _p: "# Architecture\nHonor the verified decisions.",
    )

    print("\n=== SYNTHESIZED ANSWER (seam narrative) ===\n")
    print(answer)

    # Deterministic default path — no LLM, no seam.
    det = synthesize_answer(
        question=sample_question,
        gpt_final=sample_gpt,
        gemini_final=sample_gemini,
        validated_disputes=sample_disputes,
        consensus_score=95,
    )
    assert "# Architecture" in det
    assert "# Implementation Plan" in det
    assert "# Migration Path" in det
    assert "# Risks" in det
    # Determinism: same input -> identical output.
    assert synthesize_answer(
        question=sample_question, gpt_final=sample_gpt,
        gemini_final=sample_gemini, validated_disputes=sample_disputes,
        consensus_score=95) == det
    print("\n=== SYNTHESIZED ANSWER (deterministic default) ===\n")
    print(det)
    print("\nsynthesis_engine smoke test PASSED")
