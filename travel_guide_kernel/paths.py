"""Runtime path resolution shared by travel-guide hosts."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


RUNTIME_ROOT_ENV = "TRAVEL_GUIDE_RUNTIME_ROOT"


def resolve_runtime_root(
    code_root: str | Path,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the writable runtime root without changing the code root."""
    environment = os.environ if environ is None else environ
    configured = str(environment.get(RUNTIME_ROOT_ENV) or "").strip()
    if not configured:
        return Path(code_root).resolve()
    return Path(configured).expanduser().resolve()


__all__ = ["RUNTIME_ROOT_ENV", "resolve_runtime_root"]
