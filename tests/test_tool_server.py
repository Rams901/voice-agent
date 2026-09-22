"""Honesty rules for the tool server.

The tool server is the deterministic half of the architecture, so whatever it
returns is what the agent is allowed to say out loud. That makes the risky cases
the ones where a plausible-looking answer is available but a true one is not:
a plate that matches two cars, or a customer the adapter simply cannot see.

These tests pin the cases where the tempting answer is a lie.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from frontdesk.dms import ReferenceDms, reset, set_adapter
from tool_server.app import app


def _client() -> TestClient:
    """A clean server per test.

    `FLAGS` is process-wide (it is the stand-in for a review queue), so it has
    to be cleared here or one test's flag shows up in the next one's count.
    """
    reset()
    import tool_server.app as server

    server.FLAGS.clear()
    server.FEEDBACK.clear()
    server.PENDING_JOBS.clear()
    return TestClient(app)


def _lookup(client: TestClient, plate: str,
            dealer_id: str = "northgate-uk") -> dict[str, str]:
    response = client.post("/tools/lookup_vehicle",
                           json={"plate": plate, "dealer_id": dealer_id})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "success", body
    return body["data"]


# --------------------------------------------------------------------------- #
# Satisfaction
# --------------------------------------------------------------------------- #

def _feedback(client, **body) -> dict[str, str]:
    response = client.post("/tools/record_feedback", json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.mark.parametrize("spoken,expected", [
    ("5", "5"),
    ("five", "5"),
    ("FOUR", "4"),
    ("four out of five", "4"),
    ("5 stars", "5"),
    ("3.", "3"),
])
def test_a_spoken_rating_is_normalised(spoken: str, expected: str) -> None:
    """Retell drops `enum` from tool parameters, so the allowed range cannot be
    declared in the schema. It is enforced here instead — which is the right
    home for it: a rating is data, and the model should not be the thing that
    decides whether it is valid."""
    client = _client()
    response = client.post("/tools/record_feedback", json={"rating": spoken})

    assert response.json()["data"]["rating"] == expected
    assert client.get("/feedback").json()["feedback"][0]["rating"] == expected


def test_an_unrecognised_rating_is_kept_verbatim_not_rounded() -> None:
    """The honesty rule, applied to feedback. "quite good actually" is not a
    number, and nudging it to the nearest plausible score would be inventing a
    data point the caller never gave.

    The raw text is preserved, so a human can still read what they said.
    """
    client = _client()
    response = client.post("/tools/record_feedback",
                           json={"rating": "quite good actually"})

    assert response.json()["data"]["rating"] == ""
    record = client.get("/feedback").json()["feedback"][0]
    assert record["rating"] == ""
    assert record["rating_raw"] == "quite good actually"


def test_declining_a_rating_is_recorded_as_a_decline_not_a_blank() -> None:
    """"Asked and declined" is a different signal from "never asked", and
    conflating the two flatters the numbers — the same reason an unresolved call
    is not counted as a success."""
    client = _client()
    for refusal in ("no thanks", "Rather not", "skip"):
        _feedback(client, rating=refusal)

    summary = client.get("/feedback").json()
    assert summary["declined"] == 3
    assert summary["average"] is None, "a decline must not count as a score"


def test_a_rating_outside_one_to_five_is_not_a_rating() -> None:
    """Nine is not a score on a five-point scale. Recording it would corrupt the
    average silently; recording it as blank keeps the raw text for a human."""
    client = _client()
    response = client.post("/tools/record_feedback", json={"rating": "9"})

    assert response.json()["data"]["rating"] == ""
    assert client.get("/feedback").json()["average"] is None


def test_the_comment_is_kept_verbatim() -> None:
    """The comments are the useful half of feedback: a rating says how it went,
    a comment says what to fix. Tidying the caller's words would remove exactly
    the detail that makes them worth collecting."""
    client = _client()
    comment = "The chap on the desk was great but nobody told me it'd take all day."
    _feedback(client, rating="4", comment=comment)

    assert client.get("/feedback").json()["feedback"][0]["comment"] == comment


def test_feedback_survives_a_missing_rating() -> None:
    """A caller who volunteers a comment but no number is giving useful
    information, so it is recorded rather than discarded."""
    client = _client()
    response = client.post("/tools/record_feedback",
                           json={"comment": "just glad it's fixed"})

    assert response.status_code == 200
    assert response.json()["data"]["recorded"] == "true"
    record = client.get("/feedback").json()["feedback"][0]
    assert record["comment"] == "just glad it's fixed"
    assert record["rating"] == ""


def test_feedback_is_kept_separate_from_flags() -> None:
    """A flag means "someone must look at this"; feedback is "here is what they
    thought". Merging them would bury the first inside the second."""
    client = _client()
    _feedback(client, rating="5", comment="all good")

    assert client.get("/flags").json()["count"] == 0
    assert client.get("/feedback").json()["count"] == 1


def test_the_average_ignores_declines_and_blanks() -> None:
    client = _client()
    for rating in ("5", "4", "three", "no thanks", "quite good"):
        _feedback(client, rating=rating)

    summary = client.get("/feedback").json()
    assert summary["count"] == 5
    assert summary["declined"] == 1
    # 5 + 4 + 3 over three scored answers.
    assert summary["average"] == 4.0


def test_a_nested_envelope_is_rejected_with_an_actionable_message() -> None:
    """Retell wraps arguments under `args` unless the tool sets `args_at_root`.

    The failure this guards is not the rejection itself — it is what the
    rejection *looks like* without the guard. FastAPI reports each flat field as
    "missing", which reads as the model declining to supply them, so the
    instinct is to go and tune a prompt. Naming the real setting turns an
    afternoon of prompt archaeology into one obvious line.
    """
    client = _client()

    response = client.post("/tools/validate_identifier", json={
        "name": "validate_identifier",
        "args": {"kind": "license_plate", "value": "AB12CDE", "country": "GB"},
        "call": {"call_id": "call_b25cc2840ce1cc8774f538d14bc"},
    })

    assert response.status_code == 422
    assert "args_at_root" in response.text, (
        "the error must name the setting to change"
    )


def test_the_flat_body_the_guard_exists_to_protect() -> None:
    """The counterpart: `args_at_root: true` must be accepted without ceremony."""
    client = _client()

    response = client.post("/tools/validate_identifier", json={
        "kind": "license_plate", "value": "AB12CDE", "country": "GB",
    })

    assert response.status_code == 200, response.text
    # Values are strings: they become Retell dynamic variables, and the flow
    # compares them in equations (`identifier_valid == "true"`).
    assert response.json()["data"]["valid"] == "true"


def test_a_plate_matching_two_vehicles_is_never_guessed() -> None:
    """A transferred plate is a real outcome, so both cars are offered and the
    caller picks. The agent is given no default to fall back on."""
    data = _lookup(_client(), "AB12CDE")

    assert data["found"] == "true"
    assert data["match_count"] == "2"
    assert data["ambiguous"] == "true"
    assert "BMW" in data["options"]
    assert "Mercedes-Benz" in data["options"]


def test_an_unresolvable_customer_is_not_reported_as_a_zero_balance() -> None:
    """The deployed dealer runs on Keyloop, whose API is phone-keyed and has no
    vehicle->customer path.

    Reporting "0.00" here would hand the agent a tool-sourced figure it is
    explicitly permitted to read out, to a caller who does in fact owe money.
    Unresolved must stay visibly unresolved.
    """
    data = _lookup(_client(), "XY19JKL")

    assert data["found"] == "true"
    assert data["customer_known"] == "false"
    assert data["outstanding_balance"] == ""
    assert data["customer_first_name"] == ""


def test_a_resolvable_customer_reports_the_real_balance() -> None:
    """When the adapter *can* answer, the balance is a real observed figure —
    this is the behaviour the empty-string rule above must not have broken."""
    reset()
    set_adapter("northgate-uk", ReferenceDms("northgate-uk"))

    data = _lookup(TestClient(app), "XY19JKL")

    assert data["customer_known"] == "true"
    assert data["customer_first_name"] == "Daniel"
    assert data["outstanding_balance"] == "148.50"


def test_a_plate_that_is_not_a_plate_is_rejected_not_empty() -> None:
    """Defence in depth: the guardrail is re-run even if the caller skipped it,
    and the answer says *why* rather than returning a bare no-match."""
    data = _lookup(_client(), "!!!!")

    assert data["found"] == "false"
    assert data["match_count"] == "0"
    assert data["message"]


def test_an_unknown_plate_reports_no_match_rather_than_an_error() -> None:
    data = _lookup(_client(), "ZZ99ZZZ")

    assert data["found"] == "false"
    assert data["message"]


# ------------------------------------------------------- flagging a call ---- #

def test_flagging_a_call_without_a_reason_does_not_fail() -> None:
    """Regression: `flag_conversation` required `reason`, the model omitted it
    during a rental handover, and the tool returned 422 mid-call.

    That is the worst place in the whole flow to hard-fail — the caller is at
    the exact moment they need a person. A flag with a thin reason is still a
    usable flag, so the endpoint records it rather than rejecting it.
    """
    response = _client().post("/tools/flag_conversation", json={})

    assert response.status_code == 200, response.text
    assert response.json()["data"]["recorded"] == "true"


def test_flagging_a_call_keeps_a_good_reason_and_detail() -> None:
    """Tolerating a missing reason must not lose a supplied one."""
    client = _client()
    response = client.post("/tools/flag_conversation", json={
        "reason": "rental_request",
        "severity": "high",
        "detail": "25 Sep to 29 Sep, small car, licence at collection.",
    })

    assert response.status_code == 200, response.text
    flags = client.get("/flags").json()
    assert flags["count"] == 1
    record = flags["flags"][0]
    assert record["reason"] == "rental_request"
    assert record["severity"] == "high"
    assert "25 Sep" in record["detail"]


def test_an_out_of_enum_severity_is_normalised_not_rejected() -> None:
    """A model slip is not a reason to drop the flag."""
    client = _client()
    response = client.post("/tools/flag_conversation", json={
        "reason": "wants_human", "severity": "very urgent",
    })

    assert response.status_code == 200, response.text
    assert client.get("/flags").json()["flags"][0]["severity"] == "medium"
