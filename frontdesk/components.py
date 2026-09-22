"""Subflows: the decomposition into reusable components.

Retell gives us two kinds of subflow, and the difference matters commercially as
much as technically:

    local   embedded in one flow, versioned with it, no cross-flow sync
    shared  a stand-alone library component, reusable across flows and agents

Verified against the live API (see `scripts/spike_component*.py`), including two
limits that shaped this design and are not obvious from the schema:

1. **A shared component must carry its own `tools`.** Creating one whose function
   nodes referenced tools it did not declare failed with
   `Tool id does not exist: tool_validate_identifier`. That is Retell being
   right: a component that borrowed tools from whatever flow hosted it would not
   be portable, which is the entire point of a library subflow.

2. **Components cannot nest.** `"Nested components are not supported"`.

   This one is structural, and it changed the decomposition. The obvious
   instinct is for `booking` to contain `plate_capture`, since that is the order
   they run in. But it means a skill can never reuse a shared component
   internally — so `plate_capture` cannot live inside `booking`, and the
   orchestrator has to chain them side by side instead::

       services_intro -> plate_capture -> booking -> satisfaction

   The payoff is that reuse becomes an *orchestrator* decision. Today rental
   does not capture a plate. Giving it one is a single edge, not a copy of seven
   nodes — which is the whole reason for doing this.

Outcome contract
----------------

A component has one exit ("Exit Subflow"), but the parent needs to know *how* it
finished, so components communicate through a **dynamic variable** the parent
routes on with equation edges:

    plate_capture  ->  plate_result  in {ok, failed}
    booking        ->  booking_result in {done, wants_rental, failed}
    rental         ->  rental_result in {done, declined}
    billing        ->  billing_result in {handoff}
    handoff        ->  handoff_result in {callback}
    satisfaction   ->  satisfaction_result in {more, done}

Each outcome is written by a `code` node — JavaScript in Retell's sandbox with
`response_variables` lifting the return value into a variable. That choice is
deliberate: a tool call would need the network, and an
`extract_dynamic_variables` node would need an LLM. The component's contract with
its parent is a fact about control flow, so it is set deterministically.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field
from typing import Any

from .tools import build_tools, tool_id


def local_component_id(name: str) -> str:
    """A stable id for an embedded (local) component.

    Retell wants its own format here — `cf-component-<digits>`, as the dashboard
    writes it — and rejects anything else with "invalid local component id".
    Deriving it from the name rather than using a timestamp is deliberate: the
    id appears twice in a deploy, once on the component and once on the
    ComponentNode that calls it, and a value that changed per deploy would make
    every update look like a brand-new component.
    """
    digest = hashlib.sha1(name.encode()).hexdigest()
    return f"cf-component-{int(digest[:12], 16) % 10**13:013d}"


@dataclass(frozen=True)
class Subflow:
    """One component: which nodes it owns, and how it hands control back.

    `outcomes` is the boundary wiring, and it is the whole trick. Each entry maps
    an outcome name to the set of *destination node ids that currently live
    outside this component*. Any edge found pointing at one of those is
    redirected to that outcome's router, so the component's internal graph stays
    exactly as authored while its exits become explicit.
    """

    name: str
    scope: str                      # "shared" | "local"
    entry: str
    nodes: tuple[str, ...]
    #: Names of tools this component's function nodes call. Only needed for
    #: `shared` components, which must carry them (see limit 1 above).
    tools: tuple[str, ...] = ()
    #: Variable the parent reads. Empty means the component is terminal — it
    #: either transfers or ends the call, so there is no continuation to route.
    result_var: str = ""
    #: outcome name -> destinations that are outside this component
    outcomes: dict[str, tuple[str, ...]] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# The decomposition
# --------------------------------------------------------------------------- #
#
# Grouped by *why* a thing is shared, which is not the same as where it sits:
#
#   shared  cross-cutting machinery every skill needs, identical in every market
#   local   a skill whose nodes are shaped by the market's services and rules
#
# `satisfaction` is local-shaped by every instinct (it is the ending, and endings
# differ) but nothing in it is market-specific, and every happy path needs it.
# `billing` looks shared because it has one node and no tools, but its content is
# a policy statement about what this dealership will and will not do over the
# phone — that is exactly the thing that differs by market.

SUBFLOWS: tuple[Subflow, ...] = (
    Subflow(
        name="plate_capture",
        scope="shared",
        entry="capture_plate",
        nodes=("capture_plate", "validate_plate", "plate_repair", "readback_plate",
               "lookup_vehicle", "disambiguate_vehicle", "vehicle_not_found"),
        tools=("validate_identifier", "lookup_vehicle"),
        result_var="plate_result",
        # `capture_service` is the booking skill's first node: the plate is
        # resolved and the skill carries on. `flag_and_handoff` is the escape
        # hatch when the caller cannot get past the plate at all.
        outcomes={
            "ok": ("capture_service",),
            "failed": ("flag_and_handoff",),
        },
    ),

    Subflow(
        name="handoff",
        scope="shared",
        entry="flag_and_handoff",
        nodes=("flag_and_handoff", "handoff", "escalation_fallback"),
        tools=("flag_conversation",),
        result_var="handoff_result",
        # A successful cold transfer ends the call, so the only path that
        # returns to the parent is a *failed* one — which is why `wrap_up` is the
        # single outcome rather than a "transferred" one nobody would receive.
        outcomes={"callback": ("wrap_up",)},
    ),

    Subflow(
        name="satisfaction",
        scope="shared",
        entry="satisfaction",
        nodes=("satisfaction", "feedback_log"),
        tools=("record_feedback",),
        result_var="satisfaction_result",
        outcomes={
            "more": ("fallback_help",),   # caller raised something else
            "done": ("wrap_up",),
        },
    ),

    Subflow(
        name="booking",
        scope="local",
        entry="capture_service",
        nodes=("capture_service", "capture_timing", "check_availability", "no_slots",
               "offer_slots", "confirm_booking", "create_appointment",
               "booking_pending", "verify_write", "booking_done", "booking_failed"),
        tools=("check_availability", "create_appointment", "verify_appointment"),
        result_var="booking_result",
        outcomes={
            # `rental_intro` is the rental skill's entry, so this outcome is
            # what makes "book, then arrange a car" work across the boundary.
            "wants_rental": ("rental_intro",),
            "done": ("satisfaction",),
            # The deferred-write ending (`booking_pending`) finishes via wrap-up
            # rather than the rating question. It needs its *own* outcome, not a
            # second destination on "done": one outcome maps to one variable
            # value, so two destinations would make the parent unable to tell
            # them apart and the router would silently pick the first edge.
            "pending": ("wrap_up",),
            "failed": ("flag_and_handoff",),
        },
    ),

    Subflow(
        name="rental",
        scope="local",
        entry="rental_intro",
        nodes=("rental_intro", "rental_capture", "rental_log", "rental_done"),
        tools=("flag_conversation",),
        result_var="rental_result",
        outcomes={
            "done": ("satisfaction",),
            "declined": ("wrap_up",),
        },
    ),

    Subflow(
        name="billing",
        scope="local",
        entry="billing_intro",
        nodes=("billing_intro",),
        result_var="billing_result",
        outcomes={"handoff": ("flag_and_handoff",)},
    ),
)

BY_NAME: dict[str, Subflow] = {s.name: s for s in SUBFLOWS}

#: Nodes the orchestrator keeps. Everything else must belong to a subflow, or it
#: would vanish from the deployed flow — which is the failure this constant is
#: checked against in the tests rather than discovered in production.
ORCHESTRATOR_NODES: tuple[str, ...] = (
    "consent_gate", "greet_consent", "greet_open", "consent_retry",
    "consent_refused", "services_intro", "fallback_help",
    "check_booking_capability", "booking_unavailable",
    "wrap_up", "end_call",
)


def all_component_node_ids() -> set[str]:
    return {nid for spec in SUBFLOWS for nid in spec.nodes}


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #

def _outcome_router(node_id: str, var: str, outcome: str, exit_id: str) -> dict[str, Any]:
    """A deterministic marker for one outcome.

    `wait_for_result` is required by the schema. There is no instruction, so this
    node is silent — it exists to set a variable and leave.
    """
    return {
        "id": node_id,
        "type": "code",
        "name": f"Exit: {outcome}",
        "code": f"return {{ result: '{outcome}' }};",
        "wait_for_result": True,
        "response_variables": {var: "data.result"},
        "always_edge": {
            "id": f"{node_id}_exit",
            "destination_node_id": exit_id,
            "transition_condition": {"type": "prompt", "prompt": "Always"},
        },
    }


def build_subflow(spec: Subflow, nodes: list[dict[str, Any]],
                  base_url: str) -> dict[str, Any]:
    """Turn one `Subflow` spec into a component payload for the Retell API."""
    by_id = {n["id"]: n for n in nodes}
    exit_id = f"{spec.name}_exit"
    # Prefixed with `exit_` and not `{spec.name}_{outcome}`, which reads more
    # naturally but collides with real nodes: booking's router for "done" would
    # be `booking_done`, the same id as the node that says the booking is
    # confirmed. Retell rejects the whole flow with "Duplicate node id:
    # booking_done" — and the same trap waits on `booking_pending`,
    # `booking_failed` and `rental_done`.
    router_for = {outcome: f"exit_{spec.name}_{outcome}" for outcome in spec.outcomes}
    terminal = {dest for dests in spec.outcomes.values() for dest in dests}

    picked: list[dict[str, Any]] = []
    for node_id in spec.nodes:
        node = copy.deepcopy(by_id[node_id])

        # Redirect every boundary edge to its outcome router. Edges *within* the
        # component are left alone, so the authored graph survives the move.
        for edge in node.get("edges", []):
            dest = edge["destination_node_id"]
            for outcome, dests in spec.outcomes.items():
                if dest in dests:
                    edge["destination_node_id"] = router_for[outcome]
        for key in ("always_edge", "else_edge", "edge"):
            if key in node:
                dest = node[key]["destination_node_id"]
                for outcome, dests in spec.outcomes.items():
                    if dest in dests:
                        node[key]["destination_node_id"] = router_for[outcome]

        picked.append(node)

    if spec.result_var:
        for outcome in spec.outcomes:
            picked.append(_outcome_router(router_for[outcome], spec.result_var,
                                          outcome, exit_id))
        picked.append({"id": exit_id, "type": "end", "name": "Exit Subflow"})

    payload: dict[str, Any] = {
        "name": spec.name,
        "nodes": picked,
        "start_node_id": spec.entry,
    }

    if spec.scope == "local":
        payload["conversation_flow_component_id"] = local_component_id(spec.name)

    # Every component carries the tools its own function nodes call — declared
    # for local components too, not just shared ones. A shared component refuses
    # to deploy without them ("Tool id does not exist"), and there is no reason
    # to assume a local one resolves them from the host flow; being explicit
    # costs nothing and removes the question.
    if spec.tools:
        built = {t["name"]: t for t in build_tools(base_url)}
        tools = []
        for name in spec.tools:
            tool = dict(built[name])
            tool["tool_id"] = tool_id(name)
            tools.append(tool)
        payload["tools"] = tools

    return payload


def component_node(spec: Subflow, component_id: str, *, edges: list[dict[str, Any]],
                   else_to: str, name: str | None = None) -> dict[str, Any]:
    """A `component` node for the orchestrator, wiring the outcomes onwards.

    `component_type` is `shared` or `local` and must match where the component
    actually lives, or the reference resolves to nothing at deploy time.
    """
    return {
        "id": component_node_id(spec.name),
        "type": "component",
        "name": name or spec.name.replace("_", " ").capitalize(),
        "component_id": component_id,
        "component_type": spec.scope,
        "edges": edges,
        "else_edge": {
            "id": f"{spec.name}_else",
            "destination_node_id": else_to,
            "transition_condition": {"type": "prompt", "prompt": "Else"},
        },
    }


def result_edge(spec: Subflow, outcome: str, destination: str,
                edge_id: str | None = None) -> dict[str, Any]:
    """An equation edge in the orchestrator, reading a component's outcome.

    Equation, not prompt condition, for the same reason the consent gate is: the
    variable was set by code, so asking a model to interpret it would be adding a
    judgement where a fact already exists.
    """
    return {
        "id": edge_id or f"{spec.name}_{outcome}",
        "destination_node_id": destination,
        "transition_condition": {
            "type": "equation",
            "operator": "&&",
            "equations": [{"left": f"{{{{{spec.result_var}}}}}",
                           "operator": "==", "right": outcome}],
        },
    }


# --------------------------------------------------------------------------- #
# The orchestrator
# --------------------------------------------------------------------------- #

#: Where each component is wired onwards. This table *is* the architecture: it
#: is the only place that says which skill can follow which, and reusing a
#: subflow in a new skill is a change here rather than a copy of its nodes.
#:
#: Reading it top to bottom: a resolved plate continues into booking; a booking
#: that wants a car continues into rental; anything that finishes well is offered
#: the rating question; and everything that cannot be resolved falls to handoff.
ORCHESTRATOR_WIRING: dict[str, dict[str, str]] = {
    "plate_capture": {"ok": "booking", "failed": "handoff"},
    "booking": {"done": "satisfaction", "wants_rental": "rental",
                "pending": "wrap_up", "failed": "handoff"},
    "rental": {"done": "satisfaction", "declined": "wrap_up"},
    "billing": {"handoff": "handoff"},
    "handoff": {"callback": "wrap_up"},
    "satisfaction": {"more": "fallback_help", "done": "wrap_up"},
}

#: Fallback when a component exits with a value the wiring does not name. Always
#: the safe direction: an unrecognised outcome escalates rather than continuing
#: as though something succeeded.
ORCHESTRATOR_DEFAULT: dict[str, str] = {
    "plate_capture": "handoff",
    "booking": "handoff",
    "rental": "wrap_up",
    "billing": "handoff",
    "handoff": "wrap_up",
    "satisfaction": "wrap_up",
}

#: Node id -> the component that now owns it. An orchestrator edge pointing at
#: one of these is an edge *into a subflow*, and is redirected to the component
#: node so the reference resolves.
ENTRY_OWNER: dict[str, str] = {s.entry: s.name for s in SUBFLOWS}


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #
#
# A skill being switched off must not be a dead end. "We don't do that here" is
# a worse call than "here is what I can do", so every gate that can be off has an
# alternative attached, and the alternative is a real next step rather than an
# apology followed by silence.
#
# The rating question is the one exception: when it is off there is nothing to
# say and nothing to offer, so the gate routes straight on rather than inventing
# a sentence about it.

@dataclass(frozen=True)
class Gate:
    """A capability switch in front of a subflow."""

    subflow: str
    variable: str
    #: Where to go when the skill is off. With `text`, this is a new conversation
    #: node built from it; without, it names a node that already exists.
    off_to: str
    text: str | None = None
    name: str = ""


GATES: tuple[Gate, ...] = (
    Gate(
        subflow="rental",
        variable="feature_rental",
        off_to="rental_unavailable",
        name="Hire car not offered",
        text=(
            "Say honestly that you cannot arrange a hire or courtesy car at this "
            "dealership. Do not imply one might be available or that someone "
            "could sort it out. Offer the two things you can actually do: get "
            "the service booked so the team can talk to the customer about "
            "getting them moving, or take a number and have the service team "
            "call back. Do not take the rental details down."
        ),
    ),
    Gate(
        subflow="billing",
        variable="feature_billing",
        off_to="billing_unavailable",
        name="Billing not handled",
        text=(
            "Say plainly that you cannot look at invoices or payments over the "
            "phone. Never guess at an amount, a balance or a refund, and never "
            "say a payment has or has not gone through. Offer to put the caller "
            "through to the accounts team, or take their number and have them "
            "called back."
        ),
    ),
    Gate(
        subflow="satisfaction",
        variable="feature_rating",
        off_to="wrap_up",
        text=None,
    ),
)

#: Subflow name -> its gate. Booking is deliberately absent: it already had a
#: capability branch (`check_booking_capability`), so its gate is folded into
#: that branch's equation rather than duplicated as a second node the caller
#: would pass through.
GATE_FOR: dict[str, Gate] = {g.subflow: g for g in GATES}


def _gate_node(gate: Gate) -> dict[str, Any]:
    """A branch that reads one feature variable.

    Equation, not a prompt condition: the variable is set by configuration, so
    asking a model to weigh it would add a judgement where the deployment already
    knows the answer.
    """
    return {
        "id": f"gate_{gate.subflow}",
        "type": "branch",
        "name": f"{gate.subflow.capitalize()} offered here?",
        "edges": [{
            "id": f"gate_{gate.subflow}_on",
            "destination_node_id": component_node_id(gate.subflow),
            "transition_condition": {
                "type": "equation",
                "operator": "&&",
                "equations": [{"left": f"{{{{{gate.variable}}}}}",
                               "operator": "==", "right": "true"}],
            },
        }],
        "else_edge": {
            "id": f"gate_{gate.subflow}_off",
            "destination_node_id": _resolve(gate.off_to),
            "transition_condition": {"type": "prompt", "prompt": "Else"},
        },
    }


def _unavailable_node(gate: Gate) -> dict[str, Any]:
    """What the caller hears when a skill is off, and where it leads."""
    return {
        "id": gate.off_to,
        "type": "conversation",
        "name": gate.name,
        "instruction": {"type": "prompt", "text": gate.text},
        "skippable": False,
        # Always out to a human: the skill is missing, so the honest end to the
        # turn is a person, not the assistant trying another tack.
        "always_edge": {
            "id": f"{gate.off_to}_handoff",
            "destination_node_id": "subflow_handoff",
            "transition_condition": {"type": "prompt", "prompt": "Always"},
        },
    }


def _apply_booking_gate(nodes: list[dict[str, Any]]) -> None:
    """Fold `feature_booking` into the branch booking already had.

    Booking reached its gate by a different route — a capability check that
    predates the feature flags — so extending that equation keeps one branch per
    skill instead of two in a row, and keeps the capability and the choice
    visibly checked at the same point.
    """
    for node in nodes:
        if node.get("id") != "check_booking_capability":
            continue
        for edge in node.get("edges", []):
            condition = edge.get("transition_condition", {})
            if condition.get("type") != "equation":
                continue
            already = {e["left"] for e in condition.get("equations", [])}
            if "{{feature_booking}}" not in already:
                condition.setdefault("equations", []).append(
                    {"left": "{{feature_booking}}", "operator": "==",
                     "right": "true"})
                condition["operator"] = "&&"


def component_node_id(name: str) -> str:
    """The id of a component node in the orchestrator.

    Prefixed because two subflows share their name with a leaf node inside them —
    `handoff` and `satisfaction` are each both a component and a node within it.
    Retell keeps those in separate namespaces so this is not a deploy problem,
    but the graph in `snapshot.py` merges them into one namespace when it draws
    the diagram, and a collision there would silently connect the wrong nodes.
    """
    return f"subflow_{name}"


def _resolve(destination: str) -> str:
    """An orchestrator destination is either a component or a plain node."""
    return component_node_id(destination) if destination in BY_NAME else destination


def build_orchestrator(nodes: list[dict[str, Any]],
                       component_ids: dict[str, str]) -> list[dict[str, Any]]:
    """The main flow: front door and routing, with each skill as one node.

    The skill nodes themselves are gone from here — they live in their components
    — so this reads as the shape of the conversation rather than its contents.
    """
    by_id = {n["id"]: n for n in nodes}
    picked = [copy.deepcopy(by_id[nid]) for nid in ORCHESTRATOR_NODES]

    # An edge into a component's entry node becomes an edge into that skill's
    # gate when it has one, and into the component itself when it does not. This
    # is what makes the switch reachable: every route into a skill — the menu,
    # a failed booking falling through to rental — passes the gate, so a market
    # cannot arrive at a skill it does not offer via some other path.
    def enter(target: str) -> str:
        owner = ENTRY_OWNER.get(target, target)
        return (f"gate_{owner}" if owner in GATE_FOR
                else _resolve(owner))

    for node in picked:
        for edge in node.get("edges", []):
            edge["destination_node_id"] = enter(edge["destination_node_id"])
        for key in ("always_edge", "else_edge", "edge"):
            if key in node:
                node[key]["destination_node_id"] = enter(
                    node[key]["destination_node_id"])

    for spec in SUBFLOWS:
        wiring = ORCHESTRATOR_WIRING[spec.name]
        edges = [
            result_edge(spec, outcome, enter(destination))
            for outcome, destination in wiring.items()
        ]
        picked.append(component_node(
            spec, component_ids[spec.name], edges=edges,
            else_to=enter(ORCHESTRATOR_DEFAULT[spec.name]),
        ))

    for gate in GATES:
        picked.append(_gate_node(gate))
        if gate.text:
            picked.append(_unavailable_node(gate))

    _apply_booking_gate(picked)
    return picked
