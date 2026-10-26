"""Deploy the subflow architecture: shared library components, then the flow.

Order matters and is the reason this is a script rather than three commands: a
flow that references a shared component id which does not exist yet is rejected
at deploy time, so the library has to exist first. `build_flow_payload` also
refuses to build a modular payload without ids, rather than emitting one that
would be rejected on the way in.

Run: python -m scripts.deploy_subflows [--agent-id agent_...]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

from frontdesk import components as subflows
from frontdesk.config import STATE_FILE, load_settings
from frontdesk.dealer_profiles import get_dealer
from frontdesk.flow import build_flow_payload
from frontdesk.retell_client import RetellClient

BASE = "https://api.retellai.com"
COMPONENT_STATE = Path(STATE_FILE).with_name(".frontdesk-components.json")


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def upsert_shared(key: str, spec, nodes, base_url: str) -> str:
    """Create or update one library component. Returns its id."""
    payload = subflows.build_subflow(spec, nodes, base_url)
    headers = _headers(key)

    existing = httpx.get(f"{BASE}/list-conversation-flow-components",
                         headers=headers, timeout=25).json()
    match = next((c for c in existing if c.get("name") == spec.name), None)

    if match:
        cid = match["conversation_flow_component_id"]
        r = httpx.patch(f"{BASE}/update-conversation-flow-component/{cid}",
                        headers=headers, json=payload, timeout=40)
        action = "updated"
    else:
        r = httpx.post(f"{BASE}/create-conversation-flow-component",
                       headers=headers, json=payload, timeout=40)
        action = "created"
        cid = r.json().get("conversation_flow_component_id") if r.status_code < 400 else ""

    status = "ok" if r.status_code < 400 else f"FAILED {r.status_code}"
    print(f"  {spec.name:<16} {action:<8} {status:<12} {cid}")
    if r.status_code >= 400:
        print(f"    {r.text[:400]}")
        raise SystemExit(1)
    return cid


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent-id", default="agent_59e06efc2efc96e4030bf62518",
                    help="agent to deploy the modular flow to (default: sandbox)")
    args = ap.parse_args()

    settings = load_settings()
    key = settings.api_key
    base_url = settings.tools_base_url
    dealer = get_dealer(settings.dealer_id)
    nodes = build_flow_payload(dealer, base_url)["nodes"]  # legacy nodes = source

    print("shared library components")
    component_ids: dict[str, str] = {}
    for spec in subflows.SUBFLOWS:
        if spec.scope == "shared":
            component_ids[spec.name] = upsert_shared(key, spec, nodes, base_url)

    COMPONENT_STATE.write_text(json.dumps(component_ids, indent=2) + "\n")
    print(f"  -> {COMPONENT_STATE}")

    print("\nmodular flow")
    payload = build_flow_payload(dealer, base_url, modular=True,
                                component_ids=component_ids)
    print(f"  orchestrator nodes : {len(payload['nodes'])}")
    print(f"  local components   : {[c['name'] for c in payload['components']]}")
    print(f"  shared referenced  : {sorted(component_ids)}")

    client = RetellClient(key)
    agent = client.get_agent(args.agent_id)
    flow_id = agent["response_engine"]["conversation_flow_id"]
    print(f"  target agent       : {args.agent_id}")
    print(f"  target flow        : {flow_id}")

    r = httpx.patch(f"{BASE}/update-conversation-flow/{flow_id}",
                    headers=_headers(key), json=payload, timeout=60)
    print(f"\nupdate flow -> HTTP {r.status_code}")
    if r.status_code >= 400:
        print(r.text[:1500])
        return 1

    # Read back: the references are the thing worth confirming, since a
    # component that silently failed to resolve would still deploy.
    back = httpx.get(f"{BASE}/get-conversation-flow/{flow_id}",
                     headers=_headers(key), timeout=30).json()
    comps = [n for n in back["nodes"] if n.get("type") == "component"]
    print(f"\n  flow nodes        : {len(back['nodes'])}")
    print(f"  embedded          : {[c['name'] for c in back.get('components') or []]}")
    for n in comps:
        print(f"    {n['id']:<24} {n['component_type']:<7} {n.get('component_id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
