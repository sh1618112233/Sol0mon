# Worker Requirements

## Purpose

Provide model access layer.

Workers do not contain debate logic.

---

## Files

gpt_worker.py

gemini_worker.py

---

## Responsibilities

Create session.

Send prompts.

Return responses.

Close session.

---

## Interface

start()

ask(prompt)

close()

---

## Forbidden

Workers must not:

* score consensus
* verify claims
* synthesize answers
* extract conflicts
* prioritize disputes

---

## Requirements

Use persistent sessions.

Never recreate sessions automatically.

Never rebuild conversation history manually.

Never regenerate architecture unless explicitly requested.

Debate only active disputes.
