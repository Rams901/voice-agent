"""The curated "why" layer — the one thing Retell cannot tell us.

The Retell canvas knows the *shape* of the graph. It does not know, and cannot
know, why `readback_plate` exists, which failure mode each node is defending
against, or which guardrail stage a node belongs to. That intent lives in the
heads of whoever built the flow and, if you are lucky, in comments.

So it is data here, keyed by node id, sitting alongside the machine-derived
graph. Two rules keep this honest:

* It is an **overlay, never a source of truth.** A node that is not listed still
  renders; it just gets a derived description instead of a curated one. Nothing
  in the tool breaks when somebody adds a node in the dashboard.
* It is **small and factual.** It restates the reasoning already written down in
  `flow.py`, so the diagram and the code cannot quietly disagree about intent.

The nice property: editing a node in Retell shows up immediately in the diagram
(graph), while editing *why* it exists is a one-line change here. The two evolve
independently and neither can invalidate the other.
"""

from __future__ import annotations

from typing import Any

# The eight controls from the README's plate analysis, in the order they fire.
# Exposed so a view can render the pipeline without hard-coding it.
GUARDRAIL_STAGES = [
    "Constrain", "Capture", "Read back", "Validate",
    "Confirm", "Reconcile", "Dispose", "Verify",
]

NODE_ANNOTATIONS: dict[str, dict[str, Any]] = {
    # ------------------------------ front door ------------------------------ #
    "greet_consent": {
        "why": "The caller's first turn, and it carries the recording question "
               "in the same breath as the greeting — because asking separately "
               "makes the consent request feel like a gate rather than a "
               "courtesy. The node routes only on that yes/no answer, never on "
               "intent: the caller has not been told what is on offer yet.",
        "failure_mode": "Consent read as intent. A caller who says 'yes' has "
                        "that yes judged against conditions describing bookings "
                        "and rentals, matches none of them, and falls through "
                        "the else branch — stranded somewhere they can no longer "
                        "book or rent from.",
        "guardrail_stage": None,
    },
    "consent_gate": {
        "why": "The start node, and silent: it decides which opening the caller "
               "hears. Deterministic, and a branch rather than a different node "
               "list, so every dealer still shares one graph.",
        "failure_mode": "Forking the flow per market, which is what makes a "
                        "flow unmaintainable at ten dealers.",
        "guardrail_stage": None,
    },
    "greet_open": {
        "why": "The opening for a market that does not require consent. Same "
               "graph, different first turn, chosen by a data-driven branch "
               "instead of a second flow.",
        "failure_mode": "Asking consent where it must not be asked, or forking "
                        "the whole flow per market to express one variable.",
        "guardrail_stage": None,
    },
    "consent_retry": {
        "why": "One more attempt at a clear yes or no, as its own node rather "
               "than looping back through the greeting — which on a bad line "
               "re-reads the whole opening every turn.",
        "failure_mode": "An unclear answer treated as consent, or an unbounded "
                        "re-greeting loop.",
        "guardrail_stage": None,
    },
    "consent_refused": {
        "why": "A refusal is an outcome the flow has to be able to represent. "
               "'No' ends the recorded call politely instead of being silently "
               "reinterpreted as agreement.",
        "failure_mode": "Recording a caller who declined, or arguing with them.",
        "guardrail_stage": None,
    },
    "services_intro": {
        "why": "The menu, and the only node that classifies intent. The caller "
               "is told what is on offer before being asked to choose, so the "
               "branch conditions here are the only thing the routing depends "
               "on — which is what keeps that routing reliable.",
        "failure_mode": "Classifying intent the caller has had no chance to "
                        "express, which sends them to the catch-all instead.",
        "guardrail_stage": None,
    },
    "fallback_help": {
        "why": "A general node instead of a dead end. Every flow needs an escape "
               "hatch, or an unanticipated request strands the caller.",
        "failure_mode": "Silent stall — nothing resolves and nobody is told.",
        "guardrail_stage": None,
    },
    # ------------------------------- booking -------------------------------- #
    "check_booking_capability": {
        "why": "Reads the dealer's declared DMS capability *before* the booking "
               "skill runs, so a read-only dealer is handled honestly up front "
               "rather than at the point of writing.",
        "failure_mode": "Promising a booking the dealer's system cannot accept.",
        "guardrail_stage": "Policy",
    },
    "booking_unavailable": {
        "why": "Honest degradation. One flow serves dealers whose systems can do "
               "quite different things, and this is where that difference is "
               "expressed as a routing decision instead of a runtime failure.",
        "failure_mode": "Agent confirms a booking that was never written.",
        "guardrail_stage": "Policy",
    },
    # --------------------- capture / validate / repair ---------------------- #
    "capture_plate": {
        "why": "Capture only. The instruction tells the model how to *hear* a "
               "plate, never how to judge one — judging is code's job.",
        "failure_mode": "A misheard plate is accepted without a check.",
        "guardrail_stage": "Capture",
    },
    "validate_plate": {
        "why": "Deterministic validation, guaranteed to run on node entry "
               "because it is a function node rather than an LLM-chosen tool. "
               "The model does not get to decide whether a plate is validated.",
        "failure_mode": "Plausible-but-wrong plate, or a hallucinated one.",
        "guardrail_stage": "Validate",
    },
    "plate_repair": {
        "why": "Re-runs validation rather than re-asking the whole question, and "
               "escalates after a bounded number of attempts instead of looping "
               "forever. Only asks about the characters the mask proved wrong.",
        "failure_mode": "Endless re-ask loop; the caller gives up.",
        "guardrail_stage": "Capture",
    },
    "readback_plate": {
        "why": "Moves the check to the party who actually knows the answer. The "
               "highest-value control in the pipeline, and the cheapest.",
        "failure_mode": "A wrong plate that is still format-valid goes "
                        "unchecked.",
        "guardrail_stage": "Read back",
    },
    # ----------------------------- reconcile -------------------------------- #
    "lookup_vehicle": {
        "why": "A format mask can prove a plate is *plausible*; only the record "
               "can prove it is *correct*. This is the reconcile step.",
        "failure_mode": "Wrong vehicle matched to a real-looking plate.",
        "guardrail_stage": "Reconcile",
    },
    "disambiguate_vehicle": {
        "why": "Two real plates can both match the mask. Ask the caller which "
               "vehicle they mean; never pick one.",
        "failure_mode": "Silently choosing the wrong vehicle.",
        "guardrail_stage": "Reconcile",
    },
    "vehicle_not_found": {
        "why": "Honest not-found path with a bounded retry, rather than guessing "
               "a similar-looking plate that might belong to someone else.",
        "failure_mode": "Invented or substituted vehicle.",
        "guardrail_stage": "Reconcile",
    },
    # -------------------------- service / timing ---------------------------- #
    "capture_service": {
        "why": "Captures the need and the transport preference, and is explicitly "
               "forbidden from quoting a price.",
        "failure_mode": "Invented price.",
        "guardrail_stage": None,
    },
    "capture_timing": {
        "why": "Turns vague language ('sometime next week') into an ISO range "
               "without committing to anything.",
        "failure_mode": "Assumed or misheard date.",
        "guardrail_stage": None,
    },
    # ---------------------------- availability ------------------------------ #
    "check_availability": {
        "why": "Availability is a fact, not a generation. Slots come from the "
               "dealer's diary or they do not exist.",
        "failure_mode": "Invented or stale availability.",
        "guardrail_stage": "Ground truth",
    },
    "no_slots": {
        "why": "Never invent a slot. Offer the following week or a callback.",
        "failure_mode": "Fabricated appointment time.",
        "guardrail_stage": "Ground truth",
    },
    "offer_slots": {
        "why": "May only offer times that appeared in the validated slot list.",
        "failure_mode": "Offering a time that was never available.",
        "guardrail_stage": None,
    },
    # ------------------------------ commit ---------------------------------- #
    "confirm_booking": {
        "why": "The confirmation gate. Nothing irreversible happens before it, "
               "and a hesitant answer is not a yes.",
        "failure_mode": "Committing on a partial or unsure 'I think so'.",
        "guardrail_stage": "Confirm",
    },
    "create_appointment": {
        "why": "Idempotent commit with max_retry=2 — which is only safe *because* "
               "the endpoint is idempotent. Also gated on the caller's "
               "confirmation, not the model's judgement.",
        "failure_mode": "Duplicate booking on retry.",
        "guardrail_stage": "Dispose",
    },
    "booking_pending": {
        "why": "The async middle state. CDK's Async Service Appointment API "
               "returns 202 Accepted and the appointment does not exist yet; "
               "treating that as success is how an agent confirms a booking "
               "nobody has.",
        "failure_mode": "Confirmed a booking that does not exist.",
        "guardrail_stage": "Dispose",
    },
    "verify_write": {
        "why": "'The tool returned success' is not evidence. Reading it back out "
               "of the DMS is.",
        "failure_mode": "Unverified write reported to the caller as success.",
        "guardrail_stage": "Verify",
    },
    "booking_done": {
        "why": "Confirms to the caller only after the write was read back out of "
               "the system of record.",
        "failure_mode": None,
        "guardrail_stage": "Verify",
    },
    "booking_failed": {
        "why": "Apologise plainly, do not guess why, and always hand off. A "
               "failure that ends in a human is recoverable; one that ends "
               "silently is not.",
        "failure_mode": "Dead end after a failed write.",
        "guardrail_stage": None,
    },
    # ------------------------------- rental --------------------------------- #
    "rental_intro": {
        "why": "A rental cannot be completed by voice, and pretending otherwise "
               "creates angry customers. The flow explains the licence and card "
               "requirement without quoting a price.",
        "failure_mode": "Promising a rental that cannot be honoured.",
        "guardrail_stage": "Policy",
    },
    "rental_capture": {
        "why": "Capture only. Card details are never taken over voice.",
        "failure_mode": "Payment details taken over voice.",
        "guardrail_stage": "Policy",
    },
    "rental_log": {
        "why": "Logs the request and hands it to the rental desk rather than "
               "pretending the booking is done.",
        "failure_mode": "Silent stall — the request is never actioned.",
        "guardrail_stage": None,
    },
    "rental_done": {
        "why": "States plainly that nothing is confirmed until the rental desk "
               "confirms, turning 'we'll be in touch' into a measurable "
               "commitment.",
        "failure_mode": "False confirmation to the caller.",
        "guardrail_stage": None,
    },
    # ------------------------------- billing -------------------------------- #
    "billing_intro": {
        "why": "Payment is never taken over voice. That is a policy decision "
               "rather than a technical limitation, and saying so plainly reads "
               "as competence.",
        "failure_mode": "Payment or balance discussed beyond what a tool "
                        "reported.",
        "guardrail_stage": "Policy",
    },
    # ------------------------ guardrails and endings ------------------------ #
    "flag_and_handoff": {
        "why": "Records that the call needs human review, so the frustrated tail "
               "surfaces instead of hiding behind a green dashboard.",
        "failure_mode": "Unhappy caller lost — visible only as a slightly lower "
                        "average.",
        "guardrail_stage": "Observability",
    },
    "handoff": {
        "why": "Warm handoff, with a failure edge back into the flow rather than "
               "into silence.",
        "failure_mode": "Failed transfer dead-ends the caller.",
        "guardrail_stage": None,
    },
    "escalation_fallback": {
        "why": "Reached only if the cold transfer fails. A flow that cannot get "
               "stuck is a flow that cannot strand a caller.",
        "failure_mode": "Dead end after a failed transfer.",
        "guardrail_stage": None,
    },
    "satisfaction": {
        "why": "Asks for a rating and anything the caller wants to add, and is "
               "reached only from a successful ending. Eligibility is expressed "
               "as routing rather than as a condition the model judges: the "
               "nodes that flag, transfer or decline consent go straight to "
               "wrap-up, so nobody who has just been told we cannot help gets "
               "asked to score us.",
        "failure_mode": "Asking an unhappy caller to rate the service, which is "
                        "how a neutral call becomes a one-star review. Or asking "
                        "twice, by leaving the 'anything else' question in the "
                        "node this one follows.",
        "guardrail_stage": None,
    },
    "feedback_log": {
        "why": "Persists the rating and the verbatim comment through the same "
               "deterministic layer as everything else, so satisfaction is a "
               "record rather than something the transcript might reveal later.",
        "failure_mode": "Feedback that exists only in a transcript — invisible "
                        "to anyone who is not reading every call, and lost if "
                        "post-call analysis is ever reconfigured.",
        "guardrail_stage": None,
    },
    "wrap_up": {
        "why": "One exit, reachable from every skill, so there is exactly one "
               "place a conversation ends politely.",
        "failure_mode": None,
        "guardrail_stage": None,
    },
    "end_call": {
        "why": "A single terminal state.",
        "failure_mode": None,
        "guardrail_stage": None,
    },
}

# Used for any node the overlay does not know about — a node added in the
# dashboard, or a flow from a different project entirely.
_KIND_FALLBACK = {
    "conversation": {
        "why": "A model-driven step. Keep the instruction short: anything it "
               "must not get wrong belongs in a function or branch node.",
        "failure_mode": "The model improvises where code should have decided.",
    },
    "function": {
        "why": "A function node: runs one tool on entry, deterministically. The "
               "model does not choose whether it fires.",
        "failure_mode": "A tool call that is slow or errors — check the else edge "
                        "still keeps the caller informed.",
    },
    "branch": {
        "why": "A deterministic branch over validated variables.",
        "failure_mode": "A variable that was never set, so the branch falls "
                        "through to its else edge.",
    },
    "transfer_call": {
        "why": "Hands the caller to a human.",
        "failure_mode": "A transfer that fails with no fallback.",
    },
    "end": {
        "why": "Terminal state.",
        "failure_mode": None,
    },
}


def for_node(node: dict[str, Any]) -> dict[str, Any]:
    """The annotation for one node: curated if we have it, derived if we don't."""
    curated = NODE_ANNOTATIONS.get(node.get("id") or "")
    if curated:
        return {**curated, "curated": True}

    fallback = _KIND_FALLBACK.get(node.get("kind") or "", {})
    return {
        "why": fallback.get("why", "No annotation for this node yet."),
        "failure_mode": fallback.get("failure_mode"),
        "guardrail_stage": None,
        "curated": False,
    }


def attach(snapshot: dict[str, Any], graph: dict[str, Any]) -> None:
    """Mutate `snapshot` in place with the overlay, keyed by node id.

    Deliberately keyed by id rather than inlined onto the node objects, so the
    graph stays a faithful mirror of what Retell actually stores and the
    editorial layer can be diffed, replaced or ignored independently.
    """
    snapshot["guardrail_stages"] = GUARDRAIL_STAGES
    snapshot["annotations"] = {
        node["id"]: for_node(node) for node in graph["nodes"]
    }
    covered = sum(1 for a in snapshot["annotations"].values() if a["curated"])
    graph["metrics"]["annotated_nodes"] = covered
    graph["metrics"]["unannotated_nodes"] = len(graph["nodes"]) - covered
