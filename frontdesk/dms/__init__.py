"""Canonical DMS layer.

Public surface: the canonical domain model, the capability vocabulary, the typed
error taxonomy, every registered adapter, and the registry that maps a dealer to
one.

Nothing above this boundary knows what a vendor is. Nothing below it knows what
the conversation flow is. That separation is what turns "ten more integrations"
from ten projects into ten classes plus ten config rows.
"""

from .base import DmsAdapter
from .conformance import ConformanceFailure, run_conformance, summarise
from .fortellis import FortellisCredentials, FortellisDms
from .keyloop import KeyloopCredentials, KeyloopDms
from .memory import ReferenceDms
from .model import (
    ALL_CAPABILITIES,
    CAP_ASYNC_WRITES,
    CAP_AVAILABILITY_SEARCH,
    CAP_CANCEL_APPOINTMENT,
    CAP_CREATE_APPOINTMENT,
    CAP_READ_APPOINTMENT,
    CAP_READ_CUSTOMER,
    CAP_READ_PRICING,
    CAP_READ_REPAIR_ORDER,
    CAP_READ_SERVICE_HISTORY,
    CAP_READ_TRANSPORT,
    CAP_READ_VEHICLE,
    CAP_RESCHEDULE_APPOINTMENT,
    CAP_WEBHOOK_EVENTS,
    CAP_WRITE_REPAIR_ORDER,
    CAP_WRITE_WARRANTY_CLAIM,
    Appointment,
    Customer,
    DmsAsyncPendingError,
    DmsAuthError,
    DmsCapabilityError,
    DmsError,
    DmsSlotUnavailableError,
    DmsTransientError,
    DmsValidationError,
    MemoryBackend,
    SeedData,
    Slot,
    Vehicle,
    default_seed,
)
from .partners import ExportOnlyDms, ExportSnapshot, ReadOnlyDms
from .registry import (
    FACTORIES,
    FULL_FIXED_OPS,
    READ_ONLY,
    build_adapter,
    get_dms,
    known_vendors,
    register,
    reset,
    set_adapter,
)

# Backwards-compatible aliases. The old single-module version called the
# slot-conflict error `SlotUnavailableError`; keeping the name means no call site
# has to care that the taxonomy was given proper class names.
SlotUnavailableError = DmsSlotUnavailableError
InMemoryDms = ReferenceDms

# The demo's default: the reference adapter, unless a dealer says otherwise.
DEFAULT_ADAPTER = ReferenceDms

__all__ = [
    # model
    "Vehicle", "Customer", "Appointment", "Slot", "SeedData", "MemoryBackend",
    "default_seed",
    # capabilities
    "CAP_AVAILABILITY_SEARCH", "CAP_CREATE_APPOINTMENT", "CAP_CANCEL_APPOINTMENT",
    "CAP_RESCHEDULE_APPOINTMENT", "CAP_READ_VEHICLE", "CAP_READ_CUSTOMER",
    "CAP_READ_APPOINTMENT", "CAP_READ_REPAIR_ORDER", "CAP_READ_SERVICE_HISTORY",
    "CAP_READ_PRICING", "CAP_READ_TRANSPORT", "CAP_WRITE_REPAIR_ORDER",
    "CAP_WRITE_WARRANTY_CLAIM", "CAP_ASYNC_WRITES", "CAP_WEBHOOK_EVENTS",
    "ALL_CAPABILITIES", "FULL_FIXED_OPS", "READ_ONLY",
    # errors
    "DmsError", "DmsCapabilityError", "DmsValidationError",
    "DmsSlotUnavailableError", "DmsTransientError", "DmsAuthError",
    "DmsAsyncPendingError", "SlotUnavailableError",
    # adapters
    "DmsAdapter", "ReferenceDms", "InMemoryDms", "KeyloopDms", "FortellisDms",
    "ReadOnlyDms", "ExportOnlyDms", "ExportSnapshot", "KeyloopCredentials",
    "FortellisCredentials",
    # registry
    "get_dms", "set_adapter", "reset", "register", "known_vendors",
    "build_adapter", "FACTORIES",
    # conformance
    "run_conformance", "summarise", "ConformanceFailure",
]
