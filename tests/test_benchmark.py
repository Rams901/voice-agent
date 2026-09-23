"""The benchmark's own guardrails.

A benchmark produces a number that someone will make a decision from, so the
things worth testing are not the arithmetic — they are the ways it could quietly
lie:

    * spending more than the cap
    * measuring one architecture and calling it two
    * reporting a call that never connected as a fast, cheap success
    * a journey with no metrics, which would pass without being checked

These are the failures that produce a confident, wrong report rather than an
error, which is why they are pinned here.
"""

from __future__ import annotations

import pytest

from frontdesk.benchmark import (ARCHITECTURES, CHAT_RATE_USD, GRADING_UNIT_USD,
                                 Budget, BudgetExceeded, call_stats,
                                 estimate_case_cost, measure_case_cost,
                                 resolve_architectures, transcripts_from_run)
from frontdesk.scenarios import (BENCHMARK_ARMS, BENCHMARK_JOURNEYS,
                                 SCENARIOS, benchmark_journeys)
from frontdesk.tools import build_tools


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #

def test_the_budget_refuses_work_it_cannot_afford() -> None:
    """A cap that only warns is not a cap. The check must stop the run."""
    budget = Budget(cap_usd=1.00, label="test")
    budget.charge(0.80, "first run", measured=False)

    with pytest.raises(BudgetExceeded) as exc:
        budget.afford(0.50, "next run")

    assert "0.20" in str(exc.value), "the message must say what is actually left"
    assert "test" in str(exc.value)


def test_the_budget_allows_work_that_exactly_fits() -> None:
    """The boundary must not be off by a float epsilon, or the last case in a
    suite refuses to run for no visible reason."""
    budget = Budget(cap_usd=2.00, label="test")
    budget.charge(1.50, "first", measured=False)
    budget.afford(0.50, "second")  # must not raise


def test_every_charge_is_recorded_with_its_provenance() -> None:
    """Estimated and measured costs sit in the same total, so which is which has
    to survive into the report."""
    budget = Budget(cap_usd=5.00)
    budget.charge(0.25, "batch", measured=False)
    budget.charge(0.31, "call", measured=True)

    assert [e["measured"] for e in budget.entries] == [False, True]
    assert budget.spent_usd == pytest.approx(0.56)


# --------------------------------------------------------------------------- #
# Cost model
# --------------------------------------------------------------------------- #

def test_grading_is_paid_for_even_when_a_conversation_is_empty() -> None:
    """Every case carries a grading unit whether or not the model said anything,
    so a transcript with no messages must not cost nothing."""
    assert measure_case_cost([]) == pytest.approx(GRADING_UNIT_USD)


def test_cost_counts_each_side_at_its_own_rate() -> None:
    """The agent and the simulated caller run different models at different
    rates, so a flat per-message count would misprice every case."""
    messages = [{"role": "agent"}] * 3 + [{"role": "user"}] * 2
    expected = 3 * CHAT_RATE_USD["gpt-4.1"] + 2 * CHAT_RATE_USD["gpt-4.1-mini"] \
        + GRADING_UNIT_USD

    assert measure_case_cost(messages) == pytest.approx(expected)


def test_a_longer_conversation_costs_more() -> None:
    short = measure_case_cost([{"role": "agent"}] * 2)
    long = measure_case_cost([{"role": "agent"}] * 20)
    assert long > short


def test_the_preflight_estimate_is_not_optimistic() -> None:
    """`estimate_case_cost` decides whether to start, so under-estimating is the
    dangerous direction: it would let a run through that then overspends."""
    journey = benchmark_journeys("batch")[0]
    realistic = measure_case_cost([{"role": "agent"}] * 12
                                  + [{"role": "user"}] * 12)
    assert estimate_case_cost(journey) >= realistic * 0.9


# --------------------------------------------------------------------------- #
# The journey set
# --------------------------------------------------------------------------- #

def test_every_benchmark_journey_has_a_unique_slug() -> None:
    slugs = [j["slug"] for j in benchmark_journeys()]
    assert len(slugs) == len(set(slugs))


def test_every_benchmark_journey_has_an_arm() -> None:
    """A journey with no arm is silently never run, and the report looks fine."""
    for journey in benchmark_journeys():
        assert journey["slug"] in BENCHMARK_ARMS, journey["slug"]


def test_every_benchmark_journey_is_actually_gradeable() -> None:
    """A journey with no metrics would be recorded as a pass without anything
    having been checked — the most flattering possible bug."""
    for journey in benchmark_journeys():
        assert journey.get("user_prompt"), journey["slug"]
        assert len(journey.get("metrics", [])) >= 3, journey["slug"]


def test_benchmark_mocks_only_name_real_tools() -> None:
    """A misspelled tool name makes the mock silently inert, so the run falls
    through to the live tool and the results stop being reproducible."""
    known = {t["name"] for t in build_tools("https://example.test")}
    for journey in benchmark_journeys():
        for mock in journey.get("tool_mocks", []):
            assert mock["tool_name"] in known, (journey["slug"], mock["tool_name"])


def test_benchmark_mocks_are_well_formed() -> None:
    for journey in benchmark_journeys():
        for mock in journey.get("tool_mocks", []):
            assert mock["input_match_rule"]["type"] in {"any", "partial_match"}
            assert isinstance(mock["output"], str)


def test_the_batch_arm_covers_each_architecture_sensitive_behaviour() -> None:
    """The whole reason for adding journeys rather than reusing the existing
    suite: each of these is a way the decomposition could break, and none of
    them is exercised by the original scenarios."""
    slugs = {j["slug"] for j in benchmark_journeys("batch")}

    assert "async-pending" in slugs, "the third booking outcome is untested"
    assert "gate-booking-off" in slugs, "a switched-off skill is untested"
    assert "plate-handoff" in slugs, "the plate->handoff boundary is untested"
    assert "rating-declined" in slugs, "declining the rating is untested"


def test_the_voice_arm_includes_a_boundary_crossing_journey() -> None:
    """Latency is only interesting where structure differs, which is a call that
    traverses more than one subflow."""
    assert "multi-skill" in {j["slug"] for j in benchmark_journeys("voice")}


def test_the_gate_journey_injects_the_variable_both_architectures_read() -> None:
    """`feature_booking` is read only by the modular flow, so benchmarking on it
    would show the monolith "failing" to do something it was never asked to do.
    `dealer_can_book` is read by both, so it measures the same behaviour."""
    journey = next(j for j in benchmark_journeys("batch")
                   if j["slug"] == "gate-booking-off")
    assert journey["dynamic_variables"] == {"dealer_can_book": "false"}


def test_the_control_journey_is_the_original_happy_path() -> None:
    """The control has to be the same conversation in both arms; a hand-written
    second copy would compare the copies."""
    control = next(j for j in benchmark_journeys() if j["slug"] == "happy-control")
    assert control["user_prompt"] == SCENARIOS[0]["user_prompt"]


def test_the_regression_suite_is_unchanged_in_size_and_extended_separately() -> None:
    """A benchmark journey that quietly joined the regression suite would make
    `simulate` more expensive every time someone ran it."""
    assert len(SCENARIOS) == 11
    assert len(BENCHMARK_JOURNEYS) == 5  # +1 control borrowed from SCENARIOS


# --------------------------------------------------------------------------- #
# Architecture resolution
# --------------------------------------------------------------------------- #

class _FakeClient:
    def __init__(self, name: str) -> None:
        self._name = name

    def get_agent(self, agent_id: str) -> dict[str, str]:
        return {"agent_id": agent_id, "agent_name": self._name}


def test_the_two_architectures_have_different_agents_and_flows() -> None:
    """Comparing an agent with itself would report a perfect, meaningless
    result, so the ids must not be able to collapse silently."""
    agent_ids = {s["agent_id"] for s in ARCHITECTURES.values()}
    flow_ids = {s["flow_id"] for s in ARCHITECTURES.values()}
    assert len(agent_ids) == 2
    assert len(flow_ids) == 2


def test_resolving_refuses_an_agent_that_is_not_what_it_claims() -> None:
    with pytest.raises(SystemExit) as exc:
        resolve_architectures(_FakeClient("some other agent"), "modular")
    assert "meaningless" in str(exc.value)


def test_resolving_accepts_the_expected_names() -> None:
    resolved = resolve_architectures(
        _FakeClient("Frontdesk — Northgate Motors (sub-flows agents)"), "modular")
    assert resolved["modular"]["agent_name"].endswith("(sub-flows agents)")


# --------------------------------------------------------------------------- #
# Reading results
# --------------------------------------------------------------------------- #

def test_a_call_that_never_connected_is_not_a_data_point() -> None:
    """The account already has one of these. Counting it would report a zero-cost,
    zero-latency call and drag every average towards a success that never
    happened."""
    assert call_stats("modular", {
        "call_status": "not_connected",
        "disconnection_reason": "error_user_not_joined",
        "call_cost": {"combined_cost": 0},
    }) is None


def test_call_stats_reads_cost_in_cents_and_latency_in_ms() -> None:
    """`combined_cost` is cents and `p50` is milliseconds. Mixing the two up
    would report a 30-cent call as $30."""
    stats = call_stats("monolith", {
        "call_id": "call_x",
        "call_status": "ended",
        "duration_ms": 120000,
        "call_cost": {"combined_cost": 27.07},
        "latency": {"llm": {"p50": 538, "num": 7}, "e2e": {"p50": 1887}},
        "transcript_object": [{"role": "agent"}, {"role": "user"},
                              {"role": "agent"}],
        "metadata": {"benchmark_journey": "happy-control"},
    })

    assert stats is not None
    assert stats.cost_usd == pytest.approx(0.2707)
    assert stats.duration_s == pytest.approx(120.0)
    assert stats.llm_p50_ms == 538
    assert stats.e2e_p50_ms == 1887
    assert stats.llm_turns == 7
    assert stats.agent_turns == 2
    assert stats.journey == "happy-control"


def test_transcripts_are_found_whatever_shape_the_snapshot_arrives_in() -> None:
    """The API documents this as "either X or Y". Handling one of them would
    cost a message count and therefore a cost figure, silently."""
    assert transcripts_from_run(
        {"transcript_snapshot": {"transcript_object": [{"role": "agent"}]}}
    ) == [{"role": "agent"}]
    assert transcripts_from_run(
        {"transcript_snapshot": {"transcript": [{"role": "user"}]}}
    ) == [{"role": "user"}]
    assert transcripts_from_run({}) == []
    assert transcripts_from_run({"transcript_snapshot": None}) == []


def test_an_untagged_call_contributes_no_journey() -> None:
    """Cost only means something per journey, so an unlabelled call must not
    borrow the name of one."""
    stats = call_stats("modular", {
        "call_id": "call_y", "call_status": "ended", "duration_ms": 1000,
        "call_cost": {"combined_cost": 5}, "latency": {}, "transcript_object": [],
        "metadata": {},
    })
    assert stats is not None
    assert stats.journey == ""
