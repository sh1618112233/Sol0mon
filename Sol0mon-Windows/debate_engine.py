"""
Project Sol0mon — Debate Engine (single-dispute consensus resolution).

Each user question is debated by GPT and Gemini. Architectural disagreements are
extracted once, validated, prioritized by domain, and debated one at a time.
Resolved decisions accumulate in ArchitectureMemory and are injected as
immutable constraints into subsequent disputes. Verification confirms each
resolution before it is considered final. Deadlocks are escalated to governance.
"""

import hashlib
import json
import re
import time
from dataclasses import dataclass, field

from cerebras_client import generate
from conflict_extractor import extract_conflicts
from verification_engine import (
    verify_all_disputes,
    extract_pre_emitted_outcome,
)
from answer_memory import memory
from governance import (
    OutcomeType,
    GovernedOutcome,
    InformationState,
    GovernanceResult,
    verifier_review,
)

# Telemetry & Runtime Observability V1 — PASSIVE observation only. The
# recorder is disabled by default and every emit/observe/start_stage/
# end_stage call below is a strict no-op when unconfigured AND swallows its
# own errors, so debate/verification/consensus/governance decision logic is
# byte-for-byte unchanged. No telemetry value is ever read back into control
# flow — observation calls sit alongside the existing print() calls.
from telemetry import recorder as _telemetry

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONSENSUS_THRESHOLD = 92
MAX_ROUNDS_PER_DISPUTE = 15
MAX_MEMORY_CHARS = 8000
STAGNATION_CHECKS = 3
STAGNATION_MAX_WARNINGS = 2
MAX_VERIFICATION_RETRIES = 1
MAX_CONSTRAINTS = 10
PRIORITY_WEIGHTS = {"HIGH": 5, "MEDIUM": 3, "LOW": 1}

# Research Warm-up phase — the first conflict extraction, verification pass,
# and consensus check must NOT execute until both models have completed this
# many rounds on the full question. Round 1 is the initial-positions call;
# rounds 2..WARMUP_ROUNDS are run by _research_warmup() using the same
# critique/revision prompts the per-dispute loop uses. Both models
# participate in every warmup round; neither is suppressed or made to wait.
# ponytail: gate is round count alone — _run() is structured so
# extract_conflicts / _eval_consensus / verify_all_disputes are all called
# only after _research_warmup() returns, so the "no <stage> before Round 3"
# invariants hold by construction, not by scattered if-round guards.
WARMUP_ROUNDS = 3

DOMAIN_PRIORITY = {
    "HIGH": ["database", "storage", "consistency", "architecture pattern",
             "scaling", "security", "deployment", "orchestration", "data",
             "infrastructure", "networking", "failover", "disaster"],
    "MEDIUM": ["framework", "service", "api", "messaging", "cache", "queue",
               "workflow", "pipeline", "observability", "monitoring"],
    "LOW": ["library", "naming", "formatting", "tooling", "language", "sdk"],
}


# ---------------------------------------------------------------------------
# Dispute — a single architectural disagreement
# ---------------------------------------------------------------------------
@dataclass
class Dispute:
    dispute_text: str
    normalized_key: str
    gpt_source: str = ""
    gemini_source: str = ""
    gpt_current: str = ""
    gemini_current: str = ""
    priority: str = "MEDIUM"
    priority_weight: int = 3
    round_number: int = 0
    consensus_score: int = 0
    consensus_history: list = field(default_factory=list)
    stagnation_warnings: int = 0
    verification_retries: int = 0
    status: str = "active"  # active | pending_verification | resolved | deadlocked | tradeoff | human_required
    # Governance fields
    outcome_type: str = ""  # VERIFIED_DECISION | VERIFIED_TRADEOFF | HUMAN_REQUIRED
    governed_outcome: dict | None = None  # serialized GovernedOutcome
    information_state: dict | None = None  # serialized InformationState

    def to_dict(self) -> dict:
        return {
            "dispute": self.dispute_text,  # compatibility with synthesis_engine
            "dispute_text": self.dispute_text,
            "normalized_key": self.normalized_key,
            "gpt_source": self.gpt_source,
            "gemini_source": self.gemini_source,
            "gpt_current": self.gpt_current,
            "gemini_current": self.gemini_current,
            "priority": self.priority,
            "priority_weight": self.priority_weight,
            "round_number": self.round_number,
            "consensus_score": self.consensus_score,
            "consensus_history": self.consensus_history,
            "stagnation_warnings": self.stagnation_warnings,
            "verification_retries": self.verification_retries,
            "status": self.status,
            "outcome_type": self.outcome_type,
            "governed_outcome": self.governed_outcome,
            "information_state": self.information_state,
        }


# ---------------------------------------------------------------------------
# ArchitectureMemory — resolved decisions ledger
# ---------------------------------------------------------------------------
class ArchitectureMemory:
    """Accumulates verified resolutions. Prevents re-debate of settled topics."""

    def __init__(self):
        self._decisions: dict[str, dict] = {}

    def resolve(self, dispute: Dispute):
        """Store a verified decision. Called only after verification passes."""
        self._decisions[dispute.normalized_key] = {
            "decision": dispute.dispute_text,
            "agreed_position": dispute.gpt_current[:500],
            "priority": dispute.priority,
            "priority_weight": dispute.priority_weight,
            "resolved_at_round": dispute.round_number,
        }

    def is_resolved(self, normalized_key: str) -> bool:
        return normalized_key in self._decisions

    def get_injection_context(self, current_dispute: Dispute = None) -> str:
        """Return up to MAX_CONSTRAINTS highest-priority resolved decisions."""
        if not self._decisions:
            return ""
        current_nk = current_dispute.normalized_key if current_dispute else ""
        eligible = {nk: d for nk, d in self._decisions.items() if nk != current_nk}
        if not eligible:
            return ""
        sorted_items = sorted(eligible.items(),
                              key=lambda item: item[1].get("priority_weight", 3),
                              reverse=True)
        selected = sorted_items[:MAX_CONSTRAINTS]
        dropped = len(sorted_items) - len(selected)
        lines = ["<resolved_constraints>"]
        for nk, d in selected:
            lines.append(f"  [{nk}]: {d['decision']}")
        lines.append("</resolved_constraints>")
        lines.append("The above decisions are SETTLED. Do not contradict or re-debate them.")
        if dropped > 0:
            lines.append(f"({dropped} additional resolved decisions omitted.)")
        return "\n".join(lines)

    def get_all(self) -> list[dict]:
        return [{"normalized_key": k, **v} for k, v in self._decisions.items()]


# ---------------------------------------------------------------------------
# Priority classification
# ---------------------------------------------------------------------------
def _classify_priority(dispute_text: str, normalized_key: str) -> str:
    combined = (dispute_text + " " + normalized_key).lower()
    for domain in DOMAIN_PRIORITY["HIGH"]:
        if domain in combined:
            return "HIGH"
    for domain in DOMAIN_PRIORITY["MEDIUM"]:
        if domain in combined:
            return "MEDIUM"
    return "LOW"


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------
def _critique_prompt(opponent_position: str) -> str:
    return (
        "Identify only the most important factual, architectural, or implementation "
        "disagreements with the position below. Ignore wording, formatting, and style.\n\n"
        f"{opponent_position}"
    )


def _revision_prompt(critique: str) -> str:
    return (
        "Revise your position based on this critique. Only change something if the "
        "critique provides a factual or architectural reason. Do not change merely to "
        "agree. Preserve positions that were not successfully challenged.\n\n"
        f"{critique}\n\n"
        "Return only your revised position."
    )


def _constrain_prompt(dispute_text: str, constraints: str = "") -> str:
    if constraints:
        return (
            f"{constraints}\n\n"
            "Debate only the following specific disagreement. Do NOT re-debate "
            "settled constraints above.\n\n"
            f"DISPUTE: {dispute_text}\n\n"
            "State your position on this dispute only."
        )
    return (
        "State your position on this specific dispute only:\n\n"
        f"{dispute_text}"
    )


# ---------------------------------------------------------------------------
# Evaluator helpers
# ---------------------------------------------------------------------------
def _eval_consensus(question: str, gpt_pos: str, gemini_pos: str) -> dict:
    raw = generate(
        "Compare these two positions on a scale of 0-100 where 100=identical.\n"
        'Return JSON: {"consensus_score": <int>}\n\n'
        f"QUESTION: {question}\n"
        f"GPT: {gpt_pos[:3000]}\n"
        f"GEMINI: {gemini_pos[:3000]}",
        module="consensus_check",
    )
    try:
        return json.loads(raw) if raw.startswith("{") else _extract_json(raw) or {"consensus_score": 50}
    except Exception:
        m = re.search(r'"consensus_score"\s*:\s*(\d+)', raw)
        return {"consensus_score": int(m.group(1)) if m else 50}


def _eval_validate(question: str, disputes: list, gpt_pos: str, gemini_pos: str) -> list:
    if not disputes:
        return []
    batch = [
        {"dispute": d.dispute_text,
         "gpt_evidence": _evidence_window(d.dispute_text, gpt_pos),
         "gemini_evidence": _evidence_window(d.dispute_text, gemini_pos)}
        for d in disputes
    ]
    prompt = (
        "Classify each dispute as VALID (real contradiction) or EQUIVALENT (phrasing variant).\n"
        "Provide a snake_case normalized_key for each.\n"
        'Return JSON array: [{"dispute":"...","normalized_key":"...",'
        '"classification":"VALID|EQUIVALENT","confidence":0-100}]\n\n'
        f"QUESTION: {question}\n"
        f"{json.dumps(batch, indent=2)}"
    )
    try:
        raw = generate(prompt, module="dispute_validator")
        result = _extract_json(raw)
        if isinstance(result, list):
            return result
    except Exception as e:
        print(f"[VALIDATOR ERROR] {e}")
    return [
        {"dispute": d.dispute_text,
         "normalized_key": d.normalized_key or re.sub(
             r'[^a-z0-9_]', '', d.dispute_text.lower().replace(' ', '_'))[:40],
         "classification": "VALID", "confidence": 75}
        for d in disputes
    ]


def _evidence_window(dispute_text: str, position: str, window: int = 800) -> str:
    if not position or not dispute_text:
        return position[:window] if position else ""
    keywords = [
        w.strip(":,()\"'.") for w in dispute_text.lower().split()
        if len(w) > 2 and w not in {"vs", "and", "the", "for", "system", "architecture"}
    ]
    if not keywords:
        return position[:window]
    best, best_idx = -1, 0
    for i in range(0, len(position), 100):
        chunk = position[i:i + window].lower()
        matches = sum(1 for kw in keywords if kw in chunk)
        if matches > best:
            best, best_idx = matches, i
    start = max(0, best_idx - 100)
    return position[start:start + window]


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------
def _extract_json(text: str):
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    for markers in [("{", "}"), ("[", "]")]:
        start = text.find(markers[0])
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == markers[0]:
                depth += 1
            elif text[i] == markers[1]:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception:
                        break
    return None


# ---------------------------------------------------------------------------
# Response cleaning
# ---------------------------------------------------------------------------
def _clean(text: str) -> str:
    if not text:
        return ""
    text = text.strip()
    for pfx in ["Gemini said", "GPT said", "ChatGPT said",
                "Gemini:", "GPT:", "ChatGPT:", "Claude said", "Claude:"]:
        if text.lower().startswith(pfx.lower()):
            text = text[len(pfx):].strip()
    return text


# ---------------------------------------------------------------------------
# Stagnation detection
# ---------------------------------------------------------------------------


def _enqueue_human_required(dispute: Dispute, outcome: GovernedOutcome | dict) -> str:
    if isinstance(outcome, dict):
        outcome = GovernedOutcome.from_dict(outcome)

    from human_decision_queue import enqueue_human_decision

    decision_id = enqueue_human_decision(
        workspace_id="standalone_debate",
        question=dispute.dispute_text,
        options=[outcome.option_a or "", outcome.option_b or ""],
        reasoning=outcome.reasoning,
        affected_disputes=[dispute.normalized_key],
        confidence=outcome.confidence,
        priority=dispute.priority,
    )

    print("=" * 50)
    print("HUMAN DECISION REQUIRED")
    print("=" * 50)
    print()
    print("Dispute:")
    print(dispute.dispute_text)
    print()
    print("Option A:")
    print(outcome.option_a or "")
    print()
    print("Option B:")
    print(outcome.option_b or "")
    print()
    print("Confidence:")
    print(outcome.confidence)
    print()
    print("Reasoning:")
    print(outcome.reasoning)
    print()
    print("Decision ID:")
    print(decision_id)
    print("=" * 50)
    if isinstance(dispute.governed_outcome, dict):
        dispute.governed_outcome["decision_id"] = decision_id
    return decision_id

def _print_consensus_check(dispute: Dispute, round_num: int, score: int, result: str = "ACTIVE") -> None:
    checks_remaining = max(0, (MAX_ROUNDS_PER_DISPUTE - round_num) // 3)
    print("[CONSENSUS CHECK]")
    print(f"Dispute={dispute.normalized_key}")
    print(f"Round={round_num}")
    print(f"Consensus={score}")
    print(f"Threshold={CONSENSUS_THRESHOLD}")
    print(f"ChecksCompleted={len(dispute.consensus_history)}")
    print(f"ChecksRemaining={checks_remaining}")
    print(f"Result={result or 'ACTIVE'}")


def _detect_stagnation(dispute: Dispute) -> str | None:
    history = dispute.consensus_history
    if len(history) < STAGNATION_CHECKS:
        return None
    recent = history[-STAGNATION_CHECKS:]
    if max(recent) - min(recent) < 5:
        dispute.stagnation_warnings += 1
        if dispute.stagnation_warnings > STAGNATION_MAX_WARNINGS:
            return "deadlock"
        return "warning"
    dispute.stagnation_warnings = 0
    return None


# ===================================================================
# DebateEngine
# ===================================================================
class DebateEngine:

    def run(self, question: str, memory_ids: list = None) -> dict:
        from gpt_worker import GPTSession
        from gemini_worker import GeminiSession

        gpt, gemini = GPTSession(), GeminiSession()
        gpt.start()
        gemini.start()
        try:
            return self._run(question, gpt, gemini, memory_ids)
        finally:
            gpt.close()
            gemini.close()

    # ------------------------------------------------------------------
    # Main pipeline
    # ------------------------------------------------------------------
    def _run(self, question: str, gpt, gemini, memory_ids: list | None) -> dict:
        # Load historical memory if provided
        ctx = ""
        if memory_ids:
            ctx = memory.build_context(memory_ids)
            if len(ctx) > MAX_MEMORY_CHARS:
                ctx = ctx[-MAX_MEMORY_CHARS:]
            question = f"MEMORY CONTEXT\n\n{ctx}\n\nCURRENT QUESTION\n\n{question}"

        # Get source positions — Round 1 of the Research Warm-up
        print("\n=== INITIAL POSITIONS ===")
        gpt_source = _clean(gpt.ask(question))
        gemini_source = _clean(gemini.ask(question))

        # Research Warm-up — Rounds 2..WARMUP_ROUNDS. Both models mature their
        # reasoning on the full question (research, assumptions, risks,
        # evaluation criteria, terminology) before any conflict extraction,
        # verifier pass, or consensus check runs. See _research_warmup() for
        # the gating invariant.
        gpt_source, gemini_source = self._research_warmup(
            gpt, gemini, gpt_source, gemini_source)

        # Extract conflicts ONCE from post-warmup matured positions
        print("\n=== CONFLICT EXTRACTION ===")
        conflict_result = extract_conflicts(gpt_source, gemini_source)
        raw_disputes = conflict_result.get("disputed_claims", [])

        if not raw_disputes:
            return self._synthesize_and_return(
                question, gpt_source, gemini_source, [], {}, 100, "completed",
                gov_result=GovernanceResult())

        # Build priority-ordered dispute queue
        disputes = self._build_dispute_queue(raw_disputes, gpt_source, gemini_source)
        arch_memory = ArchitectureMemory()
        gpt_current = gpt_source
        gemini_current = gemini_source
        final_consensus_score = 0

        # Governance result aggregator
        gov_result = GovernanceResult()

        # Debate one dispute at a time
        _telemetry.observe(stage="Debate", total_disputes=len(disputes),
                           remaining_disputes=len(disputes))
        _resolved_so_far = 0
        for dispute in disputes:
            constraints = arch_memory.get_injection_context(current_dispute=dispute)
            intro = _constrain_prompt(dispute.dispute_text, constraints)
            dispute.gpt_current = _clean(gpt.ask(intro))
            dispute.gemini_current = _clean(gemini.ask(intro, dispute_key=dispute.normalized_key))

            _telemetry.observe(active_dispute=(dispute.dispute_text or
                                               dispute.normalized_key),
                               current_round=0)
            result = self._debate_dispute(question, dispute, gpt, gemini)
            final_consensus_score = max(final_consensus_score, dispute.consensus_score)

            if result == "resolved":
                dispute.status = "pending_verification"
                print(f"[PENDING] {dispute.normalized_key}")
            elif result == "verified_decision":
                dispute.status = "pending_verification"  # Still needs verification pass
                print(f"[VERIFIED DECISION] {dispute.normalized_key}")
                if dispute.governed_outcome:
                    gov_result.add(GovernedOutcome.from_dict(dispute.governed_outcome))
            elif result == "verified_tradeoff":
                dispute.status = "tradeoff"
                print(f"[VERIFIED TRADEOFF] {dispute.normalized_key}")
                if dispute.governed_outcome:
                    gov_result.add(GovernedOutcome.from_dict(dispute.governed_outcome))
            elif result == "human_required":
                dispute.status = "human_required"
                print(f"[HUMAN REQUIRED] {dispute.normalized_key}")
                if dispute.governed_outcome:
                    gov_result.add(GovernedOutcome.from_dict(dispute.governed_outcome))
                    _enqueue_human_required(dispute, dispute.governed_outcome)
            else:
                dispute.status = "deadlocked"
                print(f"[DEADLOCKED] {dispute.normalized_key}")
            # Telemetry: one DisputeResolved per terminal dispute, with the
            # remaining/total counts the recorder uses to update live status.
            _resolved_so_far += 1
            _telemetry.emit("DisputeResolved", "Dispute",
                            dispute=dispute.normalized_key,
                            outcome=result,
                            remaining=max(0, len(disputes) - _resolved_so_far),
                            total=len(disputes))

        # Verification loop with retry
        verification_result = self._run_verification(
            question, disputes, gpt_current, gemini_current, gpt, gemini,
            arch_memory, final_consensus_score, gov_result)

        if verification_result.get("_failed"):
            return verification_result

        # Cross-dispute consistency audit (H8) — detect contradictions in resolved decisions
        contradiction_disputes = self._run_consistency_audit(arch_memory, disputes)
        if contradiction_disputes:
            print(f"\n[CONSISTENCY AUDIT] {len(contradiction_disputes)} contradiction(s) found. Re-entering debate.")
            # Add contradiction disputes to the queue and debate them
            new_disputes = self._build_dispute_queue(contradiction_disputes, gpt_current, gemini_current)
            for nd in new_disputes:
                constraints = arch_memory.get_injection_context(current_dispute=nd)
                intro = _constrain_prompt(nd.dispute_text, constraints)
                nd.gpt_current = _clean(gpt.ask(intro))
                nd.gemini_current = _clean(gemini.ask(intro, dispute_key=nd.normalized_key))
                r = self._debate_dispute(question, nd, gpt, gemini)
                if r in ("resolved", "verified_decision"):
                    nd.status = "pending_verification"
                elif r == "verified_tradeoff":
                    nd.status = "tradeoff"
                    if nd.governed_outcome:
                        gov_result.add(GovernedOutcome.from_dict(nd.governed_outcome))
                elif r == "human_required":
                    nd.status = "human_required"
                    if nd.governed_outcome:
                        gov_result.add(GovernedOutcome.from_dict(nd.governed_outcome))
                        _enqueue_human_required(nd, nd.governed_outcome)
                else:
                    nd.status = "deadlocked"
                final_consensus_score = max(final_consensus_score, nd.consensus_score)
                disputes.append(nd)
            # Re-run verification for new disputes
            verification_result = self._run_verification(
                question, disputes, gpt_current, gemini_current, gpt, gemini,
                arch_memory, final_consensus_score, gov_result)
            if verification_result.get("_failed"):
                return verification_result

        return self._synthesize_and_return(
            question, gpt_current, gemini_current, disputes,
            verification_result, final_consensus_score, "completed",
            arch_memory, gov_result)

    # ------------------------------------------------------------------
    # Execution-decision seam (debate-governed execution decisions).
    # Pure enrichment: no change to debate / verification / synthesis
    # mechanics. Attaches the deterministic execution_decision to the result
    # dict and into verification_result so it persists through answer_memory
    # (verification_result is a JSON-TEXT column) and survives resurrection
    # and workspace resume.
    # ------------------------------------------------------------------
    def _attach_execution_decision(
        self, question, governance_context, verification_result,
        synthesized_answer,
    ) -> dict:
        from execution_decision import (
            classify_execution_decision, attach_to_verification_result,
        )
        decision = classify_execution_decision(
            question=question,
            verified_decisions=governance_context.get("verified_decisions", []),
            verified_tradeoffs=governance_context.get("verified_tradeoffs", []),
            human_required=governance_context.get("human_required", []),
            synthesized_answer=synthesized_answer,
            verification_result=verification_result,
        )
        attach_to_verification_result(verification_result, decision)
        return decision

    # ------------------------------------------------------------------
    # Build dispute queue from raw extraction
    # ------------------------------------------------------------------
    def _build_dispute_queue(self, raw_disputes: list, gpt_source: str,
                              gemini_source: str) -> list[Dispute]:
        temp = [Dispute(dispute_text=d, normalized_key="") for d in raw_disputes]
        validation = _eval_validate(
            "",  # question not needed for validation — disputes already extracted
            temp, gpt_source, gemini_source)

        disputes = []
        for v in validation:
            if not isinstance(v, dict) or v.get("classification") != "VALID":
                continue
            try:
                conf = int(v.get("confidence", 0))
            except (ValueError, TypeError):
                conf = 0
            if conf < 70:
                continue
            nk = v.get("normalized_key", "") or re.sub(
                r'[^a-z0-9_]', '', v.get("dispute", "").lower().replace(' ', '_'))[:40]
            priority = _classify_priority(v.get("dispute", ""), nk)
            disputes.append(Dispute(
                dispute_text=v.get("dispute", ""),
                normalized_key=nk,
                gpt_source=gpt_source,
                gemini_source=gemini_source,
                priority=priority,
                priority_weight=PRIORITY_WEIGHTS.get(priority, 3),
            ))

        priority_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        disputes.sort(key=lambda d: priority_order.get(d.priority, 2))

        print(f"\n=== DISPUTE QUEUE ({len(disputes)} disputes) ===")
        for d in disputes:
            print(f"  [{d.priority}] {d.normalized_key}: {d.dispute_text[:80]}")
        print("=" * 40)
        return disputes

    # ------------------------------------------------------------------
    # Research Warm-up phase
    # ------------------------------------------------------------------
    def _research_warmup(self, gpt, gemini,
                        gpt_pos: str, gemini_pos: str) -> tuple[str, str]:
        """Run Research Warm-up rounds 2..WARMUP_ROUNDS.

        Both models critique the opponent's current position and revise their
        own on the full question — the same critique/revision prompts the
        per-dispute loop uses, applied at the macro (whole-question) level so
        research, assumptions, risks, evaluation criteria, and terminology
        mature naturally before the first conflict extraction. No model waits,
        no response is suppressed, no prompt is simplified.

        Returns the post-warmup (gpt_pos, gemini_pos). When WARMUP_ROUNDS <= 1
        this is a no-op pass-through, preserving the legacy single-round
        behavior for any caller that lowers the threshold.
        """
        for round_num in range(2, WARMUP_ROUNDS + 1):
            g_critique = _clean(gemini.ask(
                _critique_prompt(gpt_pos), dispute_key="warmup"))
            gpt_critique = _clean(gpt.ask(_critique_prompt(gemini_pos)))
            gpt_pos = _clean(gpt.ask(_revision_prompt(g_critique)))
            gemini_pos = _clean(gemini.ask(
                _revision_prompt(gpt_critique), dispute_key="warmup"))
            gpt_hash = hashlib.sha256(gpt_pos.encode()).hexdigest()[:8]
            gem_hash = hashlib.sha256(gemini_pos.encode()).hexdigest()[:8]
            print(f"  [WARMUP] Round {round_num}/{WARMUP_ROUNDS} "
                  f"GPT={gpt_hash} GEM={gem_hash}")
        return gpt_pos, gemini_pos

    # ------------------------------------------------------------------
    # Single-dispute debate loop
    # ------------------------------------------------------------------
    def _debate_dispute(self, question: str, dispute: Dispute, gpt, gemini) -> str:
        round_num = 0
        info_state = InformationState()

        while round_num < MAX_ROUNDS_PER_DISPUTE:
            round_num += 1
            dispute.round_number = round_num
            _round_t0 = time.time()
            _telemetry.observe(current_round=round_num,
                               active_dispute=(dispute.dispute_text or
                                               dispute.normalized_key))

            # Critique
            g_critique = _clean(gemini.ask(_critique_prompt(dispute.gpt_current), dispute_key=dispute.normalized_key))
            gpt_critique = _clean(gpt.ask(_critique_prompt(dispute.gemini_current)))

            # Revise
            dispute.gpt_current = _clean(gpt.ask(_revision_prompt(g_critique)))
            dispute.gemini_current = _clean(gemini.ask(_revision_prompt(gpt_critique), dispute_key=dispute.normalized_key))

            gpt_hash = hashlib.sha256(dispute.gpt_current.encode()).hexdigest()[:8]
            gem_hash = hashlib.sha256(dispute.gemini_current.encode()).hexdigest()[:8]
            print(f"  [{dispute.normalized_key}] R{round_num} GPT={gpt_hash} GEM={gem_hash}")

            # Track information state novelty
            novelty = info_state.record_round(dispute.gpt_current, dispute.gemini_current)
            if novelty["rounds_stale"] > 0:
                print(f"  [INFO STATE] {dispute.normalized_key} — {novelty['rounds_stale']} rounds without new information")

            if round_num % 3 != 0:
                continue

            # Consensus check (every 3 rounds per spec)
            score_result = _eval_consensus(question, dispute.gpt_current, dispute.gemini_current)
            score = int(score_result.get("consensus_score", 0))
            dispute.consensus_score = score
            dispute.consensus_history.append(score)

            # Telemetry: one RoundCompleted per consensus-check cadence, with
            # the round number, the active dispute, and the consensus score.
            # Pure observation — the score/return values below are unchanged.
            _telemetry.emit("RoundCompleted", "Round",
                            duration=max(0.0, time.time() - _round_t0),
                            round=round_num,
                            dispute=(dispute.dispute_text or
                                     dispute.normalized_key),
                            consensus=score)
            _telemetry.observe(consensus=score)

            # Stagnation detection — traditional score-based
            stag = _detect_stagnation(dispute)
            info_stagnated = info_state.is_stagnating(threshold=STAGNATION_CHECKS)

            if score >= CONSENSUS_THRESHOLD:
                _print_consensus_check(dispute, round_num, score, "RESOLVED")
                return "resolved"

            # Information-state stagnation (new governance check)
            if info_stagnated:
                print(f"  [GOVERNANCE] {dispute.normalized_key} — information state stagnated. Triggering verifier review.")
                dispute.information_state = info_state.to_dict()
                outcome = self._verifier_review_dispute(dispute, info_state)
                _print_consensus_check(dispute, round_num, score, outcome.outcome_type)
                if outcome.outcome_type == OutcomeType.VERIFIED_DECISION:
                    # Convert the decision into an agreed position
                    dispute.gpt_current = outcome.decision or dispute.gpt_current
                    dispute.gemini_current = outcome.decision or dispute.gemini_current
                    dispute.consensus_score = max(dispute.consensus_score, outcome.confidence)
                    dispute.outcome_type = OutcomeType.VERIFIED_DECISION
                    dispute.governed_outcome = outcome.to_dict()
                    return "verified_decision"
                elif outcome.outcome_type == OutcomeType.VERIFIED_TRADEOFF:
                    dispute.outcome_type = OutcomeType.VERIFIED_TRADEOFF
                    dispute.governed_outcome = outcome.to_dict()
                    return "verified_tradeoff"
                else:
                    dispute.outcome_type = OutcomeType.HUMAN_REQUIRED
                    dispute.governed_outcome = outcome.to_dict()
                    return "human_required"

            if stag == "deadlock":
                # Score-based deadlock → escalate to verifier review instead of hard deadlocking
                print(f"  [GOVERNANCE] {dispute.normalized_key} — score-based deadlock. Triggering verifier review.")
                dispute.information_state = info_state.to_dict()
                outcome = self._verifier_review_dispute(dispute, info_state)
                _print_consensus_check(dispute, round_num, score, outcome.outcome_type)
                if outcome.outcome_type == OutcomeType.VERIFIED_DECISION:
                    dispute.gpt_current = outcome.decision or dispute.gpt_current
                    dispute.gemini_current = outcome.decision or dispute.gemini_current
                    dispute.consensus_score = max(dispute.consensus_score, outcome.confidence)
                    dispute.outcome_type = OutcomeType.VERIFIED_DECISION
                    dispute.governed_outcome = outcome.to_dict()
                    return "verified_decision"
                elif outcome.outcome_type == OutcomeType.VERIFIED_TRADEOFF:
                    dispute.outcome_type = OutcomeType.VERIFIED_TRADEOFF
                    dispute.governed_outcome = outcome.to_dict()
                    return "verified_tradeoff"
                else:
                    dispute.outcome_type = OutcomeType.HUMAN_REQUIRED
                    dispute.governed_outcome = outcome.to_dict()
                    return "human_required"

            _print_consensus_check(dispute, round_num, score, "ACTIVE")

            if stag == "warning":
                print(f"  [STAGNATION] {dispute.normalized_key} — forcing engagement")
                force = (
                    "\n\nYour opponent has raised specific points you have not addressed. "
                    "Respond to those points directly. If you cannot, state why the point is invalid."
                )
                dispute.gpt_current = _clean(gpt.ask(dispute.gpt_current + force))
                dispute.gemini_current = _clean(gemini.ask(dispute.gemini_current + force, dispute_key=dispute.normalized_key))


        # Max rounds exhausted → verifier review
        print(f"  [GOVERNANCE] {dispute.normalized_key} — max rounds ({MAX_ROUNDS_PER_DISPUTE}) exhausted. Triggering verifier review.")
        dispute.information_state = info_state.to_dict()
        outcome = self._verifier_review_dispute(dispute, info_state)
        if outcome.outcome_type == OutcomeType.VERIFIED_DECISION:
            dispute.gpt_current = outcome.decision or dispute.gpt_current
            dispute.gemini_current = outcome.decision or dispute.gemini_current
            dispute.consensus_score = max(dispute.consensus_score, outcome.confidence)
            dispute.outcome_type = OutcomeType.VERIFIED_DECISION
            dispute.governed_outcome = outcome.to_dict()
            return "verified_decision"
        elif outcome.outcome_type == OutcomeType.VERIFIED_TRADEOFF:
            dispute.outcome_type = OutcomeType.VERIFIED_TRADEOFF
            dispute.governed_outcome = outcome.to_dict()
            return "verified_tradeoff"
        else:
            dispute.outcome_type = OutcomeType.HUMAN_REQUIRED
            dispute.governed_outcome = outcome.to_dict()
            return "human_required"

    # ------------------------------------------------------------------
    # Verifier review — governance-driven stagnation resolution
    # ------------------------------------------------------------------
    def _verifier_review_dispute(self, dispute: Dispute,
                                  info_state: InformationState) -> GovernedOutcome:
        """Invoke the governance verifier to resolve a stagnant dispute.

        The verifier evaluates across five dimensions (evidence quality,
        architectural validity, implementation impact, operational impact,
        scalability impact) and returns one of three outcomes:
        VERIFIED_DECISION, VERIFIED_TRADEOFF, or HUMAN_REQUIRED.
        """
        print("[GOVERNANCE]")
        print("Verifier review starting...")
        started = time.time()
        outcome = verifier_review(
            dispute_text=dispute.dispute_text,
            dispute_key=dispute.normalized_key,
            gpt_position=dispute.gpt_current,
            gemini_position=dispute.gemini_current,
            information_state=info_state,
        )
        print("[GOVERNANCE]")
        print("Verifier review completed.")
        print(f"Duration={time.time() - started:.2f}s")
        dims = (
            f"evidence={outcome.evidence_quality} "
            f"arch={outcome.architectural_validity} "
            f"impl={outcome.implementation_impact} "
            f"ops={outcome.operational_impact} "
            f"scale={outcome.scalability_impact}"
        )
        print(f"  [VERIFIER REVIEW] {dispute.normalized_key} → {outcome.outcome_type} "
              f"(confidence={outcome.confidence}) [{dims}]")
        if outcome.reasoning:
            print(f"  [VERIFIER REASONING] {outcome.reasoning[:200]}")
        return outcome

    # ------------------------------------------------------------------
    # Verification loop with retry
    # ------------------------------------------------------------------
    def _run_verification(self, question: str, disputes: list[Dispute],
                           gpt_current: str, gemini_current: str,
                           gpt, gemini, arch_memory: ArchitectureMemory,
                           final_consensus_score: int,
                           gov_result: GovernanceResult | None = None) -> dict:
        final_result = None

        while True:
            pending = [d for d in disputes
                       if d.status in ("pending_verification", "deadlocked")]
            if not pending:
                break

            _telemetry.start_stage("Verification")
            result = verify_all_disputes(
                question=question,
                validated_disputes=[
                    {"dispute": d.dispute_text, "normalized_key": d.normalized_key}
                    for d in pending
                ],
                gpt_position="\n\n".join(d.gpt_current or "" for d in pending),
                gemini_position="\n\n".join(d.gemini_current or "" for d in pending),
                use_external_research=False,
            )
            _telemetry.end_stage("Verification",
                                 disputes=len(pending))

            unresolved_nks = set()
            for claim in result.get("unresolved_claims", []):
                nk = claim.get("normalized_key", "") if isinstance(claim, dict) else ""
                if nk:
                    unresolved_nks.add(nk)

            # Resolve or retry or deadlock each pending dispute
            retry_list = []
            for d in disputes:
                if d.status != "pending_verification":
                    continue
                if d.normalized_key in unresolved_nks:
                    if d.verification_retries < MAX_VERIFICATION_RETRIES:
                        d.verification_retries += 1
                        d.status = "active"
                        retry_list.append(d)
                        print(f"[RETRY {d.verification_retries}/{MAX_VERIFICATION_RETRIES}] {d.normalized_key}")
                    else:
                        # Retries exhausted → escalate to governance verifier
                        # review. Honor a pre-emitted verdict (HUMAN_REQUIRED
                        # from research-budget exhaustion) without a fresh model
                        # call (Phase 4 wiring) before falling back to
                        # verifier_review.
                        print(f"[GOVERNANCE] {d.normalized_key} — verification retries exhausted. Triggering verifier review.")
                        pre = extract_pre_emitted_outcome(result, d.normalized_key)
                        outcome = (GovernedOutcome.from_dict(pre)
                                    if pre is not None
                                    else self._verifier_review_dispute(d, InformationState()))
                        if outcome.outcome_type == OutcomeType.VERIFIED_DECISION:
                            arch_memory.resolve(d)
                            d.status = "resolved"
                            d.outcome_type = OutcomeType.VERIFIED_DECISION
                            d.governed_outcome = outcome.to_dict()
                            if gov_result:
                                gov_result.add(outcome)
                            print(f"[VERIFIED DECISION] {d.normalized_key}")
                        elif outcome.outcome_type == OutcomeType.VERIFIED_TRADEOFF:
                            d.status = "tradeoff"
                            d.outcome_type = OutcomeType.VERIFIED_TRADEOFF
                            d.governed_outcome = outcome.to_dict()
                            if gov_result:
                                gov_result.add(outcome)
                            print(f"[VERIFIED TRADEOFF] {d.normalized_key}")
                        else:
                            d.status = "human_required"
                            d.outcome_type = OutcomeType.HUMAN_REQUIRED
                            d.governed_outcome = outcome.to_dict()
                            if gov_result:
                                gov_result.add(outcome)
                            _enqueue_human_required(d, outcome)
                            print(f"[HUMAN REQUIRED] {d.normalized_key}")
                else:
                    arch_memory.resolve(d)
                    d.status = "resolved"
                    print(f"[RESOLVED] {d.normalized_key} — verified")

            # Re-debate retries
            if retry_list:
                print(f"\n[RE-DEBATE] {len(retry_list)} disputes...")
                for dispute in retry_list:
                    constraints = arch_memory.get_injection_context(current_dispute=dispute)
                    intro = _constrain_prompt(dispute.dispute_text, constraints)
                    dispute.gpt_current = _clean(gpt.ask(intro))
                    dispute.gemini_current = _clean(gemini.ask(intro, dispute_key=dispute.normalized_key))
                    r = self._debate_dispute(question, dispute, gpt, gemini)
                    if r in ("resolved", "verified_decision"):
                        dispute.status = "pending_verification"
                    elif r == "verified_tradeoff":
                        dispute.status = "tradeoff"
                        if dispute.governed_outcome and gov_result:
                            gov_result.add(GovernedOutcome.from_dict(dispute.governed_outcome))
                    elif r == "human_required":
                        dispute.status = "human_required"
                        if dispute.governed_outcome and gov_result:
                            gov_result.add(GovernedOutcome.from_dict(dispute.governed_outcome))
                            _enqueue_human_required(dispute, dispute.governed_outcome)
                    else:
                        dispute.status = "deadlocked"
                final_result = result
                continue  # Re-verify

            final_result = result
            break

        # Any still-pending after retries exhausted → escalate to governance verifier
        for d in disputes:
            if d.status == "pending_verification":
                print(f"[GOVERNANCE] {d.normalized_key} — verification incomplete. Triggering verifier review.")
                # Honor a pre-emitted verdict without a fresh model call
                # (Phase 4 wiring); else fall back to verifier_review.
                pre = extract_pre_emitted_outcome(final_result, d.normalized_key)
                outcome = (GovernedOutcome.from_dict(pre)
                            if pre is not None
                            else self._verifier_review_dispute(d, InformationState()))
                if outcome.outcome_type == OutcomeType.VERIFIED_DECISION:
                    arch_memory.resolve(d)
                    d.status = "resolved"
                    d.outcome_type = OutcomeType.VERIFIED_DECISION
                    d.governed_outcome = outcome.to_dict()
                    if gov_result:
                        gov_result.add(outcome)
                    print(f"[VERIFIED DECISION] {d.normalized_key}")
                elif outcome.outcome_type == OutcomeType.VERIFIED_TRADEOFF:
                    d.status = "tradeoff"
                    d.outcome_type = OutcomeType.VERIFIED_TRADEOFF
                    d.governed_outcome = outcome.to_dict()
                    if gov_result:
                        gov_result.add(outcome)
                    print(f"[VERIFIED TRADEOFF] {d.normalized_key}")
                else:
                    d.status = "human_required"
                    d.outcome_type = OutcomeType.HUMAN_REQUIRED
                    d.governed_outcome = outcome.to_dict()
                    if gov_result:
                        gov_result.add(outcome)
                    _enqueue_human_required(d, outcome)
                    print(f"[HUMAN REQUIRED] {d.normalized_key}")

        # Weighted consensus (Critical Issue 3)
        w_sum = sum(d.priority_weight for d in disputes)
        w_score = int(sum(d.consensus_score * d.priority_weight for d in disputes) / w_sum) if w_sum > 0 else 0
        print(f"[CONSENSUS] Weighted={w_score}")

        # Hard failure gate
        if final_result:
            v_score = final_result.get("verification_score", 100)
            unresolved = len(final_result.get("unresolved_claims", []))
            resolved_disputes = [
                d for d in disputes
                if d.status == "resolved" or d.outcome_type == OutcomeType.VERIFIED_DECISION
                or (d.governed_outcome or {}).get("outcome_type") == OutcomeType.VERIFIED_DECISION
            ]
            tradeoff_disputes = [
                d for d in disputes
                if d.status == "tradeoff" or d.outcome_type == OutcomeType.VERIFIED_TRADEOFF
                or (d.governed_outcome or {}).get("outcome_type") == OutcomeType.VERIFIED_TRADEOFF
            ]
            human_required_disputes = [
                d for d in disputes
                if d.status == "human_required" or d.outcome_type == OutcomeType.HUMAN_REQUIRED
                or (d.governed_outcome or {}).get("outcome_type") == OutcomeType.HUMAN_REQUIRED
            ]
            terminal_outcomes = {
                OutcomeType.VERIFIED_DECISION,
                OutcomeType.VERIFIED_TRADEOFF,
                OutcomeType.HUMAN_REQUIRED,
            }
            terminal_statuses = {"resolved", "tradeoff", "human_required"}
            failed_keys = [
                d.normalized_key
                for d in disputes
                if d.status not in terminal_statuses
                and d.outcome_type not in terminal_outcomes
                and (d.governed_outcome or {}).get("outcome_type") not in terminal_outcomes
            ]
            print("[VERIFICATION GATE]")
            print(f"Resolved: {len(resolved_disputes)}")
            print(f"Tradeoffs: {len(tradeoff_disputes)}")
            print(f"Human Required: {len(human_required_disputes)}")
            print(f"Truly Unresolved: {len(failed_keys)}")
            if v_score < 90 and unresolved > 0:
                if failed_keys:
                    memory.save_debate(
                        question=question, gpt_final=gpt_current, gemini_final=gemini_current,
                        synthesized_answer="[FAILED VERIFICATION]",
                        consensus_score=final_consensus_score,
                        conflict_severity="FAILED_VERIFICATION",
                        conflict_points=failed_keys,
                        validated_disputes=[d.to_dict() for d in disputes],
                        verification_result=final_result,
                    )
                    return {"_failed": True, "status": "verification_failed",
                            "verification_result": final_result,
                            "final_consensus_score": final_consensus_score,
                            "gpt_final": gpt_current, "gemini_final": gemini_current,
                            "validated_disputes": [d.to_dict() for d in disputes]}

        truncated = final_result.get("truncated_count", 0) if final_result else 0
        if truncated:
            print(f"[TRUNCATION] {truncated} disputes exceeded batch limit — UNVERIFIED")

        return final_result or {}

    # ------------------------------------------------------------------
    # Cross-dispute consistency audit (H8)
    # ------------------------------------------------------------------
    def _run_consistency_audit(self, arch_memory: ArchitectureMemory,
                                disputes: list[Dispute],
                                research_context: str = "") -> list[str]:
        """Check resolved decisions for contradictions. Returns new dispute texts.

        Phase 7 of current_architecture.md: detects when resolved decision A
        contradicts resolved decision B, creating new disputes for re-debate.

        Research V2 (Phase 4): ``research_context`` is an optional evidence
        block string appended to the audit prompt so the auditor sees the
        evidence used during debate. Empty by default → base DebateEngine.run()
        behavior unchanged. The workspace engine / tests pass a
        research_planner.build_consistency_audit_context(...) block here.
        """
        decisions = arch_memory.get_all()
        if len(decisions) < 2:
            return []

        # Filter to only resolved decisions
        resolved = [d for d in decisions if d.get("decision")]
        if len(resolved) < 2:
            return []

        # Build pairs for contradiction detection
        pairs = []
        for i in range(len(resolved)):
            for j in range(i + 1, len(resolved)):
                pairs.append({
                    "decision_a": resolved[i]["decision"],
                    "key_a": resolved[i]["normalized_key"],
                    "decision_b": resolved[j]["decision"],
                    "key_b": resolved[j]["normalized_key"],
                })

        # Limit pairs to avoid token bloat (C(n,2) grows fast)
        max_pairs = 20
        if len(pairs) > max_pairs:
            print(f"[CONSISTENCY AUDIT] Limiting check to {max_pairs} of {len(pairs)} pairs")
            # Prefer HIGH-priority pairs first
            pairs = pairs[:max_pairs]

        prompt = (
            "You are a Cross-Dispute Consistency Auditor.\n\n"
            "For each pair of resolved architectural decisions below, determine if they "
            "CONTRADICT each other. A contradiction means building the system one way "
            "would prevent or undermine the other.\n\n"
            "IGNORE complementary or unrelated decisions. Only flag genuine contradictions.\n\n"
            "Return JSON array of contradictions found:\n"
            '[{"key_a": "...", "key_b": "...", "contradiction": "description of conflict", '
            '"severity": "HIGH|MEDIUM|LOW"}]\n\n'
            "If no contradictions are found, return an empty array: []\n\n"
            "RESOLVED DECISIONS\n"
            f"{json.dumps(resolved, indent=2)}\n\n"
            "DECISION PAIRS TO AUDIT\n"
            f"{json.dumps(pairs, indent=2)}"
        )
        # Phase 4 (V2): let the auditor see the evidence used during debate.
        # Empty research_context (default) leaves the base prompt unchanged.
        if research_context:
            prompt = f"{prompt}\n\n{research_context}"

        try:
            raw = generate(prompt, module="consistency_audit")
            contradictions = _extract_json(raw)
            if not isinstance(contradictions, list):
                return []
        except Exception as e:
            print(f"[CONSISTENCY AUDIT ERROR] {e}")
            return []

        # Already-disputed keys — don't create duplicates
        existing_nks = {d.normalized_key for d in disputes}
        existing_nks.update(arch_memory._decisions.keys())

        new_disputes = []
        for c in contradictions:
            if not isinstance(c, dict):
                continue
            key_a = c.get("key_a", "")
            key_b = c.get("key_b", "")
            contradiction_text = c.get("contradiction", "")
            if not contradiction_text:
                continue

            # Check if either key is still actively debated
            if key_a in existing_nks or key_b in existing_nks:
                print(f"[CONSISTENCY AUDIT] Skipping — key already in active set: {key_a}/{key_b}")
                continue

            new_disputes.append(contradiction_text)
            print(f"[CONSISTENCY AUDIT] CONTRADICTION: {key_a} vs {key_b}: {contradiction_text[:100]}")

        if not new_disputes:
            print("[CONSISTENCY AUDIT] No contradictions found among resolved decisions.")
        else:
            print(f"[CONSISTENCY AUDIT] Found {len(new_disputes)} contradiction(s).")

        return new_disputes

    # ------------------------------------------------------------------
    # Synthesis + memory + return
    # ------------------------------------------------------------------
    def _synthesize_and_return(self, question: str, gpt_current: str,
                                gemini_current: str, disputes: list[Dispute],
                                verification_result: dict, score: int,
                                status: str, arch_memory: ArchitectureMemory = None,
                                gov_result: GovernanceResult = None) -> dict:
        from synthesis_engine import synthesize_answer

        resolved = arch_memory.get_all() if arch_memory else []

        # Build governance context for synthesis
        governance_context = (gov_result or GovernanceResult()).to_dict()

        final = synthesize_answer(
            question=question,
            gpt_final=gpt_current,
            gemini_final=gemini_current,
            validated_disputes=[d.to_dict() for d in disputes],
            consensus_score=score,
            verification_result=verification_result,
            governance_context=governance_context,
            disputes=disputes,
        )
        # Telemetry: ArchitectureCompleted — the audit synthesis above embeds
        # the deterministic Architecture section (decision_renderer). Pure
        # observation; the synthesis result is unchanged.
        _telemetry.emit("Completed", "Architecture")

        # Execution-decision seam. Computed AFTER synthesis (no mechanics
        # change), BEFORE memory.save_debate so the decision rides inside
        # verification_result into answer_memory's JSON column — and is
        # therefore immutable on disk and survives resurrection/resume.
        exec_decision = self._attach_execution_decision(
            question, governance_context, verification_result, final,
        )

        # Human-Readable Synthesis V3 — a SECOND synthesis layer that renders
        # a human-facing answer from the verified decision model. Deterministic,
        # no LLM. Computed AFTER the audit synthesis (which is unchanged) and
        # AFTER the execution decision (which it consumes), so the human answer
        # reflects the same verified decisions and the same execution verdict.
        from human_synthesis_renderer import render_human_answer
        human_answer = render_human_answer(
            question=question,
            verification_result=verification_result,
            synthesized_answer=final,
            execution_decision=exec_decision,
        )
        # Telemetry: HumanSynthesisCompleted — the deterministic human-facing
        # render above. Pure observation; the human_answer is unchanged.
        _telemetry.emit("Completed", "HumanSynthesis")

        keys = [d.normalized_key for d in disputes]

        debate_id = memory.save_debate(
            question=question,
            gpt_final=gpt_current,
            gemini_final=gemini_current,
            synthesized_answer=final,
            consensus_score=score,
            conflict_severity="LOW" if status == "completed" else "HIGH",
            conflict_points=keys,
            validated_disputes=[d.to_dict() for d in disputes],
            verification_result=verification_result,
            human_answer=human_answer,
        )

        return {
            "status": status,
            "gpt_final": gpt_current,
            "gemini_final": gemini_current,
            "synthesized_answer": final,
            "human_answer": human_answer,
            "validated_disputes": [d.to_dict() for d in disputes],
            "verification_result": verification_result,
            "final_consensus_score": score,
            "resolved_decisions": resolved,
            "verified_decisions": governance_context.get("verified_decisions", []),
            "verified_tradeoffs": governance_context.get("verified_tradeoffs", []),
            "human_required": governance_context.get("human_required", []),
            # Execution Mode Gate V1: surface the saved answer id so an
            # upstream caller (e.g. the debate CLI) can chain a FULL_BUILD
            # selection straight into the build pipeline (resurrect-by-id).
            "answer_id": debate_id,
            "execution_decision": exec_decision.get("execution_decision"),
            "execution_reason": exec_decision.get("execution_reason", ""),
            "execution_confidence": exec_decision.get("execution_confidence", 0),
            "execution_decision_source": exec_decision.get(
                "execution_decision_source", ""),
        }
