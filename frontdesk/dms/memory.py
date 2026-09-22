"""Reference in-process adapter.

The baseline: implements the *full* contract with no vendor quirks, so it is the
control case in the conformance suite. If a vendor adapter fails a check that this
one passes, the problem is in the adapter, not the contract.

Used for the demo and for tests, exactly as the old mock was — but now behind the
same interface as every real vendor, which is the point.
"""

from __future__ import annotations

from datetime import datetime

from .base import DeclaredCapabilities
from .model import (
    CAP_AVAILABILITY_SEARCH, CAP_CANCEL_APPOINTMENT, CAP_CREATE_APPOINTMENT,
    CAP_READ_APPOINTMENT, CAP_READ_CUSTOMER, CAP_READ_SERVICE_HISTORY,
    CAP_READ_TRANSPORT, CAP_READ_VEHICLE, Appointment, Customer,
    MemoryBackend, Slot, Vehicle, default_seed,
)


class ReferenceDms(DeclaredCapabilities):
    """Full-capability adapter over the in-memory backend."""

    vendor = "reference"

    def __init__(self, dealer_id: str,
                 integration: str = "In-process reference adapter") -> None:
        self.dealer_id = dealer_id
        self.integration = integration
        self._backend = MemoryBackend(default_seed(dealer_id))

    def capabilities(self) -> frozenset[str]:
        return frozenset({
            CAP_AVAILABILITY_SEARCH, CAP_CREATE_APPOINTMENT,
            CAP_CANCEL_APPOINTMENT, CAP_READ_VEHICLE, CAP_READ_CUSTOMER,
            CAP_READ_APPOINTMENT, CAP_READ_SERVICE_HISTORY, CAP_READ_TRANSPORT,
        })

    # -- reads ------------------------------------------------------------- #

    def lookup_vehicle(self, plate: str) -> list[Vehicle]:
        return self._backend.find_vehicle(plate)

    def lookup_customer(self, phone: str) -> Customer | None:
        return self._backend.find_customer(phone)

    def check_availability(self, service_type: str, date_from: str,
                           date_to: str) -> list[Slot]:
        return self._backend.available_slots(date_from, date_to, self.dealer_id)

    def get_appointment(self, appointment_id: str) -> Appointment | None:
        return self._backend.appointments.get(appointment_id)

    # -- writes ------------------------------------------------------------ #

    def create_appointment(self, *, vehicle_id: str, customer_id: str,
                           service_type: str, start: str,
                           duration_minutes: int, transport_option: str,
                           notes: str, idempotency_key: str,
                           advisor_id: str = "") -> Appointment:
        return self._backend.commit(
            dealer_id=self.dealer_id, vehicle_id=vehicle_id,
            customer_id=customer_id, service_type=service_type,
            start=datetime.fromisoformat(start), duration_minutes=duration_minutes,
            transport_option=transport_option, notes=notes,
            idempotency_key=idempotency_key, source_system=self.vendor,
            advisor_id=advisor_id,
        )

    def cancel_appointment(self, appointment_id: str) -> Appointment:
        return self._backend.cancel(appointment_id)

    # -- extras used by the tool server ------------------------------------ #

    def lookup_vin(self, vin: str) -> Vehicle | None:
        return self._backend.find_vehicle_by_vin(vin)

    def customer_by_id(self, customer_id: str) -> Customer | None:
        return self._backend.customer_by_id(customer_id)

    def customer_for_vehicle(self, vehicle_id: str) -> Customer | None:
        return self._backend.customer_for_vehicle(vehicle_id)
