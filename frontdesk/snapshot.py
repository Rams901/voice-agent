"""Retell -> one canonical graph model (the IR).

This module exists because there will be three diagrams, and three hand-built
diagrams drift apart the first time a node moves.

So: exactly one normalizer, three projections. The projections live in the
frontend; everything they are allowed to know about the flow is in the snapshot
this module produces. Adding a node to `flow.py` must never require editing a
view — that is the same "config over code" argument the dealer profiles make.

Three things the Retell API does not tell you, which we compute here because the
views need them and neither Retell's canvas nor the raw payload exposes them:

* **Edge semantics.** Retell stores `edges`, `edge`, `else_edge` and
  `always_edge`, each with a `prompt` or `equation` condition. Whether a
  transition was *decided by code* or *guessed by the model* is the single most
  important fact about this graph, and it is not a first-class field.
* **Determinism.** `function` and `branch` nodes are code that always runs;
  `conversation` nodes are the model. The ratio is the "deterministic spine"
  claim, measured rather than asserted.
* **Skill clusters.** Which nodes belong to booking vs rental vs handoff, and
  which are shared spine, derived from graph reachability rather than a
  hand-maintained list.

The raw API payloads are cached on disk (not the derived snapshot), so the
normalizer can evolve without anybody having to wipe a stale cache.
"""

from __future__ import annotations

import json
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import ROOT, load_settings
from .retell_client import RetellClient, RetellError
from . import annotations, insights, journey

# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #

CACHE_DIR = ROOT / ".cache" / "retell"
CACHE_TTL_SECONDS = 300

# The dashboard path pattern is not part of the public API, so it is a
# one-line fix rather than something buried in the frontend. Verify it once
# against a real browser tab and adjust here.
RETELL_DASHBOARD = "https://dashboard.retellai.com"


def _agent_url(agent_id: str) -> str:
    return f"{RETELL_DASHBOARD}/agent/{agent_id}"


def _flow_url(flow_id: str) -> str:
    return f"{RETELL_DASHBOARD}/conversation-flow/{flow_id}"


def _cache_path(kind: str, key: str) -> Path:
    return CACHE_DIR / f"{kind}_{key}.json"


def _cached(kind: str, key: str, fetch: Callable[[], Any], *,
            refresh: bool = False, ttl: int = CACHE_TTL_SECONDS) -> Any:
    """Read-through disk cache.

    The demo needs to be fast, and it needs to survive a flaky network (or a
    plane). `refresh=True` is the escape hatch when you are editing the flow in
    Retell and want to see the change.
    """
    path = _cache_path(kind, key)
    if not refresh and path.exists():
        try:
            blob = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            blob = None
        if blob and time.time() - blob.get("fetched_at", 0) < ttl:
            return blob["payload"]

    payload = fetch()
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched_at": time.time(), "payload": payload}))
    except OSError:
        pass  # a cache miss is never worth failing the request over
    return payload


# --------------------------------------------------------------------------- #
# Graph normalisation
# --------------------------------------------------------------------------- #

# Retell hides the node's nature in `type`. These are the five we actually have,
# but the normalizer stays open to new ones so a future `subagent` node shows up
# as `unknown` rather than crashing the tool.
NODE_KIND_LABELS = {
    "conversation": "Conversation (LLM)",
    "function": "Function (code, always runs)",
    "branch": "Branch (code, deterministic)",
    "transfer_call": "Transfer to human",
    "end": "End call",
    "subagent": "Subagent (LLM)",
    "unknown": "Unknown",
}

# `conversation` is the model; everything else is code or a terminal state.
LLM_NODE_KINDS = {"conversation", "subagent", "extract_dynamic_variables"}
DETERMINISTIC_NODE_KINDS = {"function", "branch"}

EDGE_KIND_LABELS = {
    "deterministic": "Code decision (equation)",
    "llm": "Model judgement (prompt)",
    "always": "Always runs",
    "else": "Fallback (never stuck)",
    "failure": "On failure",
}


def _edge_slots(node: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every outgoing edge, tagged with which slot it came from.

    Retell uses four different keys and the slot *is* the semantics: `edges` are
    conditional branches, `edge` is a node's failure path, `else_edge` is the
    guaranteed fallback, `always_edge` is an unconditional transition. Missing
    the singular `edge` would silently drop the transfer-failure path.
    """
    out: list[tuple[str, dict[str, Any]]] = []
    for edge in node.get("edges") or []:
        if isinstance(edge, dict):
            # Tagged "edges" (plural) — deliberately *not* "edge", because the
            # singular key below means something different and the slot is the
            # semantics.
            out.append(("edges", edge))
    for slot in ("edge", "else_edge", "always_edge"):
        edge = node.get(slot)
        if isinstance(edge, dict):
            out.append((slot, edge))
    return out


def _classify_edge(slot: str, edge: dict[str, Any]) -> str:
    condition = edge.get("transition_condition") or {}
    prompt = str(condition.get("prompt") or "").strip().lower()

    if slot == "else_edge" or prompt == "else":
        return "else"
    if slot == "always_edge" or prompt == "always":
        return "always"
    if slot == "edge":
        return "failure"
    if condition.get("type") == "equation":
        return "deterministic"
    return "llm"


def _edge_label(kind: str, edge: dict[str, Any]) -> str:
    condition = edge.get("transition_condition") or {}
    if kind == "else":
        return "Else"
    if kind == "always":
        return "Always"
    if condition.get("type") == "equation":
        parts = [
            f"{e.get('left')} {e.get('operator')} {e.get('right')}"
            for e in condition.get("equations", [])
        ]
        return " && ".join(parts)
    text = str(condition.get("prompt") or "").strip()
    return text if len(text) <= 60 else text[:57] + "…"


def _instruction(node: dict[str, Any]) -> tuple[str, str]:
    instruction = node.get("instruction") or {}
    return instruction.get("type", "prompt"), instruction.get("text", "") or ""


def _prompt_tokens(text: str) -> int:
    # Same crude ~4 chars/token accounting `flow.estimate_prompt_tokens` uses, so
    # the numbers in this tool agree with the numbers in `cli plan`.
    return len(text) // 4


def _skill_entries(start_id: str, edges: list[dict[str, Any]]) -> list[str]:
    """The router's outgoing destinations, in declared order.

    Order matters: it is the priority used when a node is reachable from more
    than one skill. The router's edge order is the author's intended reading
    order.

    `start_id` here is the *router*, not necessarily the start node — see
    `_resolve_router`. The two were the same node until consent was split out,
    and conflating them made the skills silently collapse to the consent
    branch.
    """
    return [e["to"] for e in edges if e["from"] == start_id]


def _resolve_router(start_id: str, node_ids: set[str],
                    edges: list[dict[str, Any]]) -> str:
    """Which node fans out into the skills.

    The flow declares it (see `flow.ROUTER_NODE_ID`), because it cannot be
    inferred from shape: the consent gate is also a fan-out, and walking
    forward from the start node finds *that* first. The fallback to `start_id`
    keeps a snapshot of an older deployed graph — built before the router was
    declared — summarising the way it used to.
    """
    from .flow import ROUTER_NODE_ID

    if ROUTER_NODE_ID in node_ids:
        return ROUTER_NODE_ID
    return start_id


_ENTRY_LABELS = {
    "check_booking_capability": "Booking",
    "rental_intro": "Rental",
    "billing_intro": "Billing & warranty",
    "flag_and_handoff": "Handoff & review",
    "fallback_help": "Anything else",
}


def _humanise(node_id: str) -> str:
    return node_id.replace("_", " ").strip().capitalize()


def _reachable(start: str, adjacency: dict[str, list[str]]) -> set[str]:
    """Every node forward-reachable from `start`, inclusive. Cycle-safe."""
    seen = {start}
    queue = deque([start])
    while queue:
        for nxt in adjacency.get(queue.popleft(), []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def _distances(start: str, adjacency: dict[str, list[str]]) -> dict[str, int]:
    """Shortest hop count from `start`. Cycle-safe."""
    distance = {start: 0}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for nxt in adjacency.get(current, []):
            if nxt not in distance:
                distance[nxt] = distance[current] + 1
                queue.append(nxt)
    return distance


def _attach_skills(nodes: list[dict[str, Any]], edges: list[dict[str, Any]],
                   start_id: str) -> list[dict[str, Any]]:
    """Group nodes into skills, and flag the shared spine.

    Two different questions, answered two different ways — conflating them is
    what makes a naive partition degenerate:

    * **Which skill does this node belong to?** The *nearest* skill entry, by
      hop count. Reachability alone will not do it: everything eventually drains
      into `wrap_up` and `flag_and_handoff`, so "first entry that can reach it"
      hands almost the whole graph to Booking.
    * **Is it shared?** Its *immediate predecessors* come from two or more
      different skills. This is the rule that matters, and it took a wrong
      answer to find it. "Reachable from two entries" looks equivalent but is
      not: a completed booking can legitimately jump into the rental skill, so
      reachability marked the *entire rental subtree* as shared and collapsed
      the Rental card to a single node. What actually identifies shared
      infrastructure is that several skills route *into* it — `wrap_up`,
      `flag_and_handoff` — not that it is reachable from several.
    * Both are necessary: those shared nodes are what you must re-test whenever
      you touch any one skill, and the raw canvas gives no hint of it.
    """
    adjacency: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    predecessors: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for edge in edges:
        adjacency.setdefault(edge["from"], []).append(edge["to"])
        predecessors.setdefault(edge["to"], []).append(edge["from"])

    router_id = _resolve_router(start_id, {n["id"] for n in nodes}, edges)
    entries = [e for e in _skill_entries(router_id, edges) if e in adjacency]
    reach = {entry: _reachable(entry, adjacency) for entry in entries}
    distance = {entry: _distances(entry, adjacency) for entry in entries}

    skill_of: dict[str, str | None] = {}
    for node in nodes:
        node_id = node["id"]
        owners = [entry for entry in entries if node_id in reach[entry]]
        if owners:
            # Nearest owner wins; ties break on the router's declared edge order.
            skill_of[node_id] = min(
                owners,
                key=lambda entry: (distance[entry].get(node_id, 10**6),
                                   entries.index(entry)),
            )
        else:
            skill_of[node_id] = None
        node["reachable_from"] = owners

    # The front door: everything a call passes through *before* the router, plus
    # the router itself. Greeting, consent and the menu are not a skill — they
    # are what every call goes through first.
    #
    # This needs saying explicitly because `fallback_help` routes back to the
    # menu (so a caller who was misheard can still reach booking), which makes
    # the router reachable from a skill and would otherwise file it under
    # "Anything else".
    from_start = _reachable(start_id, adjacency)
    from_router = _reachable(router_id, adjacency)
    front_door = (from_start - from_router) | {router_id}

    for node in nodes:
        if node["id"] in front_door:
            skill_of[node["id"]] = None
        node["is_front_door"] = node["id"] in front_door

    for node in nodes:
        node_id = node["id"]
        node["skill"] = skill_of[node_id]
        # Ignore None: the start node belongs to no skill, and counting it would
        # mark every branch out of the router as "shared".
        linking = {skill_of.get(p) for p in predecessors[node_id]} - {None}
        node["is_shared"] = len(linking) >= 2

    # Anything that hangs off the shared spine belongs to the shared spine.
    # `end_call` is only reachable from `wrap_up`, which two skills already tie
    # for — without this it gets handed to whichever skill happened to be
    # declared first, and the terminal shows up inside "Rental".
    #
    # Propagation stops at skill entries. `flag_and_handoff` is both shared *and*
    # the Handoff skill's entry, so absorbing through it would pull the entire
    # transfer path into the spine and leave "Handoff & review" as a single node.
    entry_set = set(entries)
    shared = {n["id"] for n in nodes if n["is_shared"]}
    grew = True
    while grew:
        grew = False
        for node in nodes:
            node_id = node["id"]
            if node_id in shared or node_id == start_id or node_id in entry_set:
                continue
            preds = predecessors[node_id]
            if preds and all(p in shared and p not in entry_set for p in preds):
                shared.add(node_id)
                grew = True
    for node in nodes:
        node["is_shared"] = node["id"] in shared

    skills = []
    for entry in entries:
        members = [n["id"] for n in nodes if n["skill"] == entry]
        skills.append({
            "id": entry,
            "label": _ENTRY_LABELS.get(entry, _humanise(entry)),
            "entry_node": entry,
            "node_count": len(members),
            "nodes": members,
            "shared_nodes": [n["id"] for n in nodes
                             if n["skill"] == entry and n["is_shared"]],
        })
    return skills


# A node card is ~72px tall; a *cluster* card is not, because its footer wraps
# with the number of tools and guardrail stages it carries. These are the
# vertical gaps the two layouts need, in graph units, and they differ by more
# than 2x for that reason. Getting this wrong is how Booking (20 steps, 6 tools,
# 4 guardrail stages — three rows of chips) came to overlap Rental.
NODE_ROW_GAP = 150
CLUSTER_ROW_GAP = 240


def layered_layout(nodes: list[dict[str, Any]], edges: list[dict[str, Any]],
                   start_id: str, *, x_gap: int = 330,
                   y_gap: int = NODE_ROW_GAP) -> dict[str, dict[str, float]]:
    """A clean left-to-right layered layout.

    Retell stores its own `display_position`, and we keep it for fidelity — but
    the live flow's stored coordinates fan the off-path nodes out to y≈5000,
    which is readable *while you are dragging nodes* and unreadable as a
    summary. This is the summarising half of the problem: rank by distance from
    the start node, stack within rank.

    `y_gap` is a parameter rather than a constant because the two callers stack
    very different things. A node card is a fixed ~72px; a cluster card's height
    grows with its chip rows, and the contracted view has a 20-step cluster with
    six tools. The full graph and the summary must not share a row gap.
    """
    adjacency: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for edge in edges:
        if edge["from"] in adjacency:
            adjacency[edge["from"]].append(edge["to"])

    rank: dict[str, int] = {}
    order: list[str] = []
    if start_id in adjacency:
        rank[start_id] = 0
        queue = deque([start_id])
        while queue:
            current = queue.popleft()
            order.append(current)
            for nxt in adjacency[current]:
                if nxt not in rank:
                    rank[nxt] = rank[current] + 1
                    queue.append(nxt)

    spill = (max(rank.values()) + 1) if rank else 0
    for node in nodes:
        if node["id"] not in rank:
            rank[node["id"]] = spill
            order.append(node["id"])

    rows: dict[int, list[str]] = {}
    for node_id in order:
        rows.setdefault(rank[node_id], []).append(node_id)

    layout: dict[str, dict[str, float]] = {}
    for level, ids in rows.items():
        offset = -(len(ids) - 1) * y_gap / 2
        for index, node_id in enumerate(ids):
            layout[node_id] = {"x": level * x_gap, "y": offset + index * y_gap}
    return layout


def _graph_metrics(nodes: list[dict[str, Any]], edges: list[dict[str, Any]],
                   global_prompt: str) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    for node in nodes:
        by_kind[node["kind"]] = by_kind.get(node["kind"], 0) + 1

    by_edge_kind: dict[str, int] = {}
    for edge in edges:
        by_edge_kind[edge["kind"]] = by_edge_kind.get(edge["kind"], 0) + 1

    llm_nodes = sum(c for k, c in by_kind.items() if k in LLM_NODE_KINDS)
    det_nodes = sum(c for k, c in by_kind.items() if k in DETERMINISTIC_NODE_KINDS)

    # "Deterministic" here means *the model was not the decider*: an equation
    # gate, or an unconditional transition that must always fire.
    det_edges = by_edge_kind.get("deterministic", 0) + by_edge_kind.get("always", 0)
    judged_edges = by_edge_kind.get("llm", 0)

    node_tokens = [n["prompt_tokens"] for n in nodes]
    worst = max(nodes, key=lambda n: n["prompt_tokens"]) if nodes else None
    global_tokens = _prompt_tokens(global_prompt)

    # `{{variable}}` is a placeholder here but is *substituted* at runtime, so any
    # node referencing one costs more than this counts. Usually that is trivia —
    # `{{dealer_name}}` is three words — but `{{current_calendar}}` expands to a
    # fortnight of dated lines, which is ~150 tokens that this budget does not
    # see. A prompt budget that only measures the template is a floor, not a
    # ceiling, and the difference is worth naming rather than smoothing over.
    expanding = sorted(
        node["id"] for node in nodes if "{{" in (node.get("instruction") or "")
    )

    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes_by_kind": by_kind,
        "edges_by_kind": by_edge_kind,
        "llm_nodes": llm_nodes,
        "deterministic_nodes": det_nodes,
        "deterministic_edges": det_edges,
        "model_judged_edges": judged_edges,
        # The measured version of the README's "deterministic spine" claim.
        "deterministic_edge_share": (
            round(det_edges / len(edges), 3) if edges else 0.0
        ),
        "shared_node_count": sum(1 for n in nodes if n["is_shared"]),
        "global_prompt_tokens": global_tokens,
        "total_node_prompt_tokens": sum(node_tokens),
        "mean_node_prompt_tokens": (
            round(sum(node_tokens) / len(node_tokens), 1) if node_tokens else 0
        ),
        "max_node_prompt_tokens": worst["prompt_tokens"] if worst else 0,
        "max_node": worst["id"] if worst else None,
        "worst_case_turn_context_tokens": (
            global_tokens + (worst["prompt_tokens"] if worst else 0)
        ),
        # Placeholders are counted as literal text, so these are floors.
        "nodes_with_runtime_variables": expanding,
        "prompt_tokens_are_minimum": bool(expanding),
    }


# --------------------------------------------------------------------------- #
# Snapshot assembly
# --------------------------------------------------------------------------- #

def resolve_agent_id(client: RetellClient, requested: str | None = None) -> str:
    """Pick the agent to inspect: explicit, then local state, then the account.

    Deliberately errors when the account holds several agents and no choice was
    made, rather than silently picking the first one and showing you the wrong
    diagram.
    """
    if requested:
        return requested

    from .config import STATE_FILE

    if STATE_FILE.exists():
        try:
            state_id = json.loads(STATE_FILE.read_text()).get("agent_id")
        except (OSError, json.JSONDecodeError):
            state_id = None
        if state_id:
            return state_id

    agents = client.list_agents(limit=100)
    if len(agents) == 1:
        return agents[0]["agent_id"]
    if not agents:
        raise SystemExit("No agents on this Retell account.")
    listing = "\n".join(f"  {a['agent_id']}  {a.get('agent_name', '')}" for a in agents)
    raise SystemExit(
        "Several agents on this account — pass --agent-id.\n" + listing
    )


def _normalise_node(node: dict[str, Any], in_degrees: dict[str, int]) -> dict[str, Any]:
    kind = node.get("type", "unknown")
    instruction_type, text = _instruction(node)
    position = node.get("display_position") or {}
    tool_id = node.get("tool_id")

    return {
        "id": node.get("id"),
        "name": node.get("name") or node.get("id"),
        "kind": kind,
        "kind_label": NODE_KIND_LABELS.get(kind, kind),
        "is_model": kind in LLM_NODE_KINDS,
        "is_deterministic": kind in DETERMINISTIC_NODE_KINDS,
        "instruction_type": instruction_type,
        "instruction": text,
        "prompt_tokens": _prompt_tokens(text),
        "speaks_during_execution": bool(node.get("speak_during_execution")),
        "skippable": node.get("skippable"),
        "tool_id": tool_id,
        # Function nodes reference `tool_<name>`; the name is what a human reads.
        "tool_name": tool_id[len("tool_"):] if isinstance(tool_id, str)
                     and tool_id.startswith("tool_") else None,
        "wait_for_result": node.get("wait_for_result"),
        "transfer_number": (node.get("transfer_destination") or {}).get("number"),
        # Kept for fidelity; the views default to `layout` instead.
        "position": (
            {"x": position.get("x", 0), "y": position.get("y", 0)}
            if position else None
        ),
        "in_degree": in_degrees.get(node.get("id"), 0),
    }


def build_graph(flow: dict[str, Any]) -> dict[str, Any]:
    """Turn a raw conversation flow into nodes, edges and derived structure."""
    raw_nodes = flow.get("nodes") or []
    start_id = flow.get("start_node_id") or (raw_nodes[0]["id"] if raw_nodes else "")

    edges: list[dict[str, Any]] = []
    in_degrees: dict[str, int] = {}
    for node in raw_nodes:
        for slot, edge in _edge_slots(node):
            kind = _classify_edge(slot, edge)
            destination = edge.get("destination_node_id")
            if not destination:
                continue
            in_degrees[destination] = in_degrees.get(destination, 0) + 1
            edges.append({
                "id": edge.get("id") or f"{node.get('id')}->{destination}",
                "from": node.get("id"),
                "to": destination,
                "slot": slot,
                "kind": kind,
                "kind_label": EDGE_KIND_LABELS.get(kind, kind),
                "label": _edge_label(kind, edge),
                "is_deterministic": kind in {"deterministic", "always"},
            })

    nodes = [_normalise_node(n, in_degrees) for n in raw_nodes]
    known = {n["id"] for n in nodes}
    # An edge pointing at a vanished node is a real bug in the flow, and a
    # dangling edge would break the layout. Drop it, but do not hide it.
    dangling = [e["id"] for e in edges if e["to"] not in known or e["from"] not in known]
    edges = [e for e in edges if e["to"] in known and e["from"] in known]

    skills = _attach_skills(nodes, edges, start_id)
    layout = layered_layout(nodes, edges, start_id)
    for node in nodes:
        node["layout"] = layout.get(node["id"], {"x": 0, "y": 0})

    tools = []
    for tool in flow.get("tools") or []:
        max_retry = tool.get("max_retry") or 0
        tools.append({
            "tool_id": tool.get("tool_id"),
            "name": tool.get("name"),
            "type": tool.get("type"),
            "method": tool.get("method"),
            "url": tool.get("url"),
            "timeout_ms": tool.get("timeout_ms"),
            "max_retry": max_retry,
            # Retry is only safe on an idempotent endpoint. Surfacing this is
            # the difference between a review question and a double booking.
            "retry_requires_idempotency": max_retry > 0,
            "parameters": list((tool.get("parameters") or {}).get("properties", {})),
            "response_variables": list((tool.get("response_variables") or {}).keys()),
        })

    return {
        "start_node_id": start_id,
        "model_choice": flow.get("model_choice"),
        "model_temperature": flow.get("model_temperature"),
        "version": flow.get("version"),
        "is_published": flow.get("is_published"),
        "last_modified": flow.get("last_modification_timestamp"),
        "global_prompt": flow.get("global_prompt") or "",
        "dynamic_variables": flow.get("default_dynamic_variables") or {},
        "nodes": nodes,
        "edges": edges,
        "skills": skills,
        "tools": tools,
        "dangling_edges": dangling,
        "metrics": _graph_metrics(nodes, edges, flow.get("global_prompt") or ""),
    }


# --------------------------------------------------------------------------- #
# Contraction — the "summarised part diagram"
# --------------------------------------------------------------------------- #

def contract_graph(graph: dict[str, Any],
                   node_annotations: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Collapse the node graph into skill-level clusters.

    This is the answer to the actual complaint. A 31-node flow with a 14-step
    booking spine cannot be drawn at a legible size in one screen — 14 nodes at a
    readable 236px each is ~3300px wide, so any "fit to screen" view lands around
    0.35 zoom and the text becomes ~4px. Retell's own canvas has the same
    problem, which is why these canvases are hard to read.

    So the default view is contracted: one card per skill, plus the shared spine,
    with edges aggregated and counted. It is legible at 100% zoom and it answers
    "what does this flow do" instead of "where is every arrow". The full graph is
    still one toggle away, and is the right thing to look at *after* the summary
    has told you where to look.

    Contraction is derived, not authored: adding a node in the dashboard changes
    the cluster counts and nothing else.
    """
    nodes = graph["nodes"]
    edges = graph["edges"]
    start_id = graph["start_node_id"]
    overlay = node_annotations or {}

    entries = {s["entry_node"]: s["label"] for s in graph["skills"]}
    skill_labels = {s["id"]: s["label"] for s in graph["skills"]}
    by_id = {n["id"]: n for n in nodes}

    def group_of(node: dict[str, Any]) -> str:
        node_id = node["id"]
        if node_id == start_id or node.get("is_front_door"):
            return "__start"
        # A skill's own entry belongs to that skill even if it is also reachable
        # from elsewhere — otherwise the Rental card loses its front door.
        if node_id in entries:
            return node_id
        if node["is_shared"]:
            return "__shared"
        return node["skill"] or "__other"

    def label_for(group: str) -> str:
        if group == "__start":
            # Not the start node's own name: the cluster is the whole front
            # door — greeting, consent and the menu — not just the first node.
            return "Front door"
        if group == "__shared":
            return "Shared spine"
        if group == "__other":
            return "Unattached"
        return skill_labels.get(group) or entries.get(group) or _humanise(group)

    order: list[str] = []
    members: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        group = group_of(node)
        if group not in members:
            members[group] = []
            order.append(group)
        members[group].append(node)

    stage_order = {stage: index for index, stage
                   in enumerate(annotations.GUARDRAIL_STAGES)}

    clusters = []
    for group in order:
        group_nodes = members[group]
        stages = sorted(
            {overlay.get(n["id"], {}).get("guardrail_stage")
             for n in group_nodes} - {None},
            key=lambda s: stage_order.get(s, 99),
        )
        clusters.append({
            "id": group,
            "name": label_for(group),
            "kind": "cluster",
            "node_count": len(group_nodes),
            "model_nodes": sum(1 for n in group_nodes if n["is_model"]),
            "code_nodes": sum(1 for n in group_nodes if n["is_deterministic"]),
            "prompt_tokens": sum(n["prompt_tokens"] for n in group_nodes),
            "tools": sorted({n["tool_name"] for n in group_nodes if n["tool_name"]}),
            "stages": stages,
            "nodes": [n["id"] for n in group_nodes],
            "is_shared": group == "__shared",
            "entry_node": None if group in {"__start", "__shared", "__other"} else group,
        })

    aggregated: dict[tuple[str, str], dict[str, Any]] = {}
    internal = 0
    for edge in edges:
        source = group_of(by_id[edge["from"]])
        target = group_of(by_id[edge["to"]])
        if source == target:
            internal += 1
            continue
        key = (source, target)
        bucket = aggregated.setdefault(key, {
            "id": f"{source}->{target}",
            "from": source,
            "to": target,
            "count": 0,
            "kinds": set(),
            "labels": [],
        })
        bucket["count"] += 1
        bucket["kinds"].add(edge["kind"])
        if len(bucket["labels"]) < 3:
            bucket["labels"].append(edge["label"])

    contracted_edges = []
    for bucket in aggregated.values():
        kinds = bucket["kinds"]
        deterministic = kinds <= {"deterministic", "always"}
        model = kinds <= {"llm", "else", "failure"}
        contracted_edges.append({
            "id": bucket["id"],
            "from": bucket["from"],
            "to": bucket["to"],
            "kind": "deterministic" if deterministic
                    else ("llm" if model else "mixed"),
            "count": bucket["count"],
            "label": (f"{bucket['count']} transition"
                      f"{'' if bucket['count'] == 1 else 's'}"),
            "detail": bucket["labels"],
        })

    contracted = {
        "start_node_id": group_of(by_id[start_id]) if start_id in by_id else order[0],
        "nodes": clusters,
        "edges": contracted_edges,
        "internal_transitions": internal,
    }
    contracted["layout"] = layered_layout(clusters, contracted_edges,
                                         contracted["start_node_id"],
                                         x_gap=380, y_gap=CLUSTER_ROW_GAP)
    for cluster in clusters:
        cluster["layout"] = contracted["layout"].get(cluster["id"], {"x": 0, "y": 0})
    return contracted


def build_snapshot(agent_id: str | None = None, *, calls_limit: int = 50,
                   refresh: bool = False, client: RetellClient | None = None) -> dict[str, Any]:
    """The single object every view is rendered from.

    Everything expensive or network-bound happens here exactly once: fetch,
    normalise, annotate, model the money and the latency. The views are then
    pure functions of this dict, which is what keeps three audiences from
    needing three code paths.
    """
    settings = load_settings()
    own_client = client is None
    if own_client:
        client = RetellClient(settings.api_key)

    try:
        agent_id = resolve_agent_id(client, agent_id)

        agents = _cached("agents", "all", lambda: client.list_agents(limit=100),
                         refresh=refresh)
        agent = _cached("agent", agent_id, lambda: client.get_agent(agent_id),
                        refresh=refresh)

        engine = agent.get("response_engine") or {}
        flow_id = engine.get("conversation_flow_id")

        flows = _cached("flows", "all", lambda: client.list_conversation_flows(limit=100),
                        refresh=refresh)

        flow = None
        if flow_id:
            flow = _cached("flow", flow_id,
                           lambda: client.get_conversation_flow(flow_id),
                           refresh=refresh)

        def fetch_calls() -> Any:
            try:
                return client.list_calls(
                    limit=calls_limit,
                    filter_criteria={"agent_id": [agent_id]},
                )
            except RetellError:
                # Filtering is the fast path; without it we still get *a*
                # picture, just noisier. Better a caveated number than none.
                return client.list_calls(limit=calls_limit)

        calls = _cached("calls", agent_id, fetch_calls, refresh=refresh,
                        ttl=60)

        graph = build_graph(flow) if flow else None

        snapshot: dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "agent": {
                "agent_id": agent.get("agent_id"),
                "agent_name": agent.get("agent_name"),
                "voice_id": agent.get("voice_id"),
                "language": agent.get("language"),
                "responsiveness": agent.get("responsiveness"),
                "interruption_sensitivity": agent.get("interruption_sensitivity"),
                "begin_message_delay_ms": agent.get("begin_message_delay_ms"),
                "reminder_trigger_ms": agent.get("reminder_trigger_ms"),
                "reminder_max_count": agent.get("reminder_max_count"),
                "analysis_fields": [
                    {"name": f.get("name"), "type": f.get("type")}
                    for f in agent.get("post_call_analysis_data") or []
                ],
                "url": _agent_url(agent_id),
            },
            "flow_id": flow_id,
            "flow_url": _flow_url(flow_id) if flow_id else None,
            "flow": graph,
            "agents": [
                {"agent_id": a.get("agent_id"), "agent_name": a.get("agent_name")}
                for a in agents
            ],
            "flows": [
                {"flow_id": f.get("conversation_flow_id"),
                 "name": f.get("name") or f.get("conversation_flow_id")}
                for f in flows
            ],
            "metrics": insights.build_metrics(calls, graph, calls_limit=calls_limit),
        }

        if graph:
            annotations.attach(snapshot, graph)
            # The contracted view is derived *after* the overlay, because the
            # guardrail stages it shows on each cluster come from the overlay.
            snapshot["clusters"] = contract_graph(graph, snapshot["annotations"])
            # ...and the client story after that, for the same reason plus one
            # more: it is authored, so it has to be dereferenced against the
            # graph to prove it still describes what is deployed.
            snapshot["journey"] = journey.build_journey(
                graph, snapshot["annotations"])

        return snapshot
    finally:
        if own_client and client is not None:
            client.close()


def main(argv: list[str] | None = None) -> int:
    """`python -m frontdesk.snapshot` — dump the IR for review, no UI.

    This is the Phase 0 artefact: if the model in here is right, the views are
    mechanical, so it is worth eyeballing the JSON before writing any React.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="python -m frontdesk.snapshot")
    parser.add_argument("--agent-id", default=None)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--calls-limit", type=int, default=50)
    parser.add_argument("--out", default=None, help="write JSON here instead of stdout")
    parser.add_argument("--summary", action="store_true", help="print a short summary")
    args = parser.parse_args(argv)

    snapshot = build_snapshot(args.agent_id, calls_limit=args.calls_limit,
                              refresh=args.refresh)
    blob = json.dumps(snapshot, indent=2)

    if args.out:
        Path(args.out).write_text(blob + "\n")
        print(f"wrote {args.out} ({len(blob)} bytes)")
    if args.summary or not args.out:
        graph = snapshot["flow"] or {}
        metrics = graph.get("metrics", {})
        print(f"agent     : {snapshot['agent']['agent_name']}")
        print(f"flow      : {snapshot['flow_id']}")
        print(f"nodes     : {metrics.get('node_count')}  "
              f"edges: {metrics.get('edge_count')}")
        print(f"kinds     : {metrics.get('nodes_by_kind')}")
        print(f"edges     : {metrics.get('edges_by_kind')}")
        print(f"spine     : {metrics.get('deterministic_nodes')} deterministic nodes, "
              f"{metrics.get('deterministic_edges')} deterministic edges "
              f"({metrics.get('deterministic_edge_share', 0) * 100:.0f}% of transitions)")
        print(f"prompt    : global {metrics.get('global_prompt_tokens')}t, "
              f"worst turn {metrics.get('worst_case_turn_context_tokens')}t")
        print("skills    : " + ", ".join(
            f"{s['label']}({s['node_count']})" for s in graph.get("skills", [])))
        cost = snapshot["metrics"]["cost"]
        print(f"cost      : {cost['source']} — "
              f"{cost['modelled']['per_conversation']['mid']:.3f}/conversation, "
              f"{cost['modelled']['per_day']['mid']:.2f}/day @ "
              f"{cost['assumptions']['conversations_per_day']}/day")
    if not args.out:
        print("\n(pass --out FILE to write the full snapshot)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
