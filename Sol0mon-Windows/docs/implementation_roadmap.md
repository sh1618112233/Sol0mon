# Consensus Platform - Detailed Implementation Roadmap

## Project Goal

Build an autonomous consensus platform capable of:

Research → Debate → Consensus → Architecture → Implementation Planning → Build Generation

The platform must:

* Produce higher-quality outputs than a single model.
* Avoid artificial consensus.
* Preserve disagreement when disagreement is technically valid.
* Generate implementation-ready architectures.
* Eventually generate implementation-ready code.

---

# Current Status

## Completed

* GPT worker
* Gemini worker
* Conflict extraction
* Consensus scoring
* Verification engine
* Synthesis engine
* Debate engine prototype
* Memory storage
* Cerebras integration

## Major Problems Identified

### Problem 1

Repeated GPT/Gemini responses.

Result:

* Debate stalls.
* No progress.
* False deadlocks.

### Problem 2

Artificial consensus.

Result:

* Score increases despite disagreement.

### Problem 3

Resolved decisions lost.

Result:

* Previously resolved topics reappear later.

### Problem 4

No research-first workflow.

Current:

Architecture → Debate

Target:

Research → Evidence → Architecture → Debate

### Problem 5

Final synthesis does not fully rebuild architecture from verified decisions.

---

# Phase 1 - Debate Engine Stabilization

Priority: Critical

Goal:

Create a reliable dispute resolution engine.

## Tasks

### 1.1 Duplicate Response Detection

Implement:

* Semantic similarity scoring
* Stagnation detection
* Forced opponent engagement

Success Criteria:

* Repeated answers detected.
* Models forced to address opposing arguments.

---

### 1.2 Resolved Architecture Memory

Implement:

resolved_architecture_state

Contains:

* resolved dispute
* agreed position
* rationale

Injected into all future disputes.

Success Criteria:

* Resolved decisions never reappear.

---

### 1.3 Single Dispute Queue

Implement:

HIGH → MEDIUM → LOW

Processing order.

Success Criteria:

* Core architecture decisions resolved first.

---

### 1.4 Deadlock Detection

Implement:

* Numeric consensus plateau tracking
* Stagnation tracking

Success Criteria:

* Infinite loops impossible.

---

### 1.5 Cross-Dispute Consistency Audit

Implement:

Consistency verification across all resolved decisions.

Success Criteria:

* Contradictory decisions detected.

---

# Phase 2 - Research Layer

Priority: Critical

Current biggest missing capability.

Goal:

Research before architecture generation.

---

## Research Flow

User Prompt

↓

GPT Research

↓

Gemini Research

↓

Evidence Extraction

↓

Evidence Comparison

↓

Architecture Generation

---

## Tasks

### 2.1 Research Runner

Create:

research_runner.py

Responsibilities:

* Run GPT research
* Run Gemini research
* Collect findings

Outputs:

* research findings
* evidence
* assumptions

---

### 2.2 Evidence Extractor

Create:

evidence_extractor.py

Responsibilities:

Extract:

* claims
* facts
* evidence
* recommendations

from research output.

---

### 2.3 Research Verification

Create:

verification_research.py

Responsibilities:

Verify:

* factual claims
* assumptions
* recommendations

using Cerebras verifier.

---

### 2.4 Research Consensus

Generate:

shared research foundation

before architecture generation.

Success Criteria:

Architecture begins from verified evidence.

---

# Phase 3 - Architecture Generation

Priority: High

Goal:

Generate competing architectures from verified research.

---

## GPT Architecture

Generate:

* architecture
* implementation approach
* scaling strategy
* tradeoffs

---

## Gemini Architecture

Generate:

* architecture
* implementation approach
* scaling strategy
* tradeoffs

---

## Output

Two independent architecture proposals.

---

# Phase 4 - Conflict Extraction

Priority: High

Goal:

Identify only real disagreements.

---

## Tasks

### 4.1 Conflict Extraction

Current:

extract_conflicts()

Improve:

* structural disagreements
* implementation disagreements
* scaling disagreements

Ignore:

* wording
* formatting
* naming

---

### 4.2 Conflict Validation

Remove:

* duplicates
* equivalents
* cosmetic differences

Success Criteria:

Only real disputes enter debate.

---

# Phase 5 - Consensus Debate Engine

Priority: High

Goal:

Resolve disputes.

---

## Rules

One GPT session.

One Gemini session.

Persistent throughout topic.

No new chats.

No architecture reposting.

---

## Flow

Dispute

↓

Critique

↓

Revision

↓

Consensus Check

↓

Resolved or Continue

---

## Consensus Checks

Every 3 rounds.

Cerebras:

* scores convergence
* verifies claims

Cerebras does NOT force consensus.

---

# Phase 6 - Verification Layer

Priority: Critical

Goal:

Reject false consensus.

---

## Inputs

Resolved disputes

Deadlocked disputes

Research findings

Architecture decisions

---

## Outputs

Resolved

Equivalent

Unresolved

Verification score

---

## Rule

Verifier may reopen disputes.

Success Criteria:

Artificial consensus impossible.

---

# Phase 7 - Architecture Reconstruction

Priority: Critical

Currently Missing.

Goal:

Build final architecture from verified decisions.

---

## Inputs

Verified research

Resolved disputes

Verifier findings

Deadlocks

---

## Process

Create:

final_architecture_builder.py

Responsibilities:

* Apply resolved decisions
* Remove rejected options
* Integrate verifier findings
* Generate coherent architecture

---

## Output

Single architecture document.

---

# Phase 8 - Implementation Planning

Priority: High

Goal:

Convert architecture into build plan.

---

## Generate

* project structure
* repositories
* services
* APIs
* infrastructure
* database schema
* deployment plan

---

## Output

Implementation blueprint.

---

# Phase 9 - Code Generation

Priority: Future

Goal:

Generate production implementation.

---

## Flow

Architecture

↓

Implementation Plan

↓

Code Generation

↓

Code Review

↓

Verification

↓

Consensus

↓

Final Code

---

## Rules

No direct code generation from raw debate.

Code must be generated from final architecture.

---

# Phase 10 - Telegram Integration

Priority: Medium

Goal:

User-facing interface.

---

## Telegram Responsibilities

Submit prompt

Receive questions

Receive progress

Receive final output

---

## Telegram Must Not Receive

Full debate logs

Internal reasoning

Intermediate drafts

---

# Phase 11 - Performance Optimization

Priority: High

Current Bottlenecks

* Conflict extraction
* Verification
* Consensus scoring

---

## Optimizations

### Cerebras

Primary verifier.

### Groq

Failover verifier.

### OpenRouter

Secondary failover.

### Ollama

Emergency fallback only.

---

# Success Criteria

The system is considered complete when:

1. Research is verified before architecture generation.
2. GPT and Gemini debate only genuine disputes.
3. Artificial consensus is impossible.
4. Resolved decisions are preserved globally.
5. Contradictions are automatically detected.
6. Final architecture is reconstructed from verified decisions.
7. Implementation plans are generated automatically.
8. Code generation can be performed from the final architecture.
9. Final outputs consistently exceed single-model quality.
10. Human intervention is not required during debate execution.
