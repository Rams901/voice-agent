"""The redesigned conversation flow: router + skills + a deterministic spine.

This module is where the structure lives. The monolith it replaces was

    one flow, ~190 nodes, one system prompt > 14,000 tokens,
    greeting -> intent routing -> booking -> rental -> warranty -> payment,
    all in one pass.

The structure below is deliberately the opposite, and every choice maps to a
named failure mode:

    small global prompt        -> limits instruction drift
    router node                -> every downstream prompt stays small
    one skill per business job -> independent test/version/deploy
    function nodes for gates   -> guaranteed execution, not LLM discretion
    equation edges             -> deterministic branching on validated state
    else edges everywhere      -> the flow can never get stuck
    verify the write           -> the model's "success" is not evidence
    global intent/tone prompt   -> one coherent customer experience

`estimate_prompt_tokens` at the bottom quantifies the before/after, because the
real question is "why is 14k a smell, and what does it become?".
"""

from __future__ import annotations

from typing import Any

from .dealer_profiles import DealerProfile, dealer_dynamic_variables
from .tools import (CHECK_AVAILABILITY, CREATE_APPOINTMENT, FLAG_CONVERSATION,
                    LOOKUP_VEHICLE, RECORD_FEEDBACK, VALIDATE_IDENTIFIER,
                    VERIFY_APPOINTMENT, build_tools, tool_id)

# --------------------------------------------------------------------------- #
# Shared global prompt
# --------------------------------------------------------------------------- #
# Kept short on purpose. Everything in here applies to *every* node, so anything
# that is stage-specific belongs in a node instruction instead. This is the
# single biggest structural difference from the 14k monolith.
#
# The "never" rules are the deterministic-spine contract: the model is not
# permitted to be the final authority on any fact that code can check.

GLOBAL_PROMPT = """\
You are the service assistant for {{dealer_name}} ({{dealer_id}}), a \
franchised dealership in {{country}}. Speak {{language}} in a {{tone}} \
manner. This is a live phone call: keep replies to one or two short sentences, \
never read out lists of more than three items, and never use formatting.

Opening hours: {{opening_hours}}. Services offered: {{services_list}}.

It is currently {{current_time}}. Treat that as the only source of truth for \
today's date: your own sense of the date is out of date, and a wrong weekday \
puts a caller in the diary on the wrong day.

How you must behave:
- You are the front desk, not the system of record. Never state or imply a \
vehicle, price, slot or booking unless a tool returned it in this call.
- Never invent or "correct" a registration plate, VIN, phone number or date. \
Spell identifiers back phonetically before relying on them.
- When a tool result tells you to confirm something with the caller, do it \
before moving on.
- Commit nothing until the caller has clearly confirmed the details. If they \
sound unsure, ask again rather than assuming a yes.
- Never take card or bank details, and never discuss payment amounts beyond \
what a tool reported. Offer a transfer instead.
- If the caller is frustrated, repeats themselves, or asks for a person, stop \
the current stage and transfer to a human — do not keep retrying.
- If you cannot resolve something, say so plainly, and transfer. Do not guess.\
"""


# --------------------------------------------------------------------------- #
# Node helpers
# --------------------------------------------------------------------------- #

def _prompt_condition(text: str) -> dict[str, Any]:
    return {"type": "prompt", "prompt": text}


def _edge(edge_id: str, destination: str, condition: str) -> dict[str, Any]:
    """An LLM-evaluated transition, used for genuine language judgement."""
    return {
        "id": edge_id,
        "transition_condition": _prompt_condition(condition),
        "destination_node_id": destination,
    }


def _eq_edge(edge_id: str, destination: str,
             equations: list[tuple[str, str, str]]) -> dict[str, Any]:
    """A deterministic transition over validated dynamic variables.

    Used wherever the decision must not be a judgement call — plate validity,
    vehicle ambiguity, whether a booking was written. This is the difference
    between "the model decided the plate was fine" and "the validator said so".
    """
    return {
        "id": edge_id,
        "transition_condition": {
            "type": "equation",
            "operator": "&&",
            "equations": [
                {"left": f"{{{{{left}}}}}", "operator": op, "right": right}
                for left, op, right in equations
            ],
        },
        "destination_node_id": destination,
    }


def _else_edge(edge_id: str, destination: str) -> dict[str, Any]:
    """The fallback that guarantees the flow cannot get stuck."""
    return {
        "id": edge_id,
        "transition_condition": _prompt_condition("Else"),
        "destination_node_id": destination,
    }


def _always_edge(edge_id: str, destination: str) -> dict[str, Any]:
    """A guaranteed transition — used to enforce that a step always runs."""
    return {
        "id": edge_id,
        "transition_condition": _prompt_condition("Always"),
        "destination_node_id": destination,
    }


def _conversation(node_id: str, name: str, text: str, *,
                  edges: list[dict[str, Any]] | None = None,
                  else_to: str | None = None,
                  always_to: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": node_id,
        "type": "conversation",
        "name": name,
        "instruction": {"type": "prompt", "text": text},
    }
    if edges:
        node["edges"] = edges
    if always_to:
        node["always_edge"] = _always_edge(f"{node_id}_always", always_to)
    if else_to:
        node["else_edge"] = _else_edge(f"{node_id}_else", else_to)
    return node


def _function(node_id: str, name: str, tool_name: str, instruction: str, *,
              edges: list[dict[str, Any]] | None = None,
              else_to: str | None = None,
              always_to: str | None = None,
              tool_type: str = "local") -> dict[str, Any]:
    """A function node: runs one tool deterministically on node entry.

    Contrast with a subagent node, where the LLM decides whether and when to
    call. Anything that *must* happen at a fixed point in the flow belongs here.

    Takes a tool *name* and resolves it through `tool_id()`, so a node can never
    reference an id the flow did not register.
    """
    node: dict[str, Any] = {
        "id": node_id,
        "type": "function",
        "name": name,
        "tool_id": tool_id(tool_name),
        "tool_type": tool_type,
        "wait_for_result": True,
        "instruction": {"type": "prompt", "text": instruction},
    }
    if edges:
        node["edges"] = edges
    if always_to:
        node["always_edge"] = _always_edge(f"{node_id}_always", always_to)
    if else_to:
        node["else_edge"] = _else_edge(f"{node_id}_else", else_to)
    return node


def _transfer(node_id: str, name: str, instruction: str,
              number: str = "{{escalation_number}}") -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "transfer_call",
        "name": name,
        "instruction": {"type": "static_text", "text": instruction},
        "speak_during_execution": True,
        "transfer_destination": {"type": "predefined", "number": number},
        "transfer_option": {"type": "cold_transfer"},
        "edge": _edge(f"{node_id}_failed", "escalation_fallback",
                      "Transfer failed"),
    }


def _end(node_id: str, name: str, text: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "end",
        "name": name,
        "speak_during_execution": True,
        "instruction": {"type": "static_text", "text": text},
    }


# --------------------------------------------------------------------------- #
# Flow construction
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# The front door
# --------------------------------------------------------------------------- #
#
# Consent and routing used to share one node, and that produced a reproducible
# bug. The caller answers "yes" to the recording question; that answer satisfies
# the node's goal; the transitions are then evaluated — and every one of them
# describes *intent*, which that turn does not contain. The node falls through
# to its else branch, and the caller lands somewhere they cannot book or rent
# from, however clearly they ask from then on.
#
# The lesson generalises: a node can only route on something the caller has
# already said. So consent becomes its own node answering a yes/no question, and
# intent is classified afterwards, once the caller has actually been told what
# is on offer.

#: The node that classifies caller intent, and fans out into the skills.
#:
#: Declared rather than inferred. The summariser in `snapshot.py` used to assume
#: the *start* node was the router, which held only while the greeting and the
#: intent question were the same node. Splitting consent out put a gate in front
#: of the router, and "the start node's destinations" silently became the
#: consent branch instead of the skills — degrading the summary rather than
#: failing loudly. Anything that needs the skills reads this.
ROUTER_NODE_ID = "services_intro"


def _services_intro() -> dict[str, Any]:
    """The menu, and the only place in the flow where intent is classified.

    Phrased as a short enumeration of what is available rather than an open "how
    can I help", because a caller cannot pick an option they have not been told
    about — and because every route out of here depends on them having said
    which one they want.
    """
    return _conversation(
        "services_intro", "What we can help with",
        "Briefly say what you can help with today, in one or two sentences: "
        "booking, moving or cancelling a service, MOT, repair or diagnostic "
        "appointment; arranging a loan or rental car; and questions about an "
        "invoice, a payment or a warranty. Then ask which of those they need, "
        "and stop talking so they can answer.",
        edges=[
            _edge("services_booking", "check_booking_capability",
                  "The caller wants to book, move or cancel a service, MOT, "
                  "repair or diagnostic appointment."),
            _edge("services_rental", "rental_intro",
                  "The caller's main request is about a rental car, loan car "
                  "or courtesy car."),
            _edge("services_billing", "billing_intro",
                  "The caller is asking about an invoice, a payment, a refund, "
                  "a warranty claim or has a complaint."),
            _edge("services_human", "flag_and_handoff",
                  "The caller asks for a human, or is clearly frustrated or "
                  "angry and does not want to continue with you."),
        ],
        else_to="fallback_help",
    )


def _front_door() -> list[dict[str, Any]]:
    """Consent first, inside the greeting, then the menu.

    One graph, every dealer. Whether consent is asked at all is a *branch on a
    dynamic variable*, not a different list of nodes, because forking the graph
    per dealer is what makes a flow unmaintainable at ten dealers. The shape is
    the same as `check_booking_capability`: dealer difference expressed as data,
    read by a deterministic branch.

    The branch is the **start node**, so the consent question is in the caller's
    very first turn rather than trailing a separate greeting. The branch itself
    is silent: it decides which opening the caller hears, and the chosen node
    does the talking.

    Note what the opening node does *not* do: classify intent. Asking for
    consent and judging what the caller wants in the same node meant a plain
    "yes" was matched against conditions describing bookings and rentals, fell
    through to the else branch, and left the caller somewhere they could no
    longer book or rent from. One turn, one question.
    """
    return [
        # Start node. Silent and deterministic: does this market require
        # recording consent? Read from a dynamic variable so one graph serves
        # a dealer that must ask and a dealer that must not.
        {
            "id": "consent_gate",
            "type": "branch",
            "name": "Is recording consent required?",
            "edges": [
                _eq_edge("consent_required", "greet_consent",
                         [("requires_recording_consent", "==", "true")]),
            ],
            "else_edge": _else_edge("consent_not_required", "greet_open"),
        },

        # Greeting and consent in a single turn, because that is how the caller
        # experiences it. The only question on the table is yes/no, so there is
        # nothing for the edges to misread.
        _conversation(
            "greet_consent", "Greet and ask to record",
            "This is the first thing the caller hears. In one short sentence, "
            "greet them as the {{dealer_name}} service department, and in the "
            "same breath ask their permission to record the call for quality "
            "and training. Then stop talking so they can answer. Do not ask "
            "what they need yet, and ask for no details.",
            edges=[
                _edge("consent_yes", "services_intro",
                      "The caller has clearly agreed to the call being recorded."),
                _edge("consent_no", "consent_refused",
                      "The caller has clearly refused, or does not agree, to the "
                      "call being recorded."),
            ],
            else_to="consent_retry",
        ),

        # The market that must not ask. Same graph, different opening — which is
        # the whole point of branching on the variable rather than writing a
        # second flow for dealers with different consent rules.
        _conversation(
            "greet_open", "Greet",
            "Greet the caller as the {{dealer_name}} service department in one "
            "short sentence, and stop talking so they can respond. Do not ask "
            "for any details yet.",
            always_to="services_intro",
        ),

        # The "no clear answer" case gets its own node rather than looping back
        # to `greet`: looping would replay the whole greeting on every unclear
        # turn, which on a bad line is a plausible infinite loop.
        _conversation(
            "consent_retry", "Ask about recording once more",
            "You did not get a clear yes or no about recording. Ask once more, "
            "plainly and briefly — you just need a yes or a no. Do not move on "
            "to anything else until you have one.",
            edges=[
                _edge("consent_retry_yes", "services_intro",
                      "The caller has now clearly agreed to the call being "
                      "recorded."),
                _edge("consent_retry_no", "consent_refused",
                      "The caller has now clearly refused, or still will not "
                      "agree after being asked a second time."),
            ],
            else_to="consent_refused",
        ),
        _conversation(
            "consent_refused", "Consent declined",
            "The caller does not agree to the call being recorded. Do not "
            "argue, and do not carry on as though they had agreed. Say you are "
            "not able to continue on this line without recording, apologise "
            "once, and offer to put them through to the service team or to "
            "have someone call them back.",
            edges=[
                _edge("refused_human", "flag_and_handoff",
                      "The caller wants a person, or wants to be called back."),
            ],
            else_to="wrap_up",
        ),
        _services_intro(),
    ]


def build_nodes(dealer: DealerProfile) -> list[dict[str, Any]]:
    max_attempts = dealer.max_identifier_attempts
    loan_car_line = (
        "A loan car can be arranged."
        if dealer.offers_loan_car
        else "This dealership does not offer loan cars — offer a transfer or "
             "collection instead."
    )

    nodes: list[dict[str, Any]] = [
        # ---------------------------- front door ---------------------------- #
        *_front_door(),

        # A general node instead of a dead end. Every flow needs an escape hatch.
        #
        # Note the route *back* to the menu. Without it this node is a trap: an
        # unrecognised request lands here, and the caller can no longer reach
        # booking or rental at all, however clearly they ask.
        _conversation(
            "fallback_help", "Handle anything else",
            "Answer the caller's question briefly using only the dealership "
            "information you were given. If you do not know, say you cannot check "
            "that and offer to put them through to the service team.",
            edges=[
                _edge("fallback_services", "services_intro",
                      "The caller's request turns out to be a booking, a rental "
                      "or a billing question after all."),
                _edge("fallback_human", "flag_and_handoff",
                      "The caller wants a person, or you could not answer their question."),
            ],
            else_to="wrap_up",
        ),

        # ------------------------- skill: booking --------------------------- #
        # Capability gate. A branch node reads the dealer's declared
        # capabilities *before* the booking skill runs, so a dealer whose DMS is
        # read-only is handled honestly up front instead of at the point of
        # writing — after the caller has given up five minutes of their day.
        #
        # This is the capability matrix used as a routing decision rather than a
        # runtime failure, and it is why one flow can serve dealers whose systems
        # can do quite different things.
        {
            "id": "check_booking_capability",
            "type": "branch",
            "name": "Can this dealer book?",
            "edges": [
                _eq_edge("can_book", "capture_plate",
                         [("dealer_can_book", "==", "true")]),
            ],
            "else_edge": _else_edge("cannot_book", "booking_unavailable"),
        },

        _conversation(
            "booking_unavailable", "Booking not supported",
            "Say honestly that you cannot put a booking directly into this "
            "dealer's system, and that you do not want to promise something you "
            "cannot keep. Offer to take the details down and have the service "
            "team call back, or to put the caller through now. Do not take the "
            "booking as though it were confirmed.",
            edges=[
                _edge("unavailable_transfer", "flag_and_handoff",
                      "The caller wants a person now, or you have finished taking "
                      "the callback details."),
            ],
            else_to="flag_and_handoff",
        ),

        # Stage 1 — capture. The instruction tells the model how to *hear* a
        # plate, but not how to judge one; judging is code's job.
        _conversation(
            "capture_plate", "Capture registration",
            "Ask for the registration plate of the vehicle. If they start to give "
            "a long description, gently steer back to the plate. If they sound "
            "unsure, ask them to spell it out, letter by letter and number by "
            "number, using words for the letters if that helps. Do not repeat the "
            "plate back yet and do not comment on whether it looks right. Once you "
            "have it, stop talking.",
            always_to="validate_plate",
        ),

        # Stage 2 — validate. Deterministic, and guaranteed to run.
        _function(
            "validate_plate", "Validate registration", VALIDATE_IDENTIFIER,
            "Clicking sound only — say nothing. Report the exact result to no one; "
            "the next step decides what the caller hears. When you call this, "
            "pass the registration exactly as the caller just said or spelled it "
            "as the value — do not tidy it up or correct it first.",
            edges=[
                _eq_edge("validate_ok", "readback_plate",
                         [("identifier_valid", "==", "true")]),
            ],
            else_to="plate_repair",
        ),

        # Stage 3 — repair / targeted re-capture. Note this re-runs validation
        # rather than re-asking the whole question, and escalates after a bounded
        # number of attempts instead of looping forever.
        _conversation(
            "plate_repair", "Re-capture registration",
            "The plate did not pass validation. Do not tell the caller 'invalid'. "
            "If the validation result listed uncertain positions, ask only about "
            "those characters, one at a time, using words for letters "
            "(for example: 'was that C for Charlie or D for Delta?'). "
            "Otherwise ask them to spell the whole plate again, slowly, letter by "
            "letter and number by number. "
            f"If you have already tried this {max_attempts} times and it still is "
            "not working, stop trying and offer to put them through to the service "
            "team or send a text link so they can type the plate instead.",
            edges=[
                _edge("repair_retry", "validate_plate",
                      "The caller has just given you a registration plate to try."),
                _edge("repair_give_up", "flag_and_handoff",
                      f"You have already attempted this {max_attempts} times, or "
                      "the caller is becoming frustrated, or they have asked for a "
                      "person or for a text link instead."),
            ],
            else_to="flag_and_handoff",
        ),

        # Stage 4 — read back and confirm. The highest-value control in the
        # pipeline: it moves the check to the party who knows the answer.
        _conversation(
            "readback_plate", "Read back registration",
            "Read the validated plate back to the caller slowly and "
            "phonetically, exactly as given in {{identifier_readback}}. Then ask "
            "them to confirm it is correct. Do not accept an unsure answer, a "
            "sigh, or a hesitant 'I think so' as confirmation — ask again "
            "clearly. Never claim the plate is correct; only ask whether you "
            "heard it correctly.",
            edges=[
                _edge("readback_ok", "lookup_vehicle",
                      "The caller has clearly and unambiguously confirmed the "
                      "registration plate you read back is correct."),
                _edge("readback_wrong", "plate_repair",
                      "The caller says the plate is wrong, changes a character, "
                      "or sounds uncertain rather than confirming clearly."),
            ],
            else_to="plate_repair",
        ),

        # Stage 5 — reconcile against the system of record. A format mask can
        # never prove a plate is *correct*; only the record can.
        _function(
            "lookup_vehicle", "Look up vehicle", LOOKUP_VEHICLE,
            "Say you are just pulling up their vehicle.",
            edges=[
                _eq_edge("lookup_single", "capture_service",
                         [("vehicle_found", "==", "true"),
                          ("vehicle_ambiguous", "==", "false")]),
                _eq_edge("lookup_ambiguous", "disambiguate_vehicle",
                         [("vehicle_ambiguous", "==", "true")]),
            ],
            else_to="vehicle_not_found",
        ),

        # Two real plates can both match. Ask; never pick.
        _conversation(
            "disambiguate_vehicle", "Disambiguate vehicle",
            "More than one vehicle is registered to that plate. Read the caller "
            "the options in {{vehicle_options}} and ask which one they mean. "
            "Offer at most three, and if they cannot tell, offer to transfer.",
            edges=[
                _edge("disambig_ok", "capture_service",
                      "The caller has chosen one of the vehicles you listed."),
            ],
            else_to="flag_and_handoff",
        ),

        _conversation(
            "vehicle_not_found", "Plate not on record",
            "That plate is not on the dealer's records. Tell the caller you could "
            "not find it, and check once whether you heard it correctly by reading "
            "{{identifier_normalised}} back. Offer to try a different plate, to "
            "continue as a new customer, or to transfer.",
            edges=[
                _edge("notfound_retry", "plate_repair",
                      "The caller wants to give the registration plate again."),
                _edge("notfound_continue", "capture_service",
                      "The caller says it is a new vehicle or a new customer and "
                      "wants to go ahead with a booking anyway."),
                _edge("notfound_transfer", "flag_and_handoff",
                      "The caller wants a person, or the plate is not on record "
                      "and they cannot proceed."),
            ],
            else_to="flag_and_handoff",
        ),

        _conversation(
            "capture_service", "Capture service need",
            "You are booking for {{vehicle_summary}}. Ask what the vehicle is in "
            "for, and whether any warning light is showing. Match it to one of "
            "these services where you can: {{services_list}}. Ask once whether the "
            "caller will wait on site, needs a loan car, or wants the car collected. "
            + loan_car_line + " Do not quote a price — say the team will confirm "
              "the cost. Then move on.",
            always_to="capture_timing",
        ),

        # The date. `capture_timing` is the one node that cannot work without
        # knowing what day it is — "sometime next week" is not a range until you
        # know which week you are in. Retell supplies `{{current_calendar}}` as a
        # built-in: fourteen dated days in `system_timezone`, with today marked.
        # It is deliberately *here* rather than in the global prompt: it is ~150
        # tokens of calendar, and paying that on every turn of every call to
        # serve one node is exactly the prompt bloat this flow exists to avoid.
        # Retell names the day of the week, so the model never has to infer it.
        _conversation(
            "capture_timing", "Capture preferred timing",
            "Here is the next two weeks, with today marked:\n"
            "{{current_calendar}}\n"
            "Ask what day suits them and whether they prefer morning or "
            "afternoon. Resolve what they say against that calendar — if they "
            "say 'next week' or 'Tuesday', work out the actual dates from the "
            "list above and say the dates back to them. Never calculate a date "
            "yourself and never guess the day of the week. Turn it into an ISO "
            "date range and do not confirm anything yet; tell them what you are "
            "checking.",
            always_to="check_availability",
        ),

        # Stage 6 — availability is a fact, not a generation.
        _function(
            "check_availability", "Check availability", CHECK_AVAILABILITY,
            "Say you are checking the diary.",
            edges=[
                _eq_edge("slots_ok", "offer_slots", [("slots_available", ">", "0")]),
            ],
            else_to="no_slots",
        ),

        _conversation(
            "no_slots", "No slots in range",
            "Nothing is free in that range. Say so, then offer to look at the "
            "following week, or to take their number and have the service team "
            "call them back. Never invent a slot.",
            edges=[
                _edge("noslots_retry", "check_availability",
                      "The caller has given a different date range to check."),
                _edge("noslots_callback", "flag_and_handoff",
                      "The caller wants a callback or a person instead."),
            ],
            else_to="flag_and_handoff",
        ),

        _conversation(
            "offer_slots", "Offer slots",
            "Offer at most three of the times in {{slots}}, reading them as a "
            "human would ('Tuesday the 14th at half past eight in the morning'). "
            "Ask which suits best. Only offer times that appear in {{slots}}.",
            edges=[
                _edge("slot_chosen", "confirm_booking",
                      "The caller has chosen one of the times you offered."),
            ],
            else_to="capture_timing",
        ),

        # Stage 7 — the confirmation gate. Nothing irreversible before this.
        _conversation(
            "confirm_booking", "Confirm everything",
            "Read the whole booking back in one short pass: the vehicle, the "
            "service, the chosen day and time, and the transport arrangement. Then "
            "ask a clear yes/no question. Do not proceed on a hesitant or partial "
            "answer — ask them to confirm the specific detail you are unsure about.",
            edges=[
                _edge("confirm_yes", "create_appointment",
                      "The caller has clearly and unambiguously confirmed every "
                      "detail you read back, with no changes."),
                _edge("confirm_change", "capture_service",
                      "The caller wants to change the service, the vehicle or the "
                      "transport arrangement."),
                _edge("confirm_retime", "capture_timing",
                      "The caller wants a different day or time."),
            ],
            else_to="capture_timing",
        ),

        # Stage 8 — commit. Idempotent, retried, and gated on the caller's
        # confirmation rather than on the model's judgement.
        #
        # Note the three-way split. Booking is not binary success/failure when a
        # vendor writes asynchronously: CDK Drive's Async Service Appointment API
        # returns 202 Accepted and the appointment does not exist yet. Treating
        # that as success is how an agent confirms a booking that never happened,
        # so the flow has a distinct middle state and the caller hears the truth.
        _function(
            "create_appointment", "Create appointment", CREATE_APPOINTMENT,
            "Say you are putting it in the diary now. Pass the service being "
            "booked, the exact start time the caller chose as an ISO date-time, "
            "and how they will get moving while the car is in.",
            edges=[
                _eq_edge("create_ok", "verify_write",
                         [("booking_confirmed", "==", "true")]),
                _eq_edge("create_pending", "booking_pending",
                         [("booking_pending", "==", "true")]),
            ],
            else_to="booking_failed",
        ),

        # The async middle state. The write was accepted; it is not committed.
        # The caller is told exactly that, and a flag carries the follow-up, so
        # "we'll confirm" is a measurable commitment rather than a soft promise.
        _conversation(
            "booking_pending", "Booking pending confirmation",
            "Tell the caller you have put the request in, but the dealer's system "
            "confirms these in the background, so it is not guaranteed yet. Do not "
            "say 'booked' or 'confirmed'. Say a member of the service team will text "
            "them to confirm the time shortly, and give them the reference if one "
            "was returned. If they need certainty now, offer to put them through.",
            edges=[
                _edge("pending_transfer", "flag_and_handoff",
                      "The caller wants a person, or wants the booking confirmed now."),
            ],
            else_to="wrap_up",
        ),

        # Stage 9 — verify the write. "The tool returned success" is not
        # evidence; reading it back out of the DMS is.
        _function(
            "verify_write", "Verify the write", VERIFY_APPOINTMENT,
            "Say nothing while this runs.",
            edges=[
                _eq_edge("verify_ok", "booking_done",
                         [("write_verified", "==", "true")]),
            ],
            else_to="booking_failed",
        ),

        _conversation(
            "booking_done", "Confirm to caller",
            "Tell the caller the booking is confirmed, give them the reference "
            "{{booking_reference}}, and repeat the day and time once. Say a "
            "confirmation text will follow. Then ask if there is anything else.",
            edges=[
                _edge("done_rental", "rental_intro",
                      "The caller now wants a rental or loan car arranged."),
                _edge("done_else", "satisfaction",
                      "The caller has nothing else and is ready to end."),
            ],
            else_to="satisfaction",
        ),

        _conversation(
            "booking_failed", "Booking failed",
            "Apologise plainly: you could not complete the booking. Do not guess "
            "why and do not promise it will work later. Say you will pass this to "
            "the service team now.",
            always_to="flag_and_handoff",
        ),

        # -------------------------- skill: rental --------------------------- #
        _conversation(
            "rental_intro", "Rental request",
            "Explain briefly whether the dealer offers a loan car, and that a "
            "rental needs the driver's licence and a card at collection. "
            + loan_car_line + " Ask for the dates they need it and what size of "
              "vehicle would suit. Do not quote a price or confirm availability.",
            edges=[
                _edge("rental_wants", "rental_capture",
                      "The caller wants to go ahead and has told you the dates "
                      "they need the vehicle."),
                _edge("rental_no", "wrap_up",
                      "The caller does not want a rental after all."),
            ],
            else_to="wrap_up",
        ),

        _conversation(
            "rental_capture", "Capture rental details",
            "Collect the rental dates, the vehicle size they want, the driver's "
            "age, and confirm the licence will be presented at collection. Do not "
            "take card details. Repeat the dates back once.",
            always_to="rental_log",
        ),

        # A rental agreement can't be completed by voice, and pretending
        # otherwise is how agents create angry customers. Capture, then hand on.
        _function(
            "rental_log", "Log rental request", FLAG_CONVERSATION,
            "Say you are passing the rental request to the rental desk. When you "
            "call this tool, pass reason 'rental_request' and put the dates, the "
            "vehicle size and the driver's age in detail, so the rental desk has "
            "everything without calling the customer back.",
            always_to="rental_done",
        ),

        _conversation(
            "rental_done", "Rental handed over",
            "Tell the caller the rental desk will confirm the vehicle and the "
            "price, and that nothing is confirmed until they do. Ask if there is "
            "anything else.",
            edges=[
                _edge("rentaldone_end", "satisfaction",
                      "The caller has nothing else."),
            ],
            else_to="satisfaction",
        ),

        # --------------------------- skill: rating -------------------------- #
        # The rating is asked only on a path that actually succeeded, and this is
        # enforced by structure rather than by instruction: the only edges into
        # `satisfaction` come from `booking_done` and `rental_done`. Asking
        # someone to rate a call that failed is worse than not asking at all, and
        # a prompt could not be trusted to hold that line.
        _conversation(
            "satisfaction", "Ask for a rating",
            "One quick question before you go. Ask the caller to rate the "
            "service today, out of five if they want a scale, and whether there "
            "is anything they would like to add or pass on to the team. Keep it "
            "to one or two sentences, and accept a refusal without pressing — "
            "one ask, then move on.",
            edges=[
                _edge("satisfaction_more", "fallback_help",
                      "The caller raises something further they need help with, "
                      "or something they want fixed."),
            ],
            else_to="feedback_log",
        ),

        # Silent, and one-way. Waiting for the result means a failure to record
        # surfaces as a retry rather than a mid-sentence cut-off, and the
        # instruction forbids reading the outcome back — the caller has already
        # been thanked and should not be thanked twice.
        _function(
            "feedback_log", "Log satisfaction", RECORD_FEEDBACK,
            "Say nothing while this runs — do not read the result out or thank "
            "the caller again. When you call this tool, pass the caller's rating "
            "as they gave it, and anything they wanted to add as the comment, in "
            "their own words. If they declined to rate, pass what they said. "
            "Never invent or round a rating.",
            always_to="wrap_up",
        ),

        # -------------------------- skill: billing -------------------------- #
        # Payment is never taken over voice here. This is a policy decision, not
        # a technical limitation, and saying so plainly reads as competence.
        _conversation(
            "billing_intro", "Invoices and warranty",
            "You cannot take payments or change an invoice. If the caller has an "
            "outstanding balance, say a balance is showing on the account but do "
            "not read an amount from memory — only if a tool gave you one. If they "
            "mention a warranty claim, say the service team must assess it. "
            "Apologise once for the trouble, then offer to put them through.",
            always_to="flag_and_handoff",
        ),

        # ---------------------- guardrails and endings ---------------------- #
        _function(
            "flag_and_handoff", "Flag for review", FLAG_CONVERSATION,
            "Say you are passing this to the service team now. When you call "
            "this tool, always pass a reason from its list and a one-line detail "
            "saying why the call is being passed on.",
            always_to="handoff",
        ),

        _transfer(
            "handoff", "Warm handoff",
            "One moment, I'm putting you through to the service team now.",
        ),

        # Reached only if a cold transfer fails. The flow must not dead-end.
        _conversation(
            "escalation_fallback", "Transfer failed",
            "The transfer did not go through. Apologise, and take the caller's "
            "number and a one-line summary so the team can call them back. Read "
            "the number back to confirm it before ending.",
            else_to="wrap_up",
        ),

        _conversation(
            "wrap_up", "Wrap up",
            "Ask if there is anything else you can help with. If they are done, "
            "say goodbye and mention the dealership name once.",
            edges=[
                _edge("wrapup_more", "fallback_help",
                      "The caller has another question."),
            ],
            else_to="end_call",
        ),

        _end("end_call", "End call",
             "Thanks for calling {{dealer_name}}. Goodbye."),
    ]

    return nodes


def build_flow_payload(dealer: DealerProfile, tools_base_url: str,
                       shared_secret: str = "",
                       model: str = "gpt-4.1",
                       modular: bool = False,
                       component_ids: dict[str, str] | None = None) -> dict[str, Any]:
    """Build the full `create-conversation-flow` payload.

    Note what is *not* per-dealer: the node graph. Dealer differences live in
    `default_dynamic_variables` and in the branches that read the capability
    matrix. That is the whole scaling argument.

    `modular` switches to the subflow decomposition: the skills become components
    and the flow becomes a thin orchestrator that routes between them. It is a
    flag rather than a replacement so the two shapes can be compared on real
    calls, and so a market can be migrated one at a time.
    """
    tools: list[dict[str, Any]] = []
    if tools_base_url:
        tools = build_tools(tools_base_url, shared_secret)
        # Flow-level tools need an explicit tool_id for function nodes to target.
        # Both sides go through `tool_id()` so they cannot drift apart.
        for tool in tools:
            tool["tool_id"] = tool_id(tool["name"])

    payload: dict[str, Any] = {
        "model_choice": {"model": model, "type": "cascading"},
        "model_temperature": 0.2,
        "start_speaker": "agent",
        "start_node_id": "consent_gate",
        "global_prompt": GLOBAL_PROMPT,
        "tools": tools,
        "default_dynamic_variables": dealer_dynamic_variables(dealer),
        "nodes": build_nodes(dealer),
    }

    if not modular:
        return payload

    from . import components as subflows

    ids = dict(component_ids or {})
    missing = {s.name for s in subflows.SUBFLOWS if s.scope == "shared"} - set(ids)
    if missing:
        raise ValueError(
            f"modular flow needs shared component ids for {sorted(missing)}. "
            f"Deploy them first (`scripts/deploy_subflows.py`), then pass their "
            f"ids — a flow that references an id which does not exist is "
            f"rejected at deploy time."
        )

    # Shared components live in the library and are deployed separately; only
    # their ids are needed here. Local ones are embedded, because they are
    # market-specific and must not sync across flows, and their ids are derived
    # from their names so a redeploy updates rather than re-creates them.
    local = [s for s in subflows.SUBFLOWS if s.scope == "local"]
    payload["components"] = [
        subflows.build_subflow(spec, payload["nodes"], tools_base_url)
        for spec in local
    ]
    for spec in local:
        ids[spec.name] = subflows.local_component_id(spec.name)

    # Every tool now lives in the component that calls it, so a flow-level list
    # would be a second declaration of the same names — and tool names must be
    # unique across everything available to the model.
    payload["tools"] = []
    payload["nodes"] = subflows.build_orchestrator(payload["nodes"], ids)
    return payload


# --------------------------------------------------------------------------- #
# Evidence: the prompt budget
# --------------------------------------------------------------------------- #

def estimate_prompt_tokens(payload: dict[str, Any]) -> dict[str, Any]:
    """Rough token accounting: monolith vs this flow.

    A token is ~4 characters of English. The comparison is deliberately crude —
    it is an order-of-magnitude argument, not a benchmark — but it shows the
    structural claim: no single node carries the whole graph in its context.

    The real win isn't the smaller number, it's that **no node needs to know
    about any other node**. That's what stops instruction drift and forgetting.
    """
    global_tokens = len(payload.get("global_prompt", "")) // 4
    per_node = []
    for node in payload["nodes"]:
        instruction = node.get("instruction") or {}
        text = instruction.get("text", "") or ""
        per_node.append({
            "node": node["id"],
            "type": node["type"],
            "prompt_tokens": len(text) // 4,
        })
    node_tokens = [n["prompt_tokens"] for n in per_node]
    worst = max(per_node, key=lambda n: n["prompt_tokens"]) if per_node else None
    return {
        "global_prompt_tokens": global_tokens,
        "node_count": len(payload["nodes"]),
        "max_node_prompt_tokens": worst["prompt_tokens"] if worst else 0,
        "max_node": worst["node"] if worst else None,
        "mean_node_prompt_tokens": round(sum(node_tokens) / len(node_tokens), 1)
        if node_tokens else 0,
        "worst_case_turn_context_tokens": (
            global_tokens + (worst["prompt_tokens"] if worst else 0)
        ),
        "monolith_baseline": {
            "nodes": 190,
            "system_prompt_tokens": 14_000,
        },
        "per_node": per_node,
    }
