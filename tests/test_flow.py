"""Structural tests for the generated conversation flow.

These check the properties that make the redesign work, rather than the wording
of any particular prompt. They exist because the failure modes that matter here
are structural: a node that can strand a caller, a gate that isn't deterministic,
a function node pointing at a tool that doesn't exist.
"""

from __future__ import annotations

import pytest

from frontdesk.dealer_profiles import DEALERS, get_dealer
from frontdesk.flow import build_flow_payload, build_nodes, estimate_prompt_tokens
from frontdesk.tools import build_tools, tool_id

TOOLS_BASE = "https://tools.example.com"

# Nodes whose only job is to end the call or hand it to a human.
TERMINALS = {"end_call", "handoff", "escalation_fallback"}


def _payload(dealer_id: str = "northgate-uk", tools: bool = True) -> dict:
    return build_flow_payload(
        get_dealer(dealer_id), TOOLS_BASE if tools else "", model="gpt-4.1"
    )


# --------------------------------------------------------------------------- #
# The monolith's failure modes must not be reintroduced
# --------------------------------------------------------------------------- #

def test_node_count_is_far_below_the_monolith() -> None:
    """~190 nodes collapsing into a routeable, nameable graph."""
    assert len(_payload()["nodes"]) < 40


def test_no_single_prompt_carries_the_whole_graph() -> None:
    """The structural claim: no node needs to know about any other node."""
    stats = estimate_prompt_tokens(_payload())
    assert stats["max_node_prompt_tokens"] < 250
    assert stats["worst_case_turn_context_tokens"] < 800
    assert stats["monolith_baseline"]["system_prompt_tokens"] == 14_000


def test_global_prompt_stays_small() -> None:
    """Everything stage-specific belongs in a node, not the shared prompt."""
    stats = estimate_prompt_tokens(_payload())
    assert stats["global_prompt_tokens"] < 500


# --------------------------------------------------------------------------- #
# No dead ends
# --------------------------------------------------------------------------- #

def test_every_non_terminal_node_has_an_exit() -> None:
    """A flow that cannot get stuck is a flow that cannot strand a caller."""
    for node in _payload()["nodes"]:
        if node["id"] in TERMINALS:
            continue
        has_exit = any(
            key in node for key in ("edges", "always_edge", "else_edge", "edge")
        )
        assert has_exit, f"{node['id']} has no way forward"


def test_every_split_has_an_else_edge() -> None:
    """Branches without a fallback are how flows get stuck."""
    for node in _payload()["nodes"]:
        if node["type"] == "branch":
            assert "else_edge" in node, f"{node['id']} has no else edge"


def test_all_edge_destinations_exist() -> None:
    """A dangling edge is a runtime dead end."""
    nodes = _payload()["nodes"]
    ids = {n["id"] for n in nodes}
    for node in nodes:
        targets = [e["destination_node_id"] for e in node.get("edges", [])]
        for key in ("always_edge", "else_edge", "edge"):
            if key in node:
                targets.append(node[key]["destination_node_id"])
        for target in targets:
            assert target in ids, f"{node['id']} points at missing node {target}"


def test_every_node_is_reachable_from_the_start() -> None:
    """Unreachable nodes are the debris that accumulates in a 190-node flow."""
    payload = _payload()
    nodes = payload["nodes"]
    by_id = {n["id"]: n for n in nodes}
    seen: set[str] = set()
    frontier = [payload["start_node_id"]]
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        node = by_id[current]
        for edge in node.get("edges", []):
            frontier.append(edge["destination_node_id"])
        for key in ("always_edge", "else_edge", "edge"):
            if key in node:
                frontier.append(node[key]["destination_node_id"])
    unreachable = set(by_id) - seen
    assert not unreachable, f"unreachable nodes: {sorted(unreachable)}"


# --------------------------------------------------------------------------- #
# The deterministic spine
# --------------------------------------------------------------------------- #

def test_function_nodes_reference_registered_tools() -> None:
    """A node must never point at a tool id the flow did not register."""
    payload = _payload()
    registered = {t["tool_id"] for t in payload["tools"]}
    for node in payload["nodes"]:
        if node["type"] == "function":
            assert node["tool_id"] in registered, (
                f"{node['id']} references unregistered {node['tool_id']}"
            )


def test_commit_path_uses_equation_gates_not_prompts() -> None:
    """The gates that protect irreversible actions must not be model judgement."""
    by_id = {n["id"]: n for n in _payload()["nodes"]}
    for node_id in ("validate_plate", "create_appointment", "verify_write"):
        node = by_id[node_id]
        conditions = [e["transition_condition"]["type"] for e in node["edges"]]
        assert "equation" in conditions, f"{node_id} is not gated deterministically"


def test_plate_cannot_be_looked_up_before_it_is_confirmed() -> None:
    """The ordering that prevents failure B."""
    by_id = {n["id"]: n for n in _payload()["nodes"]}
    lookup = by_id["lookup_vehicle"]
    assert lookup["type"] == "function"
    # Reached only from readback_plate, which requires caller confirmation.
    sources = [
        n["id"] for n in by_id.values()
        if any(e["destination_node_id"] == "lookup_vehicle" for e in n.get("edges", []))
    ]
    assert sources == ["readback_plate"]


def test_write_is_verified_after_commit() -> None:
    """'The tool said success' is not evidence."""
    by_id = {n["id"]: n for n in _payload()["nodes"]}
    commit = by_id["create_appointment"]
    assert commit["edges"][0]["destination_node_id"] == "verify_write"
    verified = by_id["verify_write"]
    assert verified["edges"][0]["destination_node_id"] == "booking_done"


def test_irreversible_actions_gate_on_tool_results() -> None:
    """Commit and verify are function nodes, so they always run."""
    by_id = {n["id"]: n for n in _payload()["nodes"]}
    for node_id in ("create_appointment", "verify_write", "validate_plate"):
        assert by_id[node_id]["type"] == "function"
        assert by_id[node_id]["wait_for_result"] is True


# --------------------------------------------------------------------------- #
# Config over code
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("dealer_id", sorted(DEALERS))
def test_all_dealers_share_one_graph(dealer_id: str) -> None:
    """Adding a dealer must not mean forking the flow."""
    baseline = [n["id"] for n in build_nodes(DEALERS["northgate-uk"])]
    assert [n["id"] for n in build_nodes(DEALERS[dealer_id])] == baseline


def test_read_only_dealer_is_routed_away_from_booking() -> None:
    """Capability differences are data, checked before the caller wastes time.

    The US dealer is on a certified read-only DMS: the agent must capture and
    hand off rather than collect five minutes of booking detail it cannot commit.
    """
    read_only = _payload("midlands-us")
    assert read_only["default_dynamic_variables"]["dealer_can_book"] == "false"
    assert read_only["default_dynamic_variables"]["dealer_can_check_availability"] == "false"
    assert _payload("northgate-uk")["default_dynamic_variables"]["dealer_can_book"] == "true"


def test_no_api_dealer_is_routed_away_from_booking() -> None:
    """The long tail. No live diary means offering a time would be a guess."""
    variables = _payload("rural-fr")["default_dynamic_variables"]
    assert variables["dealer_can_book"] == "false"
    assert variables["dealer_can_check_availability"] == "false"


def test_async_dealer_declares_the_capability_the_flow_branches_on() -> None:
    """Capability differences are data, checked before the caller wastes time.

    CDK Drive's Async Service Appointment API means the dealer *can* book, but
    the booking is not confirmed when the call returns. The flow needs that
    declared so it can route to the honest middle state instead of either
    claiming success or reporting failure.
    """
    variables = _payload("rheinmain-de")["default_dynamic_variables"]
    assert variables["dealer_can_book"] == "true"
    assert variables["dealer_async_writes"] == "true"
    assert _payload("northgate-uk")["default_dynamic_variables"]["dealer_async_writes"] == "false"
    assert _payload("midlands-us")["default_dynamic_variables"]["dealer_async_writes"] == "false"


def test_async_capability_has_a_dedicated_flow_path() -> None:
    """Declaring async writes is worthless if nothing routes on it.

    A capability the flow ignores is a number in a config file, not an
    architecture. This asserts the three-way split actually exists.
    """
    nodes = {n["id"]: n for n in _payload("rheinmain-de")["nodes"]}
    assert "booking_pending" in nodes, "no node for the async middle state"

    create = nodes["create_appointment"]
    destinations = {edge["destination_node_id"] for edge in create["edges"]}
    assert {"verify_write", "booking_pending"} <= destinations
    assert create.get("else_edge", {}).get("destination_node_id") == "booking_failed"

    # The pending node must not be allowed to say "confirmed" to the caller.
    text = nodes["booking_pending"]["instruction"]["text"].lower()
    assert "not guaranteed" in text
    assert "do not" in text


def test_market_compliance_is_configuration() -> None:
    """Recording consent differs by market and must not be hard-coded.

    Note what this asserts now. Consent used to live in the greeting text, so
    the test could compare two strings. It is now a branch on a dynamic
    variable, which is the better shape — every dealer shares one node graph —
    so the check moves to the two places the difference actually lives: the
    variable, and the gate that reads it.
    """
    from frontdesk.flow import ROUTER_NODE_ID, build_nodes
    from frontdesk.dealer_profiles import dealer_dynamic_variables

    # One graph for every dealer.
    nl_nodes = [n["id"] for n in build_nodes(get_dealer("veldhoven-nl"))]
    gb_nodes = [n["id"] for n in build_nodes(get_dealer("northgate-uk"))]
    assert nl_nodes == gb_nodes

    # The difference is data...
    assert dealer_dynamic_variables(get_dealer("northgate-uk"))[
        "requires_recording_consent"] == "true"
    assert dealer_dynamic_variables(get_dealer("veldhoven-nl"))[
        "requires_recording_consent"] == "false"

    # ...and it is read by a deterministic branch, not judged by the model.
    gate = next(n for n in build_nodes(get_dealer("northgate-uk"))
                if n["id"] == "consent_gate")
    equation = gate["edges"][0]["transition_condition"]
    assert equation["type"] == "equation"
    assert equation["equations"][0]["left"] == "{{requires_recording_consent}}"

    # And the graph still routes intent from one declared place.
    router = next(n for n in build_nodes(get_dealer("northgate-uk"))
                  if n["id"] == ROUTER_NODE_ID)
    assert len(router["edges"]) >= 4, "the router must fan out into the skills"


def test_tools_are_omitted_when_no_base_url_is_configured() -> None:
    """The flow must still deploy without a tunnel."""
    payload = _payload(tools=False)
    assert payload["tools"] == []


def test_tool_ids_are_derived_not_duplicated() -> None:
    assert tool_id("validate_identifier") == "tool_validate_identifier"
    payload = _payload()
    ids = [t["tool_id"] for t in payload["tools"]]
    assert len(ids) == len(set(ids)), "duplicate tool ids"


# --------------------------------------------------------------------------- #
# Tool arguments — which are known, and which are guessed
# --------------------------------------------------------------------------- #
#
# Retell fills a property that has a `description` from the model, and applies a
# property that has a `const` verbatim (substituting `{{dynamic_variables}}`).
# A property the model must generate is a place the call can fail, so it should
# exist only where the caller genuinely holds the information.
#
# These tests failed to exist before a live call returned HTTP 422 mid-booking:
# `validate_identifier` required `kind`, `value` and `country`, the model
# supplied only `country`, and the plate step died.

_TOOLS_BY_NAME = {t["name"]: t for t in build_tools(TOOLS_BASE)}


def _props(name: str) -> dict:
    return _TOOLS_BY_NAME[name]["parameters"]["properties"]


def _required(name: str) -> list:
    return _TOOLS_BY_NAME[name]["parameters"]["required"]


@pytest.mark.parametrize("tool_name", sorted(_TOOLS_BY_NAME))
def test_a_const_parameter_is_never_asked_of_the_model(tool_name: str) -> None:
    """`required` tells Retell which properties the *model* must generate.

    Listing a constant there is self-contradictory: it asks the model to invent
    a value for something already applied directly.
    """
    props = _props(tool_name)
    for param in _required(tool_name):
        assert param in props, f"{tool_name}.{param} is required but undeclared"
        assert "const" not in props[param], (
            f"{tool_name}.{param} is a const, so it must not be required of the LLM"
        )


@pytest.mark.parametrize("tool_name", sorted(_TOOLS_BY_NAME))
def test_every_field_the_server_needs_can_actually_be_filled(tool_name: str) -> None:
    """Guards a nearby failure, not the 422 that prompted this suite.

    (Being precise, because the obvious reading is wrong: the reported 422 came
    from a field that *was* declared required — the model simply omitted it. A
    schema-level check cannot see that. What covers it is the const assertions
    below, which remove the model from the loop entirely.)

    What this does catch is the other half: a field the server requires which
    nothing in the tool can supply — neither a const nor an LLM instruction. That
    request could only ever arrive incomplete, and it is invisible until a caller
    hits that exact step.
    """
    from tool_server.app import (
        AvailabilityRequest, CreateRequest, FeedbackRequest, FlagRequest,
        LookupRequest, ValidateRequest, VerifyRequest,
    )

    models = {
        "validate_identifier": ValidateRequest,
        "lookup_vehicle": LookupRequest,
        "check_availability": AvailabilityRequest,
        "create_appointment": CreateRequest,
        "verify_appointment": VerifyRequest,
        "flag_conversation": FlagRequest,
        "record_feedback": FeedbackRequest,
    }
    model = models[tool_name]
    props = _props(tool_name)
    required = _required(tool_name)

    for field, info in model.model_fields.items():
        if not info.is_required():
            continue
        assert field in props, (
            f"{tool_name}: the server requires {field!r} but the tool never "
            f"declares it, so the request can only ever arrive incomplete"
        )
        assert "const" in props[field] or field in required, (
            f"{tool_name}.{field} is required by the server but is neither a "
            f"const nor listed in `required`, so nothing will supply it"
        )


@pytest.mark.parametrize("tool_name", sorted(_TOOLS_BY_NAME))
def test_no_parameter_is_asked_of_the_model_twice(tool_name: str) -> None:
    """A param that is both a const and required is contradictory, and a param
    both declared and undeclared is a schema the model reads inconsistently."""
    props = _props(tool_name)
    required = _required(tool_name)

    assert len(required) == len(set(required)), f"{tool_name} repeats a required entry"
    for param in required:
        assert param in props, f"{tool_name}.{param} required but not declared"
        assert "const" not in props[param], (
            f"{tool_name}.{param} is a const and must not also be required"
        )


def test_the_plate_step_never_asks_the_model_for_known_values() -> None:
    """Regression: the exact 422 seen in production."""
    props = _props("validate_identifier")

    assert props["kind"]["const"] == "license_plate"
    assert props["country"]["const"] == "{{country}}"
    # Only the plate itself is the caller's to provide.
    assert _required("validate_identifier") == ["value"]


def test_the_lookup_reuses_the_plate_the_caller_confirmed() -> None:
    """The lookup reads the value the validator normalised, rather than asking
    the model to remember the plate a second time. Two reads of one fact is two
    chances to disagree, and the read-back the caller confirmed would no longer
    be what was looked up."""
    props = _props("lookup_vehicle")

    assert props["plate"]["const"] == "{{identifier_normalised}}"
    assert props["dealer_id"]["const"] == "{{dealer_id}}"
    assert _required("lookup_vehicle") == []


def test_capability_identity_is_never_model_supplied() -> None:
    """A hallucinated dealer id would route a booking at the wrong dealership,
    so wherever a dealer id is needed it is substituted, not generated."""
    for tool_name in ("lookup_vehicle", "check_availability",
                      "create_appointment", "verify_appointment"):
        dealer_id = _props(tool_name).get("dealer_id")
        assert dealer_id is not None, f"{tool_name} lost its dealer_id"
        assert dealer_id["const"] == "{{dealer_id}}", tool_name
        assert "dealer_id" not in _required(tool_name), tool_name


def test_the_booking_reuses_the_ids_the_lookup_returned() -> None:
    """The vehicle and customer ids describe what was actually found. Asking the
    model to transcribe them back is how a booking lands on the wrong vehicle."""
    props = _props("create_appointment")

    assert props["vehicle_id"]["const"] == "{{vehicle_id}}"
    assert props["customer_id"]["const"] == "{{customer_id}}"


def test_the_verification_reuses_the_id_the_booking_returned() -> None:
    """Mistyping the appointment id would verify nothing and fail the caller
    after a booking that actually succeeded."""
    props = _props("verify_appointment")

    assert props["appointment_id"]["const"] == "{{appointment_id}}"
    assert _required("verify_appointment") == []


def test_duration_is_left_to_the_server() -> None:
    """The duration of a job is a property of the job, not of the conversation.
    The tool server derives it from its own service table."""
    props = _props("create_appointment")

    assert "duration_minutes" not in props
    assert "duration_minutes" not in _required("create_appointment")


# --------------------------------------------------------------------------- #
# Tool transport — the shape the request actually arrives in
# --------------------------------------------------------------------------- #

def test_every_tool_sends_its_arguments_at_the_root() -> None:
    """The 422 that recurred four times, and was misdiagnosed three times.

    Retell nests arguments under `args` unless `args_at_root` is set, so every
    request arrives as::

        {"name": "validate_identifier", "args": {...}, "call": {...}}

    A FastAPI model expecting flat fields matches nothing and reports every
    required one as **missing** — which reads exactly like the model refusing to
    supply them. That is why this presented as a prompt problem: `kind` and
    `value` were "missing" while the transcript showed both in the call args.

    The misdiagnosis had a cost. Making `flag_conversation.reason` optional
    "fixed" its 422, and the server then recorded flags with blank reasons —
    because it was still reading an empty body. A field going optional hid a
    request that carried nothing.

    It is one flag, it belongs on every tool, and this is the guard.
    """
    from frontdesk.tools import build_tools

    tools = build_tools("https://tools.example.com")

    assert tools, "no tools to check"
    for tool in tools:
        assert tool["method"] in {"POST", "PUT", "PATCH"}, (
            f"{tool['name']}: args_at_root only governs body-bearing methods"
        )
        assert tool.get("args_at_root") is True, (
            f"{tool['name']} would arrive wrapped under 'args', and every "
            f"required field would be reported missing"
        )


# --------------------------------------------------------------------------- #
# Plate capture — the examples the model is shown must be true
# --------------------------------------------------------------------------- #

def test_the_plate_examples_are_actually_accepted() -> None:
    """`PLATE_VALUE_EXAMPLES` is shown to the model to make "pass it verbatim"
    concrete, which makes it a claim about a deterministic function.

    A prompt that lies about what the server accepts is worse than no example:
    it teaches the model to reformat into a shape that fails. So every example
    is normalised here, and one that stops working fails the build.
    """
    from frontdesk.identifiers import validate_plate
    from frontdesk.tools import PLATE_VALUE_EXAMPLES

    assert PLATE_VALUE_EXAMPLES, "the model is shown no examples"
    for example in PLATE_VALUE_EXAMPLES:
        result = validate_plate(example, "GB")
        assert result.valid, f"example {example!r} does not validate: {result.reason}"
        assert result.normalised == "AB12CDE", (
            f"example {example!r} normalises to {result.normalised!r}, not AB12CDE"
        )


def test_the_plate_examples_reach_the_model() -> None:
    """Authored but not wired up is the same as absent.

    Specifically: they must be in the *description*. Retell strips every JSON
    Schema keyword except `type`, `description` and `const`, so an `examples`
    array is discarded on deploy — guidance placed there would be invisible to
    the model while looking perfectly correct in this repository. Checked
    against the live API, not assumed.
    """
    from frontdesk.tools import PLATE_VALUE_EXAMPLES

    value = _props("validate_identifier")["value"]
    assert "examples" not in value, (
        "Retell drops `examples`; prose in `description` is the only channel "
        "that survives a deploy."
    )
    for example in PLATE_VALUE_EXAMPLES:
        assert example in value["description"], f"{example!r} never reaches the model"


def test_the_model_is_told_not_to_reformat_the_plate() -> None:
    """The division of labour, stated plainly: the model transcribes, the server
    interprets. Without this the model may "helpfully" complete a partial plate
    into something the caller never said."""
    description = _props("validate_identifier")["value"]["description"].lower()

    assert "verbatim" in description or "exactly as the caller said" in description
    for instruction in ("do not tidy", "do not", "correct"):
        assert instruction in description


# --------------------------------------------------------------------------- #
# The front door — regression tests for a real reported bug
# --------------------------------------------------------------------------- #

def _adjacency(dealer_id: str = "northgate-uk") -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for node in build_nodes(DEALERS[dealer_id]):
        out = {e["destination_node_id"] for e in node.get("edges", [])}
        for key in ("else_edge", "always_edge", "edge"):
            if node.get(key):
                out.add(node[key]["destination_node_id"])
        graph[node["id"]] = out
    return graph


def _reachable_from(start: str, dealer_id: str = "northgate-uk") -> set[str]:
    graph = _adjacency(dealer_id)
    seen, queue = {start}, [start]
    while queue:
        for nxt in graph.get(queue.pop(), set()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def test_consent_and_intent_are_answered_by_different_nodes() -> None:
    """The reported bug: a caller said "yes" to recording and was sent down the
    else path, after which booking and rental were unreachable.

    The cause was that one node asked for consent *and* classified intent. The
    caller's "yes" satisfied the node's goal, and the transitions — all of
    which describe intent — were then evaluated against a turn containing no
    intent, matched nothing, and fell through to the else branch.

    So the structural rule is: the node that judges consent must have no
    opinion about intent, and the router must only be reached once consent is
    settled.
    """
    from frontdesk.flow import ROUTER_NODE_ID

    nodes = {n["id"]: n for n in build_nodes(DEALERS["northgate-uk"])}
    payload = _payload()

    # The gate is the start node, so the first thing that happens is a
    # deterministic decision about which opening the caller hears.
    assert payload["start_node_id"] == "consent_gate"
    assert nodes["consent_gate"]["type"] == "branch"

    # The consent question lives in the caller's first turn, alongside the
    # greeting — one utterance, not a greeting followed by an interruption.
    opening = nodes["greet_consent"]["instruction"]["text"].lower()
    assert "greet" in opening, "the first turn must still be a greeting"
    assert "record" in opening, "the consent question must be in that first turn"

    # It judges consent, and only consent.
    consent = {e["id"]: e["destination_node_id"]
               for e in nodes["greet_consent"]["edges"]}
    assert set(consent) == {"consent_yes", "consent_no"}
    assert consent["consent_yes"] == ROUTER_NODE_ID
    for edge in nodes["greet_consent"]["edges"]:
        assert "record" in edge["transition_condition"]["prompt"].lower()

    # An unclear answer re-asks rather than routing on intent.
    assert nodes["greet_consent"]["else_edge"]["destination_node_id"] == "consent_retry"

    # And the router's own conditions are about intent, never about consent.
    for edge in nodes[ROUTER_NODE_ID]["edges"]:
        prompt = edge["transition_condition"]["prompt"].lower()
        assert "record" not in prompt


def test_a_market_that_does_not_ask_consent_still_hears_a_greeting() -> None:
    """`veldhoven-nl` must not ask, and `northgate-uk` must.

    One graph serves both, because the difference is a dynamic variable read by
    a branch — not a second list of nodes. The gate picks the opening; the
    chosen node does the talking.
    """
    from frontdesk.flow import ROUTER_NODE_ID

    nl = {n["id"]: n for n in build_nodes(DEALERS["veldhoven-nl"])}
    gate = nl["consent_gate"]

    assert gate["edges"][0]["destination_node_id"] == "greet_consent"
    assert gate["else_edge"]["destination_node_id"] == "greet_open"

    # The no-consent opening greets without asking, and goes straight on. No
    # yes/no edges means no way for it to get stuck on an answer.
    assert "edges" not in nl["greet_open"]
    assert "record" not in nl["greet_open"]["instruction"]["text"].lower()
    assert nl["greet_open"]["always_edge"]["destination_node_id"] == ROUTER_NODE_ID


def test_the_menu_is_reachable_once_consent_is_given() -> None:
    """Saying yes must open the skills, not close them."""
    from frontdesk.flow import ROUTER_NODE_ID

    reachable = _reachable_from(ROUTER_NODE_ID)
    assert {"check_booking_capability", "rental_intro", "billing_intro"} <= reachable


def test_a_caller_who_was_misheard_can_still_book_or_rent() -> None:
    """The second half of the reported bug: once in the catch-all node, there
    was no way back to booking or rental — "then can't use anything from
    renting/booking". The catch-all must route back to the menu."""
    reachable = _reachable_from("fallback_help")
    assert "services_intro" in reachable
    assert "check_booking_capability" in reachable
    assert "rental_intro" in reachable


def test_declining_consent_does_not_carry_on_recording() -> None:
    """A refusal is an outcome the flow must represent, not reinterpret."""
    nodes = {n["id"]: n for n in build_nodes(DEALERS["northgate-uk"])}
    refused = nodes["consent_refused"]
    destinations = {e["destination_node_id"] for e in refused["edges"]}
    destinations.add(refused["else_edge"]["destination_node_id"])

    assert "flag_and_handoff" in destinations
    # It must not quietly continue into the skills as if consent were given.
    assert "services_intro" not in destinations


def test_every_dealer_reaches_the_skills_through_the_same_gate() -> None:
    """Consent is market configuration, so the graph shape cannot differ."""
    from frontdesk.flow import ROUTER_NODE_ID

    for dealer_id in DEALERS:
        reachable = _reachable_from(ROUTER_NODE_ID, dealer_id)
        assert "check_booking_capability" in reachable, dealer_id
        assert "rental_intro" in reachable, dealer_id
