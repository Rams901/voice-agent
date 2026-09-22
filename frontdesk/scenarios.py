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
                    LOOKUP_VEHICLE, VALIDATE_IDENTIFIER, VERIFY_APPOINTMENT)


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
                 message: str = "") -> dict[str, Any]:
    return {
        "tool_name": CREATE_APPOINTMENT,
        "input_match_rule": {"type": "any"},
        "output": _output({
            "confirmed": confirmed,
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
