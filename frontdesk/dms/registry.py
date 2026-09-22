"""Vendor registry: dealer configuration in, adapter out.

This is the seam that makes the scaling claim concrete. Adding a DMS means:

    1. write one class implementing `DmsAdapter`
    2. register its vendor name here
    3. add a dealer profile row

Nothing in `flow.py`, `tools.py`, `tool_server/` or the eval suite changes — and
`conformance.py` tells you whether the new adapter is actually correct before a
customer finds out.
"""

from __future__ import annotations

from typing import Callable

from .base import DmsAdapter
from .fortellis import FortellisDms
from .keyloop import KeyloopDms
# Re-exported so callers can talk about capability sets without reaching into
# `model`. Flake8 sees them as unused; they are part of this module's surface.
from .model import FULL_FIXED_OPS, READ_ONLY  # noqa: F401
from .partners import ExportOnlyDms, ReadOnlyDms

__all__ = [
    "FACTORIES", "FULL_FIXED_OPS", "READ_ONLY", "DmsAdapter", "ExportOnlyDms",
    "FortellisDms", "KeyloopDms", "ReadOnlyDms", "build_adapter", "get_dms",
    "known_vendors", "register", "reset", "set_adapter",
]

# Vendor name -> factory. A factory rather than an instance because two dealers
# on the same vendor need separate state (and, in production, separate credentials).
FACTORIES: dict[str, Callable[[str], DmsAdapter]] = {
    "keyloop": lambda dealer_id: KeyloopDms(dealer_id),
    "fortellis": lambda dealer_id: FortellisDms(dealer_id, async_writes=False),
    # Same vendor, different integration. This is the point: capability is a
    # property of the connection, not of the product.
    "fortellis-async": lambda dealer_id: FortellisDms(dealer_id, async_writes=True),
    "reynolds-rci": lambda dealer_id: ReadOnlyDms(dealer_id),
    "export-only": lambda dealer_id: ExportOnlyDms(dealer_id),
}


def register(vendor: str, factory: Callable[[str], DmsAdapter]) -> None:
    """Add a vendor. Called once per integration, from this module."""
    FACTORIES[vendor] = factory


def known_vendors() -> list[str]:
    return sorted(FACTORIES)


def build_adapter(dealer_id: str, vendor: str) -> DmsAdapter:
    try:
        factory = FACTORIES[vendor]
    except KeyError:
        raise SystemExit(
            f"No adapter registered for vendor {vendor!r}. "
            f"Known vendors: {', '.join(known_vendors())}"
        ) from None
    return factory(dealer_id)


# --------------------------------------------------------------------------- #
# Process-wide instances
# --------------------------------------------------------------------------- #
# In-memory for the demo. A production version would resolve credentials per
# dealer from a secret store and cache the client, so a token refresh is not a
# per-call cost.

_INSTANCES: dict[str, DmsAdapter] = {}
_OVERRIDES: dict[str, DmsAdapter] = {}


def set_adapter(dealer_id: str, adapter: DmsAdapter) -> None:
    """Test/DI hook: pin a dealer to a specific adapter."""
    _OVERRIDES[dealer_id] = adapter


def get_dms(dealer_id: str) -> DmsAdapter:
    """Return the adapter for a dealer, scoped to what they are configured for.

    Note the two-step: the vendor is resolved first, then the dealer's own
    declared scope narrows it. `build_adapter` deliberately returns the *un*
    narrowed adapter so the drift test can compare a profile's claim against
    what the vendor actually supports.
    """
    if dealer_id in _OVERRIDES:
        return _OVERRIDES[dealer_id]
    if dealer_id not in _INSTANCES:
        from ..dealer_profiles import get_dealer

        profile = get_dealer(dealer_id)
        adapter = build_adapter(dealer_id, profile.dms_vendor)
        restrict = getattr(adapter, "restrict_to", None)
        if callable(restrict):
            restrict(profile.capabilities)
        _INSTANCES[dealer_id] = adapter
    return _INSTANCES[dealer_id]


def reset() -> None:
    """Test helper: forget all adapters and overrides."""
    _INSTANCES.clear()
    _OVERRIDES.clear()
