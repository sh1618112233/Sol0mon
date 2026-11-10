import synthesis_engine
from decision_renderer import (
    render_architecture,
    render_implementation_plan,
    render_migration_path,
    render_narrative_sections,
    render_risks,
    validate_synthesis_consistency,
)


def debate_120_fixture():
    verification_result = {
        "resolved_claims": [
            {
                "dispute": "Orchestration Engine: Temporal vs custom queue",
                "normalized_key": "orchestration_engine",
                "verification": {
                    "status": "RESOLVED",
                    "confidence": 96,
                    "finding": "Resolved in favor of Temporal durable workflows; rejects custom queue orchestration.",
                },
            },
            {
                "dispute": "Primary Database: PostgreSQL vs SQLite",
                "normalized_key": "primary_database",
                "verification": {
                    "status": "RESOLVED",
                    "confidence": 95,
                    "finding": "Resolved in favor of PostgreSQL primary datastore; rejects SQLite for shared production persistence.",
                },
            },
            {
                "dispute": "Storage Strategy: Cloudflare R2 vs local-only storage",
                "normalized_key": "storage_strategy",
                "verification": {
                    "status": "RESOLVED",
                    "confidence": 94,
                    "finding": "Resolved in favor of Cloudflare R2 immutable storage plus local scratch filesystem; rejects local-only artifact storage.",
                },
            },
            {
                "dispute": "Execution Isolation: Docker vs WebContainers vs Firecracker",
                "normalized_key": "execution_isolation",
                "verification": {
                    "status": "RESOLVED",
                    "confidence": 93,
                    "finding": "Resolved in favor of Docker workers for low-risk jobs and Firecracker microVMs for high-risk jobs; rejects WebContainers for authoritative execution.",
                },
            },
            {
                "dispute": "LLM Gateway: LiteLLM vs direct provider calls",
                "normalized_key": "llm_gateway",
                "verification": {
                    "status": "RESOLVED",
                    "confidence": 92,
                    "finding": "Resolved in favor of LiteLLM gateway; rejects direct provider calls as the only gateway path.",
                },
            },
            {
                "dispute": "Governance Control Plane",
                "normalized_key": "governance_control_plane",
                "verification": {
                    "status": "RESOLVED",
                    "confidence": 91,
                    "finding": "Resolved in favor of Sol0mon control plane for governance.",
                },
            },
            {
                "dispute": "Cost Model: BYOK vs platform-only keys",
                "normalized_key": "cost_model",
                "verification": {
                    "status": "RESOLVED",
                    "confidence": 90,
                    "finding": "Resolved in favor of hybrid BYOK model; rejects platform-only keys for all tenants.",
                },
            },
            {
                "dispute": "Infrastructure Hosting and Provider Routing",
                "normalized_key": "infrastructure_hosting",
                "verification": {
                    "status": "RESOLVED",
                    "confidence": 89,
                    "finding": "Resolved in favor of provider routing through a provider abstraction layer; rejects single-provider hosting strategy.",
                },
            },
        ],
        "equivalent_claims": [],
        "unresolved_claims": [],
    }
    governance_context = {
        "verified_decisions": [],
        "verified_tradeoffs": [],
        "human_required": [],
    }
    return verification_result, governance_context


def test_architecture_is_grouped_without_disputes_or_rationale():
    vr, gc = debate_120_fixture()
    architecture = render_architecture(vr, gc)

    for heading in (
        "## Orchestration Layer",
        "## Persistence Layer",
        "## Storage Layer",
        "## Execution Layer",
        "## LLM Gateway Layer",
        "## Governance Layer",
        "## Cost Layer",
        "## Infrastructure Layer",
    ):
        assert heading in architecture

    assert "- Temporal durable workflows" in architecture
    assert "- PostgreSQL primary datastore" in architecture
    assert "- Cloudflare R2 immutable storage" in architecture
    assert "- Local scratch filesystem" in architecture
    assert "- Docker workers" in architecture
    assert "- Firecracker microVMs" in architecture
    assert "- LiteLLM" in architecture
    assert "- Sol0mon control plane" in architecture
    assert "- Hybrid BYOK model" in architecture
    assert "- Provider abstraction layer" in architecture

    assert "Orchestration Engine:" not in architecture
    assert "Primary Database:" not in architecture
    assert "Storage Strategy:" not in architecture
    assert "Resolved in favor" not in architecture
    assert "rejects" not in architecture.lower()


def test_implementation_plan_is_actionable():
    vr, gc = debate_120_fixture()
    plan = render_implementation_plan(vr, gc)

    assert "Step 1\nDeploy PostgreSQL" in plan
    assert "Step 2\nDeploy Temporal" in plan
    assert "Step 3\nConfigure Cloudflare R2" in plan
    assert "Deploy LiteLLM" in plan
    assert "Deploy Docker Workers" in plan
    assert "Deploy Firecracker Workers" in plan
    assert "Configure BYOK Credential Resolver" in plan
    assert "Storage Strategy" not in plan
    assert "Resolved in favor" not in plan


def test_migration_path_is_architecture_driven():
    vr, gc = debate_120_fixture()
    path = render_migration_path(vr, gc)

    assert "## Phase 1 MVP" in path
    assert "Core persistence" in path
    assert "Core orchestration" in path
    assert "Docker execution" in path
    assert "## Phase 2 Scale" in path
    assert "Object storage" in path
    assert "Gateway abstraction" in path
    assert "Provider routing" in path
    assert "## Phase 3 Enterprise" in path
    assert "Firecracker isolation" in path
    assert "Advanced governance" in path
    assert "Phase 1. Orchestration" not in path


def test_risks_match_actual_verified_components_only():
    vr, gc = debate_120_fixture()
    risks = render_risks(vr, gc)

    expected = {
        "## Temporal\nOperational complexity",
        "## PostgreSQL\nMigration complexity",
        "## Cloudflare R2\nVendor dependency",
        "## Firecracker\nOperational overhead",
        "## LiteLLM\nGateway dependency",
        "## BYOK\nSupport complexity",
    }
    for item in expected:
        assert item in risks
    assert "WebContainers" not in risks
    assert "custom queue" not in risks


def test_debate_120_has_no_false_contradictions_and_is_deterministic():
    vr, gc = debate_120_fixture()
    first = render_narrative_sections(vr, gc)
    second = render_narrative_sections(vr, gc)

    assert first == second
    assert validate_synthesis_consistency(first, vr, gc) == []

    answer = synthesis_engine.synthesize_answer(
        question="Debate #120",
        gpt_final="",
        gemini_final="",
        validated_disputes=[],
        consensus_score=100,
        verification_result=vr,
        governance_context=gc,
    )
    assert "# Contradiction Warning" not in answer


def test_actual_rejected_option_still_triggers_validator():
    vr, gc = debate_120_fixture()
    bad = "# Architecture\nUse WebContainers for authoritative execution."
    contradictions = validate_synthesis_consistency(bad, vr, gc)

    assert contradictions
    assert contradictions[0]["type"] == "REJECTED_OPTION_RECOMMENDED"


def test_synthesis_makes_no_model_calls(monkeypatch):
    vr, gc = debate_120_fixture()

    def fail_generate(*_args, **_kwargs):
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
    assert "## Orchestration Layer" in first
    assert "# Contradiction Warning" not in first
