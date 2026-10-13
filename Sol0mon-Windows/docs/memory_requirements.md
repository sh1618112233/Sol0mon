# Memory Requirements

## Purpose

The memory system exists to preserve valuable project knowledge across runs.

The purpose is to avoid repeating completed work.

The purpose is NOT to store conversations.

The purpose is NOT to store debate transcripts.

The purpose is NOT to store model reasoning.

Memory stores outcomes.

---

# Core Principle

Store decisions.

Do not store discussions.

Store conclusions.

Do not store debate history.

Store verified knowledge.

Do not store temporary reasoning.

---

# Why Memory Exists

Without memory:

* research repeats
* architecture decisions repeat
* resolved disputes reappear
* implementation planning restarts
* project continuity is lost

Memory provides continuity between executions.

---

# Memory Categories

## Research Memory

Stores:

* verified findings
* verified evidence
* accepted assumptions
* accepted tradeoffs

Purpose:

Prevent repeated research.

---

## Architecture Memory

Stores:

* final architecture
* resolved architecture decisions
* accepted design patterns
* accepted constraints

Purpose:

Prevent architecture drift.

---

## Debate Memory

Stores:

* resolved disputes
* shared positions
* consensus outcomes

Purpose:

Prevent resolved disputes from re-entering debate.

---

## Implementation Memory

Stores:

* implementation decisions
* repository decisions
* infrastructure decisions
* deployment decisions

Purpose:

Maintain build continuity.

---

# What Must Be Stored

Every completed debate should store:

Question

Final Synthesized Answer

Final Architecture

Consensus Score

Resolved Disputes

Verification Result

Timestamp

Project Identifier

---

# What Should Be Stored

Where available:

Research Summary

Implementation Decisions

Architecture Decisions

Deployment Decisions

Build Decisions

---

# What Must Never Be Stored

Do not store:

Full GPT transcripts

Full Gemini transcripts

Critique history

Debate rounds

Prompt chains

Chain of thought

Internal reasoning

Repeated revisions

Temporary disagreements

Duplicate content

---

# Debate Integration

When a dispute is resolved:

Store:

Dispute

Shared Position

Rationale

Verification Status

The dispute must not be debated again unless:

* verifier detects contradiction
* architecture changes invalidate decision

---

# Research Integration

Before new research:

Load relevant memory.

Use prior verified findings.

Avoid repeating completed research.

Research memory must never override new evidence.

Evidence always wins.

---

# Architecture Integration

Before architecture generation:

Load:

* architecture memory
* resolved decisions
* verified findings

Architecture generation should build on prior knowledge.

Architecture should not restart from zero.

---

# Synthesis Integration

Before synthesis:

Load:

* verified findings
* architecture decisions
* resolved disputes
* verifier results

Synthesis should use memory as source material.

Synthesis should not use debate transcripts.

---

# Memory Size Management

Memory must remain compact.

Store:

Decision

Reason

Verification Status

Source

Do not store large text blocks.

Do not store repetitive content.

Summaries are preferred over raw transcripts.

---

# Context Building

Memory context should contain:

Relevant Research

Relevant Architecture Decisions

Relevant Resolved Disputes

Relevant Implementation Decisions

Only relevant items should be loaded.

Avoid loading unrelated project history.

---

# Required Functions

save_debate()

save_research()

save_architecture()

save_decision()

build_context()

search_memory()

load_project()

load_relevant_context()

---

# Retrieval Rules

Search by:

Question

Architecture Topic

Technology

Component

Project

Dispute Key

Return only relevant records.

Avoid loading entire memory database.

---

# Performance Rules

Memory retrieval must be fast.

Memory should reduce work.

Memory should never become a bottleneck.

Memory should never become a transcript archive.

---

# Relationship To Chat Sessions

Chat Session:

Maintains active debate context.

Memory:

Maintains long-term project knowledge.

Chat Session and Memory serve different purposes.

Chat Session = temporary working memory.

Memory = persistent project knowledge.

---

# Success Criteria

The system should:

* remember completed work
* avoid repeated research
* avoid repeated debates
* preserve architecture decisions
* preserve implementation decisions
* maintain project continuity

while remaining compact, relevant, and free of transcript bloat.

Memory stores knowledge.

Memory does not store conversations.
