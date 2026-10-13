# Debate Engine Requirements

## Purpose

The Debate Engine exists to resolve genuine architectural disagreements between GPT and Gemini.

Its purpose is NOT:

* To maximize consensus scores.
* To force agreement.
* To make models converge artificially.
* To generate a compromise when disagreement remains valid.

Its purpose IS:

* Identify real disagreements.
* Debate those disagreements.
* Resolve them when evidence supports resolution.
* Preserve disagreement when evidence does not support resolution.

---

# Core Design Principles

## Principle 1

Research Quality Above Everything

Speed optimizations must never reduce:

* research quality
* factual accuracy
* implementation quality
* architecture quality

---

## Principle 2

No Artificial Consensus

Consensus must never be increased by:

* wording similarity
* formatting similarity
* summarization similarity
* forced averaging
* compromise generation

Consensus exists only when a dispute is genuinely resolved.

---

## Principle 3

Debate Only Active Disputes

The engine must not repeatedly debate:

* already resolved topics
* equivalent topics
* cosmetic differences

Only unresolved disputes remain active.

---

## Principle 4

Resolved Decisions Become Permanent Context

Once a dispute is resolved:

* remove it from active debate
* store the agreed decision
* inject it into future debate context

Resolved decisions must never be reopened unless verification identifies a contradiction.

---

## Principle 5

Preserve Valid Disagreement

If evidence supports multiple valid approaches:

* disagreement may remain
* dispute may deadlock
* system must not force convergence

---

# Session Architecture

## GPT Session

One persistent GPT session per topic.

Rules:

* created once
* reused for entire debate
* never recreated mid-debate

---

## Gemini Session

One persistent Gemini session per topic.

Rules:

* created once
* reused for entire debate
* never recreated mid-debate

---

## Forbidden

Do NOT:

* create a new chat per dispute
* create a new chat per round
* reset model memory
* restart sessions

Doing so destroys debate continuity.

---

# Research Phase

Research occurs before architecture generation.

Flow:

User Question

↓

GPT Research

↓

Gemini Research

↓

Evidence Extraction

↓

Evidence Verification

↓

Architecture Generation

---

# Architecture Generation

GPT produces architecture.

Gemini produces architecture.

Architectures are generated once.

They become:

* GPT Master Architecture
* Gemini Master Architecture

These remain immutable reference documents.

---

# Conflict Extraction

Conflict extraction runs once.

Input:

* GPT Master Architecture
* Gemini Master Architecture

Output:

* list of validated disputes

Only genuine disputes may enter debate.

---

# Dispute Requirements

Each dispute must contain:

* dispute text
* normalized key
* GPT source position
* Gemini source position
* GPT current position
* Gemini current position
* classification
* consensus score

---

# Debate Process

Debate occurs one dispute at a time.

Priority order:

HIGH

↓

MEDIUM

↓

LOW

---

# Debate Round

For each dispute:

1. GPT critiques Gemini.
2. Gemini critiques GPT.
3. GPT revises.
4. Gemini revises.
5. Consensus check occurs every 3 rounds.

---

# Consensus Checks

Performed by Cerebras.

Responsibilities:

* classify dispute
* calculate consensus score
* update dispute state

Possible outcomes:

RESOLVED

EQUIVALENT

UNRESOLVED

---

# Consensus Rules

Consensus score is:

0-100

Where:

100 = identical positions

0 = complete disagreement

Consensus score is informational only.

Consensus score alone does not resolve a dispute.

Resolution requires:

RESOLVED

or

EQUIVALENT

classification.

---

# Deadlock Detection

Deadlock is allowed.

Deadlock occurs when:

* dispute remains unresolved
* consensus score plateaus
* round limit reached

Deadlocked disputes remain recorded.

Deadlocked disputes are sent to verification.

---

# Verifier Rules

Verifier uses Cerebras.

Verifier responsibilities:

* verify claims
* verify evidence
* detect false consensus
* detect contradictions
* audit unresolved disputes

Verifier does NOT:

* debate
* create new architecture
* force agreement

---

# Cross-Dispute Consistency

Before synthesis:

run consistency audit.

Purpose:

detect contradictions between resolved decisions.

Examples:

Decision A:
Use Cassandra

Decision B:
Use PostgreSQL as primary datastore

If both apply to same system layer:

contradiction detected.

---

# Architecture Memory

Maintain:

resolved_architecture_state

Contains:

* resolved disputes
* agreed positions
* rationale

This state must be available throughout debate.

Purpose:

Prevent resolved disputes from reappearing.

---

# Duplicate Response Protection

The system must detect:

* repeated responses
* paraphrased repetition
* debate stagnation

Repeated responses must not advance consensus.

---

# Forbidden Behaviors

The debate engine must never:

* regenerate full architectures every round
* re-extract disputes every round
* reopen resolved disputes
* force consensus
* discard deadlocks
* ignore verifier findings
* overwrite original architecture positions
* use wording similarity as consensus

---

# Final Synthesis Requirements

Inputs:

* verified research
* resolved disputes
* architecture memory
* verifier results

Do NOT synthesize from debate logs.

Do NOT synthesize from consensus score alone.

The final answer must represent:

* verified research
* resolved architectural decisions
* verified implementation strategy

---

# Success Criteria

The debate engine is considered successful when:

1. Only real disputes are debated.
2. Resolved disputes never reappear.
3. Artificial consensus is impossible.
4. Deadlocks are preserved.
5. Research quality is maintained.
6. Verifier can overturn false consensus.
7. Final architecture reflects resolved decisions.
8. Final implementation plan reflects verified architecture.
9. Debate continuity is preserved across the entire topic.
10. Final output exceeds the quality of a single-model response.
