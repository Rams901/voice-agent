"""Adapters for the two integration modes that dominate the long tail.

The interesting part of "scale to 10 more DMS integrations" is that the ten are
not ten versions of the same thing. In the US alone there are 48+ DMSs in use
across franchised rooftops, and they cluster into three modes:

1. **Full API, read/write** — Keyloop SAB, Fortellis scheduling. Can commit.
2. **Certified read-only** — Reynolds's RCI programme. Partners get repair
   orders, appointments and history, and push nothing back. Matador, for
   instance, is explicit that it reads appointments from Reynolds rather than
   writing them. The agent must take a message, not promise a booking.
3. **No API at all** — the long tail. Data leaves by scheduled CSV export, so
   there is no real-time availability to check. Booking over voice is impossible
   and the honest behaviour is to capture and hand off.

Modes 2 and 3 are why `capabilities()` exists as a first-class concept rather
than a footnote. If the flow assumes mode 1, every mode-2 and mode-3 dealer
produces an agent that lies to customers — politely, and at scale.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .base import DeclaredCapabilities
from .model import (
    CAP_READ_APPOINTMENT, CAP_READ_CUSTOMER, CAP_READ_PRICING,
    CAP_READ_REPAIR_ORDER, CAP_READ_SERVICE_HISTORY, CAP_READ_VEHICLE,
    Appointment, Customer, DmsCapabilityError, MemoryBackend, Slot,
    Vehicle, default_seed,
)


# --------------------------------------------------------------------------- #
# Mode 2 — certified read-only (Reynolds RCI style)
# --------------------------------------------------------------------------- #

class ReadOnlyDms(DeclaredCapabilities):
    """A certified read-only integration.

    Reads vehicles, customers, appointments and repair orders. Declares no write
    capabilities, so `create_appointment` raises `DmsCapabilityError` — which the
    tool server turns into a clear instruction for the agent rather than a
    generic failure.

    The value of modelling this explicitly: a read-only dealer is a *supported*
    configuration, not a broken one. The flow routes to callback capture and
    everyone behaves honestly.
    """

    vendor = "reynolds-rci"

    def __init__(self, dealer_id: str, integration: str = "Reynolds RCI (read-only)") -> None:
        self.dealer_id = dealer_id
        self.integration = integration
        self._backend = MemoryBackend(default_seed(dealer_id))
        self._declared = frozenset({
            CAP_READ_VEHICLE, CAP_READ_CUSTOMER, CAP_READ_APPOINTMENT,
            CAP_READ_REPAIR_ORDER, CAP_READ_SERVICE_HISTORY, CAP_READ_PRICING,
            # Deliberately absent: availability_search, create_appointment,
            # cancel_appointment, read_transport.
        })

    def capabilities(self) -> frozenset[str]:
        return self._declared

    def _refuse(self, what: str) -> None:
        raise DmsCapabilityError(
            f"Read-only integration: this dealer's {self.vendor} connection can "
            f"{what} but cannot write back. Capture the details and transfer to "
            f"the service team.",
            code="READ_ONLY_INTEGRATION", vendor=self.vendor, status=403,
        )

    def _refuse_read(self, what: str) -> None:
        """A refusal on the *read* side reads differently to the caller.

        "I cannot see the diary" and "I cannot book" are different sentences, and
        an agent that says the wrong one sounds confused rather than limited.
        """
        raise DmsCapabilityError(
            f"This dealer's {self.vendor} integration does not expose {what}. "
            f"Do not offer or check times — take the caller's preferred window "
            f"and have the service team confirm it.",
            code="READ_NOT_AVAILABLE", vendor=self.vendor, status=403,
        )

    def lookup_vehicle(self, plate: str) -> list[Vehicle]:
        return self._backend.find_vehicle(plate)

    def lookup_customer(self, phone: str) -> Customer | None:
        return self._backend.find_customer(phone)

    def check_availability(self, service_type: str, date_from: str,
                           date_to: str) -> list[Slot]:
        # Note this is genuinely *not* knowable, rather than merely unavailable.
        # Returning an empty list here would look like "no slots", which is a
        # different and misleading answer.
        self._refuse_read("the live workshop diary")

    def get_appointment(self, appointment_id: str) -> Appointment | None:
        return self._backend.appointments.get(appointment_id)

    def create_appointment(self, **kwargs: Any) -> Appointment:
        self._refuse("read vehicles and history")

    def cancel_appointment(self, appointment_id: str) -> Appointment:
        self._refuse("read vehicles and history")

    def service_history(self, vehicle_id: str) -> list[dict[str, Any]]:
        """What RCI-certified partners actually get: the repair order picture."""
        return [
            {"date": "2025-09-14", "service": "annual_service", "mileage": 32_400,
             "declined_work": ["front brake discs"], "status": "closed"},
            {"date": "2025-03-02", "service": "mot", "mileage": 28_100,
             "declined_work": [], "status": "closed"},
        ]


# --------------------------------------------------------------------------- #
# Mode 3 — no API at all (scheduled export only)
# --------------------------------------------------------------------------- #

@dataclass
class ExportSnapshot:
    """What a scheduled data export gives you: a picture, not a live feed."""

    exported_at: datetime
    stale_after_hours: int = 24

    @property
    def is_stale(self) -> bool:
        return datetime.utcnow() > self.exported_at + timedelta(
            hours=self.stale_after_hours)


class ExportOnlyDms(DeclaredCapabilities):
    """Long-tail dealer whose DMS has no API; data arrives by scheduled export.

    This is the mode most likely to be forgotten in a scaling plan, and it is the
    one that produces the worst failures — because the data *looks* usable. An
    agent that offers a slot from a 24-hour-old export is offering something it
    cannot know is free.

    So this adapter exposes reads and refuses anything live. The distinction it
    draws is the one that matters: **stale data is not data you can commit on.**
    """

    vendor = "export-only"

    def __init__(self, dealer_id: str,
                 integration: str = "Scheduled CSV export (no API)") -> None:
        self.dealer_id = dealer_id
        self.integration = integration
        self._backend = MemoryBackend(default_seed(dealer_id))
        # The export landed at some point today; it is already partially stale.
        self.snapshot = ExportSnapshot(exported_at=datetime.utcnow()
                                       - timedelta(hours=14))
        self._declared = frozenset({
            CAP_READ_VEHICLE, CAP_READ_CUSTOMER, CAP_READ_SERVICE_HISTORY,
            # An export genuinely carries repair orders — it just cannot carry a
            # live diary or a write path.
            CAP_READ_REPAIR_ORDER,
        })

    def capabilities(self) -> frozenset[str]:
        return self._declared

    def lookup_vehicle(self, plate: str) -> list[Vehicle]:
        return self._backend.find_vehicle(plate)

    def lookup_customer(self, phone: str) -> Customer | None:
        return self._backend.find_customer(phone)

    def check_availability(self, service_type: str, date_from: str,
                           date_to: str) -> list[Slot]:
        # Not "empty" — *unknown*. A different fact, and a different sentence to
        # the caller: "I can't see the diary, so let me have the team call you"
        # rather than "there's nothing free".
        raise DmsCapabilityError(
            "This dealer's data arrives by scheduled export, so live availability "
            "is not available. Take the caller's preferred window and arrange a "
            "callback — do not offer a time.",
            code="NO_LIVE_AVAILABILITY", vendor=self.vendor, status=403,
        )

    def get_appointment(self, appointment_id: str) -> Appointment | None:
        raise DmsCapabilityError(
            "Appointments cannot be read live from an export-only integration, "
            "so a booking cannot be verified. Do not confirm one.",
            code="NO_LIVE_APPOINTMENTS", vendor=self.vendor, status=403,
        )

    def create_appointment(self, **kwargs: Any) -> Appointment:
        # The important refusal: without live availability *and* write-back,
        # there is no way to make a promise that can be kept.
        raise DmsCapabilityError(
            "This dealer has no write-back integration. Capture the caller's "
            "details and transfer to the service team.",
            code="NO_WRITE_BACK", vendor=self.vendor, status=403,
        )

    def cancel_appointment(self, appointment_id: str) -> Appointment:
        raise DmsCapabilityError(
            "This dealer has no write-back integration.",
            code="NO_WRITE_BACK", vendor=self.vendor, status=403,
        )

    def data_freshness_note(self) -> str:
        return (
            f"Data exported {self.snapshot.exported_at:%Y-%m-%d %H:%M} UTC"
            + (" — STALE, do not offer slots from it." if self.snapshot.is_stale
               else " — reasonably current.")
        )
