"""The client-facing story — what actually happens when somebody rings.

This is the one projection that is **authored rather than derived**, and it is
worth being explicit about why, because it breaks the rule the rest of the tool
follows.

`snapshot.py` can compute edge semantics, determinism and skill clusters because
those are properties *of* the graph. A plain-language story is not a property of
the graph: "Readback plate" -> "the agent asks you to confirm what it heard" is
an act of translation, and any attempt to generate it from node ids produces
word salad that reads like a machine wrote it. A dealer principal opening this
tab is deciding whether to trust the agent with their customers; that is the
wrong place to be clever.

So the narrative is written by hand. But an authored layer is a fourth thing
that can drift, and this repo already has one rule about that — from
`annotations.py`: an overlay is fine, an overlay that can *silently* disagree
with the graph is not.

Hence the dereference. Every step declares the nodes that implement it, and
`build_journey()` checks those against the graph it is given:

* A declared node that no longer exists is reported as `missing`, and a test
  fails. Delete `readback_plate` in the dashboard and the client story does not
  quietly keep promising a read-back — it goes red.
* A graph node no step mentions is reported as `uncovered`. Add a node and the
  story is visibly incomplete rather than invisibly so.

That is the whole trick: authored for the reader, verified against the machine.
"""

from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------- #
# The story
# --------------------------------------------------------------------------- #
#
# `path` is the honest part most storyboards leave out. Not every call goes
# through every step — a caller with a billing question never sees the booking
# spine — so each step says whether it is on every call or only on some.
#
#   always      every conversation passes through this
#   if_relevant this branch is taken only when the caller's need matches
#
# `you_might_hear` is a representative line, not the prompt text. It exists so a
# non-technical reader can hear the tone rather than read a description of it.

JOURNEY: list[dict[str, Any]] = [
    {
        "id": "answered",
        "title": "The phone is answered, and nothing dead-ends",
        "path": "always",
        "nodes": ["consent_gate", "greet_consent", "greet_open", "services_intro",
                  "fallback_help"],
        "what_happens": (
            "The call is picked up straight away. The agent says what it can "
            "help with — booking a service, MOT, repair or diagnostic; "
            "arranging a loan or rental car; and questions about an invoice, "
            "payment or warranty — and lets the caller choose. If the request "
            "is something it does not recognise, it still helps, and it can "
            "always come back to those options rather than getting stuck. "
            "There is no path through this call that simply stops."
        ),
        "human": False,
    },
    {
        "id": "consent",
        "title": "It asks before it starts, and accepts no for an answer",
        "path": "if_relevant",
        "nodes": ["consent_gate", "greet_consent", "consent_retry", "consent_refused"],
        "what_happens": (
            "Where the dealership or the market requires it, the very first "
            "thing the caller hears is a greeting that asks permission to "
            "record — one turn, not a greeting followed by an interruption. The "
            "agent waits for a clear answer. If it is unclear it asks once more, "
            "rather than treating silence as agreement. If the caller says no, "
            "it does not argue and does not carry on regardless — it explains "
            "and offers a person instead. Where recording consent is not "
            "required, the caller simply hears the greeting and is never asked."
        ),
        "human": False,
    },
    {
        "id": "capability",
        "title": "It checks what this dealership is allowed to do",
        "path": "always",
        "nodes": ["check_booking_capability", "booking_unavailable"],
        "what_happens": (
            "Before any booking is taken, the agent checks whether this "
            "dealership's system accepts bookings. If it does not, the caller "
            "is told that plainly at the start and a message is taken — rather "
            "than being promised an appointment that could never be written."
        ),
        "human": False,
    },
    {
        "id": "the_vehicle",
        "title": "Working out which vehicle it is",
        "path": "if_relevant",
        "nodes": [
            "capture_plate", "validate_plate", "plate_repair", "readback_plate",
            "lookup_vehicle", "disambiguate_vehicle", "vehicle_not_found",
        ],
        "what_happens": (
            "The caller gives their registration. The agent never repeats it "
            "back as fact: it checks the characters are even possible, asks the "
            "caller to confirm what it heard, and only then looks the vehicle "
            "up in the dealership's records. If two vehicles match, it asks "
            "which one rather than choosing. If nothing matches, it says so "
            "instead of guessing at a similar-looking plate."
        ),
        "you_might_hear": "Let me read that back to you — have I got that right?",
        "human": False,
    },
    {
        "id": "the_problem",
        "title": "What is wrong, and when suits",
        "path": "if_relevant",
        "nodes": ["capture_service", "capture_timing"],
        "what_happens": (
            "The caller describes the problem and when they could come in. The "
            "agent writes down what it is told. It does not diagnose the fault, "
            "and it does not quote a price for the work."
        ),
        "human": False,
    },
    {
        "id": "the_diary",
        "title": "Looking at the real diary",
        "path": "if_relevant",
        "nodes": ["check_availability", "no_slots", "offer_slots"],
        "what_happens": (
            "Appointment times are read from the dealership's own diary. The "
            "agent can only offer times that came back from it. If nothing "
            "suitable is free, it offers the following week or a callback — it "
            "does not make a time up."
        ),
        "human": False,
    },
    {
        "id": "commit",
        "title": "Confirming before anything is booked",
        "path": "if_relevant",
        "nodes": [
            "confirm_booking", "create_appointment", "booking_pending",
            "verify_write", "booking_done", "booking_failed",
        ],        "what_happens": (
            "Nothing is written until the caller has clearly said yes — a "
            "hesitant answer is not treated as one. The booking is then written, "
            "read back out of the dealership's system to prove it exists, and "
            "only then confirmed to the caller. Some systems accept a booking "
            "without committing to it immediately; when that happens the caller "
            "is told it is not confirmed yet and that somebody will text to "
            "confirm. If the write fails, the caller is told plainly and a "
            "person picks it up."
        ),
        "you_might_hear": "That is in the diary — I will text you to confirm.",
        "human": False,
    },
    {
        "id": "rental",
        "title": "Rentals and courtesy cars",
        "path": "if_relevant",
        "nodes": ["rental_intro", "rental_capture", "rental_log", "rental_done"],
        "what_happens": (
            "A rental cannot be completed over the phone — it needs a licence "
            "and card check in person. The agent explains that, takes the "
            "request, passes it to the rental desk, and says plainly that "
            "nothing is confirmed until the desk comes back. It never takes card "
            "details on the call."
        ),
        "human": True,
    },
    {
        "id": "billing",
        "title": "Questions about bills and warranty",
        "path": "if_relevant",
        "nodes": ["billing_intro"],
        "what_happens": (
            "Payment is never taken on the call, and no balance is invented. The "
            "agent only repeats what a dealership system actually reported."
        ),
        "human": False,
    },
    {
        "id": "a_person",
        "title": "When a person needs to step in",
        "path": "if_relevant",
        "nodes": ["flag_and_handoff", "handoff", "escalation_fallback"],
        "what_happens": (
            "Anything the agent cannot resolve is passed to a person, with a "
            "note left for the team so nobody has to ask what the call was "
            "about. If the transfer itself fails, the agent does not go quiet — "
            "it offers a callback instead."
        ),
        "human": True,
    },
    {
        "id": "ending",
        "title": "Ending the call, and asking how it went",
        "path": "always",
        "nodes": ["satisfaction", "feedback_log", "wrap_up", "end_call"],
        "what_happens": (
            "Every route through the conversation finishes the same polite way, "
            "so no caller is left hanging on the line. After a call that went "
            "well, the agent asks one quick question before saying goodbye: how "
            "would they rate the service, and is there anything they would like "
            "to add. The answer is recorded, along with anything they said in "
            "their own words. Nobody who has just been told we cannot help them "
            "is asked to score us — the question only follows a successful "
            "outcome, and a caller who would rather not answer is not pressed."
        ),
        "you_might_hear": "One quick thing before you go — how would you rate us today?",
        "human": False,
    },
]


# The client-facing version of the guardrails: not "what could go wrong" but
# "what it will never do". Same facts as the technical view's failure modes,
# stated as a promise rather than a defect, because that is the question a
# dealership actually asks.
#
# `enforced_by` is the dereference, exactly as on the steps — a guarantee with
# no surviving node behind it is a marketing claim, and the test says so.
GUARANTEES: list[dict[str, Any]] = [
    {
        "id": "no_invented_registration",
        "plain": "It will not invent a registration or accept a misheard one "
                 "without checking.",
        "enforced_by": ["validate_plate", "readback_plate"],
    },
    {
        "id": "no_chosen_vehicle",
        "plain": "It will not pick a vehicle for you if two match — it asks.",
        "enforced_by": ["disambiguate_vehicle"],
    },
    {
        "id": "no_invented_time",
        "plain": "It cannot make up an appointment time. Times come from the "
                 "dealership diary or they do not exist.",
        "enforced_by": ["check_availability", "offer_slots", "no_slots"],
    },
    {
        "id": "no_quoted_price",
        "plain": "It will not quote a price for work.",
        "enforced_by": ["capture_service"],
    },
    {
        "id": "no_unconfirmed_booking",
        "plain": "It will not book anything without a clear yes from the caller.",
        "enforced_by": ["confirm_booking"],
    },
    {
        "id": "no_claimed_success",
        "plain": "It will not say a booking is confirmed until the dealership's "
                 "system has been read back — and if the system has only "
                 "accepted it, it says exactly that.",
        "enforced_by": ["verify_write", "booking_done", "booking_pending"],
    },
    {
        "id": "no_card_details",
        "plain": "It will not take card or payment details over the phone.",
        "enforced_by": ["rental_capture", "billing_intro"],
    },
    {
        "id": "no_dead_end",
        "plain": "It will not leave you with nothing — every failure ends in a "
                 "person or a message.",
        "enforced_by": ["flag_and_handoff", "escalation_fallback",
                        "booking_failed", "vehicle_not_found"],
    },
]


# --------------------------------------------------------------------------- #
# Dereference
# --------------------------------------------------------------------------- #

def _resolve(declared: list[str], known: set[str]) -> tuple[list[str], list[str]]:
    """Split declared node ids into the ones that exist and the ones that don't."""
    present = [node_id for node_id in declared if node_id in known]
    missing = [node_id for node_id in declared if node_id not in known]
    return present, missing


def build_journey(graph: dict[str, Any] | None,
                  node_annotations: dict[str, dict[str, Any]] | None = None,
                  ) -> dict[str, Any]:
    """The story, checked against the graph that is actually deployed.

    Never raises on a mismatch. A step whose nodes have all been deleted still
    renders — it just carries `missing`, and the view shows it as out of date.
    Silently dropping the step would be the failure mode this whole module
    exists to prevent: a client story that reads perfectly and describes
    software that is no longer running.

    `node_annotations` is the same overlay the technical view renders, passed in
    rather than re-derived, so the guardrail stages shown beside a client step
    are the ones the technical tab shows beside the node.
    """
    graph = graph or {}
    overlay = node_annotations or {}
    known = {node["id"] for node in graph.get("nodes") or []}

    steps: list[dict[str, Any]] = []
    referenced: set[str] = set()
    missing_nodes: list[str] = []

    for order, step in enumerate(JOURNEY, start=1):
        present, missing = _resolve(step["nodes"], known)
        referenced.update(present)
        missing_nodes.extend(missing)
        steps.append({
            "id": step["id"],
            "order": order,
            "title": step["title"],
            "path": step["path"],
            "what_happens": step["what_happens"],
            "you_might_hear": step.get("you_might_hear"),
            "human": step["human"],
            "node_count": len(present),
            # Where the story and the flow are allowed to be read side by side.
            "nodes": present,
            "missing": missing,
            "stale": bool(missing),
            # The guardrail stages this step actually covers, taken from the
            # overlay so the client tab and the technical tab cannot disagree
            # about where the controls are.
            "stages": sorted({
                (overlay.get(node_id) or {}).get("guardrail_stage") or ""
                for node_id in present
            } - {""}),
        })

    guarantees = []
    for guarantee in GUARANTEES:
        present, missing = _resolve(guarantee["enforced_by"], known)
        missing_nodes.extend(missing)
        guarantees.append({
            "id": guarantee["id"],
            "plain": guarantee["plain"],
            "enforced_by": present,
            "missing": missing,
            "stale": bool(missing),
        })

    uncovered = sorted(known - referenced)

    return {
        "steps": steps,
        "step_count": len(steps),
        "guarantees": guarantees,
        "coverage": {
            "covered": len(referenced),
            "total": len(known),
            # A node the story does not mention. Not necessarily wrong, but it
            # has to be visible: it means the story is incomplete.
            "uncovered_nodes": uncovered,
        },
        "drift": {
            "missing_nodes": sorted(set(missing_nodes)),
            "uncovered_nodes": uncovered,
            "in_sync": not missing_nodes and not uncovered,
        },
    }
