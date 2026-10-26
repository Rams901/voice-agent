"""Spike: prove the shared-subflow mechanism with real nodes.

Not throwaway — this is the actual `plate_capture` extraction, used to find out
whether the primitive behaves the way the API schema implies before the rest of
the refactor is built on it.

Three things are being checked:

1. Can a component be created with our real node dicts, unchanged?
2. Does a `code` node work as a deterministic outcome router? A component has a
   single "Exit Subflow", so the parent routes on *dynamic variables* — and the
   only way to set one without an LLM or a network call is a code node.
3. Does the exit need to be a plain `end` node with an `Exit Subflow` name?

Run: python -m scripts.spike_component
"""

from __future__ import annotations

import copy
import json
import os

import httpx

from frontdesk.dealer_profiles import DEALERS
from frontdesk.flow import build_nodes

BASE = "https://api.retellai.com"
NAME = "plate_capture"

# The nodes that make up plate capture today, in the order they run.
PLATE_NODES = [
    "capture_plate", "validate_plate", "plate_repair", "readback_plate",
    "lookup_vehicle", "disambiguate_vehicle", "vehicle_not_found",
]

# Boundary rewiring. Inside the component, every edge that used to leave the
# skill must now terminate at an outcome router instead — a component's edges
# are internal, and the parent decides what happens next from the variables the
# component left behind.
LEAVES_HAPPY = {"capture_service"}       # vehicle resolved; carry on
LEAVES_FAIL = {"flag_and_handoff"}       # give up; parent hands off


def _code(node_id: str, outcome: str, exit_id: str) -> dict:
    """A deterministic outcome marker.

    `wait_for_result` is required by the schema, and the JS returns a value that
    `response_variables` lifts into a dynamic variable. No model, no network —
    which is the point: the component's contract with its parent is a fact, not
    a judgement.
    """
    return {
        "id": node_id,
        "type": "code",
        "name": f"Mark {outcome}",
        "code": f"return {{ result: '{outcome}' }};",
        "wait_for_result": True,
        "response_variables": {"plate_result": "data.result"},
        "always_edge": {"id": f"{node_id}_exit", "destination_node_id": exit_id},
    }


def build_component(dealer_id: str = "northgate-uk",
                    base_url: str = "https://tools.example.com") -> dict:
    nodes = {n["id"]: n for n in build_nodes(DEALERS[dealer_id])}
    picked = [copy.deepcopy(nodes[nid]) for nid in PLATE_NODES]

    # A shared component is self-contained, so it must carry the tools its
    # function nodes call. Without them the API rejects it outright:
    #   "Tool id does not exist: tool_validate_identifier"
    # That is a good design on Retell's part — a component that referenced tools
    # from whatever flow happened to host it would not be portable, which is the
    # whole point of a library subflow.
    from frontdesk.tools import build_tools

    wanted = {"validate_identifier", "lookup_vehicle"}
    tools = []
    for tool in build_tools(base_url):
        if tool["name"] in wanted:
            tool = dict(tool)
            # The flow's function nodes reference `tool_<name>`; make the
            # component's own tools match so the reference resolves here too.
            tool["tool_id"] = f"tool_{tool['name']}"
            tools.append(tool)

    def rewire(node: dict) -> None:
        for edge in node.get("edges", []):
            dest = edge["destination_node_id"]
            if dest in LEAVES_HAPPY:
                edge["destination_node_id"] = "plate_resolved"
            elif dest in LEAVES_FAIL:
                edge["destination_node_id"] = "plate_abandoned"
        for key in ("always_edge", "else_edge"):
            if key in node and node[key]["destination_node_id"] in LEAVES_HAPPY:
                node[key]["destination_node_id"] = "plate_resolved"
            elif key in node and node[key]["destination_node_id"] in LEAVES_FAIL:
                node[key]["destination_node_id"] = "plate_abandoned"

    for node in picked:
        rewire(node)

    picked.append(_code("plate_resolved", "ok", "plate_exit"))
    picked.append(_code("plate_abandoned", "failed", "plate_exit"))
    picked.append({
        "id": "plate_exit",
        "type": "end",
        "name": "Exit Subflow",
    })

    return {
        "name": NAME,
        "nodes": picked,
        "tools": tools,
        "start_node_id": "capture_plate",
    }


def main() -> int:
    key = os.environ["RETELL_API_KEY"]
    headers = {"Authorization": f"Bearer {key}"}

    component = build_component()
    print(f"component   : {component['name']}")
    print(f"nodes       : {[n['id'] for n in component['nodes']]}")
    print(f"start       : {component['start_node_id']}")

    # Is it already there? Then update rather than duplicate.
    existing = httpx.get(f"{BASE}/list-conversation-flow-components",
                         headers=headers, timeout=20).json()
    match = next((c for c in existing if c.get("name") == NAME), None)

    if match:
        cid = match["conversation_flow_component_id"]
        print(f"\nupdating existing {cid}")
        r = httpx.patch(f"{BASE}/update-conversation-flow-component/{cid}",
                        headers=headers, json=component, timeout=30)
    else:
        print("\ncreating")
        r = httpx.post(f"{BASE}/create-conversation-flow-component",
                       headers=headers, json=component, timeout=30)

    print(f"HTTP {r.status_code}")
    if r.status_code >= 400:
        print(r.text[:2000])
        return 1

    body = r.json()
    print(json.dumps({
        "id": body.get("conversation_flow_component_id"),
        "name": body.get("name"),
        "start_node_id": body.get("start_node_id"),
        "node_types": [n.get("type") for n in body.get("nodes", [])],
        "linked_flows": body.get("linked_conversation_flow_ids"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
