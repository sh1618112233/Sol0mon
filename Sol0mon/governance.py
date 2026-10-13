"""
Debate Deadlock Governance Framework
=====================================

Prevents infinite debate loops by enforcing:

1. Information State Rule — a round is only useful if it introduces new
   evidence, reasoning, constraints, tradeoffs, or architectural implications.

2. Stagnation Detection — tracks repeated arguments, evidence, conclusions,
   and unchanged positions.  If multiple rounds occur without meaningful
   change, the dispute is marked STAGNATING.

3. Consensus Check Frequency — consensus evaluation occurs every 3 rounds.

4. Maximum Dispute Window — no single dispute may consume unlimited rounds.
   If a dispute continues beyond configured limits without introducing
   meaningful information, verifier review is forced.

5. Verifier Review — evaluates evidence quality, architectural validity,
   implementation impact, operational impact, and scalability impact.  The
   verifier determines one of three outcomes.

6. Outcome Types:
   - VERIFIED_DECISION  — a superior solution exists
   - VERIFIED_TRADEOFF   — multiple valid solutions exist; store both
   - HUMAN_REQUIRED      — insufficient evidence; defer to user

7. Tradeoff Preservation Policy — consensus does not require elimination of
   all disagreement.  If multiple architectural options are valid, store
   them.  Do not discard them.  Do not force agreement.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from cerebras_client import generate

# ---------------------------------------------------------------------------
# Outcome types
# ---------------------------------------------------------------------------


class OutcomeType:
    """The three possible outcomes from a verifier review of a stagnant dispute."""

    VERIFIED_DECISION = "VERIFIED_DECISION"
    """A superior solution exists. Store decision. Remove from active queue."""

    VERIFIED_TRADEOFF = "VERIFIED_TRADEOFF"
    """Multiple valid solutions exist. Store both with recommendation."""

    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    """Insufficient evidence. Verifier cannot distinguish options. Risk is significant."""


# ---------------------------------------------------------------------------
# Governed outcome — the structured result of a verifier review
# ---------------------------------------------------------------------------


@dataclass
class GovernedOutcome:
    """Structured result of a verifier review over a stagnant or deadlocked dispute."""

    outcome_type: str  # VERIFIED_DECISION | VERIFIED_TRADEOFF | HUMAN_REQUIRED
    dispute_key: str
    dispute_text: str

    # For VERIFIED_DECISION
    decision: str | None = None

    # For VERIFIED_TRADEOFF
    option_a: str | None = None
    option_b: str | None = None
    recommendation: str | None = None

    # Shared
    confidence: int = 50
    reasoning: str = ""

    # Five-dimension evaluation
    evidence_quality: str = "unassessed"
    architectural_validity: str = "unassessed"
    implementation_impact: str = "unassessed"
    operational_impact: str = "unassessed"
    scalability_impact: str = "unassessed"

    def to_dict(self) -> dict:
        return {
            "outcome_type": self.outcome_type,
            "dispute_key": self.dispute_key,
            "dispute_text": self.dispute_text,
            "decision": self.decision,
            "option_a": self.option_a,
            "option_b": self.option_b,
            "recommendation": self.recommendation,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "evidence_quality": self.evidence_quality,
            "architectural_validity": self.architectural_validity,
            "implementation_impact": self.implementation_impact,
            "operational_impact": self.operational_impact,
            "scalability_impact": self.scalability_impact,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GovernedOutcome":
        return cls(
            outcome_type=data.get("outcome_type", OutcomeType.HUMAN_REQUIRED),
            dispute_key=data.get("dispute_key", ""),
            dispute_text=data.get("dispute_text", ""),
            decision=data.get("decision"),
            option_a=data.get("option_a"),
            option_b=data.get("option_b"),
            recommendation=data.get("recommendation"),
            confidence=data.get("confidence", 50),
            reasoning=data.get("reasoning", ""),
            evidence_quality=data.get("evidence_quality", "unassessed"),
            architectural_validity=data.get("architectural_validity", "unassessed"),
            implementation_impact=data.get("implementation_impact", "unassessed"),
            operational_impact=data.get("operational_impact", "unassessed"),
            scalability_impact=data.get("scalability_impact", "unassessed"),
        )




def print_human_required(outcome: "GovernedOutcome") -> None:
    print("=" * 50)
    print("HUMAN DECISION REQUIRED")
    print("=" * 23)
    print(f"Question: {outcome.dispute_text}")
    print()
    print("Option A:")
    print(outcome.option_a or "")
    print()
    print("Option B:")
    print(outcome.option_b or "")
    print()
    print(f"Reasoning: {outcome.reasoning}")
    print()
    print(f"Confidence: {outcome.confidence}")
    print("=" * 50)


# ---------------------------------------------------------------------------
# Information state — tracks what has been introduced in a debate
# ---------------------------------------------------------------------------


def _hash_text(text: str) -> str:
    """Produce a short content hash for deduplication of claims/evidence."""
    return hashlib.sha256(text.encode()).hexdigest()[:12]


@dataclass
class InformationState:
    """Tracks semantic novelty across debate rounds.

    A round is only useful if it introduces:
    - new evidence
    - new reasoning
    - new constraints
    - new tradeoffs
    - new architectural implications

    Repeating previously stated positions does not count as progress.
    """

    claims_seen: set[str] = field(default_factory=set)
    evidence_seen: set[str] = field(default_factory=set)
    rounds_since_novelty: int = 0
    last_gpt_position: str = ""
    last_gemini_position: str = ""
    total_rounds_tracked: int = 0

    def record_round(self, gpt_position: str, gemini_position: str) -> dict:
        """Record a debate round. Returns dict with novelty indicators."""
        self.total_rounds_tracked += 1

        # Extract claims from both positions
        gpt_claims = _extract_claims(gpt_position)
        gemini_claims = _extract_claims(gemini_position)

        new_gpt = [c for c in gpt_claims if _hash_text(c) not in self.claims_seen]
        new_gemini = [c for c in gemini_claims if _hash_text(c) not in self.claims_seen]

        for c in gpt_claims:
            self.claims_seen.add(_hash_text(c))
        for c in gemini_claims:
            self.claims_seen.add(_hash_text(c))

        # Check if positions materially changed from last round
        gpt_changed = gpt_position != self.last_gpt_position
        gemini_changed = gemini_position != self.last_gemini_position

        novelty_count = len(new_gpt) + len(new_gemini)

        if novelty_count == 0 and not gpt_changed and not gemini_changed:
            self.rounds_since_novelty += 1
        else:
            self.rounds_since_novelty = 0

        self.last_gpt_position = gpt_position
        self.last_gemini_position = gemini_position

        return {
            "new_claims": novelty_count,
            "gpt_changed": gpt_changed,
            "gemini_changed": gemini_changed,
            "rounds_stale": self.rounds_since_novelty,
        }

    def is_stagnating(self, threshold: int = 3) -> bool:
        """True if no novel information has been introduced for `threshold` rounds."""
        return self.rounds_since_novelty >= threshold

    def to_dict(self) -> dict:
        return {
            "claims_count": len(self.claims_seen),
            "evidence_count": len(self.evidence_seen),
            "rounds_since_novelty": self.rounds_since_novelty,
            "total_rounds_tracked": self.total_rounds_tracked,
        }


def _extract_claims(text: str) -> list[str]:
    """Heuristic claim extraction: split on sentence boundaries and filter noise.

    This is a lightweight local extraction.  The heavy semantic comparison
    is done by Cerebras during verifier review, not here.
    """
    if not text:
        return []
    sentences = re.split(r"[.!\n]+", text)
    claims = []
    for s in sentences:
        s = s.strip()
        # Filter trivial fragments
        if len(s) > 30 and any(kw in s.lower() for kw in [
            "use", "using", "should", "must", "requires", "recommend",
            "better", "worse", "because", "therefore", "however",
            "supports", "enables", "prevents", "scales", "secure",
            "database", "storage", "architecture", "pattern", "deploy",
        ]):
            claims.append(s[:300])
    return claims


# ---------------------------------------------------------------------------
# Verifier Review — Cerebras-powered stagnation resolution
# ---------------------------------------------------------------------------


VERIFIER_REVIEW_PROMPT = """
You are the Debate Deadlock Governance Verifier.

Two AI models (GPT and Gemini) have debated an architectural dispute
and have reached STAGNATION — multiple rounds have occurred without
meaningful information change.  Your task is to evaluate the dispute
and produce a governed outcome.

## Evaluation Dimensions

Evaluate the dispute across FIVE dimensions:

1. **evidence_quality** — How strong is the evidence supporting each position?
   Rate: strong | moderate | weak | absent

2. **architectural_validity** — How well does each option satisfy architectural
   requirements (consistency, availability, partition tolerance, coupling,
   cohesion, etc.)?
   Rate: high | moderate | low | unsuitable

3. **implementation_impact** — What is the cost/complexity of implementing each
   option?
   Rate: low | moderate | high | prohibitive

4. **operational_impact** — What is the ongoing operational burden of each
   option (maintenance, monitoring, debugging, onboarding)?
   Rate: low | moderate | high | prohibitive

5. **scalability_impact** — How well does each option scale with growth
   (data, users, transactions, team size)?
   Rate: strong | moderate | limited | unknown

## Outcome Rules

- If one option is clearly superior across most dimensions →
  outcome_type = "VERIFIED_DECISION"
- If both options are valid with different tradeoffs →
  outcome_type = "VERIFIED_TRADEOFF"
- If evidence is insufficient to distinguish and the risk is significant →
  outcome_type = "HUMAN_REQUIRED"

## Tradeoff Preservation Policy

Consensus does NOT require elimination of all disagreement.
If multiple architectural options are valid:
  - Store them.
  - Do NOT discard them.
  - Do NOT force agreement.
  - Provide a reasoned recommendation and confidence level.

## Output Format

Return VALID JSON ONLY matching this schema:

{{
  "outcome_type": "VERIFIED_DECISION|VERIFIED_TRADEOFF|HUMAN_REQUIRED",
  "dispute_key": "normalized_key",
  "dispute_text": "original dispute text",
  "decision": "chosen option (for VERIFIED_DECISION)",
  "option_a": "first valid option description",
  "option_b": "second valid option description",
  "recommendation": "which option is recommended and why",
  "confidence": 0-100,
  "reasoning": "detailed explanation of the evaluation",
  "evidence_quality": "strong|moderate|weak|absent",
  "architectural_validity": "high|moderate|low|unsuitable",
  "implementation_impact": "low|moderate|high|prohibitive",
  "operational_impact": "low|moderate|high|prohibitive",
  "scalability_impact": "strong|moderate|limited|unknown"
}}

## DISPUTE

{dispute_text}

## GPT POSITION

{gpt_position}

## GEMINI POSITION

{gemini_position}

## DEBATE HISTORY

Rounds completed: {total_rounds}
Rounds without new information: {stale_rounds}
Total unique claims tracked: {claims_count}

Return JSON only.
"""


def verifier_review(
    dispute_text: str,
    dispute_key: str,
    gpt_position: str,
    gemini_position: str,
    information_state: InformationState | None = None,
) -> GovernedOutcome:
    """Perform a governed verifier review of a stagnant dispute.

    Args:
        dispute_text: The original dispute description.
        dispute_key: Normalized key for the dispute.
        gpt_position: GPT's most recent position.
        gemini_position: Gemini's most recent position.
        information_state: Optional InformationState tracking the debate history.

    Returns:
        A GovernedOutcome with the verifier's determination and five-dimension
        evaluation.
    """
    info = information_state or InformationState()

    prompt = VERIFIER_REVIEW_PROMPT.format(
        dispute_text=dispute_text,
        gpt_position=gpt_position[:5000],
        gemini_position=gemini_position[:5000],
        total_rounds=info.total_rounds_tracked,
        stale_rounds=info.rounds_since_novelty,
        claims_count=len(info.claims_seen),
    )

    try:
        raw = generate(prompt, module="governance_verifier")
        result = _extract_json(raw)
    except Exception as e:
        print(f"[GOVERNANCE VERIFIER ERROR] {e}")
        # Fallback: human required when the verifier itself fails
        outcome = GovernedOutcome(
            outcome_type=OutcomeType.HUMAN_REQUIRED,
            dispute_key=dispute_key,
            dispute_text=dispute_text,
            confidence=0,
            reasoning=f"Verifier inference failed: {e}",
        )
        print_human_required(outcome)
        return outcome

    if not isinstance(result, dict):
        outcome = GovernedOutcome(
            outcome_type=OutcomeType.HUMAN_REQUIRED,
            dispute_key=dispute_key,
            dispute_text=dispute_text,
            confidence=0,
            reasoning="Verifier returned unparseable response.",
        )
        print_human_required(outcome)
        return outcome

    outcome_type = result.get("outcome_type", OutcomeType.HUMAN_REQUIRED)
    # Normalize to known types
    if outcome_type not in (
        OutcomeType.VERIFIED_DECISION,
        OutcomeType.VERIFIED_TRADEOFF,
        OutcomeType.HUMAN_REQUIRED,
    ):
        outcome_type = OutcomeType.HUMAN_REQUIRED

    try:
        confidence = int(result.get("confidence", 50))
    except (ValueError, TypeError):
        confidence = 50

    outcome = GovernedOutcome(
        outcome_type=outcome_type,
        dispute_key=dispute_key,
        dispute_text=dispute_text,
        decision=result.get("decision") if outcome_type == OutcomeType.VERIFIED_DECISION else None,
        option_a=result.get("option_a"),
        option_b=result.get("option_b"),
        recommendation=result.get("recommendation"),
        confidence=confidence,
        reasoning=result.get("reasoning", ""),
        evidence_quality=result.get("evidence_quality", "unassessed"),
        architectural_validity=result.get("architectural_validity", "unassessed"),
        implementation_impact=result.get("implementation_impact", "unassessed"),
        operational_impact=result.get("operational_impact", "unassessed"),
        scalability_impact=result.get("scalability_impact", "unassessed"),
    )
    if outcome.outcome_type == OutcomeType.HUMAN_REQUIRED:
        print_human_required(outcome)
    return outcome


# ---------------------------------------------------------------------------
# JSON extraction helper
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> Any:
    """Extract a JSON object or array from text, tolerant of surrounding noise."""
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
                        return json.loads(text[start : i + 1])
                    except Exception:
                        break
    return None


# ---------------------------------------------------------------------------
# Governance result aggregator — for the final synthesis
# ---------------------------------------------------------------------------


@dataclass
class GovernanceResult:
    """Aggregates all governed outcomes from a debate session."""

    verified_decisions: list[GovernedOutcome] = field(default_factory=list)
    verified_tradeoffs: list[GovernedOutcome] = field(default_factory=list)
    human_required: list[GovernedOutcome] = field(default_factory=list)

    def add(self, outcome: GovernedOutcome):
        if outcome.outcome_type == OutcomeType.VERIFIED_DECISION:
            self.verified_decisions.append(outcome)
        elif outcome.outcome_type == OutcomeType.VERIFIED_TRADEOFF:
            self.verified_tradeoffs.append(outcome)
        else:
            self.human_required.append(outcome)

    def to_dict(self) -> dict:
        return {
            "verified_decisions": [o.to_dict() for o in self.verified_decisions],
            "verified_tradeoffs": [o.to_dict() for o in self.verified_tradeoffs],
            "human_required": [o.to_dict() for o in self.human_required],
        }

    @property
    def total(self) -> int:
        return len(self.verified_decisions) + len(self.verified_tradeoffs) + len(self.human_required)