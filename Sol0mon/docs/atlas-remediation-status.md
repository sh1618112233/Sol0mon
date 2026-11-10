# Atlas reliability remediation status

This status is based on the checked-out `main` revision `2bd65e3` (the Atlas
reviewed revision). It is not a declaration that the release gates are met.

| Atlas finding | Current status at inspection | Source | Correction or disposition |
| --- | --- | --- | --- |
| P0 decision key mismatch | Confirmed | `answer_loader.py::_to_verified_decisions` | Loader now emits `normalized_key`; keyless claims are excluded. Workspace deserialization maps legacy `dispute_key` values. |
| Follow-up constraints built before impact removal | Confirmed | `workspace_debate_engine.py::resume_workspace` | Snapshot affected disputes, remove them before building the first prompt's constraints, retain unaffected decisions. |
| Governance outcome propagation | Partially confirmed; not fully traced or repaired | `debate_engine.py`, `answer_memory.py`, `answer_loader.py` | Deferred. Requires end-to-end source outcome contract and tests. |
| Human resolution consumed by build | Confirmed gap remains | `human_decision_store.py`, `human_decision_queue.py`, `build_pipeline.py` | Deferred. Current work does not provide a verified resolver-to-build lifecycle. |
| Failed-verification answer can be resurrected | Confirmed | `answer_memory.py`, `answer_loader.py`, `build_pipeline.py` | Normal answer resurrection now returns a block before workspace creation when `conflict_severity` is `FAILED_VERIFICATION`. Direct executor gate remains unverified. |
| Rejected options and provenance in later prompts | Not fully reproduced; remains open | `decision_model.py`, `build_prompt_generator.py`, `workspace_manager.py` | Deferred pending prompt review and negative-output tests. |
| Finalizer can overwrite workflow-owned metadata | Not fully reproduced; remains open | `build_finalizer.py`, `resume.py` | Deferred; no finalizer ownership merge implemented. |
| SQLite paths depend on current directory | Confirmed | `answer_memory.py`, `workspace_manager.py`, `human_decision_store.py` | Deferred. No path migration was attempted because safe discovery of existing databases needs a defined compatibility plan. |
| Test runner skips missing files/timeouts as success | Confirmed | `run_all_tests.py` | A pytest entry point and CI workflow were added. Full-suite result is still pending. |
| Duplicate platform trees | Confirmed | `Sol0mon/`, `Sol0mon-Windows/` | Not consolidated. Windows changes are isolated in three files; canonical tree selection and references need broader review. |
| AWS operational readiness | Not verified | README and runtime configuration | Deferred; do not deploy based on this partial remediation. |

## Reproduced defects and regressions

- `answer_loader` emitted `dispute_key` for claims whose canonical source field
  is `normalized_key`; build consumers expect the latter.
- `resume_workspace` rendered the old settled context before calculating
  affected keys. The first follow-up prompt therefore carried constraints that
  the user was asking the system to reconsider.
- The loader's answer shape omitted persisted `conflict_severity`; the normal
  resurrection path therefore could not reject a `FAILED_VERIFICATION` answer.
- `run_all_tests.py` increments its successful count when a test file is absent
  or times out.
- Initial baseline `python -m pytest` failed because pytest was not installed.
  Pytest was installed in the local environment; it is already in
  `requirements.txt`.

## Release gates still open

The following required work has not been completed: governance outcome
propagation; the human decision CLI-to-build lifecycle; direct implementation
eligibility checks; decision correction history; finalizer preservation and
idempotency; stable database directory and safe legacy database discovery;
full provenance and rejected-option prompt enforcement; complete test isolation;
duplicate tree consolidation; AWS operational verification; and the complete
offline suite/CI result. Required end-to-end tests A through H are not all
implemented. This branch is not release-ready until those gates pass.
