import json
import os
import re
from datetime import datetime


PROJECT_ROOT = "projects"

# Security: Only allow alphanumeric, underscore, hyphen, dot in names
_SAFE_NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.\-]*$')


def _validate_name(name, label="name"):
    """Reject path traversal and shell metacharacters in file/directory names."""
    if not name or not isinstance(name, str):
        raise ValueError(f"Invalid {label}: empty or non-string")
    if name.startswith('.') and name not in ('.', '..'):
        pass  # Hidden files are ok but still validated below
    if '..' in name or '/' in name or '\\' in name:
        raise ValueError(f"Invalid {label}: path traversal detected: {repr(name)}")
    if not _SAFE_NAME_RE.match(name):
        raise ValueError(f"Invalid {label}: unsafe characters: {repr(name)}")
    return name


def ensure_project(project_name):
    _validate_name(project_name, "project_name")

    project_path = os.path.join(
        PROJECT_ROOT,
        project_name
    )

    folders = [
        "research",
        "architecture",
        "implementation",
        "tasks",
        "generated",
        "reviews",
        "summaries",
        "final"
    ]

    os.makedirs(
        project_path,
        exist_ok=True
    )

    for folder in folders:

        os.makedirs(
            os.path.join(
                project_path,
                folder
            ),
            exist_ok=True
        )

    metadata_file = os.path.join(
        project_path,
        "metadata.json"
    )

    if not os.path.exists(
        metadata_file
    ):

        metadata = {
            "project_name": project_name,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat()
        }

        with open(
            metadata_file,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                metadata,
                f,
                indent=4
            )

    return project_path


def save_artifact(
    project_name,
    category,
    filename,
    content
):
    _validate_name(project_name, "project_name")
    _validate_name(category, "category")
    _validate_name(filename, "filename")

    project_path = ensure_project(
        project_name
    )

    artifact_path = os.path.join(
        project_path,
        category,
        filename
    )

    with open(
        artifact_path,
        "w",
        encoding="utf-8"
    ) as f:

        f.write(content)

    return artifact_path


def load_artifact(
    project_name,
    category,
    filename
):
    _validate_name(project_name, "project_name")
    _validate_name(category, "category")
    _validate_name(filename, "filename")

    artifact_path = os.path.join(
        PROJECT_ROOT,
        project_name,
        category,
        filename
    )

    with open(
        artifact_path,
        "r",
        encoding="utf-8"
    ) as f:

        return f.read()


def list_projects():

    if not os.path.exists(
        PROJECT_ROOT
    ):
        return []

    return sorted(
        [
            p
            for p in os.listdir(
                PROJECT_ROOT
            )
            if os.path.isdir(
                os.path.join(
                    PROJECT_ROOT,
                    p
                )
            )
        ]
    )


if __name__ == "__main__":

    ensure_project(
        "openclaw"
    )

    print(
        list_projects()
    )
