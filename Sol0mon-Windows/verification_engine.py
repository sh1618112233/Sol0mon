import json
import re
from datetime import datetime, timezone

from cerebras_client import generate

# Phase 0: Canonical Decision Model. The verifier persists a structured
# `rejected_options` field alongside each finding so downstream consumers
# (Architecture Renderer, Build Spec Engine, ...) read the rejected options
# from a single structured field instead of parsing finding prose. The
# derivation fallback lives in decision_model and is shared with the accessor
# so legacy answers (pre-dating the structured field) and new answers produce
# identical results.
from decision_model import derive_rejected_options

# Hard architectural resource constraints
MAX_RESEARCH_ESCALATIONS = 3  # Fix 2: Cap high-overhead research passes per session
MAX_BATCH_DISPUTES = 15       # Fix 4: Hard limit to shield verifier prompts from token bloat


def extract_json(text):
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass

    obj_start = text.find("{")
    arr_start = text.find("[")

    if obj_start == -1 and arr_start == -1:
        return None

    if (obj_start != -1 and obj_start < arr_start) or arr_start == -1:
        start_idx = obj_start
        open_char, close_char = "{", "}"
    else:
        start_idx = arr_start
        open_char, close_char = "[", "]"

    depth = 0
    for i in range(start_idx, len(text)):
        if text[i] == open_char:
            depth += 1
        elif text[i] == close_char:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start_idx:i + 1])
                except Exception:
                    break
    return None


def extract_relevant_evidence(dispute, position_text, window_size=800):
    """Slices localized context snippets right around technical points of friction."""
    if not position_text or not dispute:
        return "No text available for verification."

    stop_words = {'vs', 'and', 'the', 'proposes', 'uses', 'utilizes', 'for', 'system', 'architecture'}
    keywords = [w.strip(":,()\"'.") for w in dispute.lower().split() if len(w) > 2 and w not in stop_words]

    if not keywords:
        return position_text[:window_size]

    text_lower = position_text.lower()
    best_index = 0
    max_matches = -1

    step = 100
    for i in range(0, len(position_text), step):
        chunk = text_lower[i:i + window_size]
        matches = sum(1 for kw in keywords if kw in chunk)
        if matches > max_matches:
            max_matches = matches
            best_index = i

    start = max(0, best_index - 100)
    end = min(len(position_text), start + window_size)
    return position_text[start:end].strip()


# ---------------------------------------------------------------------------
# Research-Driven Verification — wiring helpers (pure functions, no I/O).
#
# The trigger lives in verify_all_disputes; these helpers normalize research
# output, propagate the persisted evidence onto dispute dicts for workspace
# resume, and surface a pre-emitted HUMAN_REQUIRED outcome to consumers so
# they don't re-run a model verifier on a dispute the research budget already
# exhausted.
# ---------------------------------------------------------------------------

def _normalize_research_output(output):
    """Accept either a markdown string (research_runner.run_research's return
    shape) or a dict (verification_research_v2.research_claim's {"claim",
    "sources"} shape) and return (summary, sources) for the evidence record."""
    if output is None:
        return "", []
    if isinstance(output, dict):
        sources = output.get("sources") or []
        if not isinstance(sources, list):
            sources = [sources] if sources else []
        summary = (
            output.get("summary")
            or output.get("claim")
            or (json.dumps(output)[:2000])
        )
        return str(summary), sources
    return str(output), []


def _persist_rejected_options(res: dict) -> list[str]:
    """Phase 0 helper: resolve the canonical `rejected_options` field for a
    verifier inference result. Prefer the LLM's structured `rejected_options`;
    fall back to the shared deterministic derivation
    (`decision_model.derive_rejected_options`) over the finding so legacy
    Answers (and any inference result the LLM emits without the field) still
    carry the structured field. Pure; no model call. Returned in canonical
    lowercase so the field is directly comparable to alias tables."""
    if isinstance(res, dict):
        explicit = res.get("rejected_options")
        if isinstance(explicit, list) and explicit:
            return [str(t).strip().lower() for t in explicit if t]
    return derive_rejected_options(str(res.get("finding", "")) if isinstance(res, dict) else "")


def _default_research_fn(topic):
    """Default research entry point: verification_research_v2.research_claim
    (the browser-based research module). Imported lazily so the engine never
    imports the browser stack (Playwright) unless research is actually
    invoked. Returns ``{"claim": topic, "sources": [...]}``; the caller's
    ``_normalize_research_output`` handles this dict shape."""
    from verification_research_v2 import research_claim
    return research_claim(topic)


def attach_research_evidence(dispute_dicts, verification_result):
    """Copy the matching research-evidence record onto each dispute dict (by
    normalized_key) so it rides along the existing workspace persistence
    (ws.resolved_disputes is a JSON-TEXT column). No new storage system; pure
    function, no I/O. Returns a new list of dicts (the inputs are not mutated)."""
    evidence_list = (verification_result or {}).get("research_evidence") or []
    by_key = {
        e.get("dispute_key"): e
        for e in evidence_list
        if isinstance(e, dict) and e.get("dispute_key")
    }
    out = []
    for d in dispute_dicts:
        if not isinstance(d, dict):
            out.append(d)
            continue
        nk = d.get("normalized_key") or d.get("dispute_key") or ""
        ev = by_key.get(nk)
        if ev is None:
            out.append(d)
        else:
            enriched = dict(d)
            enriched["research_evidence"] = ev
            out.append(enriched)
    return out


def extract_pre_emitted_outcome(verification_result, normalized_key):
    """Return the pre-emitted governance_outcome dict for a dispute (if the
    verifier already produced one — e.g. research budget exhausted ->
    HUMAN_REQUIRED), else None. Lets consumers honor the verdict WITHOUT a
    fresh verifier_review (which would be a model call and would overwrite the
    'Research budget exhausted' reason with a generic verdict)."""
    if not normalized_key:
        return None
    vr = verification_result or {}
    for bucket in ("unresolved_claims", "resolved_claims", "equivalent_claims"):
        for c in vr.get(bucket, []) or []:
            if not isinstance(c, dict):
                continue
            if c.get("normalized_key") != normalized_key:
                continue
            go = c.get("governance_outcome")
            if isinstance(go, dict) and go.get("outcome_type"):
                return go
    return None


def run_verification_batch_inference(question, batch_payload):
    """Executes a single consolidated evaluation pass over all active target frames."""
    prompt = f"""
You are an expert Architectural Verification Engine.
Analyze the following batch of structural disputes against their corresponding localized text segments.

Your task is to classify each dispute precisely as:
- "RESOLVED": Logically settled or unified down to a convergent design decision.
- "UNRESOLVED": A valid, active architectural contradiction remains.
- "EQUIVALENT": Artificial friction representing identical concepts with variant naming.

If a dispute cannot be evaluated without external benchmarks or specifications, set "requires_external_evidence" to true.

Return a JSON array containing objects matching this schema exactly:
[
  {{
    "normalized_key": "Matching normalized_key string",
    "status": "RESOLVED|UNRESOLVED|EQUIVALENT",
    "confidence": 0-100,
    "finding": "Clear analytical breakdown of conflict settlement status",
    "requires_external_evidence": false,
    "rejected_options": ["the concrete option name(s) the verifier ruled out - e.g. the losing option in an 'X vs Y' dispute. Empty list if the dispute was a unification rather than a selection."]
  }}
]

QUESTION
{question}

BATCH DATA STRIPS
{json.dumps(batch_payload, indent=2)}
"""
    try:
        raw = generate(prompt, module="verification_engine")
        return extract_json(raw)
    except Exception as e:
        print(f"[BATCH VERIFIER CRITICAL ERROR] Execution aborted: {e}")
        return None


def verify_all_disputes(
    question,
    validated_disputes,
    gpt_position,
    gemini_position,
    use_external_research=False,
    research_fn=None,
    max_research_escalations=MAX_RESEARCH_ESCALATIONS,
    prior_research_evidence=None,
):
    """Verify a batch of disputes. Pure heuristic + injected inference.

    Research-Driven Verification wiring (the audit's missing piece):
      - When a dispute's verdict sets requires_external_evidence=True, run
        research (research_fn, defaulting to verification_research_v2.research_claim) and
        re-evaluate that dispute with the research injected as external_evidence.
      - Research evidence is persisted in the returned verification_result under
        "research_evidence" (list of {dispute_key, normalized_key, sources,
        summary, timestamp}).
      - Phase 5 (resume reuse): when ``prior_research_evidence`` is provided
        (a list of evidence dicts from a prior verification round), disputes
        whose key already has evidence SKIP the research call and reuse the
        stored evidence — no repeated model/API cost, no escalation increment.
      - When the research budget is exhausted before a dispute could be resolved,
        DO NOT silently continue: emit a HUMAN_REQUIRED governance_outcome (with
        ``reason_code="research_budget_exhausted"``) on the record so consumers
        can surface it without inventing certainty.
      - Every research-assisted record carries the Phase 5/7 audit trail:
        used_research / research_dispute_key / research_summary /
        research_source_count.

    ``use_external_research`` is retained for backward-compat (existing callers
    still pass False) but no longer gates research — the trigger is the
    verifier's own requires_external_evidence flag, exactly as the audit
    requires. ``research_fn`` is a seam: tests inject an offline fake; production
    leaves it None and the default verification_research_v2.research_claim is
    used.
    """
    if not validated_disputes:
        return {
            "resolved_claims": [], "equivalent_claims": [],
            "unresolved_claims": [], "verification_score": 100,
            "truncated_count": 0, "research_evidence": [],
            "research_escalation_count": 0, "research_budget_exhausted": False,
        }

    # Phase 5: index prior research evidence by key for resume reuse.
    prior_by_key = {}
    for ev in (prior_research_evidence or []):
        if isinstance(ev, dict):
            key = ev.get("normalized_key") or ev.get("dispute_key")
            if key:
                prior_by_key[key] = ev

    # M10: Track truncation for honest audit trail
    original_count = len(validated_disputes)
    truncated_count = 0
    if len(validated_disputes) > MAX_BATCH_DISPUTES:
        truncated_count = len(validated_disputes) - MAX_BATCH_DISPUTES
        print(f"[VERIFIER WINDOW CEILING] Slicing evaluation set down to top {MAX_BATCH_DISPUTES} items. "
              f"{truncated_count} disputes are UNVERIFIED.")
        validated_disputes = validated_disputes[:MAX_BATCH_DISPUTES]

    batch_payload = []
    dispute_map = {}  

    for item in validated_disputes:
        if not isinstance(item, dict):
            continue
             
        dispute = item.get("dispute", "")
        norm_key = item.get("normalized_key", "")
        if not dispute or not norm_key:
            continue
            
        dispute_map[norm_key] = item
        
        batch_payload.append({
            "dispute": dispute,
            "normalized_key": norm_key,
            "gpt_evidence": extract_relevant_evidence(dispute, gpt_position),
            "gemini_evidence": extract_relevant_evidence(dispute, gemini_position),
            "external_evidence": "External research bypassed for initial context sweep."
        })

    inference_results = run_verification_batch_inference(question, batch_payload)
    if not inference_results:
        inference_results = [
            {"normalized_key": k, "status": "UNRESOLVED", "confidence": 50, "finding": "Inference network error fallback", "requires_external_evidence": False}
            for k in dispute_map.keys()
        ]

    re_evaluate_batch = []
    final_processed_records = {}
    research_evidence = []  # Phase 2: persisted inside verification_result
    researched_keys = set()  # keys that received research (for the Phase 5 audit trail)
    research_escalation_count = 0  # Fix 2: bounded research pass counter

    resolver = research_fn if research_fn is not None else _default_research_fn

    for res in inference_results:
        if not isinstance(res, dict):
            continue
        norm_key = res.get("normalized_key")
        if not norm_key or norm_key not in dispute_map:
            continue

        raw_item = dispute_map[norm_key]
        dispute_text = raw_item.get("dispute", "")
        needs_research = bool(res.get("requires_external_evidence"))
        budget_available = research_escalation_count < max_research_escalations
        prior_ev = prior_by_key.get(norm_key)

        if needs_research and prior_ev:
            # Phase 5 (resume reuse): prior research evidence exists for this
            # dispute — skip the research call and reuse the stored evidence.
            # No escalation count increment, no repeated model/API cost.
            print(f"[RESEARCH REUSED] {norm_key}: prior evidence restored from "
                  f"resume.")
            summary = prior_ev.get("summary", "")
            sources = prior_ev.get("sources") or []
            evidence_record = {
                "dispute_key": norm_key,
                "normalized_key": norm_key,
                "sources": sources,
                "summary": summary,
                "timestamp": prior_ev.get("timestamp", ""),
            }
            research_evidence.append(evidence_record)
            researched_keys.add(norm_key)

            re_evaluate_batch.append({
                "dispute": dispute_text,
                "normalized_key": norm_key,
                "gpt_evidence": extract_relevant_evidence(dispute_text, gpt_position),
                "gemini_evidence": extract_relevant_evidence(dispute_text, gemini_position),
                "external_evidence": summary,  # Phase 3: verifier receives evidence
                "research_sources": sources,
            })
        elif needs_research and budget_available:
            research_escalation_count += 1
            print(f"[JIT RESEARCH TRIGGER #{research_escalation_count}] "
                  f"Resolving with research for: {norm_key}")
            try:
                research_output = resolver(dispute_text)
                summary, sources = _normalize_research_output(research_output)
            except Exception as e:
                summary, sources = f"Research execution fault: {e}", []

            evidence_record = {
                "dispute_key": norm_key,
                "normalized_key": norm_key,
                "sources": sources,
                "summary": summary,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            research_evidence.append(evidence_record)
            researched_keys.add(norm_key)

            re_evaluate_batch.append({
                "dispute": dispute_text,
                "normalized_key": norm_key,
                "gpt_evidence": extract_relevant_evidence(dispute_text, gpt_position),
                "gemini_evidence": extract_relevant_evidence(dispute_text, gemini_position),
                "external_evidence": summary,  # Phase 3: verifier receives evidence
                "research_sources": sources,
            })
        elif needs_research and not budget_available:
            # Phase 4: research budget exhausted -> HUMAN_REQUIRED. No silent
            # continuation, no artificial certainty. The carried
            # governance_outcome lets consumers honor the verdict without a
            # fresh (model-calling) verifier_review.
            print(f"[RESEARCH EXHAUSTED] {norm_key}: budget reached "
                  f"({research_escalation_count}/{max_research_escalations}). "
                  f"Emitting HUMAN_REQUIRED.")
            final_processed_records[norm_key] = {
                "dispute": dispute_text,
                "normalized_key": norm_key,
                "verification": {
                    "status": "UNRESOLVED",
                    "confidence": 0,
                    "finding": "Research budget exhausted before dispute could be resolved.",
                    "requires_external_evidence": True,
                },
                "governance_outcome": {
                    "outcome_type": "HUMAN_REQUIRED",
                    "dispute_key": norm_key,
                    "dispute_text": dispute_text,
                    "reason": "Research budget exhausted before dispute could be resolved.",
                    "reason_code": "research_budget_exhausted",
                    "confidence": 0,
                },
                "used_research": False,
                "research_dispute_key": norm_key,
                "research_summary": "",
                "research_source_count": 0,
            }
        else:
            # Enforce schema integrity bounds on batched arrays immediately
            status = str(res.get("status", "UNRESOLVED")).upper()
            if status not in {"RESOLVED", "UNRESOLVED", "EQUIVALENT"}:
                status = "UNRESOLVED"
            try:
                confidence = int(res.get("confidence", 0))
            except Exception:
                confidence = 0

            final_processed_records[norm_key] = {
                "dispute": dispute_text,
                "normalized_key": norm_key,
                "verification": {
                    "status": status,
                    "confidence": confidence,
                    "finding": str(res.get("finding", "")),
                    "requires_external_evidence": bool(res.get("requires_external_evidence", False)),
                    # Phase 0: Canonical Decision Model - persist the structured
                    # rejected_options field so consumers never parse finding
                    # prose to discover what the verifier ruled out. Prefer the
                    # LLM's structured output; fall back to the shared deterministic
                    # derivation (decision_model.derive_rejected_options) so
                    # legacy and new Answers converge on identical tokens.
                    "rejected_options": _persist_rejected_options(res),
                },
                "used_research": False,
                "research_dispute_key": norm_key,
                "research_summary": "",
                "research_source_count": 0,
            }

    if re_evaluate_batch:
        print(f"[VERIFIER] Executing Pass 2 over {len(re_evaluate_batch)} escalated research vectors...")
        pass2_results = run_verification_batch_inference(question, re_evaluate_batch)
        if pass2_results:
            for res in pass2_results:
                if not isinstance(res, dict):
                    continue
                norm_key = res.get("normalized_key")
                if norm_key in dispute_map:
                    status = str(res.get("status", "UNRESOLVED")).upper()
                    if status not in {"RESOLVED", "UNRESOLVED", "EQUIVALENT"}:
                        status = "UNRESOLVED"
                    try:
                        confidence = int(res.get("confidence", 0))
                    except Exception:
                        confidence = 0

                    # Phase 5/7 audit trail: research-assisted decisions are
                    # marked so they are visible during audits.
                    summary_for_key = ""
                    sources_for_key = []
                    for ev in research_evidence:
                        if ev.get("dispute_key") == norm_key or ev.get("normalized_key") == norm_key:
                            summary_for_key = ev.get("summary", "")
                            sources_for_key = ev.get("sources") or []
                            break

                    final_processed_records[norm_key] = {
                        "dispute": dispute_map[norm_key].get("dispute"),
                        "normalized_key": norm_key,
                        "verification": {
                            "status": status,
                            "confidence": confidence,
                            "finding": str(res.get("finding", "")),
                            "requires_external_evidence": bool(res.get("requires_external_evidence", False)),
                            # Phase 0: Canonical Decision Model - same structured
                            # rejected_options field, same fallback, as the primary
                            # path above. Two storage sites, one source of truth.
                            "rejected_options": _persist_rejected_options(res),
                        },
                        "used_research": norm_key in researched_keys,
                        "research_dispute_key": norm_key if norm_key in researched_keys else "",
                        "research_summary": summary_for_key if norm_key in researched_keys else "",
                        "research_source_count": len(sources_for_key) if norm_key in researched_keys else 0,
                    }

    for norm_key, raw_item in dispute_map.items():
        if norm_key not in final_processed_records:
            final_processed_records[norm_key] = {
                "dispute": raw_item.get("dispute"),
                "normalized_key": norm_key,
                "verification": {"status": "UNRESOLVED", "confidence": 40, "finding": "Incomplete pipeline sequence mapping", "requires_external_evidence": False},
                "used_research": False,
                "research_dispute_key": norm_key,
                "research_summary": "",
                "research_source_count": 0,
            }

    resolved_claims = []
    unresolved_claims = []
    equivalent_claims = []

    for record in final_processed_records.values():
        status = record["verification"].get("status", "UNRESOLVED")
        if status == "RESOLVED":
            resolved_claims.append(record)
        elif status == "EQUIVALENT":
            equivalent_claims.append(record)
        else:
            unresolved_claims.append(record)

    total = len(resolved_claims) + len(unresolved_claims) + len(equivalent_claims)
    
    if total == 0:
        verification_score = 100
    else:
        verification_score = int(((len(resolved_claims) + len(equivalent_claims)) / total * 100))

    research_budget_exhausted = (
        research_escalation_count >= max_research_escalations
        and any(r.get("verification", {}).get("requires_external_evidence")
                and not r.get("used_research")
                for r in unresolved_claims)
    )

    print(f"[VERIFIER COMPLETED] Resolved={len(resolved_claims)} Equivalent={len(equivalent_claims)} "
          f"Unresolved={len(unresolved_claims)} Balanced Score={verification_score}% "
          f"ResearchEscalations={research_escalation_count}/{max_research_escalations}")

    return {
        "resolved_claims": resolved_claims,
        "equivalent_claims": equivalent_claims,
        "unresolved_claims": unresolved_claims,
        "verification_score": verification_score,
        "truncated_count": truncated_count,  # M10: honest audit trail
        "research_evidence": research_evidence,  # Phase 2: persisted in verification_result
        "research_escalation_count": research_escalation_count,
        "research_budget_exhausted": research_budget_exhausted,
    }


if __name__ == "__main__":
    sample = [
        {
            "dispute": "Kubernetes orchestration versus decentralized runtime architecture",
            "normalized_key": "deployment_architecture"
        }
    ]

    result = verify_all_disputes(
        question="Design a planet-scale AI platform",
        validated_disputes=sample,
        gpt_position="Use Kubernetes.",
        gemini_position="Use decentralized runtime.",
        use_external_research=False
    )
    print(json.dumps(result, indent=4))
