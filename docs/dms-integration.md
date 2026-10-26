# DMS integration — the landscape, the contract, and how to proceed

One voice flow has to work across ten more dealer/DMS
integrations". This document is the answer to *how*, and it is deliberately
about the part that is hardest and least discussed: the integrations are not ten
versions of the same thing.

Everything described here is implemented. `python -m frontdesk.cli adapters
--conformance` runs it; `tests/test_dms_conformance.py` guards it.

---

## 1. Why this is the hard part

A dealership's **DMS** (Dealer Management System) is the system of record for
everything: customers, vehicles, service history, the workshop diary, parts,
warranty claims, invoicing. Every voice agent that books a service is, whether
it admits it or not, writing into one.

Two facts make this the binding constraint:

- **Fragmentation.** There are 48+ distinct DMS products in use across US
  franchised dealerships alone, and dealer groups span several. The same
  rooftop-level vendor problem repeats market by market: Keyloop, incadea and
  Autoline in Europe; CDK, Reynolds, Dealertrack, Tekion and Dominion in the US;
  Nextlane's Ecaros/iCar/Kobra/Serauto across France, Germany, Spain and Sweden.
- **The systems were not built to be written to from outside.** DMSs were built
  to run internal dealership operations. Nothing broadcasts outward by default,
  and write access is gated behind contracts and certification programmes.

So the engineering problem is not "call an API". It is: **build one flow, expose
an honest feature set per dealer, and never let the agent promise something the
connection cannot do.**

---

## 2. The real landscape

These are the vendors a UK/European dealership is most likely to hit, with the
integration model that actually matters.

| Vendor | Region | Public API surface | Booking write? | Access model |
|---|---|---|---|---|
| **Keyloop** | UK, EU, ME, Africa, Asia | Open Platform; **Service Appointment Booking (SAB)** | Yes — create/delete service appointment | Partner contract + partner key |
| **CDK Global** | US, global | **Fortellis** marketplace; Fixed Ops APIs; Scheduling API + Appointments API | Yes — `POST /service/v1/scheduling/appointments` | Fortellis subscription + OAuth 2.0 |
| **Reynolds & Reynolds** | US | **RCI** (Certified Interface) — not self-serve | Usually read-only for scheduling partners | Certification programme |
| **Tekion** | US, global | **Automotive Partner Cloud (APC)**, OpenAPI + webhooks | Yes, tiered (Standard / Premium / Strategic) | Gated signup, ~48h review |
| **incadea** | Europe, global | API-first; International Make Layer per OEM | Varies | Certified |
| **Autoline** (Kerridge) | UK, Europe | Varies by product | Varies | Varies |
| **Dealertrack** (Cox) | US | Varies | Varies | Varies |
| **Nextlane** (Ecaros, iCar, Kobra II, Serauto) | FR, DE, ES, SE | Varies | Varies | Varies |

### The details worth quoting

**Keyloop's SAB is a workflow, not a call.** Their documented endpoints run:
service categories → service products → appointment options → advisor search →
advisor availability → create service appointment. A slot is only valid for a
given *(service product, booking option, advisor)* combination. It also exposes
`getServiceAppointment` and `deleteServiceAppointment`.

**Keyloop's own documentation says capability varies per DMS.** Their developer
portal warns that *"not all endpoints, properties within an endpoint, or
parameters within an endpoint are available or relevant for every dealer
management system"*. One vendor, many back-ends, different feature sets.

**Fortellis requires a `Subscription-Id` header on every call.** Authorisation is
a three-way relationship between dealer, solution and marketplace, not a static
key. It also uses OAuth 2.0 bearer tokens and ETags for conditional writes.

**CDK ships both sync and async appointment APIs.** Alongside "Get Service
Appointments" there is an **Async Service Appointment** API: the write returns
`202 Accepted` with a job id, and the appointment does not exist yet. This is the
single most dangerous detail in this document, and §4 explains why.

**Reynolds readers are often read-only.** Partners are explicit about it —
Matador, RCI-certified for ERA, states plainly that appointments are *read from*
Reynolds and not pushed back. Write access is a separate negotiation.

**Tekion's APC is tiered.** Standard partners get foundational APIs and standard
rate limits; higher tiers unlock more scopes and read/write depth. So two Tekion
dealers can differ too.

---

## 3. The four axes that actually differ

Vendor is only the first question. Four axes matter, and they are independent:

1. **Access model** — public API, partner key, marketplace subscription,
   certification programme, or nothing at all.
2. **Read/write depth** — full read-write, read-only, or read-only plus a
   specific write (e.g. appointments but not warranty claims).
3. **Synchronous vs asynchronous writes** — whether "accepted" means "committed".
4. **Data freshness** — live API, webhooks, polling, or a nightly export.

The fourth is the one people forget. A scheduled CSV export is not a slower API:
it is a *snapshot*, and an agent offering a slot from a 24-hour-old export is
offering a time it cannot know is free.

**The consequence:** capability is a property of the *connection*, not the
product. That is why the code declares capabilities per dealer and the
conformance test asserts two dealers on the same vendor can legitimately differ.

---

## 4. The contract

`frontdesk/dms/base.py` defines nine methods. The narrowness is deliberate: the
cost of adding a DMS is the cost of implementing everything you declare, so a
narrow interface that degrades honestly beats a wide one that forces every
integration to be a project.

```
lookup_vehicle(plate) -> list[Vehicle]        # list: a plate can match several
lookup_customer(phone) -> Customer | None
check_availability(service, from, to) -> list[Slot]
get_appointment(id) -> Appointment | None     # makes verify-the-write possible
create_appointment(...) -> Appointment
cancel_appointment(id) -> Appointment
capabilities() -> frozenset[str]
```

Three requirements make `create_appointment` safe, and all three are tested:

1. **Idempotent** on an idempotency key derived from the booking tuple, never
   from the model. A retry returns the original appointment. This is what makes
   `max_retry > 0` safe rather than a double-booking generator.
2. **Re-checks availability at commit time.** Availability is a snapshot; a slot
   offered a minute ago may be gone by the time the caller decides.
3. **Raises `DmsAsyncPendingError` rather than returning a fake success** when the
   vendor accepted the write without committing it.

### Why the third requirement matters more than it looks

This is the whole reason the error taxonomy is typed:

```python
class DmsError(RuntimeError): ...
class DmsCapabilityError(DmsError): ...    # this dealer cannot do that — a known limit
class DmsSlotUnavailableError(DmsError): ...  # stale: apologise, re-offer
class DmsTransientError(DmsError): ...        # retryable
class DmsAuthError(DmsError): ...             # needs an operator
class DmsValidationError(DmsError): ...       # bad input: ask again
class DmsAsyncPendingError(DmsError): ...     # accepted, NOT committed
```

Each implies a different sentence to the caller and a different route. Collapsing
them into "something went wrong" is exactly what produces agents that apologise
vaguely instead of transferring.

And `DmsCapabilityError` is the most important one, because it is **not a
failure**. It is a known limit, and it means the flow can route *before* the
caller invests five minutes:

```
dealer's connection cannot book
    → don't collect booking details
    → capture contact details, promise a callback
    → or transfer now
```

An agent that collects the plate, the dates, the transport preference and *then*
discovers it cannot write has wasted the caller's time and damaged trust. That is
a routing problem, and the routing input is data.

---

## 5. The capability matrix as a pre-sales artifact

`python -m frontdesk.cli adapters` prints this. It is the thing you send a dealer
*before* promising them a feature.

```
adapter                          async   avail  cancel    book    appt  customer   price   RO  history transport  vehicle
-------------------------------------------------------------------------------------------------------------------------
reynolds-rci / RCI (read-only)        -       -       -       -     yes       yes     yes  yes      yes         -      yes
keyloop / SAB v2 (full)               -     yes     yes     yes     yes       yes       -    -      yes       yes      yes
fortellis / CDK (async writes)      yes     yes       -     yes     yes       yes     yes    -      yes       yes      yes
export-only / CSV export (no API)     -       -       -       -       -       yes       -  yes      yes         -      yes
```

Five dealers, four distinct integration modes. Notice that `northgate-uk` and
`veldhoven-nl` are both Keyloop but are *not* equivalent — the Dutch DMS does not
expose booking options, so no loan car is offered there. Same vendor, different
connection.

The matrix is generated from the adapters, so it cannot go stale.

---

## 6. How the 10th DMS stays cheap

The failure mode in a scaling plan is discovering each integration's quirks in
production, because the failure is a customer being told something untrue.

`frontdesk/dms/conformance.py` is a **reusable test suite**, not a test file. It
runs against every adapter and asserts the things that actually break:

| Check | What it prevents |
|---|---|
| Retried write is idempotent | Double-booking on a Retell retry |
| Write can be read back | Confirming a booking that was never stored |
| Stale slot is refused at commit | Two customers in one bay |
| Ambiguous plate returns two vehicles | Booking the wrong car |
| Undeclared capability raises | `[]` masquerading as "no availability" |
| Availability is stable across calls | A diary that contradicts itself |
| Unparseable dates raise a *typed* error | A generic apology escaping the taxonomy |
| Async write reports pending | **Confirmation of a booking that does not exist** |

Adding a DMS is then:

1. write one class implementing the contract
2. register the vendor name
3. add a dealer profile row
4. `pytest tests/test_dms_conformance.py` — green means safe

Nothing in `flow.py`, `tools.py`, `tool_server/` or the eval suite changes. The
tool server contains no vendor name at all, which is the check that the boundary
held.

### The drift tests, which caught three real bugs while writing this

The interesting failures at scale are not in adapters, they are in *configuration
disagreeing with code*. Three tests exist for that, and each one fired during
development:

- `test_profile_capabilities_are_a_subset_of_adapter_capabilities` — caught
  `northgate-uk` claiming `write_warranty_claim` (a separate Keyloop product) and
  `rural-fr` claiming a live repair-order read. Both were fixed by *removing the
  claim*, not by faking the capability.
- `test_flow_gates_agree_with_capabilities` — the `dealer_can_book` dynamic
  variable must match the adapter. If it says `true` while the adapter cannot
  write, the bug is invisible until a customer is lied to.
- `test_async_capability_has_a_dedicated_flow_path` — declaring async writes is
  worthless if nothing routes on it. A capability the flow ignores is a number in
  a config file, not an architecture.

---

## 7. How to proceed — the sequence

**Before writing an adapter, for a new dealer:**

1. **Ask what the connection actually is.** Not "which DMS" — which *contract*
   with which vendor, and which APIs that contract unlocks. Keyloop, Fortellis and
   Tekion all gate this, and the answer changes the adapter.
2. **Get the capability answer in writing.** Which endpoints, which fields,
   read or write, sync or async. Their documentation may warn that availability
   varies per DMS — believe it.
3. **Fill in the matrix row first.** If the honest answer is "read-only", that is
   a supported configuration and the flow already handles it. It is far cheaper
   to discover that now than after promising booking.
4. **Check the OAuth/subscription/certification lead time.** These are
   weeks-to-months gates, not tickets.
5. **Check data freshness.** A nightly export means no voice booking, full stop.

**Then, in order:**

6. **Adapter against a vendor-shaped simulator.** Build the translation logic
   against something that speaks the vendor's wire format — headers, error
   shapes, `202 Accepted`, ETags. When credentials arrive, only the transport
   changes. That is exactly how `keyloop.py` and `fortellis.py` are built: the
   adapters are real, the transport is swappable.
7. **Green conformance suite.** The gate.
8. **Shadow mode, then a narrow write scope.** Read-only in production first
   (look up vehicles, quote availability), and compare what the agent *would*
   have booked against what the dealer did. Only then enable writes.
9. **Replay real transcripts.** The simulation suite is ~10 scenarios; the real
   regression net is production replay.

### The long tail

Roughly three modes dominate, and only the first supports voice booking:

```
full API, read/write   →  commit the booking
certified read-only    →  capture and hand off
no API, export only    →  capture and hand off, and never offer a slot
```

Planning for mode 1 and discovering modes 2 and 3 is what makes an integration
programme look successful in month one and produce complaints in month three.

---

## 8. The argument in one page

**Open with the reframe:**

> The DMS integration is not an API problem, it's a capability problem. There are
> 48+ DMSs and they don't just differ by vendor — they differ by connection.
> Two dealers on the same Keyloop platform can have different endpoints available.

**Then the specific detail that proves you did the reading:**

> CDK ships an Async Service Appointment API. The write returns 202 Accepted and
> the appointment doesn't exist yet. If your adapter treats that as success, your
> agent confirms bookings that were never made — and nothing in your tests catches
> it, because the adapter is behaving exactly as written.

**Then the architecture:**

> So the adapter declares capabilities, refuses loudly for anything it can't do,
> and raises a typed error for async writes. The flow branches on the capability
> *before* it collects booking details, so a read-only dealer's caller hears
> "let me take your details and have the team call you" instead of spending five
> minutes on a booking that was never going to commit.

**Then the scaling argument:**

> Adding the 10th DMS is one class and one config row, and a reusable conformance
> suite tells you it's safe in about a second. That's why the tool server has no
> vendor name in it.

**And volunteer the limitation:**

> The adapters are modelled against public documentation, because the real
> endpoints sit behind partner contracts. The translation logic and the contract
> are real; the wire format is what changes when credentials arrive — which is
> why the transport is the one swappable piece.

---

## 9. Eval and observability hooks

Integration failures surface in production as *behaviour*, not stack traces. The
fields extracted on every call (`detail_lost`, `agent_failure`,
`customer_frustration`) exist to catch the classes of failure that a capability
mismatch produces:

- a caller asked to repeat a detail the agent already had → `detail_lost`
- a booking described as confirmed that the DMS has no record of →
  `agent_failure`
- a caller routed to a human after being led through a booking flow →

Plus, at the integration boundary itself:

- **capability refusals** — if `DmsCapabilityError` fires on a dealer that should
  be able to book, the profile is wrong. That is a config bug, and it should page.
- **async writes that never complete** — a job polled to timeout is an
  appointment the caller was told about and nobody has. Highest-severity alert.
- **verification failures** — `verify_appointment` returning `false` after a
  `create_appointment` that reported success means a vendor is lying or the
  adapter is. Either way it is a hot path.

---

## 10. The skeleton — how to add a DMS

This is the whole of the work, and it is short enough to sketch from
memory.

```python
"""<Vendor> adapter, shaped after their public <Product> API."""

from .base import DeclaredCapabilities
from .model import (
    CAP_AVAILABILITY_SEARCH, CAP_CREATE_APPOINTMENT, CAP_READ_APPOINTMENT,
    CAP_READ_CUSTOMER, CAP_READ_VEHICLE, Appointment, Customer,
    DmsAuthError, DmsCapabilityError, DmsSlotUnavailableError, Slot, Vehicle,
)

class VendorApi:
    """Speaks the vendor's wire format. In production this is HTTP; here it is a
    simulator, so the adapter's translation logic is real without credentials."""

    def authenticate(self) -> None:
        # Partner key, OAuth bearer, Subscription-Id header, whatever it is.
        ...

    def search_slots(self, criteria: dict) -> dict:
        """Return the vendor's shape — advisor-keyed windows, query ids, etc."""

    def create_booking(self, payload: dict) -> dict:
        """Return the vendor's shape. Distinguish 201 from 202."""

class VendorDms(DeclaredCapabilities):
    vendor = "vendor"

    def __init__(self, dealer_id: str, *, credentials=None) -> None:
        self.dealer_id = dealer_id
        self.integration = "Vendor Product v1"
        self._api = VendorApi(...)
        # What the *product* can do — not what this dealer is configured for.
        self._declared = frozenset({
            CAP_AVAILABILITY_SEARCH, CAP_CREATE_APPOINTMENT,
            CAP_READ_VEHICLE, CAP_READ_CUSTOMER, CAP_READ_APPOINTMENT,
        })

    def capabilities(self) -> frozenset[str]:
        return self._declared

    def _require(self, capability: str) -> None:
        if capability not in self._declared:
            raise DmsCapabilityError(..., code="CAPABILITY_NOT_AVAILABLE")

    def lookup_vehicle(self, plate: str) -> list[Vehicle]:
        self._require(CAP_READ_VEHICLE)
        # ... translate vendor records -> canonical Vehicle, keep it a list
        ...

    def check_availability(self, service_type, date_from, date_to) -> list[Slot]:
        self._require(CAP_AVAILABILITY_SEARCH)
        # ... carry the advisor/option ids onto the Slot; the write will need them
        ...

    def get_appointment(self, appointment_id: str) -> Appointment | None:
        self._require(CAP_READ_APPOINTMENT)
        ...   # this method is what makes verify-the-write possible — implement it

    def create_appointment(self, *, vehicle_id, customer_id, service_type, start,
                           duration_minutes, transport_option, notes,
                           idempotency_key) -> Appointment:
        self._require(CAP_CREATE_APPOINTMENT)
        response = self._api.create_booking({... "clientReference": idempotency_key})
        if response.get("status") == 202:
            raise DmsAsyncPendingError(..., job_id=response["jobId"])
        # ... map vendor status -> canonical, return the stored appointment
        ...

    def cancel_appointment(self, appointment_id: str) -> Appointment:
        self._require(CAP_CANCEL_APPOINTMENT)
        ...
```

Then two lines of wiring and a config row:

```python
# frontdesk/dms/registry.py
FACTORIES["vendor"] = lambda dealer_id: VendorDms(dealer_id)

# frontdesk/dealer_profiles.py
"new-dealer": DealerProfile(..., dms_vendor="vendor", capabilities=FULL_FIXED_OPS, ...)
```

Then:

```
pytest tests/test_dms_conformance.py    # green = safe to put in front of a customer
python -m frontdesk.cli adapters --conformance
```

The checks most likely to fail on a first attempt, in order of how often they
bite: **idempotency** (the key is usually derived from something the model
supplied), **read-back** (the write path is implemented but the read is not), and
**stale-slot refusal** (availability is trusted from the earlier call).

---

## 11. Honest limitations

- **The wire formats are modelled, not transcribed.** Keyloop, Fortellis, Tekion
  and Reynolds all gate their real specs behind partner agreements, so the
  endpoints in `keyloop.py` and `fortellis.py` are shaped after public
  documentation. The contract, the capability model and the conformance suite are
  real; only the transport is simulated.
- **No production write path.** Every adapter runs against an in-process backend.
  Going live means replacing the transport and re-running conformance.
- **Rate limits and quotas are not modelled.** Real APIs have per-subscription
  limits, and a busy hour will hit them. That needs a queue and backoff, not a
  synchronous call from a voice turn.
- **No reconciliation job.** Webhook-primary with polling-fallback is the pattern
  for keeping two systems converged when a callback is missed. It is designed for
  and not built.
- **Consent and audit are not implemented.** Under the FTC Safeguards Rule,
  dealers must be able to demonstrate opt-in, with audit logs and documented
  consent workflows. DMS access is a compliance surface, not just a technical one.
- **One market's plate formats.** Masks cover a subset of countries; the long
  tail needs either more masks or cross-channel verification.
