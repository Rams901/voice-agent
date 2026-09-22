"""Cost and latency, modelled from the graph and corrected by real calls.

Cost per conversation and p90 latency are the two numbers a business audience
asks for. The honest answer has two halves, and conflating them is how that
audience ends up treating a guess as a commitment:

* **MODELLED** — computed from the flow itself (prompt tokens, node count, how
  many tool round trips a call can hit) plus a pricing table. Available
  immediately, no traffic required.
* **OBSERVED** — read out of Retell's own `call_cost` and `latency` fields on
  real calls. Precise, but only exists once there is traffic.

On this account there is exactly one call and it failed (`error_user_not_joined`)
so `call_cost` is zeroed and `latency` is empty. That case is the *default* state
of a new deployment, not an edge case, so it is handled explicitly rather than
drowning the UI in `NaN`.

The self-calibration is the interesting part: `call_cost.product_costs` carries
the real per-product unit prices your account is actually charged, so once
traffic exists the tool can tell you how far its own model is off — and the
pricing table stops being a guess.

Prices are *placeholders* until calibrated. They are inputs, not truth.
"""

from __future__ import annotations

import json
from statistics import median
from typing import Any

from .config import ROOT

PRICING_FILE = ROOT / "pricing.json"

# Placeholder unit prices. They exist so the model produces a number with a
# visible shape, not because they are right for your account. Replace via
# pricing.json, or let the tool calibrate them from observed calls.
DEFAULT_PRICING: dict[str, Any] = {
    "_comment": (
        "Placeholder unit prices — replace with your account's real rates, or "
        "read them off the observed block once you have traffic."
    ),
    "source": "placeholder",
    "currency": "USD",
    "platform_per_minute": 0.07,
    "tts_per_minute": 0.04,
    "telephony_per_minute": 0.01,
    "llm_input_per_1k_tokens": 0.002,
    "llm_output_per_1k_tokens": 0.008,
    "call_type": "phone",
}

DEFAULT_ASSUMPTIONS: dict[str, Any] = {
    "duration_minutes": {"low": 1.5, "mid": 3.0, "high": 5.0},
    "seconds_per_turn": 18,
    "output_tokens_per_turn": 40,
    "transcript_tokens_per_turn": 45,
    "conversations_per_day": 100,
    "days_per_month": 30,
    # 100/day is the default assumption, but it is never the only number
    # anybody wants. The model is linear in volume, so the honest way to answer
    # "and at 250?" is to show the whole ladder rather than recompute one rung.
    # The configured volume is always included, whatever this says.
    "volume_ladder": [25, 50, 100, 250, 500],
}

# Composition of a single conversational turn. These are the levers that
# actually move perceived responsiveness, and they are all tunable per agent.
DEFAULT_LATENCY: dict[str, float] = {
    "asr_finalisation_seconds": 0.35,
    "llm_ttft_seconds": 0.45,
    "tts_ttfa_seconds": 0.35,
    "network_seconds": 0.08,
    "tool_round_trip_seconds": 0.35,
}


def load_pricing() -> dict[str, Any]:
    """Defaults, overlaid with pricing.json if the user has written one."""
    pricing = dict(DEFAULT_PRICING)
    if PRICING_FILE.exists():
        try:
            supplied = json.loads(PRICING_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            supplied = {}
        if isinstance(supplied, dict):
            pricing.update(supplied)
            pricing.setdefault("source", "pricing.json")
    return pricing


def load_assumptions() -> dict[str, Any]:
    assumptions = json.loads(json.dumps(DEFAULT_ASSUMPTIONS))  # deep copy
    if PRICING_FILE.exists():
        try:
            supplied = json.loads(PRICING_FILE.read_text()).get("assumptions") or {}
        except (OSError, json.JSONDecodeError):
            supplied = {}
        for key, value in supplied.items():
            if key == "duration_minutes" and isinstance(value, dict):
                assumptions["duration_minutes"].update(value)
            else:
                assumptions[key] = value
    return assumptions


def _timing_multipliers(assumptions: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Low / mid / high duration, expressed as a multiplier of the mid case."""
    mid = assumptions["duration_minutes"]["mid"] or 1.0
    return {
        key: {"minutes": assumptions["duration_minutes"][key],
              "multiplier": assumptions["duration_minutes"][key] / mid}
        for key in ("low", "mid", "high")
    }


def _cost_for_duration(minutes: float, pricing: dict[str, Any],
                       assumptions: dict[str, Any],
                       context_tokens: int,
                       mean_node_tokens: int) -> dict[str, float]:
    """One conversation's cost, broken into the components that cause it.

    The breakdown matters more than the total: it is what lets somebody argue
    with the number ("TTS is most of it") instead of accepting or rejecting it
    wholesale.
    """
    price = float(pricing.get("tts_per_minute", 0)) + float(
        pricing.get("platform_per_minute", 0))
    if str(pricing.get("call_type", "phone")).lower() == "phone":
        price += float(pricing.get("telephony_per_minute", 0))
    voice = minutes * price

    turns = max(1.0, minutes * 60 / max(1.0, assumptions["seconds_per_turn"]))

    # Each turn re-sends the prompt plus everything said so far, so input cost
    # grows quadratically with call length. That is the mechanism behind "long
    # calls get expensive", not a flat per-minute fee.
    per_turn_constant = context_tokens + mean_node_tokens
    growth = assumptions["transcript_tokens_per_turn"]
    input_tokens = sum(
        per_turn_constant + growth * turn for turn in range(1, int(turns) + 1)
    )
    output_tokens = turns * assumptions["output_tokens_per_turn"]

    llm = (input_tokens / 1000 * float(pricing.get("llm_input_per_1k_tokens", 0))
           + output_tokens / 1000 * float(pricing.get("llm_output_per_1k_tokens", 0)))

    return {
        "voice_and_platform": round(voice, 4),
        "llm_tokens": round(llm, 4),
        "total": round(voice + llm, 4),
        "turns": round(turns, 1),
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
    }


def _observed_calls(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Everything the real calls can tell us, whether or not it is enough.

    Note `usable`: a call that never joined has `combined_cost = 0` and
    `latency = {}`. Counting those as "cheap calls" would drag every average
    down, so they are reported separately as failures instead.
    """
    total = len(calls)
    usable: list[dict[str, Any]] = []
    failed_durations: list[float] = []
    unit_prices: dict[str, list[float]] = {}
    tool_counts: dict[str, int] = {}
    latency_samples: dict[str, dict[str, list[float]]] = {}
    statuses: dict[str, int] = {}
    disconnections: dict[str, int] = {}

    for call in calls:
        cost = call.get("call_cost") or {}
        duration = float(cost.get("total_duration_seconds") or 0)
        combined = float(cost.get("combined_cost") or 0)

        status = str(call.get("call_status") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1
        reason = str(call.get("disconnection_reason") or "unknown")
        disconnections[reason] = disconnections.get(reason, 0) + 1

        for entry in cost.get("product_costs") or []:
            product = str(entry.get("product") or "unknown")
            unit = float(entry.get("unit_price") or 0)
            if unit > 0:
                unit_prices.setdefault(product, []).append(unit)

        for tool_call in call.get("tool_calls") or []:
            name = str(tool_call.get("name") or "unknown")
            tool_counts[name] = tool_counts.get(name, 0) + 1

        latency = call.get("latency")
        if isinstance(latency, dict):
            for channel, values in latency.items():
                # Retell reports percentiles per channel: {"e2e": {"p50": ...}}.
                if not isinstance(values, dict):
                    continue
                for percentile, value in values.items():
                    if isinstance(value, (int, float)) and value > 0:
                        latency_samples.setdefault(channel, {}).setdefault(
                            percentile, []).append(float(value))

        if combined > 0 and duration > 0:
            usable.append({
                "call_id": call.get("call_id"),
                "duration_seconds": duration,
                "combined_cost": combined,
                "cost_per_minute": round(combined / duration * 60, 4),
            })
        elif duration == 0:
            failed_durations.append(duration)

    if not usable:
        return {
            "call_count": total,
            "usable_calls": 0,
            "unusable_calls": total,
            "statuses": statuses,
            "disconnections": disconnections,
            "note": (
                "No billable calls yet — every figure below is MODELLED. "
                "Retell returns call_cost and latency only for calls that "
                "actually connected."
            ),
            "unit_prices": {},
            "tool_calls": tool_counts,
            "latency_percentiles": {},
        }

    durations = [c["duration_seconds"] for c in usable]
    per_minute = [c["cost_per_minute"] for c in usable]

    return {
        "call_count": total,
        "usable_calls": len(usable),
        "unusable_calls": total - len(usable),
        "statuses": statuses,
        "disconnections": disconnections,
        "note": (
            f"{len(usable)} billable call(s). These are the real numbers for "
            "this account, and the ones to trust."
        ),
        "average_duration_seconds": round(median(durations), 1),
        "blended_cost_per_minute": round(median(per_minute), 4),
        "average_cost_per_conversation": round(
            median(c["combined_cost"] for c in usable), 4),
        "unit_prices": {
            product: round(median(values), 6)
            for product, values in sorted(unit_prices.items())
        },
        "tool_calls": tool_counts,
        "latency_percentiles": {
            channel: {
                percentile: round(median(values), 3)
                for percentile, values in sorted(percentiles.items())
            }
            for channel, percentiles in sorted(latency_samples.items())
        },
    }


def _modelled_latency(latency_cfg: dict[str, float],
                      function_nodes: int) -> dict[str, Any]:
    """Per-turn latency, and what a tool-calling turn adds on top.

    The tail is the point. A mean of 1.2s can hide a p95 that callers describe
    as "the robot keeps pausing".
    """
    base = (latency_cfg["asr_finalisation_seconds"]
            + latency_cfg["llm_ttft_seconds"]
            + latency_cfg["tts_ttfa_seconds"]
            + latency_cfg["network_seconds"])
    with_tool = base + latency_cfg["tool_round_trip_seconds"]

    def band(value: float) -> dict[str, float]:
        # Deliberately simple spread: p90/p95 are the tail factors this kind of
        # system shows in practice, and they are labelled as assumptions.
        return {"p50": round(value, 2), "p90": round(value * 1.5, 2),
                "p95": round(value * 1.8, 2)}

    return {
        "per_turn": band(base),
        "per_tool_turn": band(with_tool),
        "components": latency_cfg,
        "tool_callable_nodes": function_nodes,
        "note": (
            "Function nodes are the latency spikes: each one is a synchronous "
            "tool round trip inside the turn."
        ),
    }


def build_metrics(calls: list[dict[str, Any]], graph: dict[str, Any] | None, *,
                  calls_limit: int = 50) -> dict[str, Any]:
    """The cost and latency block, modelled and observed, clearly separated."""
    pricing = load_pricing()
    assumptions = load_assumptions()
    latency_cfg = dict(DEFAULT_LATENCY)

    metrics = (graph or {}).get("metrics", {})
    context_tokens = metrics.get("worst_case_turn_context_tokens") or 0
    mean_node_tokens = int(metrics.get("mean_node_prompt_tokens") or 0)
    function_nodes = metrics.get("nodes_by_kind", {}).get("function", 0)

    bands = _timing_multipliers(assumptions)
    per_conversation = {
        key: _cost_for_duration(band["minutes"], pricing, assumptions,
                                context_tokens, mean_node_tokens)
        for key, band in bands.items()
    }

    per_day_count = assumptions["conversations_per_day"]
    days = assumptions["days_per_month"]

    per_day = {
        key: round(value["total"] * per_day_count, 2)
        for key, value in per_conversation.items()
    }
    per_month = {
        key: round(value * days, 2)
        for key, value in per_day.items()
    }

    # What actually drives the number, stated as a share instead of left for the
    # reader to infer from the breakdown. At placeholder rates duration is ~95%
    # of a conversation, and saying that out loud is the difference between a
    # business audience arguing about the *duration assumption* — the thing that
    # moves the total — and arguing about token pricing, which does not.
    mid_components = per_conversation["mid"]
    mid_total = max(mid_components["total"], 1e-9)
    drivers = {
        "voice_and_platform_share": round(
            mid_components["voice_and_platform"] / mid_total, 3),
        "llm_share": round(mid_components["llm_tokens"] / mid_total, 3),
        "duration_minutes_mid": assumptions["duration_minutes"]["mid"],
        "turns_mid": mid_components["turns"],
        "input_tokens_mid": mid_components["input_tokens"],
        "note": (
            "Duration is the whole number: voice, platform and telephony are "
            "billed per minute, so the estimate moves with call length and "
            "almost nothing else. LLM input tokens grow quadratically with "
            "turns — every turn re-sends the prompt and the transcript so far — "
            "which is why the *long* calls are the expensive ones, not the "
            "complicated ones."
        ),
    }

    ladder = sorted({int(v) for v in (assumptions.get("volume_ladder") or [])}
                    | {int(per_day_count)})
    # Rounded per rung, then multiplied — the same order the headline figures
    # use. Computing the month from the unrounded model would let the ladder row
    # for the configured volume disagree with the headline by a few cents, and a
    # table that contradicts the number above it is worse than no table.
    volume_sensitivity = []
    for count in ladder:
        day = round(mid_total * count, 2)
        volume_sensitivity.append({
            "conversations_per_day": count,
            "per_day": day,
            "per_month": round(day * days, 2),
        })

    observed = _observed_calls(calls or [])

    # Calibration: only meaningful once a billable call exists.
    calibration: dict[str, Any] = {
        "applied": False,
        "observed_unit_prices": observed.get("unit_prices", {}),
        "variance": None,
        "hint": (
            "No billable calls yet, so the pricing table is uncalibrated. Once "
            "traffic exists this compares the model against what you were "
            "actually charged."
        ),
    }
    if observed.get("usable_calls"):
        mid = per_conversation["mid"]["total"]
        actual = observed.get("average_cost_per_conversation") or 0
        if actual > 0 and mid > 0:
            calibration.update({
                "applied": True,
                "modelled_mid": mid,
                "observed_average": actual,
                "variance": round((mid - actual) / actual, 3),
                "hint": (
                    "Variance is (modelled - observed) / observed. Update "
                    "pricing.json with the observed unit prices to close it."
                ),
            })
        else:
            calibration["applied"] = True
            calibration["hint"] = "Observed unit prices available for calibration."

    source = "observed+modelled" if observed.get("usable_calls") else "modelled"

    # Placeholder unit prices produce a number with the right *shape* and the
    # wrong magnitude. The UI has to be able to say that in words, so it is a
    # boolean here rather than a string the frontend has to pattern-match.
    pricing_is_placeholder = "placeholder" in str(
        pricing.get("source", "")).lower()

    return {
        "cost": {
            "source": source,
            "currency": pricing.get("currency", "USD"),
            "pricing": pricing,
            "pricing_is_placeholder": pricing_is_placeholder,
            "assumptions": assumptions,
            "modelled": {
                "per_conversation": {
                    key: value["total"] for key, value in per_conversation.items()
                },
                "components": {
                    "mid": per_conversation["mid"],
                },
                "per_day": per_day,
                "per_month": per_month,
                "conversations_per_day": per_day_count,
                "days_per_month": days,
                "call_type": pricing.get("call_type"),
                "drivers": drivers,
                "volume_sensitivity": volume_sensitivity,
                "note": (
                    "MODELLED from the flow's own prompt budget and a pricing "
                    "table — not a quote. Duration is the dominant assumption; "
                    "widen it and the range moves with it."
                ),
            },
            "observed": observed,
            "calibration": calibration,
            "calls_sampled": min(len(calls or []), calls_limit),
        },
        "latency": {
            "modelled": _modelled_latency(latency_cfg, function_nodes),
            "observed": observed.get("latency_percentiles", {}),
            "source": ("observed" if observed.get("latency_percentiles")
                       else "modelled"),
            "note": (
                "Tail latency is what callers describe as 'the robot keeps "
                "pausing', so p90/p95 matter more than the mean."
            ),
        },
    }
