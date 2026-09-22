"""Mock DMS tool server.

This is the deterministic half of the architecture, exposed to Retell as custom
tools. It is deliberately boring: no model, no judgement, same input -> same
output. Everything the agent is not allowed to decide lives here.

It is also where the DMS adapter layer surfaces. Note what this file does *not*
contain: any vendor name. It calls the canonical interface and translates the
typed error taxonomy into sentences the agent can act on. Adding a DMS does not
touch this file — which is the whole point.

Run:
    uvicorn tool_server.app:app --reload --port 8000

Then expose it to Retell with a tunnel and set TOOLS_BASE_URL:
    cloudflared tunnel --url http://localhost:8000
    # or: ngrok http 8000
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frontdesk import identifiers  # noqa: E402
from frontdesk.dealer_profiles import get_dealer  # noqa: E402
from frontdesk.dms import (  # noqa: E402
    DmsAsyncPendingError, DmsAuthError, DmsCapabilityError, DmsError,
    DmsSlotUnavailableError, DmsTransientError, DmsValidationError, get_dms,
)

app = FastAPI(title="Frontdesk — mock DMS tool server", version="2.0.0")


class RetellToolRequest(BaseModel):
    """Base for every tool request body.

    Retell nests arguments under `args` unless the tool sets `args_at_root`, so
    a misconfigured tool sends::

        {"name": "validate_identifier", "args": {...}, "call": {...}}

    FastAPI then matches none of the flat fields and reports every required one
    as *missing* — an error that reads like the model declined to supply them.
    That is precisely how a transport problem spent three rounds being treated
    as a prompt problem, and why `flag_conversation.reason` was made optional to
    "fix" a 422 that was really the server reading an empty body.

    So the wrapped shape is rejected explicitly, naming the setting to change.
    Rejecting rather than silently unwrapping is deliberate: a tool that
    proceeds with no arguments is how a blank reason gets recorded or a lookup
    runs with no plate, and a wrong answer is worse than a loud failure.
    """

    @model_validator(mode="before")
    @classmethod
    def _reject_nested_envelope(cls, data: Any) -> Any:
        if isinstance(data, dict) and {"name", "args", "call"} <= set(data):
            raise ValueError(
                "Retell sent tool arguments nested under 'args'. Set "
                "args_at_root: true on this tool (see frontdesk/tools.py) so "
                "the request body is the flat argument object."
            )
        return data


SHARED_SECRET = os.environ.get("TOOLS_SHARED_SECRET", "")

# Service duration table. Real implementations read this from the DMS.
SERVICE_DURATION_MINUTES = {
    "annual_service": 90, "inspektion": 90, "onderhoudsbeurt": 90, "revision": 90,
    "mot": 60, "apk": 60, "hu_au": 60, "controle_technique": 60,
    "state_inspection": 60,
    "diagnostic": 60, "diagnose": 60,
    "brakes": 120, "remmen": 120, "bremsen": 120, "freins": 120,
    "tyres": 45, "banden": 45, "reifen": 45, "pneus": 45, "tire_rotation": 45,
    "aircon": 60, "airco": 60, "klimaanlage": 60, "climatisation": 60,
    "oil_change": 45, "recall": 120,
}

# Everything the server was asked to flag, so the demo can show the
# "watch the frustrated tail, not just the average" story.
FLAGS: list[dict[str, Any]] = []

# Everything the callers said about the service. Kept separate from FLAGS on
# purpose: a flag means "someone must look at this", feedback means "here is what
# they thought". Merging them would bury the first inside the second, and make
# a five-star comment look like a review case.
FEEDBACK: list[dict[str, Any]] = []
# Async write jobs the tool server is tracking, so `verify_appointment` can
# report "still pending" rather than inventing an answer.
PENDING_JOBS: dict[str, dict[str, Any]] = {}


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"status": "success", "data": data}


def _err(message: str) -> dict[str, Any]:
    return {"status": "error", "message": message}


@app.middleware("http")
async def check_secret(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Optional shared secret, so a public tunnel is not wide open."""
    if SHARED_SECRET and request.url.path.startswith("/tools/"):
        if request.headers.get("X-Frontdesk-Secret") != SHARED_SECRET:
            raise HTTPException(status_code=401, detail="Bad or missing shared secret")
    return await call_next(request)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "frontdesk-tool-server"}


@app.get("/capabilities/{dealer_id}")
def capabilities(dealer_id: str) -> dict[str, Any]:
    """What this dealer's integration can actually do.

    Exposed as an endpoint because an operator needs to be able to answer that
    question without reading code — and because it is the honest way to onboard
    a dealer: check the matrix *before* promising them a feature.
    """
    dealer = get_dealer(dealer_id)
    adapter = get_dms(dealer_id)
    return _ok({
        "dealer_id": dealer_id,
        "vendor": adapter.vendor,
        "integration": adapter.integration,
        "declared": sorted(adapter.capabilities()),
        "note": dealer.integration_note,
    })


@app.get("/capability-matrix")
def capability_matrix() -> dict[str, Any]:
    """The matrix across every configured dealer."""
    from frontdesk.dealer_profiles import all_dealers
    from frontdesk.dms import summarise

    adapters = [get_dms(d.dealer_id) for d in all_dealers()]
    return {
        "matrix": summarise(adapters),
        "dealers": [
            {
                "dealer_id": d.dealer_id,
                "vendor": get_dms(d.dealer_id).vendor,
                "integration": get_dms(d.dealer_id).integration,
                "capabilities": sorted(get_dms(d.dealer_id).capabilities()),
                "note": d.integration_note,
            }
            for d in all_dealers()
        ],
    }


# --------------------------------------------------------------------------- #
# validate_identifier — the guardrail that fixes the misheard plate
# --------------------------------------------------------------------------- #

class ValidateRequest(RetellToolRequest):
    kind: str = Field(description="license_plate | vin | phone | mileage | email")
    value: str
    country: str = "GB"


@app.post("/tools/validate_identifier")
def validate_identifier(body: ValidateRequest) -> dict[str, Any]:
    """Pure, deterministic. Note what is absent: any model involvement."""
    result = identifiers.validate_identifier(
        body.kind, body.value, country=body.country.upper()
    )
    return _ok(result)


# --------------------------------------------------------------------------- #
# lookup_vehicle — reconcile against the system of record
# --------------------------------------------------------------------------- #

class LookupRequest(RetellToolRequest):
    plate: str
    dealer_id: str


@app.post("/tools/lookup_vehicle")
def lookup_vehicle(body: LookupRequest) -> dict[str, Any]:
    dealer = get_dealer(body.dealer_id)
    adapter = get_dms(dealer.dealer_id)

    # A format mask can never prove a plate is correct, so re-validate here too:
    # defence in depth, and it catches a caller who bypassed the capture step.
    check = identifiers.validate_plate(body.plate, country=dealer.country,
                                       masks=dealer.plate_masks)
    if not check.valid and not check.normalised:
        return _ok({
            "found": "false", "match_count": "0", "ambiguous": "false",
            "message": "That did not look like a registration plate.",
        })

    try:
        vehicles = adapter.lookup_vehicle(check.normalised or body.plate)
    except DmsError as exc:
        return _err(_explain(exc))

    if not vehicles:
        return _ok({
            "found": "false", "match_count": "0", "ambiguous": "false",
            "message": f"No vehicle found for {check.normalised}.",
        })

    if len(vehicles) > 1:
        # Never pick. Two valid records exist; only the caller knows which.
        return _ok({
            "found": "true",
            "match_count": str(len(vehicles)),
            "ambiguous": "true",
            "options": " | ".join(v.to_dict()["summary"] for v in vehicles),
            "message": (
                "More than one vehicle is registered to that plate. Ask the "
                "caller which one they mean using the options provided."
            ),
        })

    vehicle = vehicles[0]

    # Not every adapter can go from a vehicle back to its customer — Keyloop's
    # API is phone-keyed and declares no such lookup, and the contract does not
    # require one. That is a genuine "I cannot tell you", and it must never be
    # dressed up as a customer who owes nothing.
    finder = getattr(adapter, "customer_for_vehicle", None)
    customer = None
    if callable(finder):
        try:
            customer = finder(vehicle.vehicle_id)
        except DmsError:
            customer = None

    data = vehicle.to_dict()
    data.update({
        "found": "true",
        "match_count": "1",
        "ambiguous": "false",
        "options": data["summary"],
        "customer_known": "true" if customer is not None else "false",
        "customer_id": customer.customer_id if customer is not None else "",
        "customer_first_name": customer.first_name if customer is not None else "",
        # Empty rather than "0.00" when unresolved. A balance is a claim the
        # agent is then allowed to repeat to the caller, and "we could not look
        # it up" is a different fact from "they owe nothing".
        "outstanding_balance": (
            f"{customer.outstanding_balance:.2f}" if customer is not None else ""
        ),
        "message": "",
    })
    return _ok(data)


# --------------------------------------------------------------------------- #
# check_availability — availability is a fact
# --------------------------------------------------------------------------- #

class AvailabilityRequest(RetellToolRequest):
    dealer_id: str
    service_type: str
    date_from: str
    date_to: str


@app.post("/tools/check_availability")
def check_availability(body: AvailabilityRequest) -> dict[str, Any]:
    dealer = get_dealer(body.dealer_id)
    adapter = get_dms(dealer.dealer_id)

    try:
        start = date.fromisoformat(body.date_from)
        end = date.fromisoformat(body.date_to)
    except ValueError:
        # The model occasionally emits a vague range. Clamp rather than fail.
        start = date.today() + timedelta(days=1)
        end = start + timedelta(days=14)

    if end < start:
        start, end = end, start

    try:
        slots = adapter.check_availability(
            body.service_type, start.isoformat(), end.isoformat()
        )
    except DmsError as exc:
        # A missing capability is not a failure — it is a routing decision. The
        # message is written so the agent can read the situation out as an
        # instruction rather than an apology.
        return _ok({"count": "0", "slots": "", "message": _explain(exc)})

    if not slots:
        return _ok({
            "count": "0", "slots": "",
            "message": f"Nothing free between {start} and {end}.",
        })
    return _ok({
        "count": str(len(slots)),
        "slots": ", ".join(str(slot) for slot in slots),
        "message": "",
    })


# --------------------------------------------------------------------------- #
# create_appointment — irreversible, so it is idempotent
# --------------------------------------------------------------------------- #

class CreateRequest(RetellToolRequest):
    dealer_id: str
    vehicle_id: str
    customer_id: str
    service_type: str
    start: str
    duration_minutes: int | None = None
    transport_option: str = "none"
    notes: str = ""


@app.post("/tools/create_appointment")
def create_appointment(body: CreateRequest) -> dict[str, Any]:
    dealer = get_dealer(body.dealer_id)
    adapter = get_dms(dealer.dealer_id)

    try:
        start_dt = datetime.fromisoformat(body.start)
    except ValueError:
        return _err(
            f"The start time {body.start!r} was not a valid ISO date-time. "
            "Ask the caller for the day and time again."
        )

    duration = body.duration_minutes or SERVICE_DURATION_MINUTES.get(body.service_type, 60)

    # Idempotency key derived from the booking tuple, not from the model. A
    # retried request (Retell retries on failure) returns the SAME appointment
    # instead of creating a duplicate. This is what makes max_retry > 0 safe.
    idempotency_key = "|".join([
        dealer.dealer_id, body.vehicle_id, body.service_type,
        start_dt.strftime("%Y-%m-%dT%H:%M"),
    ])

    try:
        appointment = adapter.create_appointment(
            vehicle_id=body.vehicle_id,
            customer_id=body.customer_id,
            service_type=body.service_type,
            start=start_dt.isoformat(timespec="minutes"),
            duration_minutes=duration,
            transport_option=body.transport_option,
            notes=body.notes,
            idempotency_key=idempotency_key,
        )
    except DmsAsyncPendingError as exc:
        # The write was accepted; it is not committed. The flow has a dedicated
        # node for this, and the caller is told the truth.
        PENDING_JOBS[exc.job_id] = {
            "dealer_id": dealer.dealer_id, "request": body.model_dump(),
            "started_at": datetime.utcnow().isoformat(timespec="seconds"),
        }
        return _ok({
            "confirmed": "false",
            "pending": "true",
            "reference": "",
            "appointment_id": "",
            "start": body.start,
            "job_id": exc.job_id,
            "message": (
                "The dealer's system accepted the request but confirms bookings "
                "in the background. Do not say the booking is confirmed."
            ),
        })
    except DmsError as exc:
        return _err(_explain(exc))

    data = appointment.to_dict()
    data["confirmed"] = "true"
    data["pending"] = "false"
    data["message"] = ""
    return _ok(data)


# --------------------------------------------------------------------------- #
# verify_appointment — "the model said it worked" is not evidence
# --------------------------------------------------------------------------- #

class VerifyRequest(RetellToolRequest):
    appointment_id: str
    dealer_id: str = ""


@app.post("/tools/verify_appointment")
def verify_appointment(body: VerifyRequest) -> dict[str, Any]:
    dealer_id = body.dealer_id or os.environ.get("DEALER_ID", "northgate-uk")
    adapter = get_dms(dealer_id)

    try:
        appointment = adapter.get_appointment(body.appointment_id)
    except DmsError as exc:
        return _ok({
            "verified": "false", "stored_summary": "",
            "message": _explain(exc),
        })

    if appointment is None or appointment.status != "confirmed":
        return _ok({
            "verified": "false", "stored_summary": "",
            "message": (
                "No confirmed appointment found. Do not tell the caller the "
                "booking is done — apologise and transfer."
            ),
        })
    return _ok({
        "verified": "true",
        "stored_summary": appointment.to_dict()["what_dms_stored"],
        "message": "",
    })


# --------------------------------------------------------------------------- #
# flag_conversation — surfacing the frustrated tail
# --------------------------------------------------------------------------- #

class FlagRequest(RetellToolRequest):
    # Deliberately tolerant. The nodes that call this are the ones a caller
    # reaches when they are already unhappy or out of options, so a hard 422
    # here turns a bad moment into a broken one — which is exactly what
    # happened: `reason` was required, the model omitted it, and the handoff
    # died mid-call. A flag with a thin reason is still a usable flag.
    reason: str = ""
    severity: str = "medium"
    detail: str = ""
    call_id: str = ""


VALID_SEVERITIES = frozenset({"low", "medium", "high"})


@app.post("/tools/flag_conversation")
def flag_conversation(body: FlagRequest) -> dict[str, Any]:
    # Normalise rather than reject: an out-of-enum severity is a model slip,
    # not a reason to drop the whole flag.
    severity = body.severity if body.severity in VALID_SEVERITIES else "medium"
    reason = body.reason.strip() or "unspecified"

    record = {
        "reason": reason,
        "severity": severity,
        "detail": body.detail,
        "call_id": body.call_id,
        "flagged_at": datetime.utcnow().isoformat(timespec="seconds"),
    }
    FLAGS.append(record)
    print(f"[FLAG] {severity.upper()} {reason}: {body.detail}", flush=True)
    return _ok({"recorded": "true", "message": "Flagged for human review."})


@app.get("/flags")
def list_flags() -> dict[str, Any]:
    """Inspect flagged calls.

    A dashboard built only on averages hides exactly these calls, which is the
    point: with review sites in play, every one of them counts.
    """
    return {"count": len(FLAGS), "flags": FLAGS}


# --------------------------------------------------------------------------- #
# Satisfaction
# --------------------------------------------------------------------------- #

#: Spoken numbers, because a caller says "four", not "4".
_NUMBER_WORDS: dict[str, str] = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
}

#: Ways of saying no. Matched as substrings so "I'd rather not, thanks" counts.
#: A decline is *not* the same as silence: "asked and declined" and "never asked"
#: are different facts, and merging them would quietly flatter the average.
_DECLINE_PHRASES: tuple[str, ...] = (
    "no thanks", "no thank you", "rather not", "prefer not", "skip", "no comment",
)


def _normalise_rating(raw: str) -> tuple[str, bool]:
    """Turn what the caller said into a score, a decline, or nothing.

    Returns `(rating, declined)`. Anything unrecognised comes back as `""` rather
    than the nearest plausible score. Nudging "quite good actually" to a 4 would
    be inventing a data point the caller never gave — the same honesty rule the
    plate matcher follows, applied to satisfaction. The raw text is kept
    alongside, so a human can still read what was actually said.
    """
    text = (raw or "").strip()
    if not text:
        return "", False

    lowered = text.lower().strip(" .!")
    if any(phrase in lowered for phrase in _DECLINE_PHRASES):
        return "", True

    # Only the first token decides. It covers "four", "four out of five",
    # "5 stars" and "3." without needing to understand the rest of the sentence.
    token = lowered.split()[0].strip(" .,!")
    if token in _NUMBER_WORDS:
        return _NUMBER_WORDS[token], False
    if token.isdigit() and 1 <= int(token) <= 5:
        return str(int(token)), False
    return "", False


class FeedbackRequest(RetellToolRequest):
    # Tolerant on purpose, like `FlagRequest`. Declining is a legitimate answer,
    # so nothing is required — and a schema that demanded a rating would invite
    # the model to invent one to satisfy it.
    rating: str = ""
    comment: str = ""


@app.post("/tools/record_feedback")
def record_feedback(body: FeedbackRequest) -> dict[str, Any]:
    rating, declined = _normalise_rating(body.rating)

    record = {
        "rating": rating,
        # Kept even when it could not be scored: the words are the useful half
        # of feedback, and a human reading "4" learns less than "engine light
        # still on".
        "rating_raw": body.rating.strip(),
        "declined": declined,
        "comment": body.comment.strip(),
        "recorded_at": datetime.utcnow().isoformat(timespec="seconds"),
    }
    FEEDBACK.append(record)

    if declined:
        outcome = "Caller declined to rate."
    elif rating:
        outcome = f"Recorded a rating of {rating}."
    else:
        outcome = "Recorded; no scorable rating given."
    print(f"[FEEDBACK] {outcome} {record['comment']!r}", flush=True)

    return _ok({"rating": rating, "recorded": "true", "message": outcome})


@app.get("/feedback")
def list_feedback() -> dict[str, Any]:
    """Satisfaction, with declines counted separately from scores.

    `average` is deliberately `None` rather than 0 when nothing was scored: an
    average over no data points is not zero, and reporting it as zero would read
    as terrible service rather than as an absence of answers.
    """
    scored = [int(r["rating"]) for r in FEEDBACK if r["rating"]]
    return {
        "count": len(FEEDBACK),
        "declined": sum(1 for r in FEEDBACK if r["declined"]),
        "average": (sum(scored) / len(scored)) if scored else None,
        "feedback": FEEDBACK,
    }


# --------------------------------------------------------------------------- #
# Error translation
# --------------------------------------------------------------------------- #

def _explain(exc: DmsError) -> str:
    """Turn a typed DMS error into an instruction the agent can act on.

    This function is why the taxonomy is typed rather than stringly-typed. Each
    error class implies a *different* thing to say and a different route, and
    collapsing them into "something went wrong" is what produces agents that
    apologise vaguely instead of transferring.
    """
    if isinstance(exc, DmsCapabilityError):
        # Not a failure: a known limit of this dealer's integration.
        return str(exc)
    if isinstance(exc, DmsSlotUnavailableError):
        alternatives = ""
        if exc.alternatives:
            alternatives = " Other times are free: " + ", ".join(exc.alternatives[:3]) + "."
        return (
            f"That time was taken while you were deciding. Apologise and offer a "
            f"different time — do not re-use the slot.{alternatives}"
        )
    if isinstance(exc, DmsTransientError):
        return (
            "The dealer's system did not respond. Apologise, and offer to have "
            "the team call back rather than retrying in front of the caller."
        )
    if isinstance(exc, DmsAuthError):
        return (
            "There is a permissions problem connecting to the dealer's system. "
            "Do not attempt the action — transfer to the service team."
        )
    if isinstance(exc, DmsValidationError):
        return (
            f"The dealer's system rejected the request: {exc}. Apologise and "
            "check the detail with the caller."
        )
    return f"The dealer's system returned an error: {exc}. Transfer to the service team."


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
