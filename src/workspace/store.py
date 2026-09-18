"""Compatibility alias for the Kernel-owned Workspace Store."""

import sys

from src.config import MATERIAL_LIBRARY_DIR, PROJECTS_DIR
from travel_guide_kernel import store as _store

_store.PROJECTS_DIR = PROJECTS_DIR
_store.MATERIAL_LIBRARY_DIR = MATERIAL_LIBRARY_DIR
sys.modules[__name__] = _store
