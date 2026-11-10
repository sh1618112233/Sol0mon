"""Stable local paths for Sol0mon's persistent state."""

from __future__ import annotations

import os
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent
# The Windows implementation is stored at the root of its own platform
# directory. Keep its default data and relative override paths there rather
# than sharing the Linux/server version's data directory.
REPOSITORY_ROOT = SOURCE_ROOT


def data_dir() -> Path:
    value = os.environ.get("SOLOMON_DATA_DIR")
    path = Path(value).expanduser() if value else REPOSITORY_ROOT / "data"
    if not path.is_absolute():
        path = REPOSITORY_ROOT / path
    return path.resolve()


def _configured_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = REPOSITORY_ROOT / path
    return path.resolve()


def database_path(filename: str, *, override: str | None = None,
                  legacy_names: tuple[str, ...] = ()) -> str:
    """Return a stable DB path, retaining a unique existing legacy DB in place.

    Ambiguous legacy files fail closed so a new empty database is never created
    beside multiple possible sources of user data.
    """
    if override:
        target = _configured_path(override)
        if not target.exists():
            external = _external_cwd_candidates(filename, legacy_names)
            if external:
                raise RuntimeError(
                    f"Possible legacy {filename} found in the current working "
                    f"directory: {external}; inspect it and configure an explicit path."
                )
            legacy = _legacy_databases(filename, legacy_names)
            if legacy:
                raise RuntimeError(
                    f"{filename} exists at {legacy}; configured path {target} "
                    "does not. Copy/restore the database or remove the override."
                )
        return str(target)

    target = data_dir() / filename
    if target.exists():
        return str(target)
    external = _external_cwd_candidates(filename, legacy_names)
    if external:
        raise RuntimeError(
            f"Possible legacy {filename} found in the current working "
            f"directory: {external}; inspect it and configure an explicit path."
        )
    legacy = _legacy_databases(filename, legacy_names)
    if len(legacy) > 1:
        paths = ", ".join(str(p) for p in legacy)
        raise RuntimeError(f"Multiple legacy {filename} databases found: {paths}")
    return str(legacy[0] if legacy else target)


def _legacy_databases(filename: str, legacy_names: tuple[str, ...]) -> list[Path]:
    roots = (SOURCE_ROOT, REPOSITORY_ROOT)
    paths = {
        (root / name).resolve()
        for root in roots
        for name in (filename, *legacy_names)
    }
    return sorted((path for path in paths if path.is_file()), key=str)


def _external_cwd_candidates(filename: str,
                              legacy_names: tuple[str, ...]) -> list[Path]:
    """Notice old CWD-relative stores outside the checkout without adopting them.

    Adopting a path based on the caller's current directory would reintroduce
    CWD-dependent behavior, so require explicit operator selection instead.
    """
    cwd = Path.cwd().resolve()
    try:
        cwd.relative_to(REPOSITORY_ROOT.resolve())
        return []
    except ValueError:
        pass
    return sorted({
        (cwd / name).resolve()
        for name in (filename, *legacy_names)
        if (cwd / name).is_file()
    }, key=str)


def persistent_dir(name: str, *, override: str | None = None,
                   legacy_names: tuple[str, ...] = ()) -> Path:
    if override:
        target = _configured_path(override)
        if not target.exists():
            external = [Path.cwd().resolve() / old for old in legacy_names
                        if (Path.cwd() / old).is_dir()]
            if external:
                raise RuntimeError(
                    f"Possible legacy {name} directory found in the current "
                    f"working directory: {external}; inspect it and configure an explicit path."
                )
            existing = [
                (root / old).resolve()
                for root in (SOURCE_ROOT, REPOSITORY_ROOT)
                for old in legacy_names
                if (root / old).is_dir()
            ]
            if existing:
                raise RuntimeError(
                    f"Existing {name} data found at {existing}; configured "
                    f"path {target} is empty. Move/copy it explicitly."
                )
        return target
    target = data_dir() / name
    if target.exists():
        return target
    external = [Path.cwd().resolve() / old for old in legacy_names
                if (Path.cwd() / old).is_dir()]
    if external:
        raise RuntimeError(
            f"Possible legacy {name} directory found in the current working "
            f"directory: {external}; inspect it and configure an explicit path."
        )
    existing = sorted({
        (root / old).resolve()
        for root in (SOURCE_ROOT, REPOSITORY_ROOT)
        for old in legacy_names
        if (root / old).is_dir()
    }, key=str)
    if len(existing) > 1:
        raise RuntimeError(f"Multiple legacy {name} directories found: {existing}")
    return existing[0] if existing else target


def workspace_database_path() -> str:
    workspace = os.environ.get("WORKSPACE_DB")
    human = os.environ.get("HUMAN_DECISION_DB")
    if workspace and human and _configured_path(workspace) != _configured_path(human):
        raise RuntimeError(
            "WORKSPACE_DB and HUMAN_DECISION_DB must point to the same database"
        )
    override = workspace or human
    return database_path(
        "workspaces.db", override=override,
        legacy_names=("database/workspaces.db",),
    )
