"""Keyloop adapter, shaped after the public Service Appointment Booking (SAB) API.

Keyloop dominates franchise groups in the UK, Europe, the Middle East and Asia,
so for this design it is the most likely first adapter. Their Open Platform exposes
SAB as a set of endpoints that mirror the booking workflow rather than a single
"create booking" call:

    service categories → service products → appointment options →
    advisor search → availability → create service appointment

That shape is the interesting part. A booking is a *pipeline*, because a slot is
only valid for a given (service product, booking option, advisor) combination. An
adapter that ignores that will offer a time the dealer cannot honour.

Their documentation is also explicit that **"not all endpoints, properties within
an endpoint, or parameters within an endpoint are available or relevant for every
dealer management system"** — which is why this adapter declares capabilities
rather than assuming them.

Modelled from developer.keyloop.io public documentation. Endpoint paths and
payload keys here are illustrative of the documented workflow; the real ones sit
behind a partner contract.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .base import DeclaredCapabilities
from .model import (
    CAP_AVAILABILITY_SEARCH, CAP_CANCEL_APPOINTMENT, CAP_CREATE_APPOINTMENT,
    CAP_READ_APPOINTMENT, CAP_READ_CUSTOMER, CAP_READ_SERVICE_HISTORY,
    CAP_READ_TRANSPORT, CAP_READ_VEHICLE, Appointment, Customer,
    DmsAuthError, DmsCapabilityError, DmsSlotUnavailableError, MemoryBackend,
    Slot, Vehicle, default_seed,
)

# --------------------------------------------------------------------------- #
# Simulated Keyloop API
# --------------------------------------------------------------------------- #
# Speaks the vendor's shape: advisor-keyed availability, a booking-options
# concept, and a status vocabulary. Kept behind a transport boundary so a real
# HTTP client can replace it without touching the adapter above.


@dataclass
class KeyloopCredentials:
    partner_key: str = ""
    dealer_code: str = ""


class SimulatedKeyloopApi:
    """In-process stand-in for the Keyloop Open Platform."""

    def __init__(self, backend: MemoryBackend, credentials: KeyloopCredentials,
                 dealer_code: str) -> None:
        self._backend = backend
        self._credentials = credentials
        self._dealer_code = dealer_code
        self._advisors = itertools.cycle(["ADV-1", "ADV-2", "ADV-3"])
        self._advisor_cache: dict[str, str] = {}

    def _require_partner_key(self) -> None:
        if not self._credentials.partner_key:
            raise DmsAuthError(
                "Keyloop partner key missing. Access requires a contract with "
                "Keyloop and a partner key issued to the authorised third party.",
                code="UNAUTHORIZED", vendor="keyloop", status=401,
            )

    # -- vehicle / customer search ------------------------------------------ #
    def search_vehicles(self, registration_plate: str) -> list[dict[str, Any]]:
        """Vehicle search by plate.

        Note this goes through the platform rather than the local store: in
        production every read is an authenticated partner call, so the auth gate
        belongs here and not only on the write path. An adapter that only
        authenticates writes will look fine until someone tests a bad key.
        """
        self._require_partner_key()
        return [
            {
                "vehicleId": vehicle.vehicle_id,
                "registrationNumber": vehicle.registration_plate,
                "make": vehicle.make,
                "model": vehicle.model,
                "modelYear": vehicle.year,
                "vin": vehicle.vin,
            }
            for vehicle in self._backend.find_vehicle(registration_plate)
        ]

    def search_customers(self, phone_number: str) -> list[dict[str, Any]]:
        self._require_partner_key()
        customer = self._backend.find_customer(phone_number)
        if customer is None:
            return []
        return [{
            "customerId": customer.customer_id,
            "firstName": customer.first_name,
            "lastName": customer.last_name,
            "phone": customer.phone,
        }]

    # -- advisor search ---------------------------------------------------- #
    def search_advisors(self, service_product_code: str) -> list[dict[str, str]]:
        self._require_partner_key()
        return [{"advisorId": f"ADV-{n}", "name": f"Advisor {n}"} for n in (1, 2, 3)]

    # -- appointment options ----------------------------------------------- #
    def find_appointment_options(self) -> list[dict[str, Any]]:
        """Booking options: what the dealer can offer alongside the work.

        This is where a loan car lives in Keyloop's model. It matters because
        'transport' is not a free-text note on the booking — it is a bookable
        option with its own availability.
        """
        self._require_partner_key()
        return [
            {"appointmentOptionId": "OPT-WAIT", "code": "WAIT",
             "description": "Customer waits on site", "requiresAvailability": False},
            {"appointmentOptionId": "OPT-LOAN", "code": "LOAN_CAR",
             "description": "Replacement vehicle", "requiresAvailability": True},
            {"appointmentOptionId": "OPT-COLLECT", "code": "COLLECT",
             "description": "Collection and delivery", "requiresAvailability": True},
        ]

    # -- availability ------------------------------------------------------ #
    def advisor_availability(self, date_from: str, date_to: str,
                             service_product_code: str) -> dict[str, Any]:
        """Availability is keyed by advisor, and each window carries a slot list.

        Note the real-world detail the adapter has to absorb: Keyloop returns
        *advisor availability*, not a flat list of times. The adapter picks an
        advisor per slot. A naive mapping from a flat slot list would lose the
        advisor and the create call would then fail.
        """
        self._require_partner_key()
        slots = self._backend.available_slots(date_from, date_to,
                                              self._backend.seed.dealer_id)
        out: list[dict[str, Any]] = []
        for index, slot in enumerate(slots):
            advisor = f"ADV-{(index % 3) + 1}"
            out.append({
                "advisorId": advisor,
                "startDateTime": slot.start,
                "durationMinutes": 60,
                "availabilityStatus": "AVAILABLE",
            })
        return {
            "dealerCode": self._dealer_code,
            "serviceProductCode": service_product_code,
            "advisorAvailability": out,
        }

    # -- create ------------------------------------------------------------ #
    def create_service_appointment(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_partner_key()
        start = datetime.fromisoformat(payload["startDateTime"])
        appointment = self._backend.commit(
            dealer_id=self._backend.seed.dealer_id,
            vehicle_id=payload["vehicleId"],
            customer_id=payload.get("customerId") or "WALK-IN",
            service_type=payload["serviceProductCode"],
            start=start,
            duration_minutes=int(payload.get("durationMinutes", 60)),
            transport_option=payload.get("appointmentOptionCode", "none"),
            notes=payload.get("notes", ""),
            idempotency_key=payload["clientReference"],
            source_system="keyloop",
            advisor_id=payload.get("advisorId", ""),
        )
        return {
            "appointmentId": appointment.appointment_id,
            "appointmentReference": appointment.reference,
            "appointmentStatus": "BOOKED",
            "advisorId": appointment.advisor_id,
            "startDateTime": appointment.start.isoformat(timespec="minutes"),
        }

    # -- read / delete ----------------------------------------------------- #
    def get_service_appointment(self, appointment_id: str) -> dict[str, Any]:
        self._require_partner_key()
        appointment = self._backend.appointments.get(appointment_id)
        if appointment is None:
            return {}
        return {
            "appointmentId": appointment.appointment_id,
            "appointmentReference": appointment.reference,
            "appointmentStatus": ("BOOKED" if appointment.status == "confirmed"
                                 else "CANCELLED"),
            "startDateTime": appointment.start.isoformat(timespec="minutes"),
            "serviceProductCode": appointment.service_type,
            "vehicleId": appointment.vehicle_id,
        }

    def delete_service_appointment(self, appointment_id: str) -> None:
        self._require_partner_key()
        self._backend.cancel(appointment_id)


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #

class KeyloopDms(DeclaredCapabilities):
    """Translates canonical calls into Keyloop SAB workflow calls."""

    vendor = "keyloop"

    def __init__(self, dealer_id: str, *, credentials: KeyloopCredentials | None = None,
                 integration: str = "Keyloop SAB v2 (full)",
                 declared: frozenset[str] | None = None) -> None:
        self.dealer_id = dealer_id
        self.integration = integration
        self._backend = MemoryBackend(default_seed(dealer_id))
        self._api = SimulatedKeyloopApi(
            self._backend, credentials or KeyloopCredentials(partner_key="demo-partner-key"),
            dealer_code=dealer_id.upper().replace("-", ""),
        )
        self._declared = declared if declared is not None else frozenset({
            CAP_AVAILABILITY_SEARCH, CAP_CREATE_APPOINTMENT, CAP_CANCEL_APPOINTMENT,
            CAP_READ_VEHICLE, CAP_READ_CUSTOMER, CAP_READ_APPOINTMENT,
            CAP_READ_SERVICE_HISTORY, CAP_READ_TRANSPORT,
        })

    def capabilities(self) -> frozenset[str]:
        return self._declared

    def _require(self, capability: str) -> None:
        """Declare-and-refuse. An undeclared capability is refused loudly rather
        than attempted and failed, so the flow can route on it in advance."""
        if capability not in self._declared:
            raise DmsCapabilityError(
                f"This dealer's Keyloop integration does not support "
                f"'{capability}'. Endpoint availability varies per DMS behind "
                f"the Open Platform.",
                code="CAPABILITY_NOT_AVAILABLE", vendor=self.vendor, status=403,
            )

    # -- reads ------------------------------------------------------------- #

    def lookup_vehicle(self, plate: str) -> list[Vehicle]:
        self._require(CAP_READ_VEHICLE)
        # Via the platform, so the partner key is actually checked.
        records = self._api.search_vehicles(plate)
        ids = {record["vehicleId"] for record in records}
        return [v for v in self._backend.seed.vehicles if v.vehicle_id in ids]

    def lookup_customer(self, phone: str) -> Customer | None:
        self._require(CAP_READ_CUSTOMER)
        records = self._api.search_customers(phone)
        if not records:
            return None
        return self._backend.customer_by_id(records[0]["customerId"])

    def check_availability(self, service_type: str, date_from: str,
                           date_to: str) -> list[Slot]:
        self._require(CAP_AVAILABILITY_SEARCH)
        # The adapter's real work: Keyloop returns advisor-keyed windows, and a
        # later create call needs the advisor. So the advisor is carried on the
        # Slot rather than discarded.
        response = self._api.advisor_availability(date_from, date_to, service_type)
        return [
            Slot(
                date=window["startDateTime"][:10],
                time=window["startDateTime"][11:16],
                advisor_id=window["advisorId"],
                duration_minutes=window.get("durationMinutes", 60),
            )
            for window in response.get("advisorAvailability", [])
            if window.get("availabilityStatus") == "AVAILABLE"
        ]

    def get_appointment(self, appointment_id: str) -> Appointment | None:
        self._require(CAP_READ_APPOINTMENT)
        record = self._api.get_service_appointment(appointment_id)
        if not record:
            return None
        return self._backend.appointments.get(appointment_id)

    # -- writes ------------------------------------------------------------ #

    def create_appointment(self, *, vehicle_id: str, customer_id: str,
                           service_type: str, start: str,
                           duration_minutes: int, transport_option: str,
                           notes: str, idempotency_key: str,
                           advisor_id: str = "") -> Appointment:
        self._require(CAP_CREATE_APPOINTMENT)

        # Keyloop models transport as a bookable option, not a note, so the
        # canonical enum is mapped to the option code here.
        option_map = {
            "wait_on_site": "WAIT", "loan_car": "LOAN_CAR",
            "collection_and_delivery": "COLLECT", "none": "WAIT",
        }
        try:
            response = self._api.create_service_appointment({
                "vehicleId": vehicle_id,
                "customerId": customer_id,
                "serviceProductCode": service_type,
                "startDateTime": start,
                "durationMinutes": duration_minutes,
                "appointmentOptionCode": option_map.get(transport_option, "WAIT"),
                "advisorId": advisor_id or "ADV-1",
                "notes": notes,
                # Keyloop requires a client reference; ours is derived from the
                # booking tuple, so a retry is recognised as the same booking.
                "clientReference": idempotency_key,
            })
        except DmsSlotUnavailableError:
            raise
        except DmsAuthError:
            raise

        appointment = self._backend.appointments[response["appointmentId"]]
        appointment.vendor_status = response.get("appointmentStatus", "")
        return appointment

    def cancel_appointment(self, appointment_id: str) -> Appointment:
        self._require(CAP_CANCEL_APPOINTMENT)
        self._api.delete_service_appointment(appointment_id)
        return self._backend.appointments[appointment_id]
