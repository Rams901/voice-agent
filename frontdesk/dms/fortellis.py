"""Fortellis adapter, shaped after CDK's Fixed Ops APIs.

Fortellis is CDK's Automotive Commerce Exchange: a marketplace where a
developer subscribes to a provider's API rather than integrating dealer-by-dealer.
That changes the access model in ways worth naming, because they show up in the
interface:

* **Every call carries a `Subscription-Id`** identifying the marketplace
  subscription between the dealer and the solution. There is no "just call the
  API" — authorisation is a three-way relationship.
* **Auth is OAuth 2.0 bearer**, not a static key.
* **Conditional writes use ETags** (`If-Match`), so a reschedule can fail safely
  when someone else has changed the appointment in the meantime.
* **CDK Drive ships both a synchronous "Get Service Appointments" API and an
  "Async Service Appointment" API.** That second one is the most valuable thing
  in this file: the write returns `202 Accepted` with a job id, and the booking
  does not exist yet. An adapter that treats 202 as success produces an agent
  that confidently confirms appointments that were never created.

The adapter therefore surfaces that case as `DmsAsyncPendingError`, and the
capability flags it, so the *flow* can decide whether to poll or to promise a
callback. Silence would be the bug.

Modelled from the public CDK Fixed Ops and Fortellis community documentation.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .base import DeclaredCapabilities
from .model import (
    CAP_ASYNC_WRITES, CAP_AVAILABILITY_SEARCH, CAP_CREATE_APPOINTMENT,
    CAP_READ_APPOINTMENT, CAP_READ_CUSTOMER, CAP_READ_PRICING,
    CAP_READ_SERVICE_HISTORY, CAP_READ_TRANSPORT, CAP_READ_VEHICLE,
    Appointment, Customer, DmsAsyncPendingError, DmsAuthError,
    DmsCapabilityError, DmsSlotUnavailableError, DmsTransientError,
    DmsValidationError, MemoryBackend, Slot, Vehicle, default_seed,
)


@dataclass
class FortellisCredentials:
    """What a Fortellis call actually needs: an OAuth token and a subscription.

    Note there is no dealer id here. The subscription *is* the scoping
    mechanism — that is the whole point of the marketplace model, and it means
    one integration can serve many dealers without per-dealer plumbing.
    """

    oauth_token: str = ""
    subscription_id: str = ""


class SimulatedFortellisApi:
    """In-process stand-in for the Fortellis service APIs.

    Speaks the documented shapes: the required header set, RFC 7807-style
    problem responses, `202 Accepted` for async writes, and a scheduling query
    identifier that can be reused to refine a search.
    """

    REQUIRED_HEADERS = ("Accept", "Accept-Charset", "Accept-Language", "Prefer",
                        "Request-Id", "Subscription-Id", "Authorization")

    def __init__(self, backend: MemoryBackend, credentials: FortellisCredentials,
                 async_writes: bool) -> None:
        self._backend = backend
        self._credentials = credentials
        self._async_writes = async_writes
        self._jobs: dict[str, dict[str, Any]] = {}
        self._job_counter = itertools.count(1)
        self._queries: dict[str, dict[str, Any]] = {}
        self._query_counter = itertools.count(1)
        self._etags: dict[str, str] = {}

    def _authorise(self, request_id: str) -> None:
        """Fortellis rejects a call missing any of its required headers, and the
        failure is a 400 rather than a 401 — which is exactly the kind of vendor
        quirk an adapter exists to absorb."""
        if not self._credentials.oauth_token:
            raise DmsAuthError(
                "Fortellis requires an OAuth 2.0 bearer token. Provider calls "
                "cannot be made with a static API key.",
                code="UNAUTHORIZED", vendor="fortellis", status=401,
            )
        if not self._credentials.subscription_id:
            raise DmsValidationError(
                "Fortellis requires a Subscription-Id header identifying the "
                "marketplace subscription between the dealer and the solution.",
                code="MISSING_SUBSCRIPTION_ID", vendor="fortellis", status=400,
            )

    # -- scheduling -------------------------------------------------------- #

    def query_appointment_slots(self, criteria: dict[str, Any]) -> dict[str, Any]:
        self._authorise(criteria.get("requestId", ""))
        query_id = f"QRY-{next(self._query_counter)}"
        self._queries[query_id] = criteria
        slots = self._backend.available_slots(
            criteria["dateFrom"], criteria["dateTo"], self._backend.seed.dealer_id,
        )
        return {
            "queryId": query_id,
            "appointmentSlots": [
                {
                "dateTime": slot.start,
                "durationMinutes": slot.duration_minutes,
                    "advisor": {"id": slot.advisor_id or "ANY"},
                    "transport": {"id": criteria.get("transportId", "NONE")},
                }
                for slot in slots
            ],
        }

    def reserve_appointment(self, request: dict[str, Any]) -> dict[str, Any]:
        """POST /service/v1/scheduling/appointments — Reserve Appointment.

        Returns either the created appointment, or a 202 job handle when the
        provider is configured for asynchronous writes.
        """
        self._authorise(request.get("requestId", ""))

        if self._async_writes:
            job_id = f"JOB-{next(self._job_counter)}"
            self._jobs[job_id] = {"status": "ACCEPTED", "request": request}
            return {"status": 202, "jobId": job_id, "state": "ACCEPTED"}

        try:
            appointment = self._backend.commit(
                dealer_id=self._backend.seed.dealer_id,
                vehicle_id=request["vehicleId"],
                customer_id=request.get("customerId", "WALK-IN"),
                service_type=request["serviceType"],
                start=datetime.fromisoformat(request["dateTime"]),
                duration_minutes=int(request.get("durationMinutes", 60)),
                transport_option=request.get("transportOption", "none"),
                notes=request.get("notes", ""),
                idempotency_key=request["requestId"],
                source_system="fortellis",
                advisor_id=request.get("advisorId", ""),
            )
        except DmsSlotUnavailableError as exc:
            return {"status": 409, "jobId": "", "state": "UNAVAILABLE",
                    "detail": str(exc)}

        self._etags[appointment.appointment_id] = f'W/"{appointment.appointment_id}-1"'
        return {
            "status": 201,
            "jobId": "",
            "state": "RESERVED",
            "appointmentId": appointment.appointment_id,
            "appointmentNumber": appointment.reference,
            "dateTime": appointment.start.isoformat(timespec="minutes"),
            "etag": self._etags[appointment.appointment_id],
        }

    def get_job(self, job_id: str) -> dict[str, Any]:
        """Poll an async write. A job is only complete once it reports COMPLETED."""
        self._authorise("")
        job = self._jobs.get(job_id)
        if job is None:
            raise DmsValidationError(f"Unknown job {job_id}.",
                                     code="JOB_NOT_FOUND", vendor="fortellis")
        if job["status"] == "ACCEPTED":
            request = job["request"]
            appointment = self._backend.commit(
                dealer_id=self._backend.seed.dealer_id,
                vehicle_id=request["vehicleId"],
                customer_id=request.get("customerId", "WALK-IN"),
                service_type=request["serviceType"],
                start=datetime.fromisoformat(request["dateTime"]),
                duration_minutes=int(request.get("durationMinutes", 60)),
                transport_option=request.get("transportOption", "none"),
                notes=request.get("notes", ""),
                idempotency_key=request["requestId"],
                source_system="fortellis",
            )
            job["status"] = "COMPLETED"
            job["appointmentId"] = appointment.appointment_id
            job["appointmentNumber"] = appointment.reference
        return job

    # -- appointment API v3 ------------------------------------------------ #

    def query_appointment(self, appointment_id: str) -> dict[str, Any]:
        self._authorise("")
        appointment = self._backend.appointments.get(appointment_id)
        if appointment is None:
            raise DmsValidationError(
                f"Appointment {appointment_id} not found.",
                code="NOT_FOUND", vendor="fortellis", status=404,
            )
        return {
            "appointmentId": appointment.appointment_id,
            "appointmentNumber": appointment.reference,
            "dateTime": appointment.start.isoformat(timespec="minutes"),
            "status": "CONFIRMED" if appointment.status == "confirmed" else "CANCELLED",
            "serviceType": appointment.service_type,
        }

    def delete_appointment(self, appointment_id: str, etag: str = "") -> None:
        self._authorise("")
        current = self._etags.get(appointment_id, "")
        if etag and current and etag != current:
            # Precondition failed: someone else changed it. The flow must refresh
            # rather than overwrite — the same reason you check a row version.
            raise DmsTransientError(
                "ETag mismatch: the appointment changed since it was read. "
                "Re-read it before amending.",
                code="PRECONDITION_FAILED", vendor="fortellis", status=412,
            )
        self._backend.cancel(appointment_id)


class FortellisDms(DeclaredCapabilities):
    """Translates canonical calls into Fortellis scheduling/appointment calls."""

    vendor = "fortellis"

    def __init__(self, dealer_id: str, *,
                 credentials: FortellisCredentials | None = None,
                 async_writes: bool = False,
                 integration: str | None = None) -> None:
        self.dealer_id = dealer_id
        self._async_writes = async_writes
        self.integration = integration or (
            "CDK Drive via Fortellis (async writes)" if async_writes
            else "CDK Drive via Fortellis (sync)"
        )
        self._backend = MemoryBackend(default_seed(dealer_id))
        self._api = SimulatedFortellisApi(
            self._backend,
            credentials or FortellisCredentials(
                oauth_token="demo-oauth-token", subscription_id="demo-subscription",
            ),
            async_writes=async_writes,
        )
        declared = {
            CAP_AVAILABILITY_SEARCH, CAP_CREATE_APPOINTMENT, CAP_READ_APPOINTMENT,
            CAP_READ_VEHICLE, CAP_READ_CUSTOMER, CAP_READ_TRANSPORT,
            CAP_READ_PRICING, CAP_READ_SERVICE_HISTORY,
        }
        # The async variant declares its extra capability, so the flow can route
        # on it *before* the caller is told anything.
        if async_writes:
            declared.add(CAP_ASYNC_WRITES)
        self._declared = frozenset(declared)

    def capabilities(self) -> frozenset[str]:
        return self._declared

    def _require(self, capability: str) -> None:
        if capability not in self._declared:
            raise DmsCapabilityError(
                f"This Fortellis integration does not declare '{capability}'.",
                code="CAPABILITY_NOT_AVAILABLE", vendor=self.vendor, status=403,
            )

    # -- reads ------------------------------------------------------------- #

    def lookup_vehicle(self, plate: str) -> list[Vehicle]:
        self._require(CAP_READ_VEHICLE)
        return self._backend.find_vehicle(plate)

    def lookup_customer(self, phone: str) -> Customer | None:
        self._require(CAP_READ_CUSTOMER)
        return self._backend.find_customer(phone)

    def check_availability(self, service_type: str, date_from: str,
                           date_to: str) -> list[Slot]:
        self._require(CAP_AVAILABILITY_SEARCH)
        response = self._api.query_appointment_slots({
            "serviceType": service_type,
            "dateFrom": date_from,
            "dateTo": date_to,
            "requestId": f"req-{service_type}-{date_from}",
        })
        return [
            Slot(
                date=slot["dateTime"][:10],
                time=slot["dateTime"][11:16],
                advisor_id=(slot.get("advisor") or {}).get("id", ""),
                duration_minutes=slot.get("durationMinutes", 60),
            )
            for slot in response.get("appointmentSlots", [])
        ]

    def get_appointment(self, appointment_id: str) -> Appointment | None:
        self._require(CAP_READ_APPOINTMENT)
        try:
            self._api.query_appointment(appointment_id)
        except DmsValidationError:
            return None
        return self._backend.appointments.get(appointment_id)

    # -- writes ------------------------------------------------------------ #

    def create_appointment(self, *, vehicle_id: str, customer_id: str,
                           service_type: str, start: str,
                           duration_minutes: int, transport_option: str,
                           notes: str, idempotency_key: str,
                           advisor_id: str = "") -> Appointment:
        self._require(CAP_CREATE_APPOINTMENT)
        response = self._api.reserve_appointment({
            "vehicleId": vehicle_id,
            "customerId": customer_id,
            "serviceType": service_type,
            "dateTime": start,
            "durationMinutes": duration_minutes,
            "transportOption": transport_option,
            "advisorId": advisor_id,
            "notes": notes,
            "requestId": idempotency_key,
        })

        state = response.get("state")
        if state == "UNAVAILABLE":
            raise DmsSlotUnavailableError(
                response.get("detail", "Slot unavailable."),
                code="SLOT_UNAVAILABLE", vendor=self.vendor, status=409,
            )
        if response.get("status") == 202 or state == "ACCEPTED":
            # The critical branch. The provider accepted the write but has not
            # committed it. Returning a fake Appointment here is precisely how an
            # agent ends up confirming a booking that does not exist.
            raise DmsAsyncPendingError(
                "CDK accepted the appointment asynchronously; it is not yet "
                "committed. Poll the job, or tell the caller you will confirm.",
                job_id=response.get("jobId", ""),
                code="ASYNC_PENDING", vendor=self.vendor, status=202,
            )
        appointment = self._backend.appointments[response["appointmentId"]]
        appointment.vendor_status = state or ""
        return appointment

    def await_appointment(self, job_id: str, attempts: int = 20) -> Appointment:
        """Poll an async write to completion.

        Provided on the adapter because the polling semantics are vendor-specific
        (job states, retry interval). The flow decides *whether* to wait; the
        adapter knows *how*.
        """
        for _ in range(attempts):
            job = self._api.get_job(job_id)
            if job.get("status") == "COMPLETED":
                return self._backend.appointments[job["appointmentId"]]
        raise DmsTransientError(
            f"Async job {job_id} did not complete in time.",
            code="ASYNC_TIMEOUT", vendor=self.vendor,
        )

    def cancel_appointment(self, appointment_id: str) -> Appointment:
        etag = self._api._etags.get(appointment_id, "")
        self._api.delete_appointment(appointment_id, etag=etag)
        return self._backend.appointments[appointment_id]
