"""The repository root, and its `scripts/` loaded by path since that directory is not a package."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str) -> ModuleType:
    """Import `scripts/<name>.py` as a module named `name`."""

    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)

    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module
