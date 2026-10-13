# Known Bugs And Fixes

## Bug #1

Problem:

Gemini repeatedly posted the same answer.

Root Cause:

No effective stagnation detection.

Fix:

Semantic similarity tracking.

Repeated responses trigger:

* Stagnation warning
* Forced opponent targeting
* Deadlock if unchanged

---

## Bug #2

Problem:

Consensus increased without real agreement.

Root Cause:

Dispute counts used as consensus score.

Fix:

Continuous consensus scoring.

Verifier audits consensus.

---

## Bug #3

Problem:

Resolved decisions disappeared.

Root Cause:

No architecture memory.

Fix:

Resolved architecture state.

All future disputes receive resolved decisions.

---

## Bug #4

Problem:

Contradictory decisions could both be accepted.

Example:

Use Cassandra

and

Use PostgreSQL

for same layer.

Fix:

Cross-dispute consistency audit.

---

## Bug #5

Problem:

Conflict extractor repeatedly re-created disputes.

Root Cause:

Re-extraction every consensus cycle.

Fix:

Extract once.

Debate existing disputes only.

---

## Bug #6

Problem:

Verifier only reviewed unresolved disputes.

Fix:

Verifier reviews:

* Resolved disputes
* Deadlocked disputes

---

## Bug #7

Problem:

Synthesis ignored debate outcomes.

Fix:

Resolution map introduced.

Synthesis built from verified resolutions.

---

## Bug #8

Problem:

Source architecture drift.

Root Cause:

Original positions overwritten.

Fix:

Separate:

* Source position
* Current position

Source positions remain immutable.

---

## Bug #9

Problem:

Debating all disputes simultaneously.

Fix:

Single-dispute queue.

One dispute debated at a time.

---

## Bug #10

Problem:

Deadlock detection unreliable.

Fix:

Numeric plateau detection.

Deadlock declared when consensus score stops moving.

---

## Remaining Major Gap

Research layer not fully implemented.

Current:

Architecture → Debate

Target:

Research → Evidence → Architecture → Debate → Consensus → Build

This is the highest-priority remaining architectural enhancement.
