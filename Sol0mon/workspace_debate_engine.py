"""
Workspace Debate Engine — workspace-aware consensus pipeline.

Extends DebateEngine with workspace lifecycle management:
- Workspace creation and persistence during debate
- Follow-up question support with targeted resumption
- Dispute reopening based on affected disputes analysis
- Human-required item tracking and resolution
- Partial debate resumption (only re-debates affected disputes)

Does NOT replace DebateEngine — wraps it for backward compatibility.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from debate_engine import (
    DebateEngine, Dispute, ArchitectureMemory,
    _classify_priority, _extract_json, _clean, _constrain_prompt,
    _eval_consensus, _eval_validate, _evidence_window, _detect_stagnation,
    CONSENSUS_THRESHOLD, MAX_ROUNDS_PER_DISPUTE, MAX_MEMORY_CHARS,
    STAGNATION_CHECKS, STAGNATION_MAX_WARNINGS, MAX_VERIFICATION_RETRIES,
    MAX_CONSTRAINTS, PRIORITY_WEIGHTS, DOMAIN_PRIORITY,
)
from answer_memory import memory
from verification_engine import (
    verify_all_disputes,
    attach_research_evidence,
    extract_pre_emitted_outcome,
)
from governance import (
    OutcomeType, GovernedOutcome, InformationState,
    GovernanceResult, verifier_review,
)
from workspace_manager import (
    Workspace, WorkspaceStatus, workspace_mgr,
)
from workspace_impact_analyzer import analyze_followup_impact, audit_entry
from research_planner import (
    plan_research, collect_research, format_research_for_prompt,
    format_research_for_dispute, build_consistency_audit_context,
    merge_evidence, audit_record,
)
from human_decision_queue import HumanDecisionQueue


# ---------------------------------------------------------------------------
# Research V2 helper — inject the evidence RELEVANT to one dispute into the
# constrained-dispute prompt. Empty block (no relevant evidence) leaves the
# intro identical to the plain _constrain_prompt output, so callers with no
# research evidence and existing regression tests are unaffected. Pure
# deterministic; no model/browser calls. (ponytail: wrap _constrain_prompt,
# do NOT change its shared signature/behavior.)
# ---------------------------------------------------------------------------
def _constrain_prompt_with_research(
    dispute_text: str,
    constraints: str = "",
    research_evidence: list | None = None,
    normalized_key: str = "",
) -> str:
    intro = _constrain_prompt(dispute_text, constraints)
    block = format_research_for_dispute(
        research_evidence, dispute_text, normalized_key)
    if block:
        intro = f"{block}\n\n{intro}"
    return intro


# ---------------------------------------------------------------------------
# WorkspaceDebateEngine
# ---------------------------------------------------------------------------

class WorkspaceDebateEngine(DebateEngine):
    """DebateEngine extended with workspace lifecycle management.

    Provides:
    - run_in_workspace(): First-time prompt → workspace creation → debate → persist
    - resume_workspace(): Follow-up question → load workspace → affected-dispute analysis
                          → targeted re-debate → persist updated state
    - reopen_dispute(): Reopen a specific resolved dispute within a workspace
    """

    # Pre-debate research seam. None -> lazy browser default
    # (research_planner.default_research_fn, graceful offline). Tests inject
    # a fake to run fully offline and to assert no-model-calls.
    _pre_debate_research_fn = None

    # ------------------------------------------------------------------
    # Primary entry point: create and run a workspace from scratch
    # ------------------------------------------------------------------
    def run_in_workspace(
        self,
        prompt: str,
        memory_ids: list | None = None,
        workspace_id: str | None = None,
    ) -> dict:
        """Run the full pipeline in a workspace context.

        Workspace is created at start and persisted after completion.
        If workspace_id is provided, resumes an existing workspace
        (performs full re-run if no prior positions exist).
        """
        from gpt_worker import GPTSession
        from gemini_worker import GeminiSession

        # Load or create workspace
        ws = None
        if workspace_id:
            ws = workspace_mgr.load_workspace(workspace_id)
            if ws is None:
                return {"_failed": True, "status": "workspace_not_found",
                        "error": f"Workspace {workspace_id} not found"}

        if ws is None:
            ws = Workspace(
                workspace_id=workspace_mgr.create_workspace(prompt, memory_ids),
                original_prompt=prompt,
            )
            ws.status = WorkspaceStatus.CREATED

        ws.status = WorkspaceStatus.DEBATING
        workspace_mgr.save_workspace(ws)

        gpt, gemini = GPTSession(), GeminiSession()
        gpt.start()
        gemini.start()

        try:
            return self._run_in_workspace(prompt, gpt, gemini, ws, memory_ids)
        finally:
            gpt.close()
            gemini.close()

    # ------------------------------------------------------------------
    # Follow-up entry point: resume a workspace with a new question
    # ------------------------------------------------------------------
    def resume_workspace(self, workspace_id: str, follow_up: str) -> dict:
        """Process a follow-up question against an existing workspace.

        Key behaviors:
        - Loads workspace state (decisions, tradeoffs, constraints, positions)
        - Analyzes the follow-up to determine which disputes are affected
        - Injects prior constraints as immutable context
        - Only re-debates disputes affected by the follow-up
        - Does NOT rerun the entire pipeline from scratch
        - Stores the follow-up answer linked to the workspace
        """
        ws = workspace_mgr.load_workspace(workspace_id)
        if ws is None:
            return {"_failed": True, "status": "workspace_not_found",
                    "error": f"Workspace {workspace_id} not found"}

        # Record this follow-up in answer history
        ws.add_answer(answer_id=0, parent_answer_id=None,
                      question=follow_up)

        print(f"\n{'='*60}")
        print(f"[WORKSPACE RESUME] {workspace_id[:8]}...")
        print(f"[FOLLOW-UP] {follow_up[:200]}")
        print(f"[WORKSPACE STATE] {len(ws.verified_decisions)} decisions, "
              f"{len(ws.verified_tradeoffs)} tradeoffs, "
              f"{len(ws.human_required_items)} human-required")
        print(f"{'='*60}\n")

        ws.status = WorkspaceStatus.DEBATING
        workspace_mgr.save_workspace(ws)

        from gpt_worker import GPTSession
        from gemini_worker import GeminiSession

        gpt, gemini = GPTSession(), GeminiSession()
        gpt.start()
        gemini.start()

        try:
            # Step 1: Build context from prior workspace decisions
            constraint_context = ws.get_constraint_context()

            # Pre-debate research (V1): plan + collect topic-keyed evidence
            # (reusing ws.research_evidence from prior runs), persist it, and
            # inject it into the follow-up so the debate is evidence-grounded.
            # follow_up stays pristine: only the GPT/Gemini prompt sees the
            # evidence block (impact analysis / verification / synthesis /
            # answer storage use the clean follow_up).
            evidence_block, follow_up = self._run_pre_debate_research(ws, follow_up)

            # Step 2: Determine affected disputes via deterministic impact
            # analysis (no model call). Only affected disputes re-debate;
            # all others stay resolved/verified/tradeoff/human_required.
            impact = analyze_followup_impact(ws, follow_up)
            affected_keys = impact["affected_disputes"]
            reopen_required = impact["reopen_required"]

            # Phase 8: persist impact audit trail to workspace metadata so
            # it survives resume/resurrection/chaining.
            ws.add_impact_audit(audit_entry(impact, follow_up))

            # Phase 9: reopen only the impacted HUMAN_REQUIRED dispute(s);
            # non-impacted pending human items stay untouched. The impact
            # analyzer only includes affected keys, so unaffected human
            # disputes are never passed to _reopen_from_workspace below.

            # Step 3: Get new positions from GPT/Gemini with workspace context
            full_prompt = (
                f"<workspace_context>\n"
                f"Original question: {ws.original_prompt}\n"
                f"{constraint_context}\n"
                f"</workspace_context>\n\n"
                f"<follow_up_question>\n"
                f"{follow_up}\n"
                f"</follow_up_question>\n\n"
                f"Previous decisions are SETTLED. Only address the follow-up question above.\n"
                f"If the follow-up requires revisiting a prior decision, explain specifically "
                f"which decision and why."
            )
            if evidence_block:
                full_prompt = f"{evidence_block}\n\n{full_prompt}"

            print("[RESUME] Getting updated positions...")
            gpt_new = _clean(gpt.ask(full_prompt))
            gemini_new = _clean(gemini.ask(full_prompt))

            # Step 4: Extract conflicts from follow-up responses
            from conflict_extractor import extract_conflicts
            conflict_result = extract_conflicts(gpt_new, gemini_new)
            raw_disputes = conflict_result.get("disputed_claims", [])

            # Step 5: Reopen affected disputes only if follow-up triggers them
            reopened = []
            if reopen_required and affected_keys:
                print(f"[RESUME] Follow-up affects {len(affected_keys)} prior dispute(s): {affected_keys}")
                for key in affected_keys:
                    # Find the original dispute in resolved or active disputes
                    reopened.append(self._reopen_from_workspace(key, ws, follow_up))
                # Remove affected disputes from settled state so the
                # re-debate replaces them; unaffected ones stay put and keep
                # their research evidence (Phase 5 reuse).
                affected_set = set(affected_keys)
                ws.verified_decisions = [
                    d for d in ws.verified_decisions
                    if d.get("normalized_key") not in affected_set
                ]
                ws.resolved_disputes = [
                    d for d in ws.resolved_disputes
                    if d.get("normalized_key") not in affected_set
                ]

            # Step 6: Build debate queue for new + reopened disputes
            all_new_disputes = raw_disputes + [r.dispute_text for r in reopened if r]
            if not all_new_disputes:
                # No new disputes — just synthesize with new positions
                print("[RESUME] No new disputes. Synthesizing from prior state.")
                return self._synthesize_resume(
                    follow_up, ws, gpt_new, gemini_new, [], [],
                    [], governance_context={})

            # Build arch_memory from workspace
            arch_memory = ArchitectureMemory()
            for d in ws.verified_decisions:
                # Reconstruct a minimal Dispute for ArchitectureMemory
                nk = d.get("normalized_key", "")
                arch_memory._decisions[nk] = {
                    "decision": d.get("decision", ""),
                    "agreed_position": d.get("agreed_position", "")[:500],
                    "priority": d.get("priority", "MEDIUM"),
                    "priority_weight": d.get("priority_weight", 3),
                    "resolved_at_round": 0,
                }

            # Make reopened disputes bypass constraint injection for their own key
            reopened_keys = set(affected_keys or [])
            for nk in reopened_keys:
                if nk in arch_memory._decisions:
                    del arch_memory._decisions[nk]

            # Build dispute queue with workspace-aware validation
            disputes = self._build_dispute_queue(all_new_disputes, gpt_new, gemini_new)

            # Debate each new/reopened dispute
            gov_result = GovernanceResult()
            gpt_current = gpt_new
            gemini_current = gemini_new
            final_consensus_score = ws.consensus_score

            for dispute in disputes:
                constraints = arch_memory.get_injection_context(current_dispute=dispute)
                intro = _constrain_prompt_with_research(
                    dispute.dispute_text, constraints,
                    ws.research_evidence, dispute.normalized_key)
                dispute.gpt_current = _clean(gpt.ask(intro))
                dispute.gemini_current = _clean(gemini.ask(intro, dispute_key=dispute.normalized_key))

                result = self._debate_dispute(follow_up, dispute, gpt, gemini)
                final_consensus_score = max(final_consensus_score, dispute.consensus_score)

                if result in ("resolved", "verified_decision"):
                    dispute.status = "pending_verification"
                elif result == "verified_tradeoff":
                    dispute.status = "tradeoff"
                    if dispute.governed_outcome:
                        gov_result.add(GovernedOutcome.from_dict(dispute.governed_outcome))
                elif result == "human_required":
                    dispute.status = "human_required"
                    if dispute.governed_outcome:
                        gov_result.add(GovernedOutcome.from_dict(dispute.governed_outcome))
                else:
                    dispute.status = "deadlocked"

            # Run verification for pending disputes
            pending = [
                d for d in disputes
                if d.status == "pending_verification"
            ]
            # Phase 5: pass prior research evidence from the workspace's
            # resolved_disputes so verification reuses existing evidence on
            # resume instead of re-running research.
            _prior_evidence = [
                d.get("research_evidence") for d in (ws.resolved_disputes or [])
                if isinstance(d, dict) and isinstance(d.get("research_evidence"), dict)
            ]
            verification_result = verify_all_disputes(
                question=follow_up,
                validated_disputes=[
                    {
                        "dispute": d.dispute_text,
                        "normalized_key": d.normalized_key,
                    }
                    for d in pending
                ],
                gpt_position="\n\n".join(d.gpt_current or "" for d in pending),
                gemini_position="\n\n".join(d.gemini_current or "" for d in pending),
                use_external_research=False,
                prior_research_evidence=_prior_evidence,
            )

            # Update workspace with new results
            for d in disputes:
                if d.status == "pending_verification":
                    unresolved_nks = {
                        c.get("normalized_key", "")
                        for c in verification_result.get("unresolved_claims", [])
                    }
                    if d.normalized_key in unresolved_nks:
                        # Escalate unresolved follow-up disputes to governance.
                        # Honor a pre-emitted verdict (HUMAN_REQUIRED from
                        # research-budget exhaustion) without a fresh model
                        # call (Phase 4 wiring).
                        outcome = self._resolve_governance(d, verification_result)
                        if outcome.outcome_type == OutcomeType.VERIFIED_DECISION:
                            ws.add_verified_decision({
                                "decision": outcome.decision or d.dispute_text,
                                "normalized_key": d.normalized_key,
                                "agreed_position": d.gpt_current[:500],
                                "priority": d.priority,
                                "priority_weight": d.priority_weight,
                            })
                        elif outcome.outcome_type == OutcomeType.VERIFIED_TRADEOFF:
                            ws.add_verified_tradeoff({
                                "option_a": outcome.option_a or "",
                                "option_b": outcome.option_b or "",
                                "recommendation": outcome.recommendation or "",
                                "confidence": outcome.confidence,
                                "reasoning": outcome.reasoning,
                                "dispute_key": d.normalized_key,
                            })
                        else:
                            from human_decision_queue import enqueue_human_decision
                            decision_id = enqueue_human_decision(
                                workspace_id=ws.workspace_id,
                                question=d.dispute_text[:200],
                                options=[outcome.option_a or "", outcome.option_b or ""],
                                reasoning=outcome.reasoning,
                                affected_disputes=[d.normalized_key],
                                confidence=outcome.confidence,
                            )
                            d.governed_outcome = outcome.to_dict()
                            d.governed_outcome["decision_id"] = decision_id
                            d.status = "human_required"
                    else:
                        ws.add_verified_decision({
                            "decision": d.dispute_text,
                            "normalized_key": d.normalized_key,
                            "agreed_position": d.gpt_current[:500],
                            "priority": d.priority,
                            "priority_weight": d.priority_weight,
                        })

            # Update positions and state
            ws.gpt_position = gpt_current
            ws.gemini_position = gemini_current
            ws.consensus_score = final_consensus_score
            ws.status = WorkspaceStatus.CONSENSUS_REACHED

            # Check for pending human items
            hq = HumanDecisionQueue()
            if hq.get_pending_count(ws.workspace_id) > 0:
                ws.status = WorkspaceStatus.AWAITING_HUMAN

            workspace_mgr.save_workspace(ws)

            # Create synthesis from updated workspace
            from synthesis_engine import synthesize_answer
            final = synthesize_answer(
                question=follow_up,
                gpt_final=gpt_current,
                gemini_final=gemini_current,
                validated_disputes=[d.to_dict() for d in disputes],
                consensus_score=final_consensus_score,
                verification_result=verification_result,
                governance_context=gov_result.to_dict(),
                disputes=disputes,
            )

            # Store answer with parent linking
            parent_id = None
            if ws.answer_history:
                # Last answer before this one is the parent
                for ah in reversed(ws.answer_history):
                    if ah.get("answer_id") and ah.get("answer_id") != 0:
                        parent_id = ah["answer_id"]
                        break

            answer_id = memory.save_debate(
                question=follow_up,
                gpt_final=gpt_current,
                gemini_final=gemini_current,
                synthesized_answer=final,
                consensus_score=final_consensus_score,
                conflict_severity="MEDIUM" if disputes else "LOW",
                conflict_points=[d.normalized_key for d in disputes],
                validated_disputes=[d.to_dict() for d in disputes],
                verification_result=verification_result,
                parent_answer_id=parent_id,
                workspace_id=ws.workspace_id,
            )

            # Update answer history with real answer_id
            ws.add_answer(answer_id=answer_id, parent_answer_id=parent_id,
                          question=follow_up)
            workspace_mgr.save_workspace(ws)

            return {
                "status": "completed",
                "workspace_id": ws.workspace_id,
                "gpt_final": gpt_current,
                "gemini_final": gemini_current,
                "synthesized_answer": final,
                "validated_disputes": [d.to_dict() for d in disputes],
                "verification_result": verification_result,
                "final_consensus_score": final_consensus_score,
                "answer_id": answer_id,
                "parent_answer_id": parent_id,
                "verified_decisions": ws.verified_decisions,
                "verified_tradeoffs": ws.verified_tradeoffs,
                "human_required": ws.pending_human_items,
            }

        finally:
            gpt.close()
            gemini.close()

    # ------------------------------------------------------------------
    # Reopen a specific dispute
    # ------------------------------------------------------------------
    def reopen_dispute(self, workspace_id: str, dispute_key: str,
                       reason: str = "") -> dict:
        """Reopen a specific resolved dispute within a workspace.

        The dispute is removed from verified decisions and re-debated
        with the current workspace constraints.
        """
        ws = workspace_mgr.load_workspace(workspace_id)
        if ws is None:
            return {"_failed": True, "status": "workspace_not_found"}

        # Find the dispute in verified decisions
        target = None
        for d in ws.verified_decisions:
            if d.get("normalized_key") == dispute_key:
                target = d
                break

        if target is None:
            for d in ws.resolved_disputes:
                if d.get("normalized_key") == dispute_key:
                    target = d
                    break

        if target is None:
            return {"_failed": True, "status": "dispute_not_found",
                    "error": f"Dispute '{dispute_key}' not found in workspace"}

        print(f"[REOPEN] Dispute '{dispute_key}': {target.get('decision', '')[:100]}")
        if reason:
            print(f"[REOPEN] Reason: {reason}")

        # Remove from verified/resolved
        ws.verified_decisions = [
            d for d in ws.verified_decisions
            if d.get("normalized_key") != dispute_key
        ]
        ws.resolved_disputes = [
            d for d in ws.resolved_disputes
            if d.get("normalized_key") != dispute_key
        ]

        # Reopen via resume with the reason as follow-up
        follow_up = reason or f"Re-evaluate decision: {target.get('decision', dispute_key)}"
        return self.resume_workspace(workspace_id, follow_up)

    # ------------------------------------------------------------------
    # Workspace analysis: determine which disputes are affected
    # ------------------------------------------------------------------
    def _analyze_follow_up(self, follow_up: str, ws: Workspace) -> list[str]:
        """Analyze a follow-up to determine which prior disputes may be
        affected. Returns list of normalized_keys to reopen.

        Deterministic: delegates to workspace_impact_analyzer
        (no LLM / model call). Kept for interface compatibility.
        """
        return analyze_followup_impact(ws, follow_up).get("affected_disputes", [])

    def _keyword_affected_analysis(self, follow_up: str,
                                   decisions: list[dict]) -> list[str]:
        """Fallback keyword-based affected decision analysis."""
        follow_up_lower = follow_up.lower()
        affected = []
        for d in decisions:
            decision_text = d.get("decision", "").lower()
            nk = d.get("normalized_key", "").lower()
            # Check if follow-up keywords overlap with decision keywords
            fw_words = set(re.findall(r'\w+', follow_up_lower))
            dw_words = set(re.findall(r'\w+', decision_text + " " + nk))
            # Ignore short/stop words
            stop_words = {"the", "a", "an", "is", "are", "was", "were", "be", "been",
                          "being", "have", "has", "had", "do", "does", "did", "will",
                          "would", "could", "should", "may", "might", "can", "shall",
                          "to", "of", "in", "for", "on", "with", "at", "by", "from",
                          "and", "or", "not", "but", "if", "then", "else", "when",
                          "up", "about", "into", "through", "during", "before", "after"}
            fw_words -= stop_words
            dw_words -= stop_words
            overlap = fw_words & dw_words
            if len(overlap) >= 2:
                affected.append(d.get("normalized_key", ""))
        return affected

    def _reopen_from_workspace(self, key: str, ws: Workspace,
                               follow_up: str) -> Dispute | None:
        """Reconstruct a Dispute from workspace data for re-debate."""
        # Search verified decisions first
        source = None
        for d in ws.verified_decisions:
            if d.get("normalized_key") == key:
                source = d
                break
        if source is None:
            for d in ws.resolved_disputes:
                if d.get("normalized_key") == key:
                    source = d
                    break

        if source is None:
            return None

        dispute_text = source.get("decision", key)
        reopened_text = (
            f"[REOPENED] {dispute_text}\n"
            f"Context: Follow-up question requires revisiting this decision: {follow_up[:200]}"
        )

        return Dispute(
            dispute_text=reopened_text,
            normalized_key=key,
            gpt_source=ws.gpt_position,
            gemini_source=ws.gemini_position,
            priority=_classify_priority(dispute_text, key),
            priority_weight=PRIORITY_WEIGHTS.get(
                _classify_priority(dispute_text, key), 3
            ),
        )

    # ------------------------------------------------------------------
    # Internal: run full pipeline in workspace context
    # ------------------------------------------------------------------
    def _run_in_workspace(self, question: str, gpt, gemini, ws: Workspace,
                          memory_ids: list | None) -> dict:
        """Run the full debate pipeline, persisting progress to workspace."""
        # Load memory context
        ctx = ""
        if memory_ids:
            ctx = memory.build_context(memory_ids)
            if len(ctx) > MAX_MEMORY_CHARS:
                ctx = ctx[-MAX_MEMORY_CHARS:]
            question = f"MEMORY CONTEXT\n\n{ctx}\n\nCURRENT QUESTION\n\n{question}"

        # Add workspace constraints if they exist
        constraint_ctx = ws.get_constraint_context()
        if constraint_ctx:
            question = f"{constraint_ctx}\n\n{question}"

        # Pre-debate research (V1): plan + collect topic-keyed evidence
        # (reusing ws.research_evidence), persist it, and inject it BEFORE
        # the initial positions are gathered so the debate is evidence-grounded.
        # question stays pristine: only the GPT/Gemini initial-positions prompt
        # sees the evidence block (verification / synthesis / answer storage
        # use the clean question).
        evidence_block, question = self._run_pre_debate_research(ws, question)

        # Get source positions
        print("\n=== INITIAL POSITIONS ===")
        initial_prompt = (f"{evidence_block}\n\n{question}"
                          if evidence_block else question)
        gpt_source = _clean(gpt.ask(initial_prompt))
        gemini_source = _clean(gemini.ask(initial_prompt))

        # Extract conflicts
        print("\n=== CONFLICT EXTRACTION ===")
        from conflict_extractor import extract_conflicts
        conflict_result = extract_conflicts(gpt_source, gemini_source)
        raw_disputes = conflict_result.get("disputed_claims", [])

        if not raw_disputes:
            result = self._synthesize_resume(
                question, ws, gpt_source, gemini_source, [], [], [],
                governance_context={})
            ws.status = WorkspaceStatus.COMPLETED
            ws.gpt_position = gpt_source
            ws.gemini_position = gemini_source
            workspace_mgr.save_workspace(ws)
            return result

        # Build dispute queue
        disputes = self._build_dispute_queue(raw_disputes, gpt_source, gemini_source)
        arch_memory = ArchitectureMemory()
        gpt_current = gpt_source
        gemini_current = gemini_source
        final_consensus_score = 0
        gov_result = GovernanceResult()

        # Debate one dispute at a time
        for dispute in disputes:
            constraints = arch_memory.get_injection_context(current_dispute=dispute)
            intro = _constrain_prompt_with_research(
                dispute.dispute_text, constraints,
                ws.research_evidence, dispute.normalized_key)
            dispute.gpt_current = _clean(gpt.ask(intro))
            dispute.gemini_current = _clean(gemini.ask(intro, dispute_key=dispute.normalized_key))

            result = self._debate_dispute(question, dispute, gpt, gemini)
            final_consensus_score = max(final_consensus_score, dispute.consensus_score)

            if result in ("resolved", "verified_decision"):
                dispute.status = "pending_verification"
            elif result == "verified_tradeoff":
                dispute.status = "tradeoff"
                if dispute.governed_outcome:
                    gov_result.add(GovernedOutcome.from_dict(dispute.governed_outcome))
            elif result == "human_required":
                dispute.status = "human_required"
                if dispute.governed_outcome:
                    gov_result.add(GovernedOutcome.from_dict(dispute.governed_outcome))
            else:
                dispute.status = "deadlocked"
                print(f"[DEADLOCKED] {dispute.normalized_key}")

        # Verification loop
        # Phase 5: seed accumulated evidence from the workspace's prior
        # resolved_disputes so research is not re-run on resume.
        _prior_evidence = [
            d.get("research_evidence") for d in (ws.resolved_disputes or [])
            if isinstance(d, dict) and isinstance(d.get("research_evidence"), dict)
        ]
        verification_result = self._run_verification_ws(
            question, disputes, gpt_current, gemini_current, gpt, gemini,
            arch_memory, final_consensus_score, gov_result,
            prior_research_evidence=_prior_evidence,
            research_evidence=ws.research_evidence)

        if verification_result.get("_failed"):
            ws.status = WorkspaceStatus.COMPLETED
            ws.error = "verification_failed"
            workspace_mgr.save_workspace(ws)
            return verification_result

        # Update workspace state from resolved disputes
        for d in disputes:
            if d.status == "resolved":
                ws.add_verified_decision({
                    "decision": d.dispute_text,
                    "normalized_key": d.normalized_key,
                    "agreed_position": d.gpt_current[:500],
                    "priority": d.priority,
                    "priority_weight": d.priority_weight,
                })
            elif d.status == "tradeoff" and d.governed_outcome:
                go = d.governed_outcome
                ws.add_verified_tradeoff({
                    "option_a": go.get("option_a", ""),
                    "option_b": go.get("option_b", ""),
                    "recommendation": go.get("recommendation", ""),
                    "confidence": go.get("confidence", 50),
                    "reasoning": go.get("reasoning", ""),
                    "dispute_key": d.normalized_key,
                })
            elif d.status == "human_required" and d.governed_outcome:
                go = d.governed_outcome
                from human_decision_queue import enqueue_human_decision
                decision_id = enqueue_human_decision(
                    workspace_id=ws.workspace_id,
                    question=d.dispute_text[:200],
                    options=[go.get("option_a", ""), go.get("option_b", "")],
                    reasoning=go.get("reasoning", ""),
                    affected_disputes=[d.normalized_key],
                    confidence=go.get("confidence", 0),
                )
                d.governed_outcome["decision_id"] = decision_id

        # Store resolved disputes in workspace. Research evidence (if any) is
        # attached onto each dispute dict so it rides the existing
        # ws.resolved_disputes JSON-TEXT column — workspace resume re-loads it
        # without a separate storage system (Phase 2: persistence).
        ws.resolved_disputes = attach_research_evidence(
            [d.to_dict() for d in disputes], verification_result
        )
        ws.gpt_position = gpt_current
        ws.gemini_position = gemini_current
        ws.consensus_score = final_consensus_score
        ws.status = WorkspaceStatus.CONSENSUS_REACHED

        # Check for human items
        if ws.pending_human_items:
            ws.status = WorkspaceStatus.AWAITING_HUMAN

        workspace_mgr.save_workspace(ws)

        return self._synthesize_resume(
            question, ws, gpt_current, gemini_current,
            disputes, verification_result,
            arch_memory.get_all() if arch_memory else [],
            governance_context=gov_result.to_dict())

    # ------------------------------------------------------------------
    # Verification loop with workspace awareness
    # ------------------------------------------------------------------
    def _run_verification_ws(self, question: str, disputes: list[Dispute],
                              gpt_current: str, gemini_current: str,
                              gpt, gemini, arch_memory: ArchitectureMemory,
                              final_consensus_score: int,
                              gov_result: GovernanceResult | None = None,
                              prior_research_evidence: list | None = None,
                              research_evidence: list | None = None) -> dict:
        """Run verification with workspace-aware governance escalation.

        Research V2 (Phase 3): ``research_evidence`` is the workspace's
        accumulated evidence; it is injected into each RE-DEBATE (retry) intro
        so a dispute entering another debate round carries forward the same
        relevant evidence — no re-research, no re-fetch. None/empty leaves
        retry intros identical to the previous behavior."""
        final_result = None

        # Phase 5: accumulate research evidence across retry iterations so
        # retried disputes reuse the evidence from the prior pass instead of
        # re-running research. Deduplicated by key.
        _accumulated_evidence = {}
        for ev in (prior_research_evidence or []):
            if isinstance(ev, dict):
                key = ev.get("normalized_key") or ev.get("dispute_key")
                if key:
                    _accumulated_evidence[key] = ev

        while True:
            pending = [d for d in disputes
                       if d.status in ("pending_verification", "deadlocked")]
            if not pending:
                break

            result = verify_all_disputes(
                question=question,
                validated_disputes=[
                    {"dispute": d.dispute_text, "normalized_key": d.normalized_key}
                    for d in pending
                ],
                gpt_position=gpt_current,
                gemini_position=gemini_current,
                use_external_research=False,
                prior_research_evidence=list(_accumulated_evidence.values()),
            )

            # Accumulate new evidence from this round (deduplicated by key).
            for ev in result.get("research_evidence", []):
                if isinstance(ev, dict):
                    key = ev.get("normalized_key") or ev.get("dispute_key")
                    if key:
                        _accumulated_evidence[key] = ev

            unresolved_nks = set()
            for claim in result.get("unresolved_claims", []):
                nk = claim.get("normalized_key", "") if isinstance(claim, dict) else ""
                if nk:
                    unresolved_nks.add(nk)

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
                        print(f"[GOVERNANCE] {d.normalized_key} — verification retries exhausted.")
                        outcome = self._resolve_governance(d, result)
                        self._apply_governance_outcome(d, outcome, arch_memory, gov_result)
                else:
                    arch_memory.resolve(d)
                    d.status = "resolved"
                    print(f"[RESOLVED] {d.normalized_key} — verified")

            if retry_list:
                print(f"\n[RE-DEBATE] {len(retry_list)} disputes...")
                for dispute in retry_list:
                    constraints = arch_memory.get_injection_context(current_dispute=dispute)
                    intro = _constrain_prompt_with_research(
                        dispute.dispute_text, constraints,
                        research_evidence, dispute.normalized_key)
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
                    else:
                        dispute.status = "deadlocked"
                final_result = result
                continue

            # Handle any still-pending
            for d in disputes:
                if d.status == "pending_verification":
                    print(f"[GOVERNANCE] {d.normalized_key} — verification incomplete.")
                    outcome = self._resolve_governance(d, result)
                    self._apply_governance_outcome(d, outcome, arch_memory, gov_result)

            final_result = result
            break

        # Weighted consensus
        w_sum = sum(d.priority_weight for d in disputes)
        w_score = int(sum(d.consensus_score * d.priority_weight for d in disputes) / w_sum) if w_sum > 0 else 0
        print(f"[CONSENSUS] Weighted={w_score}")

        if final_result:
            v_score = final_result.get("verification_score", 100)
            unresolved = len(final_result.get("unresolved_claims", []))
            if v_score < 90 and unresolved > 0:
                failed_keys = [d.normalized_key for d in disputes if d.status != "resolved"]
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
                        "final_consensus_score": final_consensus_score}

        return final_result or {}

    def _apply_governance_outcome(self, dispute: Dispute, outcome: GovernedOutcome,
                                   arch_memory: ArchitectureMemory,
                                   gov_result: GovernanceResult | None):
        """Apply a governed outcome to a dispute."""
        if outcome.outcome_type == OutcomeType.VERIFIED_DECISION:
            arch_memory.resolve(dispute)
            dispute.status = "resolved"
            dispute.outcome_type = OutcomeType.VERIFIED_DECISION
            dispute.governed_outcome = outcome.to_dict()
            if gov_result:
                gov_result.add(outcome)
        elif outcome.outcome_type == OutcomeType.VERIFIED_TRADEOFF:
            dispute.status = "tradeoff"
            dispute.outcome_type = OutcomeType.VERIFIED_TRADEOFF
            dispute.governed_outcome = outcome.to_dict()
            if gov_result:
                gov_result.add(outcome)
        else:
            dispute.status = "human_required"
            dispute.outcome_type = OutcomeType.HUMAN_REQUIRED
            dispute.governed_outcome = outcome.to_dict()
            if gov_result:
                gov_result.add(outcome)

    def _resolve_governance(self, dispute: Dispute,
                            verification_result: dict) -> GovernedOutcome:
        """Resolve a dispute's governance outcome, honoring a pre-emitted
        verdict from the verifier (e.g. HUMAN_REQUIRED from research-budget
        exhaustion) WITHOUT a fresh verifier_review (which would be a model
        call and would overwrite the 'Research budget exhausted' reason).
        Falls back to _verifier_review_dispute when no pre-emitted outcome
        is present. This is wiring (Phase 4); it does not change the debate,
        consensus, or governance-rule flows."""
        pre = extract_pre_emitted_outcome(verification_result, dispute.normalized_key)
        if pre is not None:
            return GovernedOutcome.from_dict(pre)
        return self._verifier_review_dispute(dispute, InformationState())

    # ------------------------------------------------------------------
    # Synthesis for resume/workspace
    # ------------------------------------------------------------------
    def _synthesize_resume(self, question: str, ws: Workspace,
                           gpt_current: str, gemini_current: str,
                           disputes: list, verification_result: dict,
                           resolved: list,
                           governance_context: dict | None = None) -> dict:
        """Synthesize and return result, updating workspace storage."""
        from synthesis_engine import synthesize_answer

        gov_ctx = governance_context or {}
        final = synthesize_answer(
            question=question,
            gpt_final=gpt_current,
            gemini_final=gemini_current,
            validated_disputes=[d.to_dict() for d in disputes] if disputes else [],
            consensus_score=ws.consensus_score,
            verification_result=verification_result or {},
            governance_context=gov_ctx,
            disputes=disputes,
        )

        # Save to memory with workspace linking
        answer_id = memory.save_debate(
            question=question,
            gpt_final=gpt_current,
            gemini_final=gemini_current,
            synthesized_answer=final,
            consensus_score=ws.consensus_score,
            conflict_severity="LOW" if not disputes else "MEDIUM",
            conflict_points=[d.normalized_key for d in disputes] if disputes else [],
            validated_disputes=[d.to_dict() for d in disputes] if disputes else [],
            verification_result=verification_result or {},
            workspace_id=ws.workspace_id,
        )

        ws.add_answer(answer_id=answer_id, parent_answer_id=None, question=question)
        ws.gpt_position = gpt_current
        ws.gemini_position = gemini_current
        workspace_mgr.save_workspace(ws)

        return {
            "status": "completed",
            "workspace_id": ws.workspace_id,
            "gpt_final": gpt_current,
            "gemini_final": gemini_current,
            "synthesized_answer": final,
            "validated_disputes": [d.to_dict() for d in disputes] if disputes else [],
            "verification_result": verification_result or {},
            "final_consensus_score": ws.consensus_score,
            "answer_id": answer_id,
            "verified_decisions": ws.verified_decisions,
            "verified_tradeoffs": ws.verified_tradeoffs,
            "human_required": ws.pending_human_items,
        }

    # ------------------------------------------------------------------
    # Pre-debate research (V1): plan, collect, reuse, inject, audit
    # ------------------------------------------------------------------
    def _run_pre_debate_research(self, ws: Workspace, question: str) -> tuple:
        """Plan + collect pre-debate research. Returns (evidence_block,
        question) where ``question`` is unchanged and ``evidence_block`` is
        the ``<external_research>`` injection string ("" when no usable
        evidence). Keeping the question pristine means impact analysis /
        verification / synthesis / answer storage never see the injected
        block — only GPT/Gemini's debate prompt does.

        Deterministic planning (no LLM). Topic-keyed reuse of
        ws.research_evidence prevents duplicate research calls across
        follow-ups. Evidence + audit trail are persisted on the workspace.
        """
        plan = plan_research(question)
        if not plan.get("research_required"):
            ws.add_research_audit(audit_record(plan,
                {"evidence": [], "reused_topics": [], "researched_topics": []}))
            return "", question

        # Telemetry: Research stage wraps the research collection. Passive
        # observation only — the recorder is a no-op when unconfigured. Not a
        # non-goal module (workspace orchestration wrapper, not research
        # planning logic); the plan/collect/merge mechanics below are
        # unchanged.
        from telemetry import recorder as _telemetry
        _telemetry.start_stage("Research", topics=plan.get("topics", []))
        collected = collect_research(
            plan, self._pre_debate_research_fn, ws.research_evidence)
        _telemetry.end_stage("Research",
                             topics_researched=len(collected.get("evidence", [])))

        ws.research_evidence = merge_evidence(ws.research_evidence,
                                              collected["evidence"])
        ws.add_research_audit(audit_record(plan, collected))

        block = format_research_for_prompt(collected["evidence"])
        return block, question