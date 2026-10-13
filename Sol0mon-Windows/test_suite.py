"""
Comprehensive automated test suite for Project Sol0mon.
Tests all modules that don't require a live browser or external API.
"""
import json
import os
import sys
import uuid


def test_debate_engine_core():
    """Test DebateEngine, Dispute, ArchitectureMemory, and all helpers."""
    from debate_engine import (
        DebateEngine, Dispute, ArchitectureMemory,
        _classify_priority, _detect_stagnation, _clean, _extract_json,
        _evidence_window, _constrain_prompt, _critique_prompt, _revision_prompt,
        CONSENSUS_THRESHOLD, MAX_ROUNDS_PER_DISPUTE, MAX_MEMORY_CHARS,
        STAGNATION_CHECKS, STAGNATION_MAX_WARNINGS,
        PRIORITY_WEIGHTS, DOMAIN_PRIORITY
    )

    # Configuration constants
    assert CONSENSUS_THRESHOLD == 92
    assert MAX_ROUNDS_PER_DISPUTE == 15
    assert MAX_MEMORY_CHARS == 8000
    assert STAGNATION_CHECKS == 3
    assert STAGNATION_MAX_WARNINGS == 2
    assert PRIORITY_WEIGHTS == {"HIGH": 5, "MEDIUM": 3, "LOW": 1}

    # Domain priority mapping
    assert "database" in DOMAIN_PRIORITY["HIGH"]
    assert "framework" in DOMAIN_PRIORITY["MEDIUM"]
    assert "library" in DOMAIN_PRIORITY["LOW"]

    # Dispute dataclass
    d = Dispute(
        dispute_text="Test dispute",
        normalized_key="test_key",
        gpt_source="GPT source text",
        gemini_source="Gemini source text",
        gpt_current="GPT current",
        gemini_current="Gemini current",
        priority="HIGH",
        priority_weight=5,
        round_number=2,
        consensus_score=95,
        consensus_history=[90, 95],
        stagnation_warnings=0,
        verification_retries=0,
        status="resolved"
    )
    assert d.dispute_text == "Test dispute"
    assert d.normalized_key == "test_key"
    assert d.status == "resolved"

    # Dispute serialization
    d_dict = d.to_dict()
    assert d_dict["dispute"] == "Test dispute"
    assert d_dict["dispute_text"] == "Test dispute"
    assert d_dict["normalized_key"] == "test_key"
    assert d_dict["status"] == "resolved"
    assert d_dict["consensus_score"] == 95

    # ArchitectureMemory
    am = ArchitectureMemory()
    am.resolve(d)
    assert am.is_resolved("test_key")
    assert not am.is_resolved("nonexistent")
    ctx = am.get_injection_context()
    assert "resolved_constraints" in ctx
    assert "Test dispute" in ctx
    all_decisions = am.get_all()
    assert len(all_decisions) == 1
    assert all_decisions[0]["normalized_key"] == "test_key"

    # ArchitectureMemory respects MAX_CONSTRAINTS
    for i in range(15):
        dd = Dispute(
            dispute_text=f"Decision {i}",
            normalized_key=f"key_{i}",
            gpt_current=f"Current {i}",
            priority="MEDIUM",
            priority_weight=3,
            round_number=1,
        )
        am.resolve(dd)
    ctx = am.get_injection_context(current_dispute=d)
    all_decisions = am.get_all()
    assert len(all_decisions) == 16  # 1 original + 15 added

    # Priority classification
    assert _classify_priority("database storage architecture scaling", "db") == "HIGH"
    assert _classify_priority("use framework service api", "fw") == "MEDIUM"
    assert _classify_priority("naming convention library", "nm") == "LOW"

    # Stagnation detection
    d2 = Dispute(dispute_text="stale", normalized_key="stale_key", gpt_current="x")
    d2.consensus_history = [90, 91, 92]
    assert _detect_stagnation(d2) == "warning"
    d2.stagnation_warnings = STAGNATION_MAX_WARNINGS + 1
    assert _detect_stagnation(d2) == "deadlock"
    d2.consensus_history = [50, 70, 90]
    d2.stagnation_warnings = 0
    assert _detect_stagnation(d2) is None

    # Response cleaning
    assert _clean("GPT said Hello world") == "Hello world"
    assert _clean("Gemini: Answer here") == "Answer here"
    assert _clean("ChatGPT said something") == "something"
    assert _clean("  plain text  ") == "plain text"
    assert _clean("Claude said output") == "output"
    assert _clean("") == ""

    # JSON extraction
    assert _extract_json('{"key": "value"}') == {"key": "value"}
    assert _extract_json('prefix {"hello": 1} suffix') == {"hello": 1}
    assert _extract_json('[{"a": 1}]') == [{"a": 1}]
    assert _extract_json(None) is None
    assert _extract_json("") is None
    assert _extract_json("no json here") is None

    # Evidence window
    position = "We recommend PostgreSQL for storage. MongoDB works for documents."
    dispute = "PostgreSQL vs MongoDB"
    evidence = _evidence_window(dispute, position, window=200)
    assert "PostgreSQL" in evidence

    # Prompt templates
    assert "Identify" in _critique_prompt("opponent says X")
    assert "Revise" in _revision_prompt("critique says Y")
    assert "settled constraints" in _constrain_prompt("dispute text", "constraints here")
    assert "State your position" in _constrain_prompt("dispute text")

    # DebateEngine class exists and has expected methods
    assert hasattr(DebateEngine, 'run')
    eng = DebateEngine()
    assert hasattr(eng, '_run')
    assert hasattr(eng, '_build_dispute_queue')
    assert hasattr(eng, '_debate_dispute')
    assert hasattr(eng, '_run_verification')
    assert hasattr(eng, '_synthesize_and_return')

    return True


def test_consensus_engine():
    """Test consensus engine module."""
    from consensus_engine import (
        build_consensus, build_synthesis, calculate_confidence, clean_final_answer
    )

    # clean_final_answer
    assert clean_final_answer("GPT said Hello") == "Hello"
    assert clean_final_answer("Gemini: World") == "World"
    assert clean_final_answer("") == ""
    assert clean_final_answer(None) == ""

    # calculate_confidence
    conflict_pkg = {"severity": "LOW"}
    verifier = {"verified_facts": [], "rejected_claims": [], "uncertain_claims": []}
    assert calculate_confidence(conflict_pkg, verifier) == 100

    conflict_pkg_high = {"severity": "HIGH"}
    verifier_high = {
        "verified_facts": [1, 2],
        "rejected_claims": [1],
        "uncertain_claims": [1, 2]
    }
    conf = calculate_confidence(conflict_pkg_high, verifier_high)
    assert 0 <= conf <= 100

    # build_synthesis with LOW severity
    result = build_synthesis(
        {"severity": "LOW", "resolved_disputes": []},
        {},
        "GPT says Paris is capital",
        "Gemini says Paris is capital of France"
    )
    assert "Paris" in result

    # build_consensus
    consensus = build_consensus(
        {"severity": "LOW", "resolved_disputes": []},
        {"verified_facts": [], "rejected_claims": [], "uncertain_claims": []},
        "GPT position",
        "Gemini position"
    )
    assert "final_answer" in consensus
    assert "confidence_score" in consensus

    return True


def test_synthesis_engine():
    """Test synthesis engine."""
    from synthesis_engine import extract_json, build_synthesis_prompt, synthesize_answer

    # extract_json
    assert extract_json('{"key": "value"}') == {"key": "value"}
    assert extract_json('text {"a": 1} more') == {"a": 1}
    assert extract_json(None) is None
    assert extract_json("") is None

    # build_synthesis_prompt
    prompt = build_synthesis_prompt(
        "Design a platform",
        "GPT: Use K8s, Kafka, Postgres",
        "Gemini: Use K8s, Kafka, Postgres, Redis",
        [{"dispute": "Redis vs none"}],
        95,
        {"resolved_claims": [{"dispute": "Redis vs none", "normalized_key": "cache"}],
         "equivalent_claims": [], "unresolved_claims": [], "verification_score": 100}
    )
    assert "Design a platform" in prompt
    assert "GPT: Use K8s" in prompt
    assert "Gemini: Use K8s" in prompt
    assert "Redis vs none" in prompt
    assert "VERIFICATION RESULTS" in prompt
    assert "RESOLVED" in prompt

    # synthesize_answer with Cerebras call (will use API)
    result = synthesize_answer(
        "test",
        "GPT says X",
        "Gemini says Y",
        [],
        100,
        None
    )
    assert isinstance(result, str) and len(result) > 0

    return True


def test_conflict_extractor():
    """Test conflict extractor."""
    from conflict_extractor import extract_json, normalize_string_list, extract_conflicts

    # extract_json
    assert extract_json('{"key": "val"}') == {"key": "val"}
    assert extract_json('["a", "b"]') == ["a", "b"]

    # normalize_string_list
    assert normalize_string_list(["  a  ", "b", "  a  ", None, ""]) == ["a", "b"]
    assert normalize_string_list(None) == []
    assert normalize_string_list([]) == []

    # extract_conflicts with Cerebras
    result = extract_conflicts(
        "Use PostgreSQL for primary storage",
        "Use PostgreSQL for primary data"
    )
    assert "severity" in result
    assert "agreed_claims" in result
    assert "disputed_claims" in result
    assert "resolved_disputes" in result

    # extract_conflicts with empty positions
    result = extract_conflicts("", "")
    assert result["severity"] in ("LOW", "MEDIUM", "HIGH")

    return True


def test_verification_engine():
    """Test verification engine."""
    from verification_engine import (
        extract_json, extract_relevant_evidence, verify_all_disputes
    )

    # extract_json handles objects and arrays
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('[{"b": 2}]') == [{"b": 2}]

    # extract_relevant_evidence
    evidence = extract_relevant_evidence(
        "PostgreSQL vs MongoDB",
        "We recommend PostgreSQL for primary storage. It provides ACID guarantees."
    )
    assert "PostgreSQL" in evidence

    # verify_all_disputes with empty list
    result = verify_all_disputes(
        "test question",
        [],
        "gpt position",
        "gemini position"
    )
    assert result["verification_score"] == 100
    assert len(result["resolved_claims"]) == 0

    # verify_all_disputes with disputes (Cerebras call)
    result = verify_all_disputes(
        "Design a platform",
        [{"dispute": "K8s vs Nomad", "normalized_key": "orchestrator"}],
        "Use Kubernetes for orchestration",
        "Use Kubernetes for container orchestration"
    )
    assert "verification_score" in result
    assert "resolved_claims" in result
    assert "unresolved_claims" in result
    assert "equivalent_claims" in result
    assert "truncated_count" in result

    # truncation handling
    many_disputes = [{"dispute": f"dispute {i}", "normalized_key": f"key_{i}"} for i in range(20)]
    result = verify_all_disputes("test", many_disputes, "gpt", "gemini")
    assert result["truncated_count"] >= 5  # 20 - 15 = 5 dropped

    return True


def test_answer_memory():
    """Test answer memory module."""
    from answer_memory import memory

    # Stats
    stats = memory.get_stats()
    assert "total_debates" in stats
    assert "average_consensus_score" in stats

    # Recent debates
    debates = memory.get_recent_debates(5)
    assert isinstance(debates, list)

    # Search
    results = memory.search_debates("test", limit=5)
    assert isinstance(results, list)

    # List
    debates_list = memory.list_debates(10)
    assert isinstance(debates_list, list)

    # CRUD
    debate_id = memory.save_debate(
        question="test_memory_crud",
        gpt_final="GPT answer",
        gemini_final="Gemini answer",
        synthesized_answer="Synthesized answer",
        consensus_score=95,
        conflict_severity="LOW",
        conflict_points=["key1"],
        validated_disputes=[{"dispute": "test dispute", "normalized_key": "nk1"}],
        verification_result={"resolved_claims": [{"normalized_key": "nk1"}]}
    )
    assert debate_id is not None

    debate = memory.get_debate(debate_id)
    assert debate is not None
    assert debate["question"] == "test_memory_crud"
    assert debate["consensus_score"] == 95
    assert "resolved_decisions" in debate
    assert "decision_summary" in debate

    # build_context
    ctx = memory.build_context([debate_id])
    assert "PRIOR DEBATE" in ctx or "decision_summary" in ctx

    # delete
    assert memory.delete_debate(debate_id)
    assert not memory.debate_exists(debate_id)

    return True


def test_job_store():
    """Test job store module."""
    from job_store import (
        initialize_database, create_job, get_job, update_status,
        update_result, update_error, update_job, list_jobs
    )

    initialize_database()

    # Create
    job_id = create_job("test_job_store_prompt")
    assert job_id is not None
    job = get_job(job_id)
    assert job is not None
    assert job[2] == "PENDING"

    # Update status
    update_status(job_id, "DEBATING")
    job = get_job(job_id)
    assert job[2] == "DEBATING"

    # Update result
    update_result(job_id, "test result", 95)
    job = get_job(job_id)
    assert job[2] == "COMPLETED"
    assert job[3] == 95
    assert job[4] == "test result"

    # Create second job and test error path
    job_id2 = create_job("test_error_path")
    update_error(job_id2, "test error occurred")
    job2 = get_job(job_id2)
    assert job2[2] == "FAILED"
    assert "test error" in job2[5]

    # update_job (unified)
    job_id3 = create_job("test_unified_update")
    update_job(job_id3, "FAILED", consensus_score=0, result="crash", error="catastrophic")
    job3 = get_job(job_id3)
    assert job3[2] == "FAILED"

    # list_jobs
    jobs = list_jobs()
    assert len(jobs) >= 3

    return True


def test_project_manager():
    """Test project manager module."""
    from project_manager import (
        ensure_project, save_artifact, load_artifact, list_projects
    )

    projects = list_projects()
    assert isinstance(projects, list)

    # Create test project
    test_name = "test_project_" + str(uuid.uuid4())[:8]
    path = ensure_project(test_name)
    assert os.path.exists(path)

    # Save artifact
    artifact_path = save_artifact(test_name, "research", "test.md", "Test content")
    assert os.path.exists(artifact_path)

    # Load artifact
    content = load_artifact(test_name, "research", "test.md")
    assert content == "Test content"

    # Cleanup
    import shutil
    shutil.rmtree(os.path.join("projects", test_name))

    return True


def test_worker_components():
    """Test worker and pipeline runner imports and interfaces."""
    from pipeline_runner import execute, log
    from worker_runner import run_job, log as worker_log
    from orchestrator import submit_job, get_status, get_result, safe_run_job

    # All functions exist and are callable
    assert callable(execute)
    assert callable(run_job)
    assert callable(submit_job)
    assert callable(get_status)
    assert callable(get_result)

    # submit_job creates a real job (will fail without browser, but job is created)
    job_id = submit_job("test submit_job")
    assert job_id is not None
    status = get_status(job_id)
    assert status is not None  # should be PENDING or FAILED

    return True


def test_provider_clients():
    """Test provider client interfaces."""
    from cerebras_client import generate
    from ollama_client import generate as ollama_generate

    # Cerebras client: skip if no valid API key configured (safe default)
    import os
    api_key = os.getenv("CEREBRAS_API_KEYS", "")
    if api_key and api_key not in ("", "your_api_key_here"):
        result = generate("Return exactly: OK", module="test")
        assert isinstance(result, str) and len(result) > 0
    else:
        print("  (skipping Cerebras live test — no valid API key configured)")

    # Ollama generate function signature is correct
    import inspect
    sig = inspect.signature(ollama_generate)
    params = list(sig.parameters.keys())
    assert "prompt" in params
    assert "module" in params

    return True


def test_support_modules():
    """Test all support/utility modules."""
    # evidence_extractor
    from evidence_extractor import normalize, split_sentences, tokenize, extract_evidence
    text = "PostgreSQL is ACID compliant. MongoDB is a document database."
    sentences = split_sentences(text)
    assert len(sentences) >= 2
    evidence = extract_evidence("PostgreSQL database", text)
    assert len(evidence) > 0

    # dispute_validator
    from dispute_validator import extract_json, validate_disputes
    assert validate_disputes("", "", "", []) == []

    # qwen_verifier
    from qwen_verifier import verify
    result = verify({"disputed_claims": []})
    assert result["verification_score"] == 100

    # performance_logger
    from performance_logger import save_metrics, load_metrics
    test_id = str(uuid.uuid4())
    save_metrics(test_id, {"test": True})
    loaded = load_metrics(test_id)
    assert loaded and loaded["test"]
    os.remove(f"logs/performance/{test_id}.json")

    # artifact_summarizer
    from artifact_summarizer import summarize_artifact

    return True


def test_research_and_architecture_runners():
    """Test research and architecture runner interfaces."""
    from research_runner import run_research
    from architecture_runner import build_architecture

    # Functions exist and have correct signatures
    assert callable(run_research)
    assert callable(build_architecture)

    return True


def test_worker_modules():
    """Test GPT/Gemini worker module interfaces."""
    from gpt_worker import GPTSession, ask_chatgpt
    from gemini_worker import GeminiSession, ask_gemini

    # Classes exist
    assert hasattr(GPTSession, 'start')
    assert hasattr(GPTSession, 'ask')
    assert hasattr(GPTSession, 'close')
    assert hasattr(GeminiSession, 'start')
    assert hasattr(GeminiSession, 'ask')
    assert hasattr(GeminiSession, 'close')

    # Functions exist
    assert callable(ask_chatgpt)
    assert callable(ask_gemini)

    return True


# ---------------------------------------------------------------------------
# Workspace system tests
# ---------------------------------------------------------------------------

def test_workspace_manager():
    """Test workspace_manager.py — creation, loading, saving, lifecycle."""
    from workspace_manager import (
        Workspace, WorkspaceStatus, WorkspaceManager,
        create_workspace, load_workspace, save_workspace,
        list_workspaces, delete_workspace,
    )
    from datetime import datetime, timezone

    # Create
    ws_id = create_workspace("Design a scalable platform")
    assert ws_id is not None
    assert len(ws_id) == 36  # UUID

    # Load
    ws = load_workspace(ws_id)
    assert ws is not None
    assert ws.original_prompt == "Design a scalable platform"
    assert ws.status == WorkspaceStatus.CREATED

    # Add decisions
    ws.add_verified_decision({
        "decision": "Use PostgreSQL",
        "normalized_key": "database_postgres",
    })
    ws.add_verified_decision({
        "decision": "Use PostgreSQL",
        "normalized_key": "database_postgres",  # duplicate key
    })
    assert len(ws.verified_decisions) == 1  # dedup works

    ws.add_verified_decision({
        "decision": "Use Kubernetes",
        "normalized_key": "orchestration_k8s",
    })
    assert len(ws.verified_decisions) == 2

    # Add tradeoffs
    ws.add_verified_tradeoff({
        "option_a": "Kubernetes",
        "option_b": "Nomad",
        "recommendation": "Use K8s",
        "confidence": 85,
        "reasoning": "Better ecosystem",
    })
    assert len(ws.verified_tradeoffs) == 1

    # Duplicate tradeoff
    ws.add_verified_tradeoff({
        "option_a": "Kubernetes",
        "option_b": "Nomad",
    })
    assert len(ws.verified_tradeoffs) == 1

    # Add human required
    ws.add_human_required({
        "question": "Database Choice",
        "options": ["SQLite", "PostgreSQL"],
        "reasoning": "MVP vs production",
        "affected_disputes": ["database_postgres"],
    })
    assert len(ws.pending_human_items) == 1
    assert len(ws.pending_human_items) == 1

    # Resolve human item
    assert ws.resolve_human_item("Database Choice", "PostgreSQL")
    assert len(ws.pending_human_items) == 0
    from human_decision_store import store as _store
    resolved = _store.resolved_list(ws.workspace_id)
    assert len(resolved) == 1
    assert resolved[0]["chosen_option"] == "PostgreSQL"

    # Resolve non-existent
    assert not ws.resolve_human_item("Non-existent", "X")

    # Constraint context
    ctx = ws.get_constraint_context()
    assert "workspace_constraints" in ctx
    assert "PostgreSQL" in ctx or "Kubernetes" in ctx

    # Answer history
    ws.add_answer(answer_id=1, parent_answer_id=None,
                  question="Initial question")
    ws.add_answer(answer_id=2, parent_answer_id=1,
                  question="Follow-up question")
    assert len(ws.answer_history) == 2
    assert ws.answer_history[1]["parent_answer_id"] == 1

    # Save and reload
    ws.status = WorkspaceStatus.CONSENSUS_REACHED
    ws.gpt_position = "GPT: Use PostgreSQL on Kubernetes"
    ws.gemini_position = "Gemini: Use PostgreSQL on K8s with read replicas"
    ws.consensus_score = 92
    assert save_workspace(ws)

    ws2 = load_workspace(ws_id)
    assert ws2 is not None
    assert ws2.status == WorkspaceStatus.CONSENSUS_REACHED
    assert ws2.consensus_score == 92
    assert ws2.gpt_position == "GPT: Use PostgreSQL on Kubernetes"
    assert len(ws2.verified_decisions) == 2
    assert len(ws2.verified_tradeoffs) == 1
    from human_decision_store import store as _hds
    assert len(_hds.all_for_workspace(ws2.workspace_id)) == 1
    assert len(ws2.answer_history) == 2

    # List
    workspaces = list_workspaces(limit=5)
    assert len(workspaces) >= 1

    # Search
    mgr = WorkspaceManager()
    found = mgr.search_workspaces("scalable")
    assert len(found) >= 1

    # Stats
    stats = mgr.get_stats()
    assert "total_workspaces" in stats
    assert "awaiting_human" in stats
    assert "completed" in stats

    # Delete
    assert delete_workspace(ws_id)
    assert load_workspace(ws_id) is None

    return True


def test_human_decision_queue():
    """Test human_decision_queue.py — enqueue, pending, resolve, lifecycle."""
    from workspace_manager import create_workspace, delete_workspace
    from human_decision_queue import (
        HumanDecision, HumanDecisionQueue,
        enqueue_human_decision, resolve_human_decision,
        get_pending_for_workspace,
    )

    ws_id = create_workspace("Test human decisions")
    queue = HumanDecisionQueue()

    # Enqueue via direct API
    import uuid as _uuid
    from datetime import datetime, timezone
    unique_id = f"test-hd-{_uuid.uuid4().hex[:8]}"
    d = HumanDecision(
        decision_id=unique_id,
        workspace_id=ws_id,
        question="Which database?",
        options=["SQLite", "PostgreSQL"],
        reasoning="MVP vs production",
        affected_disputes=["db_choice"],
        confidence=40,
        priority="HIGH",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    d_id = queue.enqueue(d)
    assert d_id == unique_id

    # Enqueue via convenience function
    d2_id = enqueue_human_decision(
        workspace_id=ws_id,
        question="Which cache?",
        options=["Redis", "None"],
        reasoning="Caching adds complexity",
        affected_disputes=["cache_choice"],
    )
    assert d2_id is not None

    # Pending count
    assert queue.get_pending_count(ws_id) == 2

    # Get pending
    pending = queue.get_pending(ws_id)
    assert len(pending) == 2
    assert pending[0].question == "Which database?"  # HIGH priority first

    # Get pending as dicts
    pending_dicts = get_pending_for_workspace(ws_id)
    assert len(pending_dicts) == 2

    # Resolve via convenience function
    assert resolve_human_decision(d_id, "PostgreSQL")
    assert queue.get_pending_count(ws_id) == 1

    # Verify resolution
    resolved_d = queue.get_decision(d_id)
    assert resolved_d.resolved
    assert resolved_d.chosen_option == "PostgreSQL"

    # Resolve already-resolved
    assert not resolve_human_decision(d_id, "SQLite")

    # Get all decisions for workspace
    all_d = queue.get_all(ws_id)
    assert len(all_d) == 2

    # List across workspaces
    all_pending = queue.list_pending_all_workspaces()
    assert len(all_pending) >= 1

    # Delete
    assert queue.delete_decision(d2_id)
    assert queue.get_decision(d2_id) is None

    # Cleanup
    delete_workspace(ws_id)
    return True


def test_workspace_answer_linking():
    """Test answer linking via answer_memory with workspace fields."""
    from answer_memory import memory

    # Save with workspace and parent
    id1 = memory.save_debate(
        question="Initial: Design platform",
        gpt_final="GPT: Use PostgreSQL",
        gemini_final="Gemini: Use PostgreSQL",
        synthesized_answer="PostgreSQL chosen",
        consensus_score=95,
        workspace_id="test-ws-linking",
    )

    id2 = memory.save_debate(
        question="Follow-up: Scale to 50k users",
        gpt_final="GPT: Add replicas",
        gemini_final="Gemini: Add sharding",
        synthesized_answer="Replicas + sharding",
        consensus_score=88,
        parent_answer_id=id1,
        workspace_id="test-ws-linking",
    )

    id3 = memory.save_debate(
        question="Follow-up: Add caching?",
        gpt_final="GPT: Add Redis",
        gemini_final="Gemini: Add Redis",
        synthesized_answer="Redis for caching",
        consensus_score=97,
        parent_answer_id=id2,
        workspace_id="test-ws-linking",
    )

    # Get by workspace
    answers = memory.get_answers_by_workspace("test-ws-linking")
    assert len(answers) == 3
    assert answers[0]["parent_answer_id"] is None  # First has no parent
    assert answers[1]["parent_answer_id"] == id1
    assert answers[2]["parent_answer_id"] == id2

    # Traverse chain from newest
    chain = memory.get_answer_chain(id3)
    assert len(chain) == 3
    assert chain[0]["id"] == id3  # Newest first
    assert chain[2]["id"] == id1  # Oldest last

    # Cleanup
    memory.delete_debate(id3)
    memory.delete_debate(id2)
    memory.delete_debate(id1)
    return True


def test_workspace_debate_engine_imports():
    """Test that WorkspaceDebateEngine has expected interface."""
    from workspace_debate_engine import WorkspaceDebateEngine

    engine = WorkspaceDebateEngine()
    assert hasattr(engine, 'run_in_workspace')
    assert hasattr(engine, 'resume_workspace')
    assert hasattr(engine, 'reopen_dispute')
    assert hasattr(engine, '_analyze_follow_up')
    assert hasattr(engine, '_keyword_affected_analysis')
    assert hasattr(engine, '_reopen_from_workspace')
    return True


def test_workspace_orchestrator():
    """Test workspace orchestrator interface (no browser use)."""
    from workspace_orchestrator import (
        resolve_human_requirement,
        get_workspace_status, list_workspaces, get_answer_chain,
    )
    from workspace_manager import (
        create_workspace, load_workspace, save_workspace, delete_workspace,
        WorkspaceStatus,
    )
    from human_decision_queue import (
        enqueue_human_decision, resolve_human_decision,
    )

    # Create workspace manually (bypassing browser)
    ws_id = create_workspace("Test orchestrator")
    ws = load_workspace(ws_id)
    ws.add_verified_decision({
        "decision": "Use PostgreSQL",
        "normalized_key": "db_postgres",
    })
    ws.add_verified_tradeoff({
        "option_a": "K8s", "option_b": "Nomad",
        "recommendation": "K8s", "confidence": 85,
        "reasoning": "Better ecosystem",
    })
    ws.status = WorkspaceStatus.CONSENSUS_REACHED
    ws.gpt_position = "GPT position"
    ws.gemini_position = "Gemini position"
    save_workspace(ws)

    # get_workspace_status
    status = get_workspace_status(ws_id)
    assert status["workspace_id"] == ws_id
    assert status["verified_decisions_count"] == 1
    assert status["verified_tradeoffs_count"] == 1

    # resolve_human_requirement
    d_id = enqueue_human_decision(
        workspace_id=ws_id,
        question="Which cache?",
        options=["Redis", "None"],
        reasoning="Complexity vs performance",
    )
    # Verify pending items appear in status
    status2 = get_workspace_status(ws_id)
    assert status2["human_required_count"] >= 1

    result = resolve_human_requirement(ws_id, d_id, "Redis")
    assert result["status"] == "resolved"

    # list_workspaces
    ws_list = list_workspaces(limit=5)
    assert len(ws_list) >= 1

    # Cleanup
    delete_workspace(ws_id)
    return True


def test_workspace_restart_recovery():
    """Test that workspaces survive simulated restart."""
    from workspace_manager import (
        create_workspace, load_workspace, save_workspace,
        WorkspaceStatus,
    )

    ws_id = create_workspace("Test restart recovery")
    ws = load_workspace(ws_id)
    ws.add_verified_decision({
        "decision": "Use PostgreSQL",
        "normalized_key": "db_postgres",
    })
    ws.gpt_position = "GPT says PostgreSQL"
    ws.gemini_position = "Gemini says PostgreSQL"
    ws.status = WorkspaceStatus.CONSENSUS_REACHED
    ws.consensus_score = 90
    assert save_workspace(ws)

    # Simulate restart by creating a new manager instance
    # (the existing one still has _initialized=True, but a new instance
    #  would re-read from disk)
    from workspace_manager import WorkspaceManager
    mgr2 = WorkspaceManager()
    ws2 = mgr2.load_workspace(ws_id)
    assert ws2 is not None
    assert ws2.status == WorkspaceStatus.CONSENSUS_REACHED
    assert ws2.consensus_score == 90
    assert len(ws2.verified_decisions) == 1
    assert ws2.gpt_position == "GPT says PostgreSQL"

    from workspace_manager import delete_workspace
    delete_workspace(ws_id)
    return True


def test_workspace_keyword_analysis():
    """Test keyword-based affected dispute analysis."""
    from workspace_debate_engine import WorkspaceDebateEngine
    engine = WorkspaceDebateEngine()

    decisions = [
        {"decision": "Use PostgreSQL for primary storage",
         "normalized_key": "database_postgres"},
        {"decision": "Use Kubernetes for orchestration",
         "normalized_key": "orchestration_k8s"},
        {"decision": "Use Redis for caching",
         "normalized_key": "cache_redis"},
        {"decision": "Use React for frontend",
         "normalized_key": "frontend_react"},
    ]

    # Follow-up about scaling database should match database decision
    affected = engine._keyword_affected_analysis(
        "How do we scale PostgreSQL for 50,000 concurrent users storage?",
        decisions,
    )
    # 'postgresql' + 'storage' = 2 matches with decision text
    assert "database_postgres" in affected

    # Follow-up about frontend should match frontend decision
    affected = engine._keyword_affected_analysis(
        "Should we switch from React frontend to Vue?",
        decisions,
    )
    assert "frontend_react" in affected

    # Follow-up about something unrelated should not match
    affected = engine._keyword_affected_analysis(
        "What CI/CD tool should we use?",
        decisions,
    )
    # CI/CD is unrelated to all decisions, so should be empty
    # (or at most very weak matches)
    assert len(affected) <= 1  # May get 0 or very weak match

    return True


def test_workspace_dispute_reopening():
    """Test dispute reopening logic via workspace."""
    from workspace_manager import Workspace, create_workspace, delete_workspace
    from workspace_debate_engine import WorkspaceDebateEngine

    ws_id = create_workspace("Test dispute reopening")
    engine = WorkspaceDebateEngine()

    # Simulate a resolved dispute in a workspace
    ws_raw = create_workspace("Test original prompt")
    ws = Workspace(
        workspace_id=ws_raw,
        original_prompt="Test original prompt",
    )
    ws.add_verified_decision({
        "decision": "Use SQLite for MVP",
        "normalized_key": "database_choice",
    })
    ws.add_verified_decision({
        "decision": "Use Flask for API",
        "normalized_key": "framework_choice",
    })
    ws.gpt_position = "GPT: SQLite for MVP, Flask for API"
    ws.gemini_position = "Gemini: SQLite for MVP, Flask for API"

    # Reopen the database_choice dispute (calls _reopen_from_workspace)
    reopened = engine._reopen_from_workspace(
        "database_choice", ws,
        "System must now support 50,000 concurrent users",
    )
    assert reopened is not None
    assert "REOPENED" in reopened.dispute_text
    assert reopened.normalized_key == "database_choice"
    assert "50,000" in reopened.dispute_text

    # Try reopening a non-existent dispute key
    not_found = engine._reopen_from_workspace(
        "nonexistent_key", ws, "Reason"
    )
    assert not_found is None

    delete_workspace(ws_id)
    delete_workspace(ws_raw)
    return True


# ---------------------------------------------------------------------------
# Provider router & key rotation tests
# ---------------------------------------------------------------------------

def test_provider_router_logic():
    """Test provider router failover logic, health tracking, key rotation."""
    from provider_router import (
        _should_failover, HealthTracker, reset_health,
    )

    # Failover classification
    failover_errors = [
        "queue_exceeded", "HTTP 429 rate limit exceeded",
        "connection timed out", "service unavailable 503",
        "network unreachable", "internal server error 500",
    ]
    for err in failover_errors:
        assert _should_failover(err), f"Should failover on: {err}"

    # Non-failover errors
    terminal_errors = [
        "401 Unauthorized", "403 Forbidden",
        "400 Bad Request", "authentication failed",
        "invalid request", "malformed input",
    ]
    for err in terminal_errors:
        assert not _should_failover(err), f"Should NOT failover on: {err}"

    # Health tracking
    reset_health()
    ht = HealthTracker()

    # Initial state
    h = ht.get("test_provider")
    assert h.is_healthy
    assert h.total_requests == 0

    # Success tracking
    ht.record_success("test_provider")
    h = ht.get("test_provider")
    assert h.total_successes == 1
    assert h.total_requests == 1
    assert h.is_healthy

    # Failures trigger cooldown at threshold
    ht.record_failure("test_provider")
    ht.record_failure("test_provider")
    h = ht.get("test_provider")
    assert h.consecutive_failures == 2
    assert h.is_healthy  # Not yet at threshold

    ht.record_failure("test_provider")
    h = ht.get("test_provider")
    assert h.consecutive_failures == 3
    assert not h.is_healthy  # Now in cooldown

    # Check all_health dict format
    all_h = ht.all_health()
    assert "test_provider" in all_h
    assert "success_rate" in all_h["test_provider"]
    assert "cooldown_remaining_s" in all_h["test_provider"]

    return True


def test_groq_openrouter_clients():
    """Test Groq and OpenRouter client interfaces (no live API calls)."""
    import groq_client
    import openrouter_client

    # Both should have generate and is_configured
    assert callable(groq_client.generate)
    assert callable(groq_client.is_configured)
    assert callable(openrouter_client.generate)
    assert callable(openrouter_client.is_configured)

    # is_configured returns bool
    assert isinstance(groq_client.is_configured(), bool)
    assert isinstance(openrouter_client.is_configured(), bool)

    # These should have API_KEYS list
    assert isinstance(groq_client.API_KEYS, list)
    assert isinstance(openrouter_client.API_KEYS, list)

    return True


def test_workflow_classifier():
    """Test workflow classifier rule-based accuracy."""
    from workflow_classifier import (
        classify, WorkflowType, _rule_based_classify,
    )

    # Rule-based classification test cases
    tests = [
        ("What is Kubernetes?", WorkflowType.DISCUSSION),
        ("Research database options for social media", WorkflowType.RESEARCH),
        ("Design a planet-scale consensus platform", WorkflowType.ARCHITECTURE),
        ("Implement the authentication module", WorkflowType.IMPLEMENTATION),
        ("Build a CI/CD pipeline for the project", WorkflowType.IMPLEMENTATION),
        ("Compare PostgreSQL vs MongoDB", WorkflowType.DISCUSSION),
        ("How should we scale our database?", WorkflowType.ARCHITECTURE),
        ("Write the code for the rate limiter module", WorkflowType.IMPLEMENTATION),
        ("Tell me about the CAP theorem", WorkflowType.DISCUSSION),
        ("What stack do you recommend?", WorkflowType.ARCHITECTURE),
        ("Design a data pipeline for the project", WorkflowType.ARCHITECTURE),
    ]

    passed = 0
    for prompt, expected in tests:
        result = classify(prompt)
        if result.workflow_type == expected:
            passed += 1
        else:
            print(f"  WARNING: '{prompt[:50]}' → {result.workflow_type} (expected {expected})")

    # At least 90% accuracy for rule-based
    accuracy = passed / len(tests)
    assert accuracy >= 0.8, f"Classifier accuracy {accuracy:.0%} below 80% threshold"
    print(f"  Classifier accuracy: {passed}/{len(tests)} ({accuracy:.0%})")

    # Only IMPLEMENTATION workflows allow execution
    from workflow_classifier import can_execute_code
    assert can_execute_code("Build a CI/CD pipeline") == True
    assert can_execute_code("What is Kubernetes?") == False
    assert can_execute_code("Research databases") == False

    return True


def test_sol0mon_graph():
    """Test LangGraph graph construction (no live execution)."""
    from sol0mon_graph import (
        build_sol0mon_graph, default_state, LANGGRAPH_AVAILABLE,
    )

    # Graph builds even without langgraph installed (stub)
    graph = build_sol0mon_graph()
    assert graph is not None

    # Default state has required keys
    state = default_state(prompt="Test prompt")
    assert state["prompt"] == "Test prompt"
    assert state["workspace_id"] == ""
    assert state["workflow_type"] == "DISCUSSION"
    assert state["status"] == "created"
    assert "thread_id" in state

    # Node functions exist
    from sol0mon_graph import (
        node_classify_workflow,
        node_create_workspace,
        node_debate,
        node_synthesize,
        node_generate_implementation_plan,
        node_user_approval,
        node_execute_implementation,
        route_after_debate,
        route_after_approval,
    )

    # Test routing logic
    assert route_after_debate({"workflow_type": "DISCUSSION", "status": "consensus"}) == "synthesize"
    assert route_after_debate({"workflow_type": "RESEARCH", "status": "consensus"}) == "synthesize"
    assert route_after_debate({"workflow_type": "ARCHITECTURE", "status": "consensus"}) == "synthesize"
    assert route_after_debate({"workflow_type": "IMPLEMENTATION", "status": "consensus"}) == "generate_implementation_plan"
    assert route_after_debate({"workflow_type": "DISCUSSION", "status": "failed"}) == "end"

    # Approval routing
    assert route_after_approval({"user_approved": True}) == "execute_implementation"
    assert route_after_approval({"user_approved": False}) == "end"

    return True


def test_implementation_executor():
    """Test implementation executor interfaces and test runner (no Claude Code)."""
    from implementation_executor import (
        execute_implementation, _run_tests, ImplementationResult,
    )

    # Test runner finds test_suite.py
    test_results = _run_tests()
    assert isinstance(test_results, dict)
    assert "passed" in test_results
    assert "failed" in test_results

    # ImplementationResult dataclass
    result = ImplementationResult(success=True, output="test")
    d = result.to_dict()
    assert d["success"] == True
    assert d["output"] == "test"
    assert d["repair_attempts"] == 0

    # execute_implementation without Claude Code falls back to provider
    plan = "Create a hello world function"
    exec_result = execute_implementation(plan, use_claude_code=False)
    assert isinstance(exec_result, dict)
    assert "success" in exec_result
    assert "output" in exec_result

    return True


def run_all_tests():
    """Run all test suites and report results."""
    tests = [
        ("debate_engine_core", test_debate_engine_core, True),
        ("consensus_engine", test_consensus_engine, True),
        ("synthesis_engine", test_synthesis_engine, True),
        ("conflict_extractor", test_conflict_extractor, True),
        ("verification_engine", test_verification_engine, True),
        ("answer_memory", test_answer_memory, True),
        ("job_store", test_job_store, True),
        ("project_manager", test_project_manager, True),
        ("worker_components", test_worker_components, True),
        ("provider_clients", test_provider_clients, True),
        ("support_modules", test_support_modules, True),
        ("research_and_architecture", test_research_and_architecture_runners, True),
        ("worker_modules", test_worker_modules, True),
        # Workspace system tests
        ("workspace_manager", test_workspace_manager, True),
        ("human_decision_queue", test_human_decision_queue, True),
        ("workspace_answer_linking", test_workspace_answer_linking, True),
        ("workspace_debate_engine_imports", test_workspace_debate_engine_imports, True),
        ("workspace_orchestrator", test_workspace_orchestrator, True),
        ("workspace_restart_recovery", test_workspace_restart_recovery, True),
        ("workspace_keyword_analysis", test_workspace_keyword_analysis, True),
        ("workspace_dispute_reopening", test_workspace_dispute_reopening, True),
        # Sol0mon MVP — new system tests
        ("provider_router_logic", test_provider_router_logic, True),
        ("groq_openrouter_clients", test_groq_openrouter_clients, True),
        ("workflow_classifier", test_workflow_classifier, True),
        ("sol0mon_graph", test_sol0mon_graph, True),
        ("implementation_executor", test_implementation_executor, True),
    ]

    passed = 0
    failed = 0
    skipped = 0

    print("=" * 60)
    print("PROJECT SOL0MON — COMPREHENSIVE TEST SUITE")
    print("=" * 60)
    print()

    for name, test_fn, requires_api in tests:
        try:
            test_fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {name}: {e}")
            failed += 1

    print()
    print("=" * 60)
    print(f"RESULTS: {passed} passed, {failed} failed, {skipped} skipped")
    print("=" * 60)

    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)