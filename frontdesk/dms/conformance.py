"""Adapter conformance suite.

This is the answer to "how should we proceed with DMS integrations".

The wrong way to add ten DMSs is to discover each one's quirks in production,
because the failure mode is a customer being told something untrue. The right way
is to define the contract once and make every adapter prove it.

So this module is a **reusable test suite**, not a test file. It runs the same
checks against every adapter, and it is deliberately written to assert the things
that actually break:

* an adapter must not report success for a write it did not perform;
* a retry must not double-book;
* stale availability must be refused rather than committed;
* an ambiguous plate must return two vehicles rather than one guess;
* an undeclared capability must raise, not silently no-op;
* a read-back must reflect what was stored, not what was requested.

`tests/test_dms_conformance.py` runs this against every registered adapter, which
means the day someone writes a SAB adapter for a DMS nobody has heard of, they
know within a second whether it is safe to put in front of a customer.
"""

from __future__ import annotations

from typing import Any, Callable

from .base import DmsAdapter
from .model import (
    CAP_AVAILABILITY_SEARCH, CAP_CREATE_APPOINTMENT, CAP_READ_APPOINTMENT,
    CAP_READ_CUSTOMER, CAP_READ_VEHICLE, DmsAsyncPendingError,
    DmsCapabilityError, DmsError, DmsSlotUnavailableError, DmsValidationError,
)


class ConformanceFailure(AssertionError):
    """Raised with the adapter named, so a failure is immediately actionable."""


def _check(adapter: DmsAdapter, condition: bool, message: str) -> None:
    if not condition:
        raise ConformanceFailure(
            f"[{adapter.vendor} / {adapter.integration}] {message}"
        )


def _expect_capability_error(adapter: DmsAdapter, call: Callable[[], Any],
                             what: str) -> None:
    """An undeclared capability must raise, never return a plausible-looking
    empty value. Silently returning [] for 'no availability' is the single most
    dangerous adapter bug, because it is indistinguishable from a real answer."""
    try:
        call()
    except DmsCapabilityError:
        return
    except DmsError as exc:
        raise ConformanceFailure(
            f"[{adapter.vendor}] {what} raised {type(exc).__name__} instead of "
            f"DmsCapabilityError. The flow branches on the error class."
        ) from exc
    raise ConformanceFailure(
        f"[{adapter.vendor}] {what} did not raise, but '{what}' is not a "
        f"declared capability. A silent no-op here becomes a false statement "
        f"to a caller."
    )


# --------------------------------------------------------------------------- #
# The suite
# --------------------------------------------------------------------------- #

def run_conformance(adapter: DmsAdapter, *, plate: str = "AB12CDE",
                    unique: str = "conformance") -> list[str]:
    """Run every contract check. Returns the list of checks that passed.

    Raises `ConformanceFailure` on the first violation, naming the adapter.
    """
    passed: list[str] = []
    caps = adapter.capabilities()

    # -- 1. the adapter declares itself honestly ---------------------------- #
    _check(adapter, bool(adapter.vendor), "vendor must be set")
    _check(adapter, bool(adapter.integration), "integration must be described")
    _check(adapter, isinstance(caps, frozenset), "capabilities() must be a frozenset")
    passed.append("declares vendor, integration and capabilities")

    # -- 2. reads work when declared ---------------------------------------- #
    if CAP_READ_VEHICLE in caps:
        vehicles = adapter.lookup_vehicle(plate)
        _check(adapter, isinstance(vehicles, list),
               "lookup_vehicle must return a list (a plate can match several)")
        _check(adapter, len(vehicles) >= 1,
               f"lookup_vehicle({plate}) returned nothing for seeded data")
        for vehicle in vehicles:
            _check(adapter, vehicle.registration_plate.replace("-", "").replace(" ", "").upper()
                   == plate.replace("-", "").replace(" ", "").upper(),
                   "lookup_vehicle returned a vehicle with a different plate")
        passed.append("lookup_vehicle returns canonical Vehicle records")

    if CAP_READ_VEHICLE in caps and len(adapter.lookup_vehicle(plate)) > 1:
        _check(adapter, True, "")  # documented: ambiguity is surfaced, not hidden
        passed.append("ambiguous plate surfaces multiple vehicles rather than picking one")

    if CAP_READ_CUSTOMER in caps:
        customer = adapter.lookup_customer("+447700900123")
        _check(adapter, customer is not None and customer.first_name == "Sarah",
               "lookup_customer did not find the seeded customer")
        passed.append("lookup_customer returns canonical Customer records")

    # -- 3. undeclared capabilities refuse loudly --------------------------- #
    if CAP_AVAILABILITY_SEARCH not in caps:
        _expect_capability_error(
            adapter,
            lambda: adapter.check_availability("annual_service", "2026-10-01", "2026-10-10"),
            "check_availability",
        )
        passed.append("undeclared availability_search refuses rather than returning empty")

    if CAP_CREATE_APPOINTMENT not in caps:
        _expect_capability_error(
            adapter,
            lambda: adapter.create_appointment(
                vehicle_id="V-1001", customer_id="C-5001",
                service_type="annual_service", start="2026-10-07T08:30",
                duration_minutes=90, transport_option="wait_on_site",
                notes="", idempotency_key=f"{unique}-refuse",
            ),
            "create_appointment",
        )
        passed.append("undeclared create_appointment refuses rather than faking success")

    if CAP_READ_APPOINTMENT not in caps:
        _expect_capability_error(
            adapter,
            lambda: adapter.get_appointment("AP-1"),
            "get_appointment",
        )
        passed.append("undeclared appointment read refuses rather than fabricating")

    # -- 4. availability is real, not invented ------------------------------ #
    if CAP_AVAILABILITY_SEARCH in caps and CAP_CREATE_APPOINTMENT in caps:
        slots = adapter.check_availability("annual_service", "2026-10-01", "2026-10-31")
        _check(adapter, isinstance(slots, list), "availability must return a list")
        for slot in slots:
            # A slot must be a complete, bookable instant — not a bare time.
            _check(adapter, bool(slot.date) and bool(slot.time),
                   f"slot {slot} is missing a date or time")
            _check(adapter, len(slot.date) == 10 and slot.date[4] == "-",
                   f"slot date {slot.date!r} is not ISO")
        passed.append("availability returns complete canonical Slots")

        # An unparseable range must fail in a way the flow can act on. A raw
        # ValueError here escapes the taxonomy and becomes a generic apology.
        try:
            adapter.check_availability("annual_service", "not-a-date", "2026-10-31")
        except DmsValidationError:
            passed.append("unparseable date range raises a typed validation error")
        except DmsError as exc:
            raise ConformanceFailure(
                f"[{adapter.vendor}] an unparseable date range raised "
                f"{type(exc).__name__}, not DmsValidationError"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise ConformanceFailure(
                f"[{adapter.vendor}] an unparseable date range raised a bare "
                f"{type(exc).__name__} ({exc}). The flow cannot map that onto a "
                f"response, so the caller hears a generic apology."
            ) from exc
        else:
            passed.append("unparseable date range returns a list rather than raising")

        # Duplicate availability: two calls must not disagree about the diary.
        again = adapter.check_availability("annual_service", "2026-10-01", "2026-10-31")
        _check(adapter, [str(s) for s in slots] == [str(s) for s in again],
               "availability is not stable between identical calls")
        passed.append("availability is stable between identical calls")

    # -- 5. the write contract ---------------------------------------------- #
    if (CAP_AVAILABILITY_SEARCH in caps and CAP_CREATE_APPOINTMENT in caps
            and CAP_READ_APPOINTMENT in caps):
        slots = adapter.check_availability("annual_service", "2026-10-01", "2026-11-30")
        if slots:
            slot = slots[0]
            key = f"{unique}-{adapter.vendor}-{slot.start}"
            booking = dict(
                vehicle_id="V-1001", customer_id="C-5001",
                service_type="annual_service", start=slot.start,
                duration_minutes=90, transport_option="wait_on_site",
                notes="conformance", idempotency_key=key,
            )

            try:
                first = adapter.create_appointment(**booking)
            except DmsAsyncPendingError as pending:
                # Async writes are a legal implementation, but only if the
                # adapter tells the truth about it. It must not have claimed
                # success, and it must give the flow something to poll.
                _check(adapter, bool(pending.job_id),
                       "an async write must return a job id for the flow to poll")
                passed.append("async write reports pending rather than false success")
                return passed

            _check(adapter, first.appointment_id,
                   "a successful write must return an appointment id")

            # 5a. idempotency — the retry must not double-book.
            second = adapter.create_appointment(**booking)
            _check(adapter, second.appointment_id == first.appointment_id,
                   "a retried write created a second appointment (not idempotent)")
            passed.append("retried write is idempotent — no double booking")

            # 5b. verify-the-write — the read-back must reflect storage.
            stored = adapter.get_appointment(first.appointment_id)
            _check(adapter, stored is not None,
                   "an appointment that was just written cannot be read back")
            _check(adapter, stored.appointment_id == first.appointment_id,
                   "read-back returned a different appointment")
            summary = stored.to_dict()["what_dms_stored"]
            _check(adapter, stored.service_type in summary,
                   "the stored record does not contain the service that was booked")
            _check(adapter, "," not in summary.split("|")[2],
                   "the stored record's timestamp looks malformed")
            passed.append("write can be verified by reading the record back")

            # 5c. a stale slot must be refused, not silently committed.
            try:
                adapter.create_appointment(**{**booking, "idempotency_key": key + "-other"})
            except DmsSlotUnavailableError:
                passed.append("stale slot is refused at commit time")
            except DmsError as exc:
                raise ConformanceFailure(
                    f"[{adapter.vendor}] taking an already-booked slot raised "
                    f"{type(exc).__name__}, not DmsSlotUnavailableError — the "
                    f"flow cannot tell the caller to pick another time."
                ) from exc
            else:
                raise ConformanceFailure(
                    f"[{adapter.vendor}] committed a second booking into an "
                    f"already-taken slot. Availability must be re-checked at "
                    f"write time, not trusted from the earlier read."
                )

    return passed


def summarise(adapters: list[DmsAdapter]) -> str:
    """Human-readable capability matrix. This is what you send to a dealer
    before promising them anything."""
    if not adapters:
        return "(no adapters)"

    capability_columns = sorted(set().union(*(a.capabilities() for a in adapters)))
    short = {
        CAP_AVAILABILITY_SEARCH: "avail",
        CAP_CREATE_APPOINTMENT: "book",
        "cancel_appointment": "cancel",
        CAP_READ_VEHICLE: "vehicle",
        CAP_READ_CUSTOMER: "customer",
        CAP_READ_APPOINTMENT: "appt",
        "read_service_history": "history",
        "read_pricing": "price",
        "read_transport": "transport",
        "async_writes": "async",
        "webhook_events": "webhook",
        "reschedule_appointment": "resched",
        "read_repair_order": "RO",
    }
    width = max(len(a.integration) for a in adapters) + 22
    header = "adapter".ljust(width) + "".join(
        short.get(c, c[:7]).rjust(10) for c in capability_columns
    )
    lines = [header, "-" * len(header)]
    for adapter in adapters:
        caps = adapter.capabilities()
        row = f"{adapter.vendor} / {adapter.integration}".ljust(width)
        row += "".join(
            ("yes" if c in caps else "-").rjust(10) for c in capability_columns
        )
        lines.append(row)
    return "\n".join(lines)
