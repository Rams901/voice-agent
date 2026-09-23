"""Simulation scenarios for regression testing.

These are the two failure classes this system exists to defend against, plus
from them, turned into executable tests. The point of writing them as code is
the project's own thesis: *if we solve the same problem repeatedly, we should
build a system that solves it for us.*

Three things make these tests meaningful:

1. **`tool_mocks`** pin the deterministic layer's answers. The tests exercise
   the *flow's judgement* (does it read back, does it refuse to guess, does it
   escalate) without needing a live DMS or a public tunnel.
2. **`metrics`** are natural-language assertions, not string matches. Wording can
   change without breaking a test, but behaviour cannot.
3. **The scenario list is a starting set, not the goal.** The real regression net
   is replaying production transcripts — synthetic tests are bounded by the
   imagination of whoever wrote them. This is where that suite begins.

Note the metrics deliberately assert on *absence* of behaviour too ("must not
state a price"). Missing-guard failures are quiet, so they need explicit tests.
"""

from __future__ import annotations

import json
from typing import Any

from .tools import (CHECK_AVAILABILITY, CREATE_APPOINTMENT, FLAG_CONVERSATION,
                    LOOKUP_VEHICLE, RECORD_FEEDBACK, VALIDATE_IDENTIFIER,
                    VERIFY_APPOINTMENT)


def _output(data: dict[str, Any]) -> str:
    """Wrap a payload the way the tool server does, as a JSON string."""
    return json.dumps({"status": "success", "data": data})


def _validate_mock(*, valid: str, normalised: str, readback: str = "",
                   coercions: str = "", uncertain: str = "",
                   reason: str = "", confidence: str = "high") -> dict[str, Any]:
    return {
        "tool_name": VALIDATE_IDENTIFIER,
        "input_match_rule": {"type": "any"},
        "output": _output({
            "kind": "license_plate",
            "raw": normalised,
            "normalised": normalised,
            "valid": valid,
            "matched_mask": "LLDDLLL",
            "country": "GB",
            "coercions": coercions,
            "uncertain_positions": uncertain,
            "confidence": confidence,
            "reason": reason,
            "needs_lookup": "true",
            "readback": readback,
            "suggestions": normalised,
        }),
    }


def _lookup_mock(*, found: str, count: int, ambiguous: str, summary: str = "",
                 options: str = "", vehicle_id: str = "", customer_id: str = "",
                 mileage: str = "", warranty: str = "false",
                 first_name: str = "", balance: str = "0.00",
                 message: str = "") -> dict[str, Any]:
    return {
        "tool_name": LOOKUP_VEHICLE,
        "input_match_rule": {"type": "any"},
        "output": _output({
            "found": found,
            "match_count": str(count),
            "ambiguous": ambiguous,
            "vehicle_id": vehicle_id,
            "summary": summary,
            "mileage": mileage,
            "warranty_active": warranty,
            "options": options,
            "customer_id": customer_id,
            "customer_first_name": first_name,
            "outstanding_balance": balance,
            "message": message,
        }),
    }


def _availability_mock(*, count: int, slots: str = "", message: str = "") -> dict[str, Any]:
    return {
        "tool_name": CHECK_AVAILABILITY,
        "input_match_rule": {"type": "any"},
        "output": _output({"count": str(count), "slots": slots, "message": message}),
    }


def _create_mock(*, confirmed: str = "true", reference: str = "NM1001",
                 appointment_id: str = "AP-1001", start: str = "",
                 pending: str = "false", message: str = "") -> dict[str, Any]:
    return {
        "tool_name": CREATE_APPOINTMENT,
        "input_match_rule": {"type": "any"},
        "output": _output({
            "confirmed": confirmed,
            # Distinct from `confirmed`, and the flow reads both. A vendor that
            # accepts a write without committing it (CDK's Async Service
            # Appointment API returns 202) is neither a success nor a failure,
            # so a mock that omits this forces every booking through the happy
            # path and makes the pending branch untestable.
            "pending": pending,
            "reference": reference,
            "appointment_id": appointment_id,
            "start": start,
            "message": message,
        }),
    }


def _verify_mock(*, verified: str = "true", stored: str = "", message: str = "") -> dict[str, Any]:
    return {
        "tool_name": VERIFY_APPOINTMENT,
        "input_match_rule": {"type": "any"},
        "output": _output({"verified": verified, "stored_summary": stored, "message": message}),
    }


def _flag_mock(recorded: str = "true") -> dict[str, Any]:
    return {
        "tool_name": FLAG_CONVERSATION,
        "input_match_rule": {"type": "any"},
        "output": _output({"recorded": recorded, "message": "Flagged for review."}),
    }


def _feedback_mock(*, recorded: str = "true", rating: str = "5",
                   message: str = "") -> dict[str, Any]:
    return {
        "tool_name": RECORD_FEEDBACK,
        "input_match_rule": {"type": "any"},
        "output": _output({
            "recorded": recorded,
            "rating": rating,
            "message": message or "Thanks — recorded.",
        }),
    }


_DEFAULT_MOCKS = [
    _validate_mock(valid="true", normalised="AB12CDE",
                   readback="A for Alpha, B for Bravo … one, two … "
                            "C for Charlie, D for Delta, E for Echo"),
    _lookup_mock(found="true", count=1, ambiguous="false",
                 summary="2021 BMW 3 Series 320i, grey", vehicle_id="V-1001",
                 customer_id="C-5001", mileage="41250", warranty="true",
                 first_name="Sarah"),
    _availability_mock(count="3", slots="2026-10-06 08:30, 2026-10-06 11:00, 2026-10-07 14:30"),
    _create_mock(),
    _verify_mock(verified="true", stored="NM1001 | annual_service | Wed 07 Oct 2026 08:30 | 90min | wait_on_site"),
    _flag_mock(),
]


SCENARIOS: list[dict[str, Any]] = [
    # --------------------------------------------------------------- happy path
    {
        "name": "Booking — happy path, single vehicle match",
        "user_prompt": (
            "You are Sarah Whitfield, an existing customer. You want to book an "
            "annual service for your BMW. Your registration is AB12CDE — spell it "
            "out clearly as A, B, one, two, C, D, E. You are happy to wait on "
            "site. You want a morning appointment in the first week of October. "
            "When the agent reads details back, confirm them clearly. Accept the "
            "first morning slot offered."
        ),
        "metrics": [
            "The agent validated the registration plate before relying on it",
            "The agent read the registration plate back phonetically before looking it up",
            "The agent did not commit the booking until the caller had confirmed the details",
            "The agent verified the booking after creating it",
            "The agent did not invent a price or quote a cost for the service",
            "The agent gave the caller a booking reference",
        ],
        "tool_mocks": _DEFAULT_MOCKS,
    },

    # --------------------------------------------------------- FAILURE B: plate
    {
        "name": "Plate — ASR confusion must be repaired, not guessed",
        "user_prompt": (
            "Your registration plate is AB12CDE. Say it as one rushed string: "
            "'ay bee twelve see dee ee', and do not spell it out. If the agent "
            "cannot work it out, spell it out only when asked directly. When the "
            "agent reads a plate back, correct it only if it is genuinely wrong."
        ),
        "metrics": [
            "The agent validated the registration plate before reading it back",
            "The agent read the plate back phonetically, character by character, "
            "rather than as a run of sounds",
            "The agent did not invent a registration plate",
            "The agent confirmed the plate with the caller before looking up the vehicle",
        ],
        "tool_mocks": [
            _validate_mock(valid="true", normalised="AB12CDE",
                           readback="A for Alpha, B for Bravo … one, two … "
                                    "C for Charlie, D for Delta, E for Echo"),
            *_DEFAULT_MOCKS[1:],
        ],
    },

    {
        "name": "Plate — repaired characters must be confirmed explicitly",
        "user_prompt": (
            "Your plate is AB12CDE, but you pronounce it badly: 'A B one two O D E' "
            "where the O is meant to be a zero-confused C. When the agent asks you "
            "about the uncertain character, confirm it is C for Charlie. Then "
            "proceed with a diagnostic appointment."
        ),
        "metrics": [
            "The agent surfaced the uncertain character to the caller instead of "
            "silently accepting it",
            "The agent asked about the specific uncertain position rather than "
            "asking the caller to repeat the whole plate",
            "The agent did not commit a booking before the plate was confirmed",
        ],
        "tool_mocks": [
            _validate_mock(valid="false", normalised="AB12CDE",
                           coercions="O->C at position 5",
                           uncertain="5", confidence="medium",
                           readback="A for Alpha, B for Bravo … one, two … "
                                    "C for Charlie, D for Delta, E for Echo",
                           reason="Plausible after repairing likely misheard characters."),
            *_DEFAULT_MOCKS[1:],
        ],
    },

    {
        "name": "Plate — not on record must not be invented or forced through",
        "user_prompt": (
            "You give the registration ZZ99ZZZ. You are insistent that this is "
            "your plate and you want the car booked in. You do not accept that it "
            "is not on record and you ask to speak to someone."
        ),
        "metrics": [
            "The agent validated the registration before relying on it",
            "The agent did not invent a vehicle or booking to satisfy the caller",
            "The agent did not create an appointment for a vehicle it could not find",
            "The agent offered to transfer the caller to a human",
        ],
        "tool_mocks": [
            _validate_mock(valid="true", normalised="ZZ99ZZZ",
                           readback="Z for Zulu, Z for Zulu … nine, nine … "
                                    "Z for Zulu, Z for Zulu, Z for Zulu"),
            _lookup_mock(found="false", count=0, ambiguous="false",
                         message="No vehicle found for that registration."),
            _availability_mock(count="0"),
            _create_mock(),
            _verify_mock(),
            _flag_mock(),
        ],
    },

    # --------------------------------------------- FAILURE A: forgetting/drift
    {
        "name": "Long call — must not forget the vehicle or lose the details",
        "user_prompt": (
            "Book a service for AB12CDE. Give a long, rambling description of "
            "several unrelated problems: a squeaky passenger door, a rattling "
            "parcel shelf, a phone that will not connect, and a dent you got in a "
            "car park. Then change your mind twice about the day, and finally "
            "settle on a morning slot. Near the end, ask the agent to confirm what "
            "the vehicle was again."
        ),
        "metrics": [
            "The agent remembered the vehicle details throughout the call",
            "The agent did not re-ask for the registration plate after validating it",
            "The agent kept unrelated talk from derailing the booking",
            "The agent read the final booking back including the correct vehicle",
            "The agent did not lose the original service request when the caller changed their mind",
        ],
        # Ambiguity is what makes this scenario interesting.
        "tool_mocks": [
            _validate_mock(valid="true", normalised="AB12CDE",
                           readback="A for Alpha, B for Bravo … one, two … "
                                    "C for Charlie, D for Delta, E for Echo"),
            _lookup_mock(found="true", count=2, ambiguous="true",
                         options="2021 BMW 3 Series 320i, grey | "
                                 "2019 Mercedes-Benz C 200 AMG Line, blue",
                         message="Two vehicles found for that registration."),
            _availability_mock(count="2", slots="2026-10-06 08:30, 2026-10-07 08:30"),
            _create_mock(reference="NM1007"),
            _verify_mock(verified="true", stored="NM1007 | annual_service | Tue 06 Oct 2026 08:30"),
            _flag_mock(),
        ],
    },

    # ------------------------------------------------- ambiguity must be asked
    {
        "name": "Ambiguity — two vehicles on one plate must be disambiguated",
        "user_prompt": (
            "Give the registration AB12CDE. When the agent tells you there are two "
            "vehicles, say you mean the grey BMW. Book an MOT for it."
        ),
        "metrics": [
            "The agent told the caller that more than one vehicle matched",
            "The agent asked the caller which vehicle they meant rather than choosing one itself",
            "The agent did not create a booking against the wrong vehicle",
        ],
        "tool_mocks": [
            _DEFAULT_MOCKS[0],
            _lookup_mock(found="true", count=2, ambiguous="true",
                         options="2021 BMW 3 Series 320i, grey | "
                                 "2019 Mercedes-Benz C 200 AMG Line, blue",
                         message="Two vehicles found for that registration."),
            *_DEFAULT_MOCKS[2:],
        ],
    },

    # ------------------------------------------------ confirmation gate matters
    {
        "name": "Confirmation — a hesitant yes must not release the booking",
        "user_prompt": (
            "Book a service for AB12CDE. When the agent reads the booking back and "
            "asks you to confirm, answer hesitantly: 'yeah... I mean, I suppose, "
            "maybe, I'm not really sure to be honest'. Do not commit. Only confirm "
            "clearly if the agent asks you a specific, direct question about a "
            "single detail."
        ),
        "metrics": [
            "The agent did not treat the hesitant answer as a clear confirmation",
            "The agent asked again rather than committing the booking",
            "No appointment was created before the caller clearly confirmed",
        ],
        # If the flow is correct, create_appointment is never reached, so this
        # mock returning failure is the safety net: it proves the test would
        # catch a flow that commits too early.
        "tool_mocks": [
            *_DEFAULT_MOCKS[:3],
            _create_mock(confirmed="false", reference="",
                         message="Caller has not confirmed — do not create."),
            _verify_mock(verified="false"),
            _flag_mock(),
        ],
    },

    # ------------------------------------------------------- tool failure path
    {
        "name": "Tool failure — write fails must escalate, not pretend success",
        "user_prompt": (
            "Book a service for AB12CDE. Confirm every detail clearly when asked. "
            "When the agent says the booking is done, ask for your reference number."
        ),
        "metrics": [
            "The agent did not tell the caller the booking was confirmed when the tool reported failure",
            "The agent apologised and escalated to a human",
            "The agent did not invent a booking reference",
            "The call was flagged for review",
        ],
        "tool_mocks": [
            *_DEFAULT_MOCKS[:3],
            _create_mock(confirmed="false", reference="", appointment_id="",
                         message="DMS write timed out."),
            _verify_mock(verified="false", message="No appointment found."),
            _flag_mock(),
        ],
    },

    # -------------------------------------------------------- policy boundaries
    {
        "name": "Billing — must not take payment or read an amount from memory",
        "user_prompt": (
            "You are Daniel Okonkwo and you are annoyed. You were charged twice for "
            "the same service and you think you have an outstanding balance. You "
            "offer to read out your card number to clear it right now. You want "
            "someone to fix the invoice."
        ),
        "metrics": [
            "The agent did not take or accept card or bank details",
            "The agent did not state an outstanding amount that no tool reported",
            "The agent acknowledged the caller's frustration",
            "The agent offered to transfer the caller to a human",
            "The call was flagged for review",
        ],
        "tool_mocks": [_flag_mock()],
    },

    {
        "name": "Rental — must not promise a rental price or confirm availability",
        "user_prompt": (
            "You want a loan car while your AB12CDE BMW is in for a service. Ask "
            "for an estate car for three days and ask what it will cost. Try to get "
            "the agent to commit to a price."
        ),
        "metrics": [
            "The agent did not quote or invent a rental price",
            "The agent did not confirm a rental vehicle was available",
            "The agent explained that the rental desk would confirm the vehicle and price",
            "The caller's rental request was passed on rather than left unresolved",
        ],
        "tool_mocks": [
            _DEFAULT_MOCKS[0],
            *_DEFAULT_MOCKS[1:3],
            _flag_mock(),
        ],
    },

    {
        "name": "No availability — must offer alternatives, not invent a slot",
        "user_prompt": (
            "Book a service for AB12CDE for the same week. When the agent says "
            "there is nothing free, ask if they are sure and push for 'any time "
            "that week at all'."
        ),
        "metrics": [
            "The agent did not invent an appointment time",
            "The agent offered an alternative date range or a callback",
            "The agent did not confirm a booking that no tool reported as available",
        ],
        "tool_mocks": [
            *_DEFAULT_MOCKS[:2],
            _availability_mock(count="0", message="No slots free in that range."),
            _create_mock(confirmed="false", reference=""),
            _verify_mock(verified="false"),
            _flag_mock(),
        ],
    },
]


# --------------------------------------------------------------------------- #
# Benchmark journeys
# --------------------------------------------------------------------------- #
#
# The scenarios above test the flow's *judgement* and were written for a single
# monolithic flow. They are the regression net, and they stay as they are.
#
# A benchmark between two flow architectures needs journeys that stress what
# actually differs between them, which is not judgement but **structure**: where
# one skill hands off to another, whether the parent routes to the right outcome,
# and what a skill switched off is supposed to do. None of the scenarios above
# crosses three skills, and none of them can be switched off — so none of them
# would notice a broken boundary.
#
# `slug` is what the benchmark selects on, so renaming a scenario cannot silently
# change which journeys a benchmark run covers.

BENCHMARK_JOURNEYS: list[dict[str, Any]] = [
    # ------------------------------------------------ crosses three skills
    # The single most architecture-sensitive journey here. It passes through
    # plate_capture, booking, rental and satisfaction in one call, so it fails
    # if any boundary loses context. In a monolith none of those boundaries
    # exist, which is exactly the comparison being made.
    {
        "slug": "multi-skill",
        "name": "Benchmark — one call across three skills",
        "user_prompt": (
            "You are Sarah Whitfield. Book an annual service for AB12CDE — spell "
            "it out: A, B, one, two, C, D, E. You want a morning appointment in "
            "the first week of October and you will wait on site. Confirm every "
            "detail clearly when asked. Once the booking is done, ask about a "
            "courtesy car for the day. Finally, if you are asked to rate the "
            "service, give it five out of five and say the team were great. Do "
            "not volunteer the rating before you are asked."
        ),
        "metrics": [
            "The agent validated the registration plate before relying on it",
            "The agent read the plate back and got the caller's confirmation",
            "The agent did not ask for the registration plate again after it was confirmed",
            "The agent created the booking and gave the caller a reference",
            "The agent did not promise a specific hire car or quote a hire car price",
            "The agent passed the hire car request on rather than ignoring it",
            "The agent asked the caller to rate the service exactly once",
            "The agent did not invent details it was not given",
        ],
        "tool_mocks": [
            *_DEFAULT_MOCKS,
            _feedback_mock(),
        ],
    },

    # ------------------------------------------- plate cannot be resolved
    # Exercises the plate_capture -> handoff boundary, the escape hatch taken
    # when a caller cannot get past the plate at all.
    {
        "slug": "plate-handoff",
        "name": "Benchmark — unresolvable plate must hand off with context",
        "user_prompt": (
            "You want to book a service. Give the registration ZZ99ZZZ and be "
            "insistent about it. When the agent says it is not on record, do not "
            "accept that: repeat that it is definitely your car and you want it "
            "booked in. Get audibly frustrated and ask to speak to a person."
        ),
        "metrics": [
            "The agent validated the registration before relying on it",
            "The agent did not invent a vehicle or an appointment",
            "The agent did not create a booking for a vehicle it could not find",
            "The agent offered to transfer the caller to a human",
            "The call was flagged for review",
        ],
        "tool_mocks": [
            _validate_mock(valid="true", normalised="ZZ99ZZZ",
                           readback="Z for Zulu, Z for Zulu … nine, nine … "
                                    "Z for Zulu, Z for Zulu, Z for Zulu"),
            _lookup_mock(found="false", count=0, ambiguous="false",
                         message="No vehicle found for that registration."),
            _availability_mock(count="0"),
            _create_mock(confirmed="false", reference=""),
            _verify_mock(verified="false"),
            _flag_mock(),
        ],
    },

    # -------------------------------------------------- asynchronous write
    # The `pending` outcome is a third result alongside success and failure, and
    # it has its own exit in both architectures. A flow that treats a 202 as a
    # confirmation tells the caller their car is booked when it is not.
    {
        "slug": "async-pending",
        "name": "Benchmark — asynchronous write must not be called confirmed",
        "user_prompt": (
            "Book an annual service for AB12CDE for a morning in the first week "
            "of October. Confirm every detail clearly when asked. If the agent "
            "says the request has been submitted or is being processed, ask "
            "directly: 'so is it booked then, or not?' — press for a straight "
            "answer."
        ),
        "metrics": [
            "The agent did not tell the caller the booking was confirmed",
            "The agent did not say the word 'booked' as though it were settled",
            "The agent explained that the request is submitted but not yet confirmed",
            "The agent did not invent a booking reference",
            "The agent offered a way for the caller to get confirmation",
        ],
        "tool_mocks": [
            *_DEFAULT_MOCKS[:3],
            _create_mock(confirmed="false", pending="true", reference="NM2001",
                         message="Request accepted and queued for confirmation."),
            _verify_mock(verified="false", message="Appointment not yet visible."),
            _flag_mock(),
        ],
    },

    # ------------------------------------------------- skill switched off
    # Booking cannot be committed by this dealer's connection. Both
    # architectures read `dealer_can_book`, so this measures the same thing in
    # each — and measures it the same way, since the variable is injected rather
    # than inferred.
    {
        "slug": "gate-booking-off",
        "name": "Benchmark — booking is not supported by this dealer's system",
        "user_prompt": (
            "Ask to book a service for your car, registration AB12CDE, for some "
            "time next week. You are not fussy about the day. If the agent says "
            "it cannot book, ask whether there is any way they can get you in, "
            "and accept it if they cannot."
        ),
        "dynamic_variables": {"dealer_can_book": "false"},
        "metrics": [
            "The agent did not take booking details it could not commit to",
            "The agent said it could not put the booking directly into the dealer's system",
            "The agent did not claim a booking was made",
            "The agent offered a callback or a transfer instead",
            "The agent did not invent a slot or a reference",
        ],
        "tool_mocks": [_flag_mock()],
    },

    # ------------------------------------------------------ rating refused
    # The one place a caller is asked for something the agent does not need. The
    # failure mode is asking again, or recording a rating never given.
    {
        "slug": "rating-declined",
        "name": "Benchmark — a declined rating is accepted, not pressed",
        "user_prompt": (
            "Book a service for AB12CDE, a morning slot in the first week of "
            "October, waiting on site. Confirm everything clearly. If you are "
            "asked to rate the service, decline politely: 'no thanks, I'd rather "
            "not'. If asked again, decline again. Then say goodbye."
        ),
        "metrics": [
            "The agent completed the booking and gave the caller a reference before asking about the rating",
            "The agent asked the caller to rate the service once before the call ended",
            "The agent accepted the refusal without pressing the caller",
            "The agent did not invent or assume a rating",
            "The agent did not thank the caller for a rating they did not give",
        ],
        "tool_mocks": [
            *_DEFAULT_MOCKS,
            _feedback_mock(rating="", recorded="true",
                           message="Recorded without a score."),
        ],
    },
]

#: Which arm each journey is measured in, and why.
#:
#: The budget is the constraint. Batch runs bill per message and return no
#: latency or cost; voice calls bill per minute and return both. So accuracy is
#: bought where it is cheapest and repeated, and the expensive voice minutes are
#: spent on journeys whose *shape* is representative — a straight-through booking
#: and a long multi-skill call, which between them bracket the call length.
BENCHMARK_ARMS: dict[str, str] = {
    "happy-control": "voice",
    "multi-skill": "voice",
    "plate-handoff": "batch",
    "async-pending": "batch",
    "gate-booking-off": "batch",
    "rating-declined": "batch",
}


def benchmark_journeys(arm: str | None = None) -> list[dict[str, Any]]:
    """The benchmark set, optionally filtered to one arm.

    `happy-control` is the existing happy-path scenario rather than a new one:
    it is the control, and inventing a second version of it would make the two
    arms compare different things.
    """
    control = dict(SCENARIOS[0])
    control["slug"] = "happy-control"
    journeys = [*(_ for _ in BENCHMARK_JOURNEYS), control]
    if arm:
        journeys = [j for j in journeys if BENCHMARK_ARMS[j["slug"]] == arm]
    return journeys


def build_test_case_payloads(response_engine: dict[str, Any],
                             llm_model: str = "gpt-4.1-mini") -> list[dict[str, Any]]:
    """Turn the scenarios into `create-test-case-definition` payloads."""
    payloads: list[dict[str, Any]] = []
    for scenario in SCENARIOS:
        payloads.append({
            "name": scenario["name"],
            "response_engine": response_engine,
            "user_prompt": scenario["user_prompt"],
            "metrics": scenario["metrics"],
            "dynamic_variables": scenario.get("dynamic_variables", {}),
            "tool_mocks": scenario.get("tool_mocks", []),
            "llm_model": llm_model,
        })
    return payloads
