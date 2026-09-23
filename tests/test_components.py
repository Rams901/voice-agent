"""The subflow contract.

The decomposition's risk is not in any single node — it is at the seams. A
component that cannot route to its parent, or that references a node outside
itself, breaks the flow in a way that no node-level test would notice and that a
graded conversation can still report as a pass.

That last point is not hypothetical. A shipped bug returned the wrong shape from
the outcome router, so every component exit fell through to its fallback and the
modular flow collapsed into handoff at the first opportunity. Three of four
graded journeys still reported *pass*, because their assertions were all about
things not happening. These tests exist because the seams need pinning
independently of whether a conversation went well.
"""

from __future__ import annotations

import re

import pytest

from frontdesk import components as C
from frontdesk.dealer_profiles import (FEATURE_VARIABLES, DEALERS,
                                       all_dealers)
from frontdesk.flow import build_nodes


@pytest.fixture(scope="module")
def nodes() -> list[dict]:
    return build_nodes(DEALERS["northgate-uk"])


@pytest.fixture(scope="module")
def component_ids() -> dict[str, str]:
    return {
        spec.name: (f"conversation_flow_component_{i}" if spec.scope == "shared"
                    else C.local_component_id(spec.name))
        for i, spec in enumerate(C.SUBFLOWS)
    }


# --------------------------------------------------------------------------- #
# The partition
# --------------------------------------------------------------------------- #

def test_every_node_belongs_to_exactly_one_place(nodes: list[dict]) -> None:
    """A node in no group is a node that vanishes; a node in two is ambiguous.
    Either way the deployed flow is not the flow anyone designed."""
    actual = {n["id"] for n in nodes}
    declared = set(C.ORCHESTRATOR_NODES) | C.all_component_node_ids()

    assert actual - declared == set(), "these would vanish on deploy"
    assert declared - actual == set(), "these are declared but do not exist"

    ownership: dict[str, list[str]] = {}
    for spec in C.SUBFLOWS:
        for nid in spec.nodes:
            ownership.setdefault(nid, []).append(spec.name)
    clash = {k: v for k, v in ownership.items() if len(v) > 1}
    assert not clash, f"owned by more than one component: {clash}"


def test_every_component_is_self_contained(nodes: list[dict]) -> None:
    """A component cannot reference nodes outside itself: it would not be
    portable, which is the only reason to make it a component."""
    for spec in C.SUBFLOWS:
        payload = C.build_subflow(spec, nodes, "https://example.test")
        inside = {n["id"] for n in payload["nodes"]}
        for node in payload["nodes"]:
            for edge in node.get("edges", []):
                assert edge["destination_node_id"] in inside, (
                    f"{spec.name}: {node['id']} -> "
                    f"{edge['destination_node_id']} is outside the component")
            for key in ("always_edge", "else_edge", "edge"):
                if key in node:
                    assert node[key]["destination_node_id"] in inside, (
                        f"{spec.name}: {node['id']}[{key}] points outside")


def test_the_orchestrator_never_points_into_a_component(nodes: list[dict],
                                                        component_ids) -> None:
    """The parent routes *between* components, never at a node inside one. An
    edge that reaches inside would skip the component's entry and start the
    subflow in the middle."""
    orchestrator = C.build_orchestrator(nodes, component_ids)
    ids = {n["id"] for n in orchestrator}
    internal = C.all_component_node_ids()

    for node in orchestrator:
        for edge in node.get("edges", []):
            destination = edge["destination_node_id"]
            assert destination in ids, f"{node['id']} -> {destination} dangles"
            assert destination not in internal, (
                f"{node['id']} reaches into a component: {destination}")
        for key in ("always_edge", "else_edge", "edge"):
            if key in node:
                destination = node[key]["destination_node_id"]
                assert destination in ids, f"{node['id']}[{key}] dangles"
                assert destination not in internal


def test_the_graph_is_closed_for_every_market(nodes: list[dict],
                                              component_ids) -> None:
    """The flow is built once and shared, so a market-specific branch that
    dead-ends would dead-end for all of them."""
    for dealer in all_dealers():
        market_nodes = build_nodes(dealer)
        orchestrator = C.build_orchestrator(market_nodes, component_ids)
        ids = {n["id"] for n in orchestrator}
        for node in orchestrator:
            for edge in node.get("edges", []):
                assert edge["destination_node_id"] in ids
            for key in ("always_edge", "else_edge", "edge"):
                if key in node:
                    assert node[key]["destination_node_id"] in ids


# --------------------------------------------------------------------------- #
# The outcome contract  (this is the one that shipped broken)
# --------------------------------------------------------------------------- #

def test_every_router_sets_its_variable_at_the_path_it_declares() -> None:
    """`response_variables` paths are resolved against the node's return value,
    and Retell's convention is that the payload sits under `data` — the same
    shape a tool returns.

    The bug this pins: the router returned `{ result: 'ok' }` while declaring
    `data.result`, so the path resolved to nothing, the parent's equation was
    false, and every `ok` exit silently fell through to the fallback. Nothing
    errored; the flow just took the wrong branch on every call.
    """
    for spec in C.SUBFLOWS:
        for outcome in spec.outcomes:
            router = C._outcome_router(f"r_{outcome}", spec.result_var,
                                       outcome, "exit")
            path = router["response_variables"][spec.result_var]
            code = router["code"]

            # The declared path must start at `data.` ...
            assert path.startswith("data."), path

            # ... and the returned object must actually contain that key, with
            # the outcome baked in as a literal.
            field = path.split(".", 1)[1]
            assert re.search(rf"data:\s*\{{[^}}]*\b{field}\b", code), (
                f"{spec.name}/{outcome}: code {code!r} does not produce {path!r}")
            assert f"'{outcome}'" in code


def test_a_router_returns_its_own_outcome_and_not_anothers() -> None:
    """A copy-paste that left the literal behind would route every exit to the
    same branch, which looks like a working flow until the wrong one fires."""
    for spec in C.SUBFLOWS:
        for outcome in spec.outcomes:
            router = C._outcome_router(f"r_{outcome}", spec.result_var,
                                       outcome, "exit")
            assert f"'{outcome}'" in router["code"]


def test_router_ids_cannot_collide_with_real_nodes(nodes: list[dict]) -> None:
    """Booking's router for `done` reads naturally as `booking_done` — which is
    also the id of the node that tells the caller the booking is confirmed.
    Retell rejects the whole flow ("Duplicate node id"), so this is pinned."""
    existing = {n["id"] for n in nodes}
    for spec in C.SUBFLOWS:
        for outcome in spec.outcomes:
            router = C._outcome_router(f"exit_{spec.name}_{outcome}",
                                       spec.result_var, outcome, "exit")
            assert router["id"] not in existing


def test_the_orchestrator_reads_each_outcome_with_an_equation() -> None:
    """The variable was set by code, so interpreting it with a model would add
    a judgement where the answer is already known."""
    for name, wiring in C.ORCHESTRATOR_WIRING.items():
        spec = C.BY_NAME[name]
        for outcome in wiring:
            edge = C.result_edge(spec, outcome, "somewhere")
            condition = edge["transition_condition"]
            assert condition["type"] == "equation"
            assert condition["equations"] == [{
                "left": f"{{{{{spec.result_var}}}}}",
                "operator": "==",
                "right": outcome,
            }]


def test_every_component_outcome_is_wired_somewhere() -> None:
    """An outcome with no wiring can be produced but never handled, so the
    caller is left at the end of a subflow with nowhere to go."""
    for spec in C.SUBFLOWS:
        if not spec.result_var:
            continue
        wiring = C.ORCHESTRATOR_WIRING.get(spec.name, {})
        missing = set(spec.outcomes) - set(wiring)
        assert not missing, f"{spec.name} produces {missing} but nobody routes it"


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #

def test_every_optional_skill_has_a_gate_in_front_of_it() -> None:
    """A skill that can be switched off needs somewhere to send the caller when
    it is off, and the switch has to be reachable from every route in.

    The two vocabularies differ (`rating` is the feature, `satisfaction` is the
    subflow), so this asserts the mapping exists for every feature rather than
    assuming the names line up.
    """
    for name, variable in FEATURE_VARIABLES.items():
        subflow = C.FEATURE_SUBFLOWS.get(name)
        assert subflow, f"{name} can be switched off with no subflow mapped"
        assert variable.startswith("feature_")
        # Booking's switch is folded into its existing capability check.
        if name == "booking":
            continue
        assert subflow in C.GATE_FOR, f"{name} -> {subflow} has no gate"


def test_every_gate_maps_back_to_a_real_feature() -> None:
    """The reverse direction. A gate on a subflow no feature controls could
    never be switched off, so it would be a branch nobody can reach."""
    for subflow in C.GATE_FOR:
        assert subflow in C.FEATURE_SUBFLOWS.values(), subflow
    for subflow, gate in C.GATE_FOR.items():
        feature = next(f for f, s in C.FEATURE_SUBFLOWS.items() if s == subflow)
        assert gate.variable == FEATURE_VARIABLES[feature]


def test_a_switched_off_skill_still_sends_the_caller_somewhere() -> None:
    """"We don't do that here" followed by silence is worse than the skill being
    missing. Every gate that can be off must offer a real next step unless there
    is genuinely nothing to say."""
    for gate in C.GATES:
        if gate.text:
            assert gate.off_to not in C.all_component_node_ids()
            # The alternative must lead somewhere, not sit as a dead end.
            node = C._unavailable_node(gate)
            assert node["always_edge"]["destination_node_id"]


def test_the_rating_gate_has_nothing_to_say_and_says_nothing() -> None:
    """The one gate with no alternative: when the rating is off there is no
    question to ask, so it must route straight on rather than inventing a
    sentence about a feature the caller never asked for."""
    gate = C.GATE_FOR["satisfaction"]
    assert gate.text is None
    assert gate.off_to == "wrap_up"


def test_booking_checks_capability_and_choice_at_the_same_branch(nodes: list[dict],
                                                                 component_ids) -> None:
    """Booking already had a capability check, so its feature switch is folded
    into that equation rather than added as a second branch the caller would
    walk through."""
    orchestrator = C.build_orchestrator(nodes, component_ids)
    gate = next(n for n in orchestrator if n["id"] == "check_booking_capability")
    equations = gate["edges"][0]["transition_condition"]["equations"]
    lefts = {e["left"] for e in equations}

    assert "{{dealer_can_book}}" in lefts
    assert "{{feature_booking}}" in lefts
    assert gate["edges"][0]["transition_condition"]["operator"] == "&&"


def test_every_route_into_a_skill_passes_its_gate(nodes: list[dict],
                                                  component_ids) -> None:
    """The switch is only real if it cannot be walked around. Every edge that
    would land on a gated skill — the menu, or a finished booking falling
    through to rental — must arrive at the gate instead."""
    orchestrator = C.build_orchestrator(nodes, component_ids)
    gated = {f"subflow_{name}" for name in C.GATE_FOR}
    gated |= {f"subflow_{n}" for n in ("booking",)}

    for node in orchestrator:
        destinations = [e["destination_node_id"] for e in node.get("edges", [])]
        for key in ("always_edge", "else_edge", "edge"):
            if key in node:
                destinations.append(node[key]["destination_node_id"])
        for destination in destinations:
            if destination in gated and node["id"].startswith("gate_"):
                continue
            assert destination not in {f"subflow_{n}" for n in C.GATE_FOR} or \
                node["id"].startswith("gate_"), (
                f"{node['id']} bypasses the gate for {destination}")
