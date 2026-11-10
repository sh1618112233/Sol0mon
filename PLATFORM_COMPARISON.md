# Linux/server and Windows versions

This report compares the original `main` trees at `2bd65e3` with the
reliability-remediated source trees. Both platform directories are intentional
deliverables and must remain in the repository.

## Inventory and original differences

At the `main` baseline, both directories contained 187 tracked files with the
same relative paths. 184 files were byte-identical. The three differences
were intentional:

| File | Difference | Handling |
| --- | --- | --- |
| `README.md` | Platform-specific installation and Windows requirements. | Kept separate; root README now directs users to the correct version. |
| `implementation_executor.py` | Windows used `sys.executable` for Python subprocesses; Linux used the literal `python3`. | Preserved in Windows. Linux has also adopted `sys.executable` for virtual-environment correctness. |
| `tests/test_debate_warmup.py` | Windows used a portable temporary directory; Linux used a hard-coded `/tmp` path. | Preserved the portable temporary-directory behavior. |

The Windows version also has its own `.gitignore`; it remains in place.

## Shared behavior and reliability changes

The versions share the debate, verification, governance, synthesis, build,
workspace, and human-decision flows. The remediation was applied to the
corresponding Windows modules using three-way merges against the Windows
baseline, rather than replacing Windows files with Linux files.

| Functionality | Relevant files | Correction applied to Windows |
| --- | --- | --- |
| Canonical decision identity and correction | `answer_loader.py`, `workspace_debate_engine.py`, `workspace_manager.py`, `tests/test_workspace_impact_analysis.py` | Normalize legacy keys, retain identity when reopening, suspend affected constraints before prompting, and persist correction history. |
| Governance and implementation eligibility | `answer_memory.py`, `debate_engine.py`, `build_pipeline.py`, `build_prompt_generator.py`, `build_spec_engine.py`, `build_finalizer.py` | Preserve verifier and governance outcomes separately, propagate eligible decisions/trade-offs, and block failed or malformed verification. |
| Human decision lifecycle | `human_decision_store.py`, `human_decision_queue.py`, `sol0mon_cli.py`, `tests/test_human_decision_store.py` | Add workspace-scoped choice validation and consumption by the resumed build. |
| Stable persistence and safe search | `answer_memory.py`, `workspace_manager.py`, `job_store.py`, `build_workspace.py`, new `storage_paths.py` | Resolve paths relative to the Windows application directory, preserve recognized legacy stores, fail on ambiguity, and escape SQL `LIKE` metacharacters. |
| Prompt provenance and finalization | `research_planner.py`, `build_prompt_generator.py`, `build_finalizer.py` | Mark external/source content as untrusted data and preserve workflow-owned metadata across finalization. |
| Test isolation and release gates | `run_all_tests.py`, `tests/conftest.py`, `tests/`, new `pytest.ini` | Use pytest for the Windows offline suite, isolate test state, and add reliability regression coverage. |

The new Windows `storage_paths.py` uses `Sol0mon-Windows/` as its application
root. Its default `data/` and relative override paths are separate from the
Linux/server version. Known existing databases and workspace directories are
discovered in place; ambiguous legacy locations fail closed instead of being
silently replaced.

## Platform-specific behavior and remaining drift

- Windows browser setup, PowerShell commands, its requirements manifest, and
  Windows-specific documentation remain under `Sol0mon-Windows/`.
- Linux/server operator and AWS validation preparation remain under
  `Sol0mon/docs/`; no AWS assumption is added to Windows.
- Platform test suites and CI jobs use separate Python environments.
- Linux-only additions (`storage_paths.py`, its tests, and operator guide)
  have corresponding Windows adaptations where they support shared runtime
  behavior. The Linux operator guide is intentionally not copied to Windows.

Some files remain different between the current platform trees, including
their READMEs and the source/test files carrying the reliability deltas. Those
differences are reviewed by feature above; identical file contents are not a
requirement where path, shell, process, or platform setup differs.

## Validation status

Both platform directories are present. Python compilation, CLI startup, test
suite, storage, and decision-lifecycle validation are tracked independently
for each platform in the pull request checks. Windows compatibility is not
claimed based on Linux-only results.
