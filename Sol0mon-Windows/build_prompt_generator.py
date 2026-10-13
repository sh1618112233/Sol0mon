"""
Project Sol0mon - Phase 4: Build Prompt Generator.

Converts a synthesized answer (human-readable) into the structured pieces a
coding agent needs, then into a Build Prompt that the existing debate
methodology can run against to produce an implementation specification.

Flow (pure functions, no I/O, no LLM calls here):

    synthesized answer
        -> extract_decisions()      # VERIFIED_DECISION outcomes
        -> extract_constraints()    # verified decisions reused as immutable rules
        -> extract_tradeoffs()      # VERIFIED_TRADEOFF outcomes
        -> generate_build_prompt()  # markdown prompt for the implementation debate

The Build Prompt is then handed to the existing debate engine (see
build_spec_engine.build_spec) which runs GPT Position -> Gemini Position ->
Critique -> Revision -> Consensus and returns the implementation_spec text.

Shape of `debate_result` (from pipeline_runner.execute / DebateEngine.run):
    {
      "synthesized_answer": str,
      "gpt_final": str, "gemini_final": str,
      "validated_disputes": [ {outcome_type, governed_outcome, normalized_key, ...} ],
      "verified_decisions":   [ GovernedOutcome.to_dict() ],   # outcome_type == VERIFIED_DECISION
      "verified_tradeoffs":   [ GovernedOutcome.to_dict() ],   # outcome_type == VERIFIED_TRADEOFF
      "human_required":       [ GovernedOutcome.to_dict() ],   # outcome_type == HUMAN_REQUIRED
      "final_consensus_score": int,
      ...
    }

A "constraint" is not a governance concept; it is a verified decision reused
as immutable context (mirrors debate_engine.ArchitectureMemory
.get_injection_context and workspace_manager.Workspace.get_constraint_context).
"""

from __future__ import annotations

import os
import re
from typing import Any

# Heading sections every implementation specification must contain. The build
# prompt instructs the implementation debate to produce exactly these.
#
# NOTE: ``SPEC_SECTIONS`` is the LEGACY audit-debate shape (used when a caller
# passes ``debate_fn=<callable>`` to ``build_spec_engine.build_spec``). The
# production default (``debate_fn=None``, the Phase 3 deterministic path) emits
# an executable spec with the sections in ``EXECUTABLE_SPEC_SECTIONS`` instead;
# see ``validate_executable_spec_sections`` for the enforced contract. The two
# lists coexist so existing audit-path callers / tests stay byte-for-byte
# unchanged; production never builds from the legacy audit shape anymore.
SPEC_SECTIONS: list[str] = [
    "architecture",
    "components",
    "file structure",
    "dependencies",
    "constraints",
    "testing requirements",
    "acceptance criteria",
]

# The 6 sections every executable implementation specification MUST contain.
# The deterministic Phase 3 renderer emits exactly this shape and the defence-
# in-depth gate in ``build_pipeline.execute_build_spec`` rejects any spec text
# that is missing any of them (it never lets a decision record / debate
# summary / verification report reach OpenCode). Headings are matched
# case-insensitively on the bare phrase, exactly like ``SPEC_SECTIONS``.
EXECUTABLE_SPEC_SECTIONS: list[str] = [
    "implementation objective",
    "files to create",
    "files to modify",
    "implementation steps",
    "acceptance criteria",
    "output contract",
]


def _key(d: dict) -> str:
    return d.get("dispute_key") or d.get("normalized_key") or d.get("dispute_text") or ""


def _normalize_bucket(result: dict, bucket: str, fallback: bool = True) -> list[dict]:
    """Return a governance outcome bucket as a list. Falls back to scanning
    validated_disputes by outcome_type when the bucket is absent (older shape)."""
    items = result.get(bucket) or []
    if items:
        return items
    if not fallback:
        return []
    disputes = result.get("validated_disputes") or []
    want = {
        "verified_decisions": "VERIFIED_DECISION",
        "verified_tradeoffs": "VERIFIED_TRADEOFF",
        "human_required": "HUMAN_REQUIRED",
    }.get(bucket, "")
    if not want:
        return []
    out = []
    for d in disputes:
        if d.get("outcome_type") == want and d.get("governed_outcome"):
            out.append(d["governed_outcome"])
    return out


def extract_decisions(result: dict) -> list[dict]:
    """VERIFIED_DECISION outcomes -> [{key, decision, reasoning}]."""
    out = []
    for d in _normalize_bucket(result, "verified_decisions"):
        decision = d.get("decision") or d.get("dispute_text") or ""
        if not decision:
            continue
        out.append({
            "key": _key(d),
            "decision": decision,
            "reasoning": d.get("reasoning", ""),
        })
    return out


def extract_constraints(result: dict) -> list[dict]:
    """Verified decisions reused as immutable implementation rules.

    A constraint is a settled decision the implementation MUST honor. Same
    source bucket as extract_decisions, framed as a rule rather than a choice.
    """
    out = []
    for d in _normalize_bucket(result, "verified_decisions"):
        rule = d.get("decision") or d.get("dispute_text") or ""
        if not rule:
            continue
        out.append({
            "key": _key(d),
            "constraint": rule,
            "reasoning": d.get("reasoning", ""),
        })
    return out


def extract_tradeoffs(result: dict) -> list[dict]:
    """VERIFIED_TRADEOFF outcomes -> [{key, option_a, option_b, recommendation, reasoning}]."""
    out = []
    for d in _normalize_bucket(result, "verified_tradeoffs"):
        out.append({
            "key": _key(d),
            "option_a": d.get("option_a", ""),
            "option_b": d.get("option_b", ""),
            "recommendation": d.get("recommendation", ""),
            "reasoning": d.get("reasoning", ""),
        })
    return out


def generate_build_prompt(
    source_answer: str,
    decisions: list[dict] | None = None,
    constraints: list[dict] | None = None,
    tradeoffs: list[dict] | None = None,
    target_components: list[str] | None = None,
) -> str:
    """Compose the markdown prompt the implementation debate will run against.

    The prompt instructs the debaters to produce an implementation
    specification with exactly the SPEC_SECTIONS headings, honoring the
    extracted decisions and constraints. Reuses the existing debate engine
    (the caller runs it); this function only builds the prompt text.
    """
    decisions = decisions or []
    constraints = constraints or []
    tradeoffs = tradeoffs or []

    lines: list[str] = []
    lines.append("# Build Prompt: Implementation Specification")
    lines.append("")
    lines.append("You are producing an **implementation-ready specification** "
                 "for a coding agent. The output must be a single markdown "
                 "document with exactly these top-level sections, in order:")
    lines.append("")
    for i, s in enumerate(SPEC_SECTIONS, 1):
        lines.append(f"{i}. **{s.title()}**")
    lines.append("")
    lines.append("Honor every settled decision and constraint below as "
                 "immutable. Do not reopen agreed architectural choices.")
    lines.append("")

    lines.append("## Source Synthesized Answer")
    lines.append("```markdown")
    lines.append(source_answer.strip() or "(empty)")
    lines.append("```")
    lines.append("")

    lines.append("## Settled Decisions")
    if decisions:
        for d in decisions:
            lines.append(f"- [{d.get('key', '?')}] {d.get('decision', '')}"
                         + (f" — {d.get('reasoning')}" if d.get("reasoning") else ""))
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("## Constraints (immutable)")
    if constraints:
        for c in constraints:
            lines.append(f"- [{c.get('key', '?')}] MUST: {c.get('constraint', '')}"
                         + (f" — {c.get('reasoning')}" if c.get("reasoning") else ""))
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("## Tradeoffs")
    if tradeoffs:
        for t in tradeoffs:
            rec = t.get("recommendation") or "(no recommendation)"
            lines.append(f"- [{t.get('key', '?')}] {t.get('option_a', '?')} "
                         f"vs {t.get('option_b', '?')} -> **{rec}**"
                         + (f" — {t.get('reasoning')}" if t.get("reasoning") else ""))
    else:
        lines.append("- (none)")
    lines.append("")

    if target_components:
        lines.append("## Target Components")
        for c in target_components:
            lines.append(f"- {c}")
        lines.append("")

    lines.append("## Output Contract")
    lines.append("Return ONLY the implementation specification markdown. "
                 "Each of the " + str(len(SPEC_SECTIONS)) + " sections above "
                 "MUST be present and non-empty. Be concrete: name files, "
                 "list dependencies with versions where known, state explicit "
                 "acceptance criteria a test suite can check.")
    lines.append("")
    return "\n".join(lines)


def validate_spec_sections(spec_text: str) -> list[str]:
    """Return the list of SPEC_SECTIONS that are MISSING from spec_text.
    Empty list = all sections present. Heading match is case-insensitive on
    the bare section name (e.g. '## File Structure' satisfies 'file structure')."""
    low = spec_text.lower()
    return [s for s in SPEC_SECTIONS if s not in low]


def validate_executable_spec_sections(spec_text: str) -> list[str]:
    """Return the list of ``EXECUTABLE_SPEC_SECTIONS`` that are MISSING from
    ``spec_text``. Empty list = all executable-spec sections present.

    This is the contract the defence-in-depth gate at
    ``build_pipeline.execute_build_spec`` enforces BEFORE dispatching to the
    implementation executor: a spec text that is missing any of these is
    structurally incomplete (or is a debate summary / verification report /
    decision record dressed up as a spec), and is rejected with a structured
    ``incomplete_executable_spec`` block — the executor is never invoked."""
    low = (spec_text or "").lower()
    return [s for s in EXECUTABLE_SPEC_SECTIONS if s not in low]


# Regex used by ``parse_files_to_create``: a single ``-`` bullet path under
# the ``# Files to Create`` heading. Accepts leading whitespace + ``-``, then
# a relative path. Relative paths only (the gate resolves them under the
# build workspace's working_dir); absolute paths and "None" sentinels are
# ignored. The match is permissive on the path chars (PATH-like) and stops
# at the first whitespace run after the path token; a trailing comment
# (anything after two spaces or "  # ") is allowed and discarded.
_FILES_TO_CREATE_BULLET_RE = re.compile(
    r"^\s*[-*]\s+"                            # bullet + space
    r"((?:[A-Za-z0-9._\-/]+/)*"               # optional directory prefix
    r"[A-Za-z0-9._\-/]+)"                     # filename itself
    r"(?:\s{2,}#.*)?\s*$"                     # optional trailing comment
)


def parse_files_to_create(spec_text: str) -> list[str]:
    """Parse the ``# Files to Create`` section of an executable spec and return
    the list of relative file paths declared there. Used by the post-
    execution QualityGate in ``build_pipeline.execute_build_spec`` to verify
    each required file actually exists in the build workspace after the
    coding agent ran (the "required files exist" success requirement).

    Returns ``[]`` when the section is absent or has no bullet entries; the
    caller treats an empty list as "no required files declared" (the post-
    execution file-existence check is a no-op in that case, the spec already
    failed ``validate_executable_spec_sections`` which DOES require the
    section heading to be present).

    Only relative paths are returned; absolute paths (an OpenCode prompt
    should never declare them) and the ``(no files)`` sentinel are filtered
    out. Pure; no I/O."""
    if not spec_text:
        return []
    # Slice from "# Files to Create" (any case) to the next "#" heading
    # (or end of spec). Case-insensitive heading match.
    low = spec_text.lower()
    start_marker = "# files to create"
    start = low.find(start_marker)
    if start == -1:
        return []
    body = spec_text[start:]
    # Walk to end-of-section (next "# " heading line that is NOT the
    # section's own heading).
    lines = body.splitlines()
    section_body: list[str] = []
    for i, line in enumerate(lines):
        if i == 0:
            continue  # the "# Files to Create" heading itself
        if re.match(r"^#+\s+", line):
            break
        section_body.append(line)
    out: list[str] = []
    seen: set[str] = set()
    for line in section_body:
        m = _FILES_TO_CREATE_BULLET_RE.match(line)
        if not m:
            continue
        path = m.group(1).strip()
        if not path or path.lower() in {"(no", "none"}:
            continue
        if os.path.isabs(path):
            continue
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


if __name__ == "__main__":
    # Smoke self-check with a canned debate result (the real engine shape).
    canned = {
        "synthesized_answer": "Use PostgreSQL with a Redis cache layer.",
        "verified_decisions": [
            {"outcome_type": "VERIFIED_DECISION", "dispute_key": "db",
             "decision": "PostgreSQL as primary store",
             "reasoning": "relational + ACID needed"},
        ],
        "verified_tradeoffs": [
            {"outcome_type": "VERIFIED_TRADEOFF", "dispute_key": "cache",
             "option_a": "Redis", "option_b": "Memcached",
             "recommendation": "Redis", "reasoning": "persistence + types"},
        ],
        "human_required": [],
    }
    d = extract_decisions(canned)
    c = extract_constraints(canned)
    t = extract_tradeoffs(canned)
    assert len(d) == 1 and d[0]["decision"] == "PostgreSQL as primary store"
    assert len(c) == 1 and c[0]["constraint"] == "PostgreSQL as primary store"
    assert len(t) == 1 and t[0]["recommendation"] == "Redis"
    prompt = generate_build_prompt(canned["synthesized_answer"], d, c, t)
    for s in SPEC_SECTIONS:
        assert s in prompt.lower(), f"section missing from prompt: {s}"
    assert "PostgreSQL as primary store" in prompt
    assert "Redis" in prompt
    sample_spec = "# Architecture\n...\n# Components\n# File Structure\n" \
                   "# Dependencies\n# Constraints\n# Testing Requirements\n" \
                   "# Acceptance Criteria\n"
    assert validate_spec_sections(sample_spec) == []
    assert "architecture" in validate_spec_sections("# Components only")

    # Executable-spec section contract (production default path).
    exec_sample = (
        "# Implementation Objective\nBuild the auth module.\n\n"
        "# Files to Create\n- src/auth/__init__.py\n- src/auth/login.py\n"
        "- tests/test_auth.py  # login contract\n\n"
        "# Files to Modify\n(none)\n\n"
        "# Implementation Steps\n1. Implement login.\n\n"
        "# Acceptance Criteria\npytest exits 0 with >=1 pass.\n\n"
        "# Output Contract\npytest exit 0; all required files exist.\n"
    )
    assert validate_executable_spec_sections(exec_sample) == []
    assert "implementation objective" in validate_executable_spec_sections(
        "# Files to Create\n- x.py\n")
    assert parse_files_to_create(exec_sample) == [
        "src/auth/__init__.py", "src/auth/login.py", "tests/test_auth.py"]
    assert parse_files_to_create(
        "# Files to Create\n- /etc/passwd\n- relative/path.py\n"
    ) == ["relative/path.py"], "absolute paths must be filtered"
    assert parse_files_to_create("# Architecture\nno section") == []
    print("build_prompt_generator smoke test PASSED")
