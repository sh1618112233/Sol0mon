import synthesis_engine
from decision_renderer import (
    render_architecture,
    render_implementation_plan,
    render_migration_path,
    render_narrative_sections,
    render_risks,
)


def debate_120_inputs():
    verification_result = {
        "resolved_claims": [
            {
                "dispute": "Orchestration runtime",
                "normalized_key": "orchestration",
                "verification": {
                    "status": "RESOLVED",
                    "confidence": 96,
                    "finding": "Temporal durable workflows",
                },
            },
            {
                "dispute": "Primary datastore",
                "normalized_key": "persistence",
                "verification": {
                    "status": "RESOLVED",
                    "confidence": 95,
                    "finding": "PostgreSQL primary datastore",
                },
            },
            {
                "dispute": "Artifact and scratch storage",
                "normalized_key": "storage",
                "verification": {
                    "status": "RESOLVED",
                    "confidence": 94,
                    "finding": "Cloudflare R2 immutable storage and local scratch filesystem",
                },
            },
            {
                "dispute": "Execution isolation",
                "normalized_key": "execution",
                "verification": {
                    "status": "RESOLVED",
                    "confidence": 93,
                    "finding": "Docker workers and Firecracker microVMs",
                },
            },
            {
                "dispute": "LLM routing",
                "normalized_key": "llm",
                "verification": {
                    "status": "RESOLVED",
                    "confidence": 92,
                    "finding": "LiteLLM and Sol0mon control plane",
                },
            },
            {
                "dispute": "Deployment posture",
                "normalized_key": "infrastructure",
                "verification": {
                    "status": "RESOLVED",
                    "confidence": 91,
                    "finding": "Provider abstraction layer",
                },
            },
        ]
    }
    return verification_result, {"verified_decisions": [], "verified_tradeoffs": [], "human_required": []}


def test_architecture_contains_domain_sections():
    vr, gc = debate_120_inputs()
    architecture = render_architecture(vr, gc)

    for section in (
        "Orchestration",
        "Persistence",
        "Storage",
        "Execution",
        "LLM Gateway",
        "Infrastructure",
    ):
        assert section in architecture

    assert "Temporal durable workflows" in architecture
    assert "PostgreSQL primary datastore" in architecture
    assert "Cloudflare R2 immutable storage" in architecture
    assert "Local scratch filesystem" in architecture
    assert "Docker workers" in architecture
    assert "Firecracker microVMs" in architecture
    assert "LiteLLM" in architecture
    assert "Sol0mon control plane" in architecture
    assert "Provider abstraction layer" in architecture


def test_implementation_plan_contains_ordered_steps():
    vr, gc = debate_120_inputs()
    plan = render_implementation_plan(vr, gc)

    assert "Step 1\nDeploy PostgreSQL" in plan
    assert "Step 2\nDeploy Temporal" in plan
    assert "Step 3\nConfigure Cloudflare R2" in plan
    assert "Step 4\nConfigure local scratch filesystem" in plan
    assert "Step 5\nDeploy LiteLLM" in plan
    assert "Step 6\nDeploy Docker Workers" in plan
    assert "Step 7\nDeploy Firecracker Workers" in plan
    assert "Step 8\nDeploy Sol0mon Control Plane" in plan
    assert "Step 9\nConfigure Provider Routing" in plan


def test_migration_path_contains_three_phases():
    vr, gc = debate_120_inputs()
    path = render_migration_path(vr, gc)

    assert "Phase 1 MVP" in path
    assert "Phase 2 Scale" in path
    assert "Phase 3 Enterprise" in path
    assert "Core persistence" in path
    assert "Gateway abstraction" in path
    assert "Firecracker isolation" in path


def test_risks_are_generated_from_verified_decisions_only():
    vr, gc = debate_120_inputs()
    risks = render_risks(vr, gc)

    assert "## Temporal\nOperational complexity" in risks
    assert "## PostgreSQL\nMigration complexity" in risks
    assert "## Cloudflare R2\nVendor dependency" in risks
    assert "## Firecracker\nOperational overhead" in risks
    assert "## LiteLLM\nGateway dependency" in risks


def test_narrative_is_deterministic_and_not_repeated_decision_blocks():
    vr, gc = debate_120_inputs()
    first = render_narrative_sections(vr, gc)
    second = render_narrative_sections(vr, gc)

    assert first == second
    plan = first.split("# Implementation Plan", 1)[1].split("# Migration Path", 1)[0]
    assert "Temporal durable workflows" not in plan
    assert "Deploy Temporal" in plan
    assert "# Architecture" in first
    assert "# Implementation Plan" in first
    assert "# Migration Path" in first
    assert "# Risks" in first


def test_synthesis_default_path_makes_no_model_calls(monkeypatch):
    vr, gc = debate_120_inputs()

    def fail_generate(*args, **kwargs):
        raise AssertionError("generate() must not be called")

    monkeypatch.setattr(synthesis_engine, "generate", fail_generate)

    first = synthesis_engine.synthesize_answer(
        question="Debate #120",
        gpt_final="",
        gemini_final="",
        validated_disputes=[],
        consensus_score=100,
        verification_result=vr,
        governance_context=gc,
    )
    second = synthesis_engine.synthesize_answer(
        question="Debate #120",
        gpt_final="",
        gemini_final="",
        validated_disputes=[],
        consensus_score=100,
        verification_result=vr,
        governance_context=gc,
    )

    assert first == second
    assert "# Verified Decisions" in first
    assert "# Verified Tradeoffs" in first
    assert "# Human Decisions Required" in first
    assert "# Architecture" in first
    assert "# Implementation Plan" in first
    assert "# Migration Path" in first
    assert "# Risks" in first
    assert "Orchestration Layer" in first
