# Chat Session Requirements

## Purpose

Maintain persistent debate context for a single topic.

---

## Rules

One GPT session per topic.

One Gemini session per topic.

Sessions created once.

Sessions destroyed only after debate completion.

No new chat per dispute.

No new chat per round.

No history reconstruction.

No architecture reposting every round.

---

## Responsibilities

Store:

* original architecture
* resolved decisions
* active dispute
* latest revisions

Maintain conversation continuity.

Preserve model memory throughout debate.

---

## Interface

start()

ask(prompt)

close()

---

## Requirements

Session must survive:

* multiple disputes
* multiple rounds
* consensus checks

Session must not:

* recreate history
* lose context
* reset state

---

## Failure Recovery

If session fails:

* restore checkpoint
* continue debate

Never start a fresh debate.
