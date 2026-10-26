"""Spike 2: can a flow invoke a shared subflow, and nest one?

Verified on a throwaway flow, created and deleted here, so the sandbox agent and
the live original are both left untouched. Two questions:

1. Does a `component` node accept `component_type: "shared"` and deploy?
2. Can a component contain a `component` node (nesting)? This decides whether
   skills may reuse `plate_capture` internally, or whether the orchestrator has
   to chain them side by side.

Run: python -m scripts.spike_component_node
"""

from __future__ import annotations

import os

import httpx

BASE = "https://api.retellai.com"
COMPONENT_ID = "conversation_flow_component_35e1ce98dcd4"  # plate_capture


def _conversation(node_id: str, name: str, text: str, **extra) -> dict:
    node = {
        "id": node_id,
        "type": "conversation",
        "name": name,
        "instruction": {"type": "prompt", "text": text},
        "skippable": False,
    }
    node.update(extra)
    return node


def main() -> int:
    key = os.environ["RETELL_API_KEY"]
    headers = {"Authorization": f"Bearer {key}"}

    # --- 1. a flow that calls a shared component --------------------------- #
    nodes = [
        _conversation("start", "Start", "Greet the caller.",
                      edges=[{
                          "id": "to_plate",
                          "destination_node_id": "grab_plate",
                          "transition_condition": {
                              "type": "prompt",
                              "prompt": "The caller wants to book.",
                          },
                      }],
                      else_edge={"id": "start_else",
                                 "destination_node_id": "done",
                                 "transition_condition": {"type": "prompt",
                                                          "prompt": "Else"}}),
        {
            "id": "grab_plate",
            "type": "component",
            "name": "Capture the registration",
            "component_id": COMPONENT_ID,
            "component_type": "shared",
            "edges": [{
                "id": "plate_ok",
                "destination_node_id": "done",
                "transition_condition": {
                    "type": "equation",
                    "operator": "&&",
                    "equations": [{"left": "{{plate_result}}", "operator": "==",
                                   "right": "ok"}],
                },
            }],
            "else_edge": {"id": "plate_else",
                          "destination_node_id": "done",
                          "transition_condition": {"type": "prompt",
                                                   "prompt": "Else"}},
        },
        {"id": "done", "type": "end", "name": "Done"},
    ]

    payload = {
        "start_speaker": "agent",
        "model_choice": {"type": "cascading", "model": "gpt-4.1"},
        "nodes": nodes,
        "start_node_id": "start",
    }

    r = httpx.post(f"{BASE}/create-conversation-flow", headers=headers,
                   json=payload, timeout=30)
    print(f"[1] create flow with a shared ComponentNode -> HTTP {r.status_code}")
    if r.status_code >= 400:
        print(r.text[:1500])
        return 1

    flow_id = r.json()["conversation_flow_id"]
    print(f"    flow id: {flow_id}")

    # Read it back: did the component node survive, and does the link register?
    back = httpx.get(f"{BASE}/get-conversation-flow/{flow_id}",
                     headers=headers, timeout=20).json()
    node = next((n for n in back["nodes"] if n.get("type") == "component"), None)
    print(f"    component node survived : {node is not None}")
    if node:
        print(f"    component_type          : {node.get('component_type')}")
        print(f"    component_id            : {node.get('component_id')}")

    comp = httpx.get(f"{BASE}/get-conversation-flow-component/{COMPONENT_ID}",
                     headers=headers, timeout=20).json()
    linked = comp.get("linked_conversation_flow_ids") or []
    print(f"    linked_conversation_flow_ids: {linked}")
    print(f"    our throwaway flow is linked: {flow_id in linked}")

    # --- 2. nesting: a component containing a component -------------------- #
    nested = {
        "name": "spike_outer",
        "nodes": [
            _conversation("outer_start", "Outer", "Say hello.",
                          always_edge={"id": "outer_to_inner",
                                       "destination_node_id": "inner",
                                       "transition_condition": {
                                           "type": "prompt", "prompt": "Always"}}),
            {
                "id": "inner",
                "type": "component",
                "name": "Inner shared subflow",
                "component_id": COMPONENT_ID,
                "component_type": "shared",
                "else_edge": {"id": "inner_else",
                              "destination_node_id": "outer_end",
                              "transition_condition": {"type": "prompt",
                                                       "prompt": "Else"}},
            },
            {"id": "outer_end", "type": "end", "name": "Exit Subflow"},
        ],
        "start_node_id": "outer_start",
    }
    r2 = httpx.post(f"{BASE}/create-conversation-flow-component",
                    headers=headers, json=nested, timeout=30)
    print(f"\n[2] nested component (component inside component) -> HTTP {r2.status_code}")
    if r2.status_code >= 400:
        print(f"    REJECTED: {r2.text[:400]}")
        print("    => skills cannot reuse plate_capture internally; the "
              "orchestrator must chain them side by side")
        nested_id = None
    else:
        nested_id = r2.json()["conversation_flow_component_id"]
        print(f"    accepted: {nested_id}")
        print("    => nesting works")

    # --- cleanup ----------------------------------------------------------- #
    print("\ncleanup")
    for label, path in (("throwaway flow",
                         f"{BASE}/delete-conversation-flow/{flow_id}"),):
        d = httpx.request("DELETE", path, headers=headers, timeout=20)
        print(f"    {label}: HTTP {d.status_code}")
    if nested_id:
        d = httpx.request("DELETE",
                          f"{BASE}/delete-conversation-flow-component/{nested_id}",
                          headers=headers, timeout=20)
        print(f"    nested component: HTTP {d.status_code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
