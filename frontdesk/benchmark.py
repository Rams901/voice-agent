"""Benchmark the two flow architectures on cost, latency and accuracy.

The two architectures are the same conversation with different structure:
a single 39-node flow, and a 22-node orchestrator routing between six
subflows. The interesting question is not which one is "better" in the
abstract, but what the decomposition costs and what it buys:

    accuracy   does splitting a skill into a component lose context across
               the boundary, or route to the wrong outcome?
    latency    a component boundary is an extra transition. Does it add an
               LLM round-trip per journey, and does that show up end to end?
    cost       more transitions mean more tokens, but smaller prompts mean
               fewer tokens per turn. The direction is genuinely unknown.

Two arms, because the platform bills and reports them differently and neither
alone can answer all three questions:

    batch  text simulation. Bills per message, returns pass/fail and the
           transcript, but **no latency and no cost fields**. Cheap enough to
           repeat, so this is where accuracy comes from.
    voice  real web calls. Bills per minute, returns `latency.*` percentiles
           and an itemised `call_cost`. Expensive, so it is spent on two
           journeys whose length brackets a typical call.

Budget
------

The cap is the binding constraint on the whole design, so it is enforced here
rather than trusted to arithmetic in someone's head. `Budget` refuses to start
work it cannot afford and accounts for every run afterwards. Batch cost cannot
be read back from the API, so it is estimated from the real message counts in
the returned transcript, at published per-message rates — and labelled as an
estimate everywhere it appears, because a number that looks measured but is not
would be worse than no number.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .retell_client import RetellClient, RetellError, conversation_flow_engine

# --------------------------------------------------------------------------- #
# The two things being compared
# --------------------------------------------------------------------------- #
#
# Resolved by id and validated by name. A benchmark that silently measured the
# same agent twice would produce a clean, plausible, completely wrong report —
# so `resolve_architectures` fetches each one and checks.
#
# The ids below belong to the account this was built in. They are NOT portable:
# a fresh Retell account has different ids, and a benchmark pointed at ids that
# do not exist there fails in a confusing way. Override per architecture with
# FRONTDESK_<ARCH>_AGENT_ID / FRONTDESK_<ARCH>_FLOW_ID (and put them in .env so
# they survive), or re-point the defaults after deploying both architectures.
_ARCHITECTURE_DEFAULTS: dict[str, dict[str, str]] = {
    "monolith": {
        "label": "Monolith (single flow)",
        "agent_id": "agent_e633333f5fae1f46ea35053778",
        "flow_id": "conversation_flow_e36cda2ff5ff",
        "expect": "(modular booking)",
    },
    "modular": {
        "label": "Modular (subflows)",
        "agent_id": "agent_59e06efc2efc96e4030bf62518",
        "flow_id": "conversation_flow_b8908d1d44cc",
        "expect": "sub-flows",
    },
}


def _architectures() -> dict[str, dict[str, str]]:
    """Apply FRONTDESK_<ARCH>_{AGENT,FLOW}_ID overrides to the defaults."""
    resolved: dict[str, dict[str, str]] = {}
    for name, spec in _ARCHITECTURE_DEFAULTS.items():
        prefix = f"FRONTDESK_{name.upper()}"
        resolved[name] = {
            **spec,
            "agent_id": os.environ.get(f"{prefix}_AGENT_ID", "").strip()
            or spec["agent_id"],
            "flow_id": os.environ.get(f"{prefix}_FLOW_ID", "").strip()
            or spec["flow_id"],
        }
    return resolved


ARCHITECTURES: dict[str, dict[str, str]] = _architectures()

#: Published chat rates, USD per message, as of the pricing page at build time.
#: These are used only to *estimate* batch cost, because the test API reports
#: none. Treat them as a budget guard, not an invoice.
CHAT_RATE_USD: dict[str, float] = {
    "gpt-4.1": 0.02,        # the agent's own turns
    "gpt-4.1-mini": 0.008,  # the model playing the caller
}

#: Batch testing adds one grading analysis unit per case.
GRADING_UNIT_USD = 0.01

#: The model the simulated caller runs on, set by `build_test_case_payloads`.
SIMULATOR_MODEL = "gpt-4.1-mini"


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #

class BudgetExceeded(RuntimeError):
    """Raised when work would take the run past its cap."""


@dataclass
class Budget:
    """A hard ceiling on spend, per architecture.

    Deliberately refuses up front rather than warning: a benchmark that
    overspends has done real harm, and a warning printed to a log is not a
    control.
    """

    cap_usd: float
    label: str = ""
    spent_usd: float = 0.0
    entries: list[dict[str, Any]] = field(default_factory=list)

    @property
    def remaining_usd(self) -> float:
        return self.cap_usd - self.spent_usd

    def afford(self, amount_usd: float, what: str) -> None:
        if amount_usd > self.remaining_usd + 1e-9:
            raise BudgetExceeded(
                f"{what} needs ~${amount_usd:.2f} but only "
                f"${self.remaining_usd:.2f} of ${self.cap_usd:.2f} is left "
                f"for {self.label}. Raise the cap or shorten the suite."
            )

    def charge(self, amount_usd: float, what: str, measured: bool) -> None:
        self.spent_usd += amount_usd
        self.entries.append({
            "what": what,
            "usd": round(amount_usd, 4),
            "measured": measured,
        })


# --------------------------------------------------------------------------- #
# Cost model
# --------------------------------------------------------------------------- #

def estimate_case_cost(journey: dict[str, Any]) -> float:
    """Rough pre-flight cost for one batch case, from the prompt's length.

    Deliberately pessimistic. The real cost is computed afterwards from actual
    message counts; this exists only to decide whether to start.
    """
    # The simulated caller's instructions and the metrics are input tokens on
    # every turn, so length correlates with cost, but the message count is what
    # dominates. Assume a busy conversation.
    assumed_turns = 12
    agent = assumed_turns * CHAT_RATE_USD["gpt-4.1"]
    callers = assumed_turns * CHAT_RATE_USD[SIMULATOR_MODEL]
    return agent + callers + GRADING_UNIT_USD


def measure_case_cost(messages: list[dict[str, Any]]) -> float:
    """Cost of a batch case from its own transcript.

    Retell returns no cost for a test run, so this is the closest thing to a
    real figure: count who said what, and charge each side at its own rate.
    """
    agent = sum(1 for m in messages if m.get("role") == "agent")
    caller = sum(1 for m in messages if m.get("role") == "user")
    return (agent * CHAT_RATE_USD["gpt-4.1"]
            + caller * CHAT_RATE_USD[SIMULATOR_MODEL]
            + GRADING_UNIT_USD)


# --------------------------------------------------------------------------- #
# Architectures
# --------------------------------------------------------------------------- #

def resolve_architectures(client: RetellClient,
                          only: str | None = None) -> dict[str, dict[str, str]]:
    """Check each agent is what the table says it is.

    Cheap insurance. The failure this prevents is comparing an agent against
    itself, which would look like a perfect result.
    """
    resolved: dict[str, dict[str, str]] = {}
    names = [only] if only else list(ARCHITECTURES)
    for name in names:
        spec = dict(ARCHITECTURES[name])
        try:
            agent = client.get_agent(spec["agent_id"])
        except RetellError as exc:
            raise SystemExit(f"Could not load agent for {name}: {exc}") from None
        actual = agent.get("agent_name", "")
        if spec["expect"] not in actual:
            raise SystemExit(
                f"Agent {spec['agent_id']} is named {actual!r}, which does not "
                f"look like the {name} architecture (expected {spec['expect']!r}). "
                f"Refusing to benchmark — the report would be meaningless."
            )
        spec["agent_name"] = actual
        resolved[name] = spec
    return resolved


def transcripts_from_run(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull a message list out of a test run, whatever shape Retell used.

    `transcript_snapshot` is documented as "either ConversationFlowPlayground-
    Snapshot or RetellLlmPlaygroundSnapshot" — an unhelpful thing to discover in
    a benchmark loop, so both are handled and anything else is treated as empty
    rather than crashing the run.
    """
    snap = run.get("transcript_snapshot") or {}
    for key in ("transcript_object", "transcript", "messages"):
        value = snap.get(key)
        if isinstance(value, list):
            return value
    return []


# --------------------------------------------------------------------------- #
# Prepared cases
# --------------------------------------------------------------------------- #

@dataclass
class Prepared:
    architecture: str
    slug: str
    case_id: str
    name: str


def prepare_cases(client: RetellClient, arch: str, spec: dict[str, str],
                  journeys: list[dict[str, Any]],
                  replace: bool = False) -> tuple[list[Prepared], str]:
    """Create the test cases for one architecture.

    Cases are named per architecture. The suite this project already has reuses
    one definition per scenario and deletes-then-recreates it, which is fine for
    a single arm and wrong for two: while benchmarking, deleting the name a run
    is still using would corrupt the comparison.
    """

    engine = conversation_flow_engine(spec["flow_id"])
    existing: dict[str, str] = {}
    try:
        for case in client.list_test_case_definitions(limit=200):
            existing[case["name"]] = case["test_case_definition_id"]
    except RetellError:
        pass

    prepared: list[Prepared] = []
    for journey in journeys:
        name = f"[{arch}] {journey['name']}"
        payload = {
            "name": name,
            "response_engine": engine,
            "user_prompt": journey["user_prompt"],
            "metrics": journey["metrics"],
            "dynamic_variables": journey.get("dynamic_variables", {}),
            "tool_mocks": journey.get("tool_mocks", []),
            "llm_model": SIMULATOR_MODEL,
        }
        if replace and name in existing:
            client.delete_test_case_definition(existing[name])
        created = client.create_test_case_definition(payload)
        prepared.append(Prepared(
            architecture=arch, slug=journey["slug"],
            case_id=created["test_case_definition_id"], name=name,
        ))
    return prepared, spec["flow_id"]


def run_batch(client: RetellClient, spec: dict[str, str],
              case_ids: list[str], timeout_s: float = 900.0,
              poll_s: float = 10.0) -> dict[str, Any]:
    """Start a batch and wait for it, returning the finished batch object."""
    import time

    engine = conversation_flow_engine(spec["flow_id"])
    batch = client.create_batch_test(case_ids, engine)
    batch_id = batch["test_case_batch_job_id"]

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        batch = client.get_batch_test(batch_id)
        if batch.get("status") == "complete":
            return batch
        time.sleep(poll_s)
    raise TimeoutError(f"batch {batch_id} did not complete within {timeout_s:.0f}s")


# --------------------------------------------------------------------------- #
# Voice-call data
# --------------------------------------------------------------------------- #

@dataclass
class CallStats:
    architecture: str
    call_id: str
    duration_s: float
    cost_usd: float
    llm_turns: int
    llm_p50_ms: float | None
    e2e_p50_ms: float | None
    agent_turns: int
    journey: str = ""


def calls_for(client: RetellClient, agent_id: str,
              limit: int = 100) -> list[dict[str, Any]]:
    """All calls for one agent, newest first."""
    calls = client.list_calls(limit=limit)
    return [c for c in calls if c.get("agent_id") == agent_id]


def call_stats(arch: str, call: dict[str, Any]) -> CallStats | None:
    """Summarise one call, or None if it never connected.

    A call that failed to join is not a data point about the agent — counting it
    would report a fast, cheap zero.
    """
    if call.get("call_status") != "ended" and call.get("disconnection_reason") in {
        "error_user_not_joined", "error_llm_websocket_closed",
    }:
        return None
    cost = (call.get("call_cost") or {}).get("combined_cost", 0) / 100.0
    latency = call.get("latency") or {}
    llm = latency.get("llm") or {}
    e2e = latency.get("e2e") or {}
    transcript = call.get("transcript_object") or []
    return CallStats(
        architecture=arch,
        call_id=call.get("call_id", ""),
        duration_s=(call.get("duration_ms") or 0) / 1000.0,
        cost_usd=cost,
        llm_turns=int(llm.get("num") or 0),
        llm_p50_ms=llm.get("p50"),
        e2e_p50_ms=e2e.get("p50"),
        agent_turns=sum(1 for t in transcript if t.get("role") == "agent"),
        journey=(call.get("metadata") or {}).get("benchmark_journey", ""),
    )


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


@dataclass
class ArchitectureReport:
    architecture: str
    label: str
    #: slug -> ("pass" | "fail" | "error", explanation)
    accuracy: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: Calls tagged as belonging to this benchmark.
    calls: list[CallStats] = field(default_factory=list)
    #: Calls on the same agent that are not part of the benchmark. Kept apart
    #: rather than folded in: they were made for other reasons, at other times,
    #: against other flow versions, and adding their cost to the benchmark's
    #: would misstate what this exercise spent.
    other_calls: list[CallStats] = field(default_factory=list)
    batch_spent_usd: float = 0.0
    voice_spent_usd: float = 0.0

    @property
    def passed(self) -> int:
        return sum(1 for s, _ in self.accuracy.values() if s == "pass")

    @property
    def graded(self) -> int:
        return len(self.accuracy)


def build_report(arch: str, spec: dict[str, str],
                 accuracy: dict[str, tuple[str, str]],
                 calls: list[CallStats],
                 batch_spent: float,
                 other_calls: list[CallStats] | None = None) -> ArchitectureReport:
    return ArchitectureReport(
        architecture=arch,
        label=spec["label"],
        accuracy=accuracy,
        calls=calls,
        other_calls=other_calls or [],
        batch_spent_usd=batch_spent,
        voice_spent_usd=sum(c.cost_usd for c in calls),
    )


def render(reports: list[ArchitectureReport]) -> str:
    """A text report, deliberately plain so it can be diffed between runs."""
    lines: list[str] = []
    add = lines.append

    add("=" * 78)
    add("ARCHITECTURE BENCHMARK")
    add("=" * 78)

    # ------------------------------------------------------------- accuracy
    add("\nACCURACY  (batch simulation, graded against per-journey metrics)")
    slugs = sorted({s for r in reports for s in r.accuracy})
    if slugs:
        width = max(len(s) for s in slugs) + 2
        header = "  " + "journey".ljust(width) + "".join(
            r.architecture.center(14) for r in reports)
        add(header)
        add("  " + "-" * (len(header) - 2))
        for slug in slugs:
            row = "  " + slug.ljust(width)
            for r in reports:
                status = r.accuracy.get(slug, ("-", ""))[0]
                mark = {"pass": "PASS", "fail": "FAIL", "error": "ERROR"}.get(status, "-")
                row += mark.center(14)
            add(row)
        add("")
        for r in reports:
            rate = (r.passed / r.graded * 100) if r.graded else 0.0
            add(f"  {r.label:<28} {r.passed}/{r.graded} passed ({rate:.0f}%)")
    else:
        add("  (not run)")

    # ------------------------------------------------------------ latency
    add("\nLATENCY  (voice calls — llm is the part structure can move)")
    add(f"  {'':<28}{'calls':>6}{'dur s':>8}{'llmN':>7}"
        f"{'llm p50':>9}{'e2e p50':>9}")
    for r in reports:
        if not r.calls:
            add(f"  {r.label:<28}{'0':>6}   (no calls yet)")
            continue
        add(f"  {r.label:<28}{len(r.calls):>6}"
            f"{_mean([c.duration_s for c in r.calls]):>8.1f}"
            f"{_mean([float(c.llm_turns) for c in r.calls]):>7.1f}"
            f"{(f'{_mean([c.llm_p50_ms for c in r.calls if c.llm_p50_ms]):.0f}' if any(c.llm_p50_ms for c in r.calls) else '-'):>9}"
            f"{(f'{_mean([c.e2e_p50_ms for c in r.calls if c.e2e_p50_ms]):.0f}' if any(c.e2e_p50_ms for c in r.calls) else '-'):>9}")

    # --------------------------------------------------------------- cost
    add("\nCOST")
    add(f"  {'':<28}{'batch':>10}{'voice':>10}{'total':>10}")
    for r in reports:
        total = r.batch_spent_usd + r.voice_spent_usd
        add(f"  {r.label:<28}{r.batch_spent_usd:>10.2f}"
            f"{r.voice_spent_usd:>10.2f}{total:>10.2f}")

    if any(r.other_calls for r in reports):
        add("\n  not part of this benchmark (same agent, untagged calls):")
        for r in reports:
            if r.other_calls:
                other = sum(c.cost_usd for c in r.other_calls)
                add(f"    {r.label:<26}{len(r.other_calls):>3} calls"
                    f"{other:>9.2f}")

    add("\nNOTE  batch cost is estimated from transcript message counts at")
    add("      published chat rates, because the test API returns no cost.")
    add("      Voice cost is the platform's own itemised figure.")
    return "\n".join(lines)
