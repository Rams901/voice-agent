"""Retell tool definitions.

Every tool here is a thin HTTP wrapper around the *deterministic* layer
(`identifiers.py` and `dms.py`). The agent never gets to compute a plate's
validity itself, decide whether a slot exists, or assert that a booking
succeeded. It proposes; code disposes.

Two design choices worth calling out:

* `validate_identifier` is a **function node** target, not an LLM-chosen tool,
  because validation must always run at a fixed point in the flow.
* `create_appointment` is idempotent server-side and sets `max_retry > 0`, which
  is only safe *because* it is idempotent. Retell's own docs warn about this:
  retries repeat the request, so retry is only safe on idempotent endpoints.
"""

from __future__ import annotations

from typing import Any

TIMEOUT_MS = 12_000


def _const(value: str, description: str) -> dict[str, Any]:
    """A parameter Retell applies directly, so the LLM never generates it.

    This is the difference between a call that works and a 422. Retell fills a
    property that has a `description` from the model's judgement, but applies a
    property that has a `const` verbatim — and substitutes `{{dynamic_variables}}`
    inside it.

    That distinction matters more than it looks. `validate_identifier` requires
    `kind`, `value` and `country`; the model supplied only `country` and the
    endpoint returned 422 in the middle of a booking, because the other two were
    simply absent. Both were knowable in advance: `kind` is always a plate here,
    and `country` is the dealer's, already sitting in a dynamic variable.

    So the rule this helper encodes: **if we already know the value, it is a
    const.** Anything left for the model is a guess we have chosen to accept, and
    there should be as few of those as possible.
    """
    return {"type": "string", "const": value, "description": description}


def _llm(description: str, **schema: Any) -> dict[str, Any]:
    """A parameter the model must supply, because only the caller knows it."""
    return {"type": "string", "description": description, **schema}


#: Ways a caller actually says a plate, used twice: shown to the model so
#: "verbatim" is concrete, and asserted in tests to still normalise to one plate.
#:
#: The second use is the point. Examples in a prompt are a claim about a
#: deterministic function, and claims rot — change the normaliser and a prompt
#: can quietly start lying about what it accepts. These are checked instead.
#: (See `tests/test_flow.py::test_the_plate_examples_are_actually_accepted`.)
PLATE_VALUE_EXAMPLES = [
    "AB12CDE",
    "A B one two C D E",
    "Alpha Bravo twelve Charlie Delta Echo",
    "a for alpha, b for bravo, one, two, c for charlie, d for delta, e for echo",
    # Hedging is included deliberately. A caller who is unsure is the normal
    # case, not the exception, and this example is what keeps that path working:
    # the assertion below fails if a hedge ever stops normalising.
    "AB12CDE, I think",
]


def _tool(name: str, base_url: str, path: str, description: str,
          properties: dict[str, Any], required: list[str],
          response_variables: dict[str, str],
          speak_during_execution: bool = False, max_retry: int = 0,
          timeout_ms: int = TIMEOUT_MS) -> dict[str, Any]:
    return {
        "type": "custom",
        "name": name,
        "url": f"{base_url}{path}",
        "method": "POST",
        "description": description,
        "speak_during_execution": speak_during_execution,
        "speak_after_execution": False,
        "timeout_ms": timeout_ms,
        "max_retry": max_retry,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
        # Retell nests arguments under `args` unless this is set, so the request
        # body is {"name":..., "args":{...}, "call":{...}} and a flat
        # `ValidateRequest` never matches. FastAPI then reports the required
        # fields as "missing" — which reads exactly like the model declined to
        # supply them, and sends you tuning prompts for a transport problem.
        #
        # This single flag is the root cause of every 422 in this project's
        # history, including two that were "fixed" by making fields optional.
        # Those fixes were wrong: the server was still reading an empty body, so
        # `flag_conversation` recorded blank reasons and lookups ran with no
        # plate. A field going optional hid a request that carried nothing.
        "args_at_root": True,
        "response_variables": response_variables,
    }


def build_tools(base_url: str, shared_secret: str = "") -> list[dict[str, Any]]:
    """Build the tool list for a dealer, pointed at the tool server.

    `base_url` is the public URL of `tool_server/`, e.g. an ngrok/cloudflared
    tunnel to localhost:8000.
    """
    validate = _tool(
        name="validate_identifier",
        base_url=base_url,
        path="/tools/validate_identifier",
        description=(
            "Deterministically validate a registration plate the caller spelled "
            "out. ALWAYS call this immediately after capturing the plate and "
            "BEFORE reading it back or acting on it. Pass the caller's words "
            "verbatim; do not correct, tidy or reformat them yourself. If valid "
            "is false and readback is present, read the readback to the caller "
            "and ask them to confirm or correct it. Never invent a plate."
        ),
        properties={
            # `kind` is a constant in this flow — it only ever validates plates.
            # `country` comes from the dealer profile, so it is a const pointing
            # at a dynamic variable rather than something the model must recall.
            # Only `value` is left to the model, because only the caller knows
            # it. One guess instead of three.
            "kind": _const("license_plate",
                           "Always a registration plate in this flow."),
            # The server normalises (see frontdesk/identifiers.py), so the model
            # is not asked to understand plate formats — only to transcribe
            # faithfully. The examples exist because "verbatim" is abstract, and
            # without them a model may helpfully "fix" what it heard. Every form
            # below is accepted, so this guides without narrowing.
            "value": {
                "type": "string",
                # The examples are interpolated into the prose rather than set as
                # a JSON Schema `examples` array. Retell silently drops every
                # schema keyword except `type`, `description` and `const` —
                # verified against the live API, where `examples` came back
                # absent. So `description` is the only channel to the model, and
                # relying on `examples` would have shipped guidance that exists
                # in this file and nowhere the model can see it.
                "description": (
                    "The plate exactly as the caller said or spelled it. Do not "
                    "tidy, reorder, complete or correct it — the server does the "
                    "interpreting. Pass the spoken words, for example: "
                    + "; ".join(f"'{example}'" for example in PLATE_VALUE_EXAMPLES)
                    + ". If they spell it letter by letter, keep the letters as "
                    "they said them. If they sound unsure, pass what you heard "
                    "anyway — the readback step is what confirms it."
                ),
            },
            "country": _const("{{country}}", "The dealer's ISO country code."),
        },
        # Only the LLM-generated parameter is listed. `required` tells Retell
        # which properties the model must generate, so listing a const here
        # would invite the model to invent a value for something already applied.
        required=["value"],
        response_variables={
            "identifier_valid": "data.valid",
            "identifier_normalised": "data.normalised",
            "identifier_confidence": "data.confidence",
            "identifier_readback": "data.readback",
            "identifier_reason": "data.reason",
            "identifier_coercions": "data.coercions",
            "identifier_uncertain_positions": "data.uncertain_positions",
            "identifier_suggestions": "data.suggestions",
        },
    )

    lookup_vehicle = _tool(
        name="lookup_vehicle",
        base_url=base_url,
        path="/tools/lookup_vehicle",
        description=(
            "Look up vehicles in the dealer's DMS by registration plate. Call "
            "this AFTER the caller has confirmed the plate. Returns vehicle "
            "details so the conversation can be personalised. If match_count "
            "is greater than 1 you MUST ask the caller which vehicle they mean "
            "using the returned summaries — never choose one yourself."
        ),
        properties={
            # The plate is not the model's to remember. It is the value the
            # validator already normalised and the caller already confirmed, so
            # it is substituted straight out of a dynamic variable. The lookup
            # cannot disagree with the read-back any more, because there is only
            # one value in play.
            "plate": _const("{{identifier_normalised}}",
                            "The plate the caller confirmed."),
            "dealer_id": _const("{{dealer_id}}", "The dealer id for this call."),
        },
        # Nothing for the model to generate: both values are already known.
        required=[],
        response_variables={
            "vehicle_found": "data.found",
            "vehicle_match_count": "data.match_count",
            "vehicle_ambiguous": "data.ambiguous",
            "vehicle_id": "data.vehicle_id",
            "vehicle_summary": "data.summary",
            "vehicle_mileage": "data.mileage",
            "vehicle_warranty_active": "data.warranty_active",
            "vehicle_options": "data.options",
            "customer_id": "data.customer_id",
            "customer_known": "data.customer_known",
            "customer_first_name": "data.customer_first_name",
            "customer_outstanding_balance": "data.outstanding_balance",
            "vehicle_lookup_message": "data.message",
        },
    )

    check_availability = _tool(
        name="check_availability",
        base_url=base_url,
        path="/tools/check_availability",
        description=(
            "Check real appointment slots in the dealer's DMS for a date range. "
            "Use the ISO dates the caller asked for. Only ever offer slots "
            "returned by this tool — never invent or estimate a time."
        ),
        properties={
            "dealer_id": _const("{{dealer_id}}", "The dealer id."),
            "service_type": _llm(
                "The service being booked, e.g. annual_service, mot."),
            "date_from": _llm("ISO start date, YYYY-MM-DD."),
            "date_to": _llm("ISO end date, YYYY-MM-DD."),
        },
        required=["service_type", "date_from", "date_to"],
        response_variables={
            "slots_available": "data.count",
            "slots": "data.slots",
            "slots_message": "data.message",
        },
        speak_during_execution=True,
    )

    create_appointment = _tool(
        name="create_appointment",
        base_url=base_url,
        path="/tools/create_appointment",
        description=(
            "Commit a service appointment to the dealer's DMS. This is an "
            "IRREVERSIBLE action: only call it after you have read the vehicle, "
            "service and chosen time back to the caller and they have clearly "
            "confirmed. The endpoint is idempotent, so a retry cannot "
            "double-book. Never call it speculatively to 'check' something."
        ),
        properties={
            # Identity and the vehicle were settled by lookup_vehicle and the
            # caller's confirmation, so they are substituted, not recalled.
            "dealer_id": _const("{{dealer_id}}", "The dealer id."),
            "vehicle_id": _const("{{vehicle_id}}", "The DMS vehicle id."),
            "customer_id": _const("{{customer_id}}", "The DMS customer id."),
            "service_type": _llm("The service being booked."),
            "start": _llm("ISO date-time of the slot, YYYY-MM-DDTHH:MM."),
            # Deliberately absent: the server derives duration from its own
            # service table. Letting the model guess a duration for a job it
            # cannot see was a needless way to be wrong.
            "transport_option": _llm(
                "How the customer gets moving while the car is in.",
                enum=["wait_on_site", "loan_car", "collection_and_delivery", "none"],
            ),
            "notes": _llm("What the customer reported."),
        },
        required=["service_type", "start", "transport_option"],
        response_variables={
            "booking_confirmed": "data.confirmed",
            # Distinct from confirmed. Some vendors accept a write without
            # committing it (CDK's Async Service Appointment API returns
            # 202 Accepted), and conflating the two is how an agent confirms a
            # booking that does not exist.
            "booking_pending": "data.pending",
            "booking_reference": "data.reference",
            "appointment_id": "data.appointment_id",
            "booking_start": "data.start",
            "booking_message": "data.message",
        },
        speak_during_execution=True,
        # Safe ONLY because the endpoint is idempotent on a server-derived key.
        max_retry=2,
    )

    verify_appointment = _tool(
        name="verify_appointment",
        base_url=base_url,
        path="/tools/verify_appointment",
        description=(
            "Read an appointment back out of the DMS to confirm it was stored "
            "correctly. Always call this immediately after create_appointment, "
            "before telling the caller the booking is done. If write_verified "
            "is not true, apologise and transfer to a human."
        ),
        properties={
            # The id came back from create_appointment and was mapped to a
            # dynamic variable. Asking the model to repeat it back was inviting
            # it to mistype the one value the whole verification depends on.
            "appointment_id": _const("{{appointment_id}}",
                                     "Id returned when booking."),
            "dealer_id": _const("{{dealer_id}}", "The dealer id."),
        },
        required=[],
        response_variables={
            "write_verified": "data.verified",
            "stored_summary": "data.stored_summary",
            "verify_message": "data.message",
        },
    )

    flag_conversation = _tool(
        name="flag_conversation",
        base_url=base_url,
        path="/tools/flag_conversation",
        description=(
            "Record that this call needs human review. Use it whenever the "
            "caller sounds frustrated, repeats a request, disputes an invoice, "
            "or you could not resolve their issue. This is how the tail of "
            "unhappy customers surfaces instead of hiding behind a green "
            "dashboard."
        ),
        properties={
            "reason": {
                "type": "string",
                "description": (
                    "Short reason code for the review, from this list where one "
                    "fits: rental_request, booking_failed, billing_query, "
                    "warranty_claim, wants_human, frustrated, unresolved, "
                    "consent_declined, transfer_failed. Use the closest match."
                ),
            },
            "severity": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": (
                    "How urgent the review is. Defaults to medium if omitted."
                ),
            },
            "detail": {
                "type": "string",
                "description": (
                    "What happened, in one or two sentences — enough for a "
                    "colleague to pick the call up without replaying it."
                ),
            },
        },
        # Only `reason` is declared required. `severity` is not: the server
        # defaults it, and a schema that demands a field the endpoint is happy
        # without is what invites the model to invent a value to satisfy it.
        required=["reason"],
        response_variables={"flag_recorded": "data.recorded", "flag_message": "data.message"},
    )

    # The last thing that happens on a successful call, and deliberately the
    # least demanding. `required=[]` because refusing is a legitimate answer: a
    # schema that insists on a rating is a schema that invites the model to
    # invent one to satisfy it, which is the opposite of what a satisfaction
    # measure is for. The instruction says to pass what they said verbatim.
    record_feedback = _tool(
        name=RECORD_FEEDBACK,
        base_url=base_url,
        path="/tools/record_feedback",
        description=(
            "Record the caller's satisfaction at the end of a successful call. "
            "Call this once, after asking the caller how the service was. "
            "Capture their rating and anything they wanted to add, and do not "
            "ask again or argue with the answer."
        ),
        properties={
            "rating": {
                "type": "string",
                "description": (
                    "The rating the caller gave, as they said it — a number or "
                    "word from one to five, e.g. '5', 'four', 'four out of "
                    "five'. If they declined to rate, pass what they said "
                    "instead, e.g. 'no thanks'. Pass an empty string if they "
                    "gave no clear answer. Never invent or round a rating."
                ),
            },
            "comment": {
                "type": "string",
                "description": (
                    "Anything the caller wanted to add, in their own words, "
                    "kept verbatim — a compliment, a complaint, or a "
                    "suggestion. Empty string if they had nothing to add. Do "
                    "not tidy or summarise it."
                ),
            },
        },
        required=[],
        response_variables={
            "feedback_recorded": "data.recorded",
            "feedback_message": "data.message",
        },
    )

    tools = [validate, lookup_vehicle, check_availability, create_appointment,
             verify_appointment, flag_conversation, record_feedback]
    if shared_secret:
        for tool in tools:
            tool["headers"] = {"X-Frontdesk-Secret": shared_secret}
    return tools


# Tool names, referenced by the flow builder so a rename cannot silently break it.
VALIDATE_IDENTIFIER = "validate_identifier"
LOOKUP_VEHICLE = "lookup_vehicle"
CHECK_AVAILABILITY = "check_availability"
CREATE_APPOINTMENT = "create_appointment"
VERIFY_APPOINTMENT = "verify_appointment"
FLAG_CONVERSATION = "flag_conversation"
RECORD_FEEDBACK = "record_feedback"


def tool_id(name: str) -> str:
    """The id a flow-level tool is registered under.

    Function nodes must reference *this*, not the tool's `name`. Keeping one
    function as the single source of truth is the difference between a rename
    being a no-op and a rename silently breaking every function node — which is
    exactly the bug this helper exists to prevent.
    """
    return f"tool_{name}"
