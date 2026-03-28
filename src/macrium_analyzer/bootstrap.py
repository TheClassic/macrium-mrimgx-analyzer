"""Runtime bootstrap helpers for local vendored dependencies."""

from __future__ import annotations

import sys
from pathlib import Path


def add_local_deps_to_path() -> str | None:
    """Add the project-local .deps directory to sys.path if it exists."""

    project_root = Path(__file__).resolve().parents[2]
    deps_dir = project_root / ".deps"
    if not deps_dir.is_dir():
        return None

    deps_path = str(deps_dir)
    if deps_path not in sys.path:
        sys.path.insert(0, deps_path)
    return deps_path


add_local_deps_to_path()

