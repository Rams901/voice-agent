"""Canonical domain model and error taxonomy.

The flow talks in these types. No vendor-specific shape appears above this line,
which is what makes a new DMS an *adapter* rather than a fork of the flow.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

# --------------------------------------------------------------------------- #
# Capability vocabulary
# --------------------------------------------------------------------------- #
# The axes on which real DMS integrations genuinely differ. Kept as constants
# rather than booleans on the dealer profile so a new capability is a one-line
# addition and a typo is a KeyError rather than a silently-false flag.
#
# This list comes from the actual vendor documentation, not from imagination:
# Keyloop's Open Platform explicitly warns that "not all endpoints ... are
# available or relevant for every dealer management system", and Reynolds
# partners ship read-only integrations (appointments read, not pushed back).
# So capability must be declared per *dealer*, not per vendor.

CAP_AVAILABILITY_SEARCH = "availability_search"
CAP_CREATE_APPOINTMENT = "create_appointment"
CAP_CANCEL_APPOINTMENT = "cancel_appointment"
CAP_RESCHEDULE_APPOINTMENT = "reschedule_appointment"   # often ≠ cancel + create
CAP_READ_VEHICLE = "read_vehicle"
CAP_READ_CUSTOMER = "read_customer"
CAP_READ_APPOINTMENT = "read_appointment"
CAP_READ_REPAIR_ORDER = "read_repair_order"
CAP_READ_SERVICE_HISTORY = "read_service_history"
CAP_READ_PRICING = "read_pricing"
CAP_READ_TRANSPORT = "read_transport"                   # loan car / courtesy options
CAP_WRITE_REPAIR_ORDER = "write_repair_order"
CAP_WRITE_WARRANTY_CLAIM = "write_warranty_claim"
CAP_ASYNC_WRITES = "async_writes"                       # accepted, not yet committed
CAP_WEBHOOK_EVENTS = "webhook_events"                   # push, vs polling

ALL_CAPABILITIES = frozenset({
    CAP_AVAILABILITY_SEARCH, CAP_CREATE_APPOINTMENT, CAP_CANCEL_APPOINTMENT,
    CAP_RESCHEDULE_APPOINTMENT, CAP_READ_VEHICLE, CAP_READ_CUSTOMER,
    CAP_READ_APPOINTMENT, CAP_READ_REPAIR_ORDER, CAP_READ_SERVICE_HISTORY,
    CAP_READ_PRICING, CAP_READ_TRANSPORT, CAP_WRITE_REPAIR_ORDER,
    CAP_WRITE_WARRANTY_CLAIM, CAP_ASYNC_WRITES, CAP_WEBHOOK_EVENTS,
})

#: A well-integrated dealership: modern API, read/write, live diary.
FULL_FIXED_OPS = frozenset({
    CAP_AVAILABILITY_SEARCH, CAP_CREATE_APPOINTMENT, CAP_CANCEL_APPOINTMENT,
    CAP_READ_VEHICLE, CAP_READ_CUSTOMER, CAP_READ_APPOINTMENT,
    CAP_READ_SERVICE_HISTORY, CAP_READ_TRANSPORT,
})

#: The certified-partner pattern: rich reads, no write-back.
READ_ONLY = frozenset({
    CAP_READ_VEHICLE, CAP_READ_CUSTOMER, CAP_READ_APPOINTMENT,
    CAP_READ_SERVICE_HISTORY, CAP_READ_PRICING,
})


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
# Typed, because the flow's response differs by class. A retryable transient
# error is not the same as "this dealer cannot do that", and treating them the
# same is how a caller hears a vague apology instead of a concrete route.

class DmsError(RuntimeError):
    """Base class. Every adapter must raise a subclass of this, never a raw
    HTTP or SDK exception — the flow should never have to know what an httpx
    error looks like."""

    retryable = False

    def __init__(self, message: str, *, code: str = "", vendor: str = "",
                 status: int | None = None) -> None:
        self.code = code
        self.vendor = vendor
        self.status = status
        super().__init__(message)


class DmsCapabilityError(DmsError):
    """This dealer's integration cannot do that. Not a failure — a known limit."""


class DmsValidationError(DmsError):
    """The request was malformed. Almost always a bug on our side, not the DMS's."""


class DmsSlotUnavailableError(DmsError):
    """Availability went stale between offering a slot and committing it.

    Distinct from a validation error because the correct response is to
    apologise and re-offer, not to log a bug.
    """

    def __init__(self, message: str, *, alternatives: list[str] | None = None,
                 **kwargs: Any) -> None:
        self.alternatives = alternatives or []
        super().__init__(message, **kwargs)


class DmsTransientError(DmsError):
    """Retryable: network, rate limit, vendor 5xx. Safe only on idempotent calls."""

    retryable = True


class DmsAuthError(DmsError):
    """Credentials, consent or permissions. Needs an operator, not a retry."""


class DmsAsyncPendingError(DmsError):
    """The write was *accepted* but is not yet committed.

    This is the CDK Drive "Async Service Appointment" case, and it matters:
    'success' here does not mean the appointment exists. The flow must either
    poll the job or promise a callback — it must not tell the caller it's done.
    """

    def __init__(self, message: str, *, job_id: str = "", **kwargs: Any) -> None:
        self.job_id = job_id
        super().__init__(message, **kwargs)


# --------------------------------------------------------------------------- #
# Domain types
# --------------------------------------------------------------------------- #

@dataclass
class Vehicle:
    vehicle_id: str
    registration_plate: str
    make: str
    model: str
    year: int
    colour: str
    fuel_type: str
    mileage: int
    vin: str
    warranty_active: bool
    warranty_expiry: str | None = None
    # Vendor-normalised: some DMSs key vehicles by VIN only, some by plate.
    source_system: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "vehicle_id": self.vehicle_id,
            "registration_plate": self.registration_plate,
            "make": self.make,
            "model": self.model,
            "year": self.year,
            "colour": self.colour,
            "fuel_type": self.fuel_type,
            "mileage": self.mileage,
            "vin": self.vin,
            "warranty_active": "true" if self.warranty_active else "false",
            "warranty_expiry": self.warranty_expiry or "",
            "summary": f"{self.year} {self.make} {self.model}, {self.colour}",
        }


@dataclass
class Customer:
    customer_id: str
    first_name: str
    last_name: str
    phone: str
    email: str
    vehicle_ids: list[str] = field(default_factory=list)
    outstanding_balance: float = 0.0


@dataclass
class Appointment:
    appointment_id: str
    reference: str
    dealer_id: str
    customer_id: str
    vehicle_id: str
    service_type: str
    start: datetime
    duration_minutes: int
    transport_option: str
    notes: str
    idempotency_key: str
    status: str = "confirmed"
    created_at: datetime = field(default_factory=datetime.utcnow)
    source_system: str = ""
    # Vendor's own status vocabulary, kept so support can talk to the DMS.
    vendor_status: str = ""
    transport_id: str = ""
    advisor_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "appointment_id": self.appointment_id,
            "reference": self.reference,
            "dealer_id": self.dealer_id,
            "customer_id": self.customer_id,
            "vehicle_id": self.vehicle_id,
            "service_type": self.service_type,
            "start": self.start.isoformat(timespec="minutes"),
            "duration_minutes": str(self.duration_minutes),
            "transport_option": self.transport_option,
            "notes": self.notes,
            "status": self.status,
            "advisor_id": self.advisor_id,
            # What the *system of record* holds, for write verification.
            "what_dms_stored": (
                f"{self.reference} | {self.service_type} | "
                f"{self.start:%a %d %b %Y %H:%M} | {self.duration_minutes}min | "
                f"{self.transport_option}"
            ),
        }


@dataclass
class Slot:
    """One offerable appointment slot, normalised across vendors.

    Vendors return slots in wildly different shapes: Keyloop returns advisor
    availability windows keyed by service product and booking option; Fortellis
    returns a query result with a reusable query id. Both become this.
    """

    date: str          # ISO date
    time: str          # HH:MM
    advisor_id: str = ""
    duration_minutes: int = 60
    score: float = 0.0  # vendor relevance, if supplied

    @property
    def start(self) -> str:
        return f"{self.date}T{self.time}"

    def __str__(self) -> str:
        return f"{self.date} {self.time}"


# --------------------------------------------------------------------------- #
# Shared in-memory vendor backend
# --------------------------------------------------------------------------- #
# Each vendor adapter translates to/from a backend that speaks that vendor's
# wire format. In the demo the backend is in-process, so the adapters' real
# translation logic is exercised without credentials. In production you swap the
# transport for an HTTP client and nothing else changes — which is exactly the
# argument for building it this way.

@dataclass
class SeedData:
    """Deterministic fixture data, shared so every adapter returns the same
    canonical answers for the same dealer. That is what makes the conformance
    suite meaningful: any adapter can stand in for any other."""

    dealer_id: str
    vehicles: list[Vehicle] = field(default_factory=list)
    customers: list[Customer] = field(default_factory=list)
    slots: dict[str, list[str]] = field(default_factory=dict)


def default_seed(dealer_id: str) -> SeedData:
    vehicles = [
        Vehicle("V-1001", "AB12CDE", "BMW", "3 Series 320i", 2021, "grey",
                "petrol", 41_250, "WBA5E51020G123456", True, "2027-03-01"),
        # Same plate, different vehicle — forces disambiguation rather than a
        # guess. Transferred plates are a real-world cause of this.
        Vehicle("V-1002", "AB12CDE", "Mercedes-Benz", "C 200 AMG Line", 2019,
                "blue", "diesel", 88_400, "WDD2050141F987654", False),
        Vehicle("V-2001", "XY19JKL", "Toyota", "Corolla Hybrid", 2019, "white",
                "hybrid", 62_100, "SB1KZ3JE20E123987", True, "2026-11-15"),
        Vehicle("V-3001", "12AB34", "Volkswagen", "Golf 1.5 TSI", 2020, "black",
                "petrol", 74_900, "WVWZZZ1KZAW123456", False),
        Vehicle("V-4001", "BAB1234", "Audi", "A4 40 TFSI", 2022, "silver",
                "petrol", 29_800, "WAUZZZF48NA123456", True, "2028-01-20"),
    ]
    customers = [
        Customer("C-5001", "Sarah", "Whitfield", "+447700900123",
                 "sarah.whitfield@example.com", ["V-1001"], 0.0),
        Customer("C-5002", "Daniel", "Okonkwo", "+447700900456",
                 "d.okonkwo@example.com", ["V-2001"], 148.50),
    ]
    return SeedData(dealer_id=dealer_id, vehicles=vehicles, customers=customers,
                    slots=_seed_slots())


def _seed_slots() -> dict[str, list[str]]:
    """Next 14 weekdays, three slots each. Deterministic, so tests are stable."""
    slots: dict[str, list[str]] = {}
    day = date.today()
    added = 0
    while added < 14:
        day += timedelta(days=1)
        if day.weekday() >= 5:
            continue
        slots[day.isoformat()] = ["08:30", "11:00", "14:30"]
        added += 1
    return slots


class MemoryBackend:
    """Stores appointments and enforces the invariants every real DMS enforces.

    Deliberately shared by all adapters so the *behaviour* is comparable: the
    adapters differ in wire format and capability, not in truth.
    """

    def __init__(self, seed: SeedData) -> None:
        self.seed = seed
        self.appointments: dict[str, Appointment] = {}
        self.idempotency: dict[str, str] = {}
        self._counter = itertools.count(1001)
        self._lock = __import__("threading").RLock()

    def next_number(self) -> int:
        return next(self._counter)

    def taken_slots(self, dealer_id: str) -> set[str]:
        return {
            a.start.strftime("%Y-%m-%d %H:%M")
            for a in self.appointments.values()
            if a.dealer_id == dealer_id and a.status == "confirmed"
        }

    def find_vehicle(self, plate: str) -> list[Vehicle]:
        needle = _squash(plate)
        return [v for v in self.seed.vehicles if _squash(v.registration_plate) == needle]

    def find_vehicle_by_vin(self, vin: str) -> Vehicle | None:
        return next((v for v in self.seed.vehicles
                     if v.vin.upper() == vin.upper()), None)

    def find_customer(self, phone: str) -> Customer | None:
        needle = _squash(phone)
        return next((c for c in self.seed.customers
                     if _squash(c.phone) == needle), None)

    def customer_by_id(self, customer_id: str) -> Customer | None:
        return next((c for c in self.seed.customers
                     if c.customer_id == customer_id), None)

    def customer_for_vehicle(self, vehicle_id: str) -> Customer | None:
        return next((c for c in self.seed.customers
                     if vehicle_id in c.vehicle_ids), None)

    def available_slots(self, date_from: str, date_to: str,
                        dealer_id: str) -> list[Slot]:
        try:
            start = date.fromisoformat(date_from)
            end = date.fromisoformat(date_to)
        except ValueError:
            raise DmsValidationError(
                f"Date range {date_from}..{date_to} is not ISO. "
                "Ask the caller for the day again.",
                code="INVALID_DATE_RANGE",
            ) from None
        if end < start:
            start, end = end, start

        taken = self.taken_slots(dealer_id)
        out: list[Slot] = []
        for day, times in sorted(self.seed.slots.items()):
            current = date.fromisoformat(day)
            if not (start <= current <= end):
                continue
            for time in times:
                if f"{day} {time}" not in taken:
                    out.append(Slot(date=day, time=time))
        return out[:6]

    def commit(self, *, dealer_id: str, vehicle_id: str, customer_id: str,
               service_type: str, start: datetime, duration_minutes: int,
               transport_option: str, notes: str, idempotency_key: str,
               source_system: str, advisor_id: str = "") -> Appointment:
        with self._lock:
            # Idempotency first: a retried request must return the original.
            if idempotency_key in self.idempotency:
                return self.appointments[self.idempotency[idempotency_key]]

            key = start.strftime("%Y-%m-%d %H:%M")
            if key in self.taken_slots(dealer_id):
                # Availability is a snapshot; it can go stale mid-conversation.
                raise DmsSlotUnavailableError(
                    f"Slot {key} was taken while the caller was deciding.",
                    code="SLOT_UNAVAILABLE",
                    alternatives=[str(s) for s in
                                  self.available_slots(
                                      start.date().isoformat(),
                                      (start.date() + timedelta(days=7)).isoformat(),
                                      dealer_id)],
                )

            number = self.next_number()
            appointment = Appointment(
                appointment_id=f"AP-{number}",
                reference=f"NM{number}",
                dealer_id=dealer_id,
                customer_id=customer_id,
                vehicle_id=vehicle_id,
                service_type=service_type,
                start=start,
                duration_minutes=duration_minutes,
                transport_option=transport_option,
                notes=notes,
                idempotency_key=idempotency_key,
                source_system=source_system,
                advisor_id=advisor_id,
            )
            self.appointments[appointment.appointment_id] = appointment
            self.idempotency[idempotency_key] = appointment.appointment_id
            return appointment

    def cancel(self, appointment_id: str) -> Appointment:
        appointment = self.appointments.get(appointment_id)
        if appointment is None:
            raise DmsValidationError(
                f"No appointment {appointment_id} to cancel.",
                code="APPOINTMENT_NOT_FOUND",
            )
        appointment.status = "cancelled"
        return appointment


def _squash(value: str) -> str:
    return value.upper().replace(" ", "").replace("-", "")
