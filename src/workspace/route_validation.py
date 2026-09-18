"""Backward-compatible imports for the public route structure API."""

from travel_guide_kernel.route import (
    route_structure_diff,
    route_structure_hash,
    route_structure_snapshot,
    validate_route_structure,
)

__all__ = [
    "route_structure_diff",
    "route_structure_hash",
    "route_structure_snapshot",
    "validate_route_structure",
]
