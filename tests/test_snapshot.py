"""Tests for the diagram normalizer — the Retell -> IR layer.

These run entirely offline. Instead of mocking a Retell response, they build the
graph from `flow.build_flow_payload`, which is the exact payload we deploy. That
makes them a real contract test: if the flow builder and the diagram normalizer
disagree about node or edge shape, one of them is wrong and this fails.

The live `get-conversation-flow` response is this payload plus `display_position`
on every node, so the only thing not covered here is Retell's own layout numbers.
"""

from __future__ import annotations

import json

import pytest

from frontdesk import annotations, insights, journey
from frontdesk.dealer_profiles import dealer_dynamic_variables, get_dealer
from frontdesk.flow import GLOBAL_PROMPT, build_flow_payload
from frontdesk.snapshot import (
    CLUSTER_ROW_GAP,
    NODE_ROW_GAP,
    build_graph,
    contract_graph,
    layered_layout,
)

KNOWN_EDGE_KINDS = {"deterministic", "always", "llm", "else", "failure"}


@pytest.fixture(scope="module")
def graph() -> dict:
    payload = build_flow_payload(
        get_dealer("northgate-uk"), "https://tools.example.test"
    )
    return build_graph(payload)


# ------------------------------------------------------------------ graph ---- #

def test_every_node_gets_a_kind_and_an_id(graph: dict) -> None:
    ids = [node["id"] for node in graph["nodes"]]
    assert len(ids) == len(set(ids)), "duplicate node ids"
    assert all(node["kind"] for node in graph["nodes"])
    assert all(node["kind_label"] for node in graph["nodes"])


def test_no_dangling_edges(graph: dict) -> None:
    """An edge to a missing node would silently break the layout."""
    assert graph["dangling_edges"] == []


def test_every_edge_kind_is_classified(graph: dict) -> None:
    """The four edge slots collapse to five kinds, and the singlular `edge`
    (a node's failure path) must not be mistaken for a conditional branch."""
    kinds = {edge["kind"] for edge in graph["edges"]}
    assert kinds <= KNOWN_EDGE_KINDS
    assert kinds == KNOWN_EDGE_KINDS, f"missing kinds: {KNOWN_EDGE_KINDS - kinds}"


def test_equation_edges_are_deterministic_and_prompt_edges_are_not(graph: dict) -> None:
    for edge in graph["edges"]:
        if edge["slot"] == "edges":
            # A conditional branch is deterministic only if it is an equation.
            assert edge["is_deterministic"] == (edge["kind"] == "deterministic")
        # `always` is guaranteed execution, so it counts as not-the-model's-call.
        assert edge["is_deterministic"] == (
            edge["kind"] in {"deterministic", "always"}
        )


def test_function_nodes_resolve_their_tool_name(graph: dict) -> None:
    functions = [n for n in graph["nodes"] if n["kind"] == "function"]
    assert functions, "expected function nodes"
    for node in functions:
        assert node["tool_id"].startswith("tool_")
        # `tool_validate_identifier` -> `validate_identifier`
        assert node["tool_name"] == node["tool_id"][len("tool_"):]
        assert node["tool_name"] in {t["name"] for t in graph["tools"]}


def test_tools_are_registered_for_every_function_node(graph: dict) -> None:
    registered = {t["tool_id"] for t in graph["tools"]}
    for node in graph["nodes"]:
        if node["kind"] == "function":
            assert node["tool_id"] in registered


def test_retry_is_flagged_as_requiring_idempotency(graph: dict) -> None:
    """`create_appointment` sets max_retry=2, which is only safe because the
    endpoint is idempotent. The diagram must say so rather than show a bare 2."""
    by_name = {t["name"]: t for t in graph["tools"]}
    assert by_name["create_appointment"]["max_retry"] == 2
    assert by_name["create_appointment"]["retry_requires_idempotency"] is True
    assert by_name["validate_identifier"]["retry_requires_idempotency"] is False


# ---------------------------------------------------------------- metrics ---- #

def test_prompt_budget_matches_the_documented_claim(graph: dict) -> None:
    """The README claims a ~309 token global prompt and ~442 worst turn. This
    is what stops the diagram and the plan command drifting apart."""
    metrics = graph["metrics"]
    assert metrics["global_prompt_tokens"] == len(GLOBAL_PROMPT) // 4
    assert metrics["worst_case_turn_context_tokens"] == (
        metrics["global_prompt_tokens"] + metrics["max_node_prompt_tokens"]
    )
    # Far below the 14k monolith this replaces.
    assert metrics["worst_case_turn_context_tokens"] < 1000


def test_the_budget_admits_that_placeholders_expand(graph: dict) -> None:
    """`{{current_calendar}}` counts as one placeholder here and ~150 tokens at
    runtime. A tool whose job is measuring the prompt budget must say which of
    its numbers are floors, or the measurement is quietly wrong in the
    direction that flatters it."""
    metrics = graph["metrics"]
    assert metrics["prompt_tokens_are_minimum"] is True
    assert "capture_timing" in metrics["nodes_with_runtime_variables"]
    # The date is the thing that makes this matter at all — and it must be
    # present, because "sometime next week" is unanswerable without it.
    timing = next(n for n in graph["nodes"] if n["id"] == "capture_timing")
    assert "{{current_calendar}}" in timing["instruction"]


def test_the_dealer_timezone_is_seeded_under_retells_reserved_name() -> None:
    """Retell resolves `{{current_calendar}}` in `system_timezone`, which
    defaults to America/Los_Angeles. For a UK dealer that means "next Tuesday"
    can be computed against the wrong day, so the dealer's own timezone has to
    be supplied under Retell's name and not merely our own."""
    dealer = get_dealer("northgate-uk")
    variables = dealer_dynamic_variables(dealer)
    assert variables["system_timezone"] == dealer.timezone == "Europe/London"
    # ...and our own key still exists, because node instructions reference it.
    assert variables["timezone"] == dealer.timezone


def test_deterministic_spine_is_measured_not_asserted(graph: dict) -> None:
    metrics = graph["metrics"]
    assert metrics["deterministic_nodes"] > 0
    assert metrics["deterministic_edges"] > 0
    assert metrics["model_judged_edges"] > 0
    assert 0 < metrics["deterministic_edge_share"] < 1
    assert metrics["edge_count"] == sum(metrics["edges_by_kind"].values())


# ----------------------------------------------------------------- skills ---- #

def test_skills_partition_the_graph(graph: dict) -> None:
    """Every node is either a skill member or front door — nothing is orphaned.

    The front door used to be a single node, so "everything except the start is
    owned by a skill" held. Splitting greeting, consent and the menu apart made
    that several nodes, and the assertion moves to the rule that actually
    matters: no node is left unaccounted for.
    """
    unassigned = {n["id"] for n in graph["nodes"] if not n["skill"]}
    front_door = {n["id"] for n in graph["nodes"] if n.get("is_front_door")}

    assert unassigned == front_door, "only the front door may be unowned"
    assert graph["start_node_id"] in front_door
    assert graph["skills"], "expected skill clusters"

    assigned = [n for n in graph["nodes"] if n["skill"]]
    total = sum(skill["node_count"] for skill in graph["skills"])
    assert total == len(assigned)
    # The front door is genuinely more than the greeting now.
    assert len(front_door) > 1


def test_shared_spine_is_the_nodes_several_skills_route_into(graph: dict) -> None:
    shared = {n["id"] for n in graph["nodes"] if n["is_shared"]}
    # Both are reached from booking, rental, billing and the fallback path.
    assert "flag_and_handoff" in shared
    assert "wrap_up" in shared
    # ...but a normal step on the booking spine is not shared.
    assert "confirm_booking" not in shared
    # A completed booking can jump into rental, which must NOT make the whole
    # rental subtree "shared".
    assert "rental_capture" not in shared


def test_layered_layout_positions_every_node(graph: dict) -> None:
    layout = layered_layout(graph["nodes"], graph["edges"], graph["start_node_id"])
    assert set(layout) == {n["id"] for n in graph["nodes"]}
    for point in layout.values():
        assert isinstance(point["x"], (int, float))
        assert isinstance(point["y"], (int, float))


# ------------------------------------------------------------- contraction --- #

def test_contraction_covers_every_node_exactly_once(graph: dict) -> None:
    contracted = contract_graph(graph, {})
    covered = [nid for cluster in contracted["nodes"] for nid in cluster["nodes"]]
    assert sorted(covered) == sorted(n["id"] for n in graph["nodes"])


def test_contraction_is_far_smaller_than_the_graph(graph: dict) -> None:
    """The whole point: legible at 100% zoom. A 31-node / 14-step spine cannot
    be, which is the complaint the tool exists to answer."""
    contracted = contract_graph(graph, {})
    assert len(contracted["nodes"]) <= 8
    assert len(contracted["nodes"]) < len(graph["nodes"])


def test_contracted_edges_are_aggregated_and_counted(graph: dict) -> None:
    contracted = contract_graph(graph, {})
    nodes = {c["id"] for c in contracted["nodes"]}
    for edge in contracted["edges"]:
        assert edge["from"] in nodes and edge["to"] in nodes
        assert edge["count"] >= 1
        assert "transition" in edge["label"]
    # Nothing is lost: contracted edges plus internal ones account for the whole
    # original edge set.
    assert (sum(e["count"] for e in contracted["edges"])
            + contracted["internal_transitions"]) == len(graph["edges"])


def test_contraction_exposes_guardrail_stages(graph: dict) -> None:
    """Clusters inherit the guardrail stages of their members, which is what
    turns the summary into an explanation rather than a box count."""
    annotations = {
        "capture_plate": {"guardrail_stage": "Capture"},
        "validate_plate": {"guardrail_stage": "Validate"},
        "readback_plate": {"guardrail_stage": "Read back"},
    }
    contracted = contract_graph(graph, annotations)
    booking = next(c for c in contracted["nodes"] if c["id"] == "check_booking_capability")
    assert booking["stages"] == ["Capture", "Read back", "Validate"]


def test_clusters_are_spaced_for_their_own_height(graph: dict) -> None:
    """The overlap bug, pinned.

    Five skills share rank 1, and the Booking card carries enough chips to be
    far taller than a node card. At the node gap (150) it rendered over Rental.
    A cluster row gap has to exceed the tallest card, and the tallest card is
    unbounded in principle — so the frontend caps its chips to bound it, and this
    asserts the gap that makes that bound sufficient.
    """
    contracted = contract_graph(graph, {})
    layout = contracted["layout"]

    columns: dict[float, list[float]] = {}
    for cluster in contracted["nodes"]:
        point = layout[cluster["id"]]
        columns.setdefault(point["x"], []).append(point["y"])

    # A cluster card with the chip cap reached is ~180px; 240 clears it with
    # room for the selection ring, which draws outside the box.
    assert CLUSTER_ROW_GAP >= 200
    assert CLUSTER_ROW_GAP > NODE_ROW_GAP

    for x, ys in columns.items():
        ys.sort()
        for lower, upper in zip(ys, ys[1:]):
            assert upper - lower == CLUSTER_ROW_GAP, (
                f"column x={x} has a {upper - lower}px gap"
            )


# ---------------------------------------------------------------- insights --- #

def test_metrics_without_calls_are_labelled_modelled(graph: dict) -> None:
    metrics = insights.build_metrics([], graph)
    cost = metrics["cost"]
    assert cost["source"] == "modelled"
    assert cost["observed"]["usable_calls"] == 0
    assert cost["calibration"]["applied"] is False
    # A failed call must not be counted as a cheap call.
    assert cost["observed"]["unusable_calls"] == 0
    assert metrics["latency"]["source"] == "modelled"


def test_a_failed_call_is_not_treated_as_a_cheap_call(graph: dict) -> None:
    """Retell returns a zeroed `call_cost` for calls that never connected. If
    those are averaged in, the cost estimate silently trends to zero."""
    failed = [{
        "call_id": "call_x",
        "call_cost": {"total_duration_seconds": 0, "combined_cost": 0,
                      "product_costs": []},
        "latency": {},
        "disconnection_reason": "error_user_not_joined",
    }]
    metrics = insights.build_metrics(failed, graph)
    assert metrics["cost"]["observed"]["call_count"] == 1
    assert metrics["cost"]["observed"]["usable_calls"] == 0
    assert metrics["cost"]["source"] == "modelled"


def test_cost_bands_are_ordered_and_scale_with_volume(graph: dict) -> None:
    cost = insights.build_metrics([], graph)["cost"]["modelled"]
    low = cost["per_conversation"]["low"]
    mid = cost["per_conversation"]["mid"]
    high = cost["per_conversation"]["high"]
    assert 0 < low < mid < high
    per_day = cost["conversations_per_day"]
    assert cost["per_day"]["mid"] == pytest.approx(mid * per_day, abs=0.01)
    assert cost["per_month"]["mid"] == pytest.approx(
        cost["per_day"]["mid"] * cost["days_per_month"], abs=0.1)
    # The components must account for the whole, or the breakdown lies.
    components = cost["components"]["mid"]
    assert components["voice_and_platform"] + components["llm_tokens"] == pytest.approx(
        mid, abs=0.001)


def test_a_billable_call_switches_the_source_to_observed(graph: dict) -> None:
    """The self-calibration path: real unit prices arrive from `product_costs`,
    and the tool starts reporting what was actually charged."""
    calls = [
        {
            "call_id": "call_ok",
            "call_cost": {
                "total_duration_seconds": 180,
                "combined_cost": 0.30,
                "product_costs": [
                    {"product": "elevenlabs_tts", "unit_price": 0.0007, "cost": 0.126},
                    {"product": "retell_platform", "unit_price": 0.0008, "cost": 0.144},
                ],
            },
            "latency": {"e2e": {"p50": 1.1, "p90": 1.9, "p95": 2.4}},
            "tool_calls": [{"name": "validate_identifier"}, {"name": "lookup_vehicle"}],
        }
    ]
    metrics = insights.build_metrics(calls, graph)
    observed = metrics["cost"]["observed"]
    assert observed["usable_calls"] == 1
    assert observed["blended_cost_per_minute"] == pytest.approx(0.10, abs=0.001)
    assert observed["unit_prices"]["elevenlabs_tts"] == pytest.approx(0.0007)
    assert observed["tool_calls"]["lookup_vehicle"] == 1
    assert metrics["cost"]["source"] == "observed+modelled"
    assert metrics["cost"]["calibration"]["applied"] is True
    assert metrics["latency"]["source"] == "observed"
    assert metrics["latency"]["observed"]["e2e"]["p95"] == pytest.approx(2.4)


def test_latency_tail_is_worse_than_the_median(graph: dict) -> None:
    """The tail is what callers experience; a model that only reports p50 would
    hide the thing the tool is meant to surface."""
    latency = insights.build_metrics([], graph)["latency"]["modelled"]
    assert latency["per_turn"]["p50"] < latency["per_turn"]["p90"] < latency["per_turn"]["p95"]
    # A synchronous tool round trip must cost something.
    assert latency["per_tool_turn"]["p50"] > latency["per_turn"]["p50"]


# ------------------------------------------------------- the business view --- #

def test_drivers_agree_with_the_component_breakdown(graph: dict) -> None:
    """The business view states in words that "duration is the whole number".
    That is a measurement, so it is asserted here rather than trusted to the
    prose: the shares must be derived from the same components the breakdown
    shows, and they must add up."""
    modelled = insights.build_metrics([], graph)["cost"]["modelled"]
    comp = modelled["components"]["mid"]
    drivers = modelled["drivers"]
    total = comp["voice_and_platform"] + comp["llm_tokens"]

    assert drivers["voice_and_platform_share"] == pytest.approx(
        comp["voice_and_platform"] / total, abs=0.002)
    assert drivers["llm_share"] == pytest.approx(
        comp["llm_tokens"] / total, abs=0.002)
    assert drivers["voice_and_platform_share"] + drivers["llm_share"] == pytest.approx(
        1.0, abs=0.002)
    assert drivers["turns_mid"] == comp["turns"]
    assert drivers["input_tokens_mid"] == comp["input_tokens"]

    # The headline claim. If token prices ever dominate, this test should fail
    # rather than the UI quietly lying.
    assert drivers["voice_and_platform_share"] > 0.9


def test_volume_ladder_includes_the_configured_volume(graph: dict) -> None:
    """The ladder exists to answer "and at 250/day?", so the row somebody
    actually cares about — the volume they configured — must never be the one
    row missing. The model is linear in volume, so each rung is plain
    multiplication of the mid case."""
    modelled = insights.build_metrics([], graph)["cost"]["modelled"]
    rows = modelled["volume_sensitivity"]
    counts = [row["conversations_per_day"] for row in rows]

    assert counts == sorted(counts)
    assert modelled["conversations_per_day"] in counts
    mid = modelled["per_conversation"]["mid"]
    for row in rows:
        assert row["per_day"] == pytest.approx(
            mid * row["conversations_per_day"], abs=0.01)
        assert row["per_month"] == pytest.approx(
            row["per_day"] * modelled["days_per_month"], abs=0.01)

    # The rung that matches the configured volume has to agree with the headline
    # to the cent. A table that contradicts the number above it costs more trust
    # than the table buys.
    configured = next(row for row in rows
                      if row["conversations_per_day"] == modelled["conversations_per_day"])
    assert configured["per_day"] == modelled["per_day"]["mid"]
    assert configured["per_month"] == modelled["per_month"]["mid"]


def test_ladder_and_pricing_are_operable_from_pricing_json(
        graph: dict, tmp_path, monkeypatch) -> None:
    """Both halves of the business view's honesty are inputs, not constants:
    the ladder is a presentation choice, and the placeholder warning has to be
    able to *disappear* — a warning that cannot be cleared is just noise."""
    monkeypatch.setattr(insights, "PRICING_FILE", tmp_path / "pricing.json")
    (tmp_path / "pricing.json").write_text(json.dumps({
        "source": "pricing.json",
        "assumptions": {"volume_ladder": [10, 20]},
    }))

    cost = insights.build_metrics([], graph)["cost"]
    counts = [row["conversations_per_day"]
              for row in cost["modelled"]["volume_sensitivity"]]
    # The supplied ladder, plus the configured volume it did not mention.
    assert counts == [10, 20, 100]
    assert cost["pricing_is_placeholder"] is False

    # ...and with no pricing.json at all, the warning is on.
    monkeypatch.setattr(insights, "PRICING_FILE", tmp_path / "absent.json")
    assert insights.build_metrics([], graph)["cost"]["pricing_is_placeholder"] is True


# ------------------------------------------------------------- the journey --- #

def test_journey_accounts_for_every_node_in_the_flow(graph: dict) -> None:
    """The client story is authored by hand, which is exactly why it needs this
    test: adding a node to `flow.py` must fail here until somebody decides how
    to describe it, rather than leaving a page that reads perfectly and
    describes software that no longer exists."""
    resolved = journey.build_journey(graph, {})
    assert resolved["coverage"]["uncovered_nodes"] == []
    assert resolved["coverage"]["covered"] == resolved["coverage"]["total"]
    assert resolved["coverage"]["total"] == len(graph["nodes"])


def test_journey_references_only_nodes_that_exist(graph: dict) -> None:
    resolved = journey.build_journey(graph, {})
    assert resolved["drift"]["missing_nodes"] == []
    assert resolved["drift"]["in_sync"] is True
    for step in resolved["steps"]:
        assert step["nodes"], f"{step['id']} resolves to nothing"


def test_a_deleted_node_makes_the_story_visibly_stale(graph: dict) -> None:
    """The behaviour that matters: the step is *not* dropped. Silently removing
    it would hide the drift, which is the failure this module exists to stop —
    a client story that reads perfectly and promises a check that is gone."""
    pruned = {**graph, "nodes": [n for n in graph["nodes"]
                                 if n["id"] != "readback_plate"]}
    resolved = journey.build_journey(pruned, {})

    step = next(s for s in resolved["steps"] if "readback_plate" in s["missing"])
    assert step["stale"] is True
    assert "readback_plate" not in step["nodes"]
    assert "readback_plate" in resolved["drift"]["missing_nodes"]
    assert resolved["drift"]["in_sync"] is False

    # Still renders, and still says what happens.
    assert step["what_happens"]
    assert len(resolved["steps"]) == len(journey.JOURNEY)


def test_an_undescribed_node_is_reported_not_ignored(graph: dict) -> None:
    """The other direction of drift: something new appears in the flow and the
    story has nothing to say about it."""
    grown = {**graph, "nodes": graph["nodes"] + [
        {"id": "some_new_node", "kind": "conversation"},
    ]}
    resolved = journey.build_journey(grown, {})
    assert resolved["coverage"]["uncovered_nodes"] == ["some_new_node"]
    assert resolved["drift"]["in_sync"] is False


def test_every_guarantee_is_enforced_by_something_running(graph: dict) -> None:
    """A promise with no live check behind it is a marketing claim, and this is
    the assertion that stops one from being made by accident."""
    resolved = journey.build_journey(graph, {})
    assert resolved["guarantees"]
    for guarantee in resolved["guarantees"]:
        assert guarantee["enforced_by"], (
            f"{guarantee['id']} promises something nothing enforces"
        )
        assert guarantee["stale"] is False


def test_the_story_is_written_in_plain_language(graph: dict) -> None:
    """Authoring this layer by hand is only worth it if it reads like English.
    A node id leaking into the prose means the translation was skipped."""
    resolved = journey.build_journey(graph, {})
    for step in resolved["steps"]:
        assert step["title"] and step["what_happens"]
        for text in (step["title"], step["what_happens"],
                     step["you_might_hear"] or ""):
            assert "_" not in text, f"node id leaked into {step['id']}: {text!r}"
            assert "{{" not in text, f"variable leaked into {step['id']}"
    for guarantee in resolved["guarantees"]:
        assert "_" not in guarantee["plain"]


def test_steps_are_ordered_and_typed(graph: dict) -> None:
    resolved = journey.build_journey(graph, {})
    assert [s["order"] for s in resolved["steps"]] == list(
        range(1, resolved["step_count"] + 1))
    assert all(s["path"] in {"always", "if_relevant"} for s in resolved["steps"])
    # Some calls reach a person; not all of them do.
    assert any(s["human"] for s in resolved["steps"])
    assert any(not s["human"] for s in resolved["steps"])


def test_step_stages_are_the_overlays_stages(graph: dict) -> None:
    """The client tab and the technical tab must not disagree about where the
    guardrails are, so the stages are read from the same overlay."""
    snapshot: dict = {}
    annotations.attach(snapshot, graph)
    resolved = journey.build_journey(graph, snapshot["annotations"])

    vehicle = next(s for s in resolved["steps"] if s["id"] == "the_vehicle")
    expected = sorted({
        snapshot["annotations"][node_id]["guardrail_stage"]
        for node_id in vehicle["nodes"]
    } - {None})
    assert vehicle["stages"] == expected
    assert vehicle["stages"] == ["Capture", "Read back", "Reconcile", "Validate"]


def test_journey_of_an_agent_with_no_flow_does_not_crash() -> None:
    """An agent on a single-prompt engine has no graph. The tab should say so,
    not throw."""
    resolved = journey.build_journey(None, None)
    assert resolved["step_count"] == len(journey.JOURNEY)
    assert resolved["drift"]["in_sync"] is False
    assert all(step["stale"] for step in resolved["steps"])
