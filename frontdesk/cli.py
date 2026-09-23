"""Command line interface: deploy, inspect, call and test the agent.

Usage:
    python -m frontdesk.cli doctor
    python -m frontdesk.cli plan
    python -m frontdesk.cli deploy
    python -m frontdesk.cli webcall
    python -m frontdesk.cli simulate
    python -m frontdesk.cli cleanup
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from .config import STATE_FILE, load_settings
from .dealer_profiles import get_dealer
from .flow import build_flow_payload, estimate_prompt_tokens
from .retell_client import RetellClient, RetellError, conversation_flow_engine
from .scenarios import (SCENARIOS, benchmark_journeys,
                      build_test_case_payloads)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def _banner(text: str) -> None:
    print(f"\n\033[1m{text}\033[0m")


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #

def cmd_doctor(args: argparse.Namespace) -> int:
    settings = load_settings()
    _banner("Environment")
    print(f"  API key          : ...{settings.api_key[-6:]}")
    print(f"  dealer           : {settings.dealer_id}")
    print(f"  tools base url   : {settings.tools_base_url or '(not set — live tools disabled)'}")

    _banner("Retell API")
    try:
        with RetellClient(settings.api_key) as client:
            voices = client.list_voices()
            agents = client.list_agents(limit=100)
            print(f"  voices available : {len(voices)}")
            print(f"  agents on account: {len(agents)}")
            state = load_state()
            if state.get("agent_id"):
                print(f"  deployed agent   : {state['agent_id']} (flow {state.get('flow_id')})")
    except RetellError as exc:
        print(f"  \033[31mFAILED\033[0m {exc}")
        return 1

    _banner("Tool server")
    base = settings.tools_base_url
    if not base:
        print("  skipped (TOOLS_BASE_URL not set)")
        print("  The flow will still deploy; validation is enforced by the flow's")
        print("  function nodes and simulation tests use tool mocks.")
    else:
        from .net import check

        ok, note = check(f"{base}/health")
        if ok:
            print(f"  {base}/health -> {note}")
        else:
            print(f"  \033[31mFAILED\033[0m {note}")
            print("  Is the server running and the tunnel pointed at it?")
            return 1
    return 0


# --------------------------------------------------------------------------- #
# plan — the architecture artefact, no API calls
# --------------------------------------------------------------------------- #

def cmd_plan(args: argparse.Namespace) -> int:
    settings = load_settings(require_api_key=False)
    dealer = get_dealer(settings.dealer_id)
    payload = build_flow_payload(dealer, settings.tools_base_url, settings.shared_secret)
    tokens = estimate_prompt_tokens(payload)

    _banner(f"Flow plan — {dealer.display_name} ({dealer.dealer_id})")
    print(f"  country / language : {dealer.country} / {dealer.language}")
    print(f"  dms                : {dealer.dms_vendor}")
    print(f"  capabilities       : {', '.join(sorted(dealer.capabilities)) or '(none)'}")
    print(f"  tools registered   : {len(payload['tools'])}")

    _banner("Nodes")
    header = f"  {'node':<24} {'type':<14} {'~tokens':>8}  edges"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for node in tokens["per_node"]:
        raw = next(n for n in payload["nodes"] if n["id"] == node["node"])
        edge_count = len(raw.get("edges", []))
        for special in ("always_edge", "else_edge"):
            if special in raw:
                edge_count += 1
        print(f"  {node['node']:<24} {node['type']:<14} {node['prompt_tokens']:>8}  {edge_count}")

    _banner("Prompt budget — the structural claim")
    print(f"  global prompt            : ~{tokens['global_prompt_tokens']} tokens")
    print(f"  nodes                    : {tokens['node_count']}")
    print(f"  largest node prompt      : ~{tokens['max_node_prompt_tokens']} "
          f"('{tokens['max_node']}')")
    print(f"  mean node prompt         : ~{tokens['mean_node_prompt_tokens']} tokens")
    print(f"  worst-case turn context  : ~{tokens['worst_case_turn_context_tokens']} tokens")
    print()
    print(f"  baseline monolith        : ~{tokens['monolith_baseline']['system_prompt_tokens']:,} "
          f"tokens in ONE prompt across {tokens['monolith_baseline']['nodes']} nodes")
    print()
    print("  The number matters less than the shape: no node needs to know about")
    print("  any other node, so nothing drifts and nothing is forgotten mid-call.")

    if args.json:
        print(json.dumps(tokens, indent=2))
    return 0


# --------------------------------------------------------------------------- #
# deploy
# --------------------------------------------------------------------------- #

def cmd_deploy(args: argparse.Namespace) -> int:
    settings = load_settings()
    dealer = get_dealer(settings.dealer_id)
    payload = build_flow_payload(dealer, settings.tools_base_url,
                                 settings.shared_secret, model=args.model)
    state = load_state()

    if args.dry_run:
        _banner("Dry run — payload that would be sent")
        print(json.dumps(payload, indent=2)[:4000])
        print(f"\n  ({len(json.dumps(payload))} bytes total)")
        return 0

    with RetellClient(settings.api_key) as client:
        # --- conversation flow -------------------------------------------- #
        _banner("Conversation flow")
        if state.get("flow_id"):
            flow = client.update_conversation_flow(state["flow_id"], payload)
            print(f"  updated flow {flow['conversation_flow_id']} (v{flow.get('version')})")
        else:
            flow = client.create_conversation_flow(payload)
            state["flow_id"] = flow["conversation_flow_id"]
            print(f"  created flow {flow['conversation_flow_id']} (v{flow.get('version')})")

        # --- agent --------------------------------------------------------- #
        _banner("Agent")
        voice_id = args.voice or client.pick_voice()
        agent_payload = build_agent_payload(
            dealer_name=dealer.display_name,
            dealer=dealer,
            flow_id=state["flow_id"],
            voice_id=voice_id,
        )
        if state.get("agent_id"):
            agent = client.update_agent(state["agent_id"], agent_payload)
            print(f"  updated agent {agent['agent_id']}")
        else:
            agent = client.create_agent(agent_payload)
            state["agent_id"] = agent["agent_id"]
            print(f"  created agent {agent['agent_id']}")

    state.update({
        "dealer_id": dealer.dealer_id,
        "voice_id": voice_id,
        "model": args.model,
        "tools_base_url": settings.tools_base_url,
    })
    save_state(state)

    _banner("Done")
    print(f"  agent_id : {state['agent_id']}")
    print(f"  flow_id  : {state['flow_id']}")
    print(f"  voice    : {voice_id}")
    print(f"  state    : {STATE_FILE}")
    print("\n  Next: python -m frontdesk.cli webcall")
    return 0


def build_agent_payload(*, dealer_name: str, dealer: Any, flow_id: str,
                        voice_id: str) -> dict[str, Any]:
    """Agent-level settings: audio behaviour, analytics, and safety rails.

    Note the split of responsibility. The flow owns *what to do*; the agent owns
    *how it sounds and what we measure*. Keeping them separate is what lets ten
    dealers share one flow but keep their own voice, language and metrics.
    """
    return {
        "agent_name": f"Frontdesk — {dealer_name} (modular booking)",
        "response_engine": conversation_flow_engine(flow_id),
        "voice_id": voice_id,
        "language": dealer.language,
        # Turn-taking. Responsiveness high enough not to feel sluggish on a
        # phone line, low enough not to talk over a caller spelling a plate.
        "responsiveness": 0.8,
        "interruption_sensitivity": 0.6,
        "voice_speed": 1.0,
        # Nudge a caller who has gone quiet — common when they are reading a
        # plate off a document. One reminder only; never nag.
        "reminder_trigger_ms": 9000,
        "reminder_max_count": 1,
        "begin_message_delay_ms": 400,
        # Biasing the ASR toward the vocabulary that actually appears on calls.
        "boosted_keywords": [
            dealer_name, "registration", "number plate", "service", "MOT",
            "booking", "appointment", "warranty", "invoice", "loan car",
            *dealer.services[:6],
        ],
        # This is the observability half of the answer: extract the fields that
        # let us find the frustrated tail, not just the average.
        "post_call_analysis_data": [
            {"type": "enum", "name": "call_outcome",
             "description": "How the call ended.",
             "choices": ["booked", "rescheduled", "cancelled", "info_only",
                         "transferred", "unresolved", "abandoned"],
             "required": True},
            {"type": "enum", "name": "primary_intent",
             "description": "The caller's main reason for calling.",
             "choices": ["booking", "rental", "warranty", "billing",
                         "complaint", "other"],
             "required": True},
            {"type": "boolean", "name": "plate_read_back",
             "description": "Was the registration plate read back phonetically "
                            "before being relied on?",
             "required": True},
            {"type": "boolean", "name": "plate_confirmed_by_caller",
             "description": "Did the caller explicitly confirm the plate?"},
            {"type": "boolean", "name": "booking_verified",
             "description": "Was the booking read back out of the DMS after writing?"},
            {"type": "string", "name": "booking_reference",
             "description": "The booking reference given to the caller, if any.",
             "examples": ["NM1001"]},
            {"type": "enum", "name": "customer_frustration",
             "description": "Did the caller become frustrated or repeat themselves?",
             "choices": ["none", "mild", "clear", "severe"],
             "required": True},
            {"type": "boolean", "name": "escalated_to_human",
             "description": "Was the caller transferred to a human?"},
            {"type": "string", "name": "agent_failure",
             "description": "If something went wrong, what did the agent get "
                            "wrong? Empty if nothing went wrong. Be specific."},
            {"type": "string", "name": "detail_lost",
             "description": "Any detail the agent forgot or contradicted during "
                            "the call. Empty if none."},
        ],
        "post_call_analysis_model": "gpt-4.1-mini",
        "data_storage_setting": "everything",
    }


# --------------------------------------------------------------------------- #
# webcall
# --------------------------------------------------------------------------- #

def cmd_webcall(args: argparse.Namespace) -> int:
    settings = load_settings()
    state = load_state()
    agent_id = args.agent_id or state.get("agent_id")
    if not agent_id:
        print("No agent deployed yet. Run: python -m frontdesk.cli deploy")
        return 1

    dealer = get_dealer(state.get("dealer_id", settings.dealer_id))
    variables = {
        "customer_name": args.customer_name or "Test Caller",
        "dealer_id": dealer.dealer_id,
    }

    # Tagged so a benchmark can pair a call with the journey it was meant to
    # exercise. Without this the calls are an unlabelled pile, and cost and
    # latency cannot be attributed to a scenario — which is most of the point
    # of running them.
    metadata: dict[str, Any] = {}
    if getattr(args, "journey", None):
        metadata["benchmark_journey"] = args.journey
    if getattr(args, "arch", None):
        metadata["benchmark_arch"] = args.arch

    with RetellClient(settings.api_key) as client:
        call = client.create_web_call(agent_id, dynamic_variables=variables,
                                      metadata=metadata or None)

    token = call["access_token"]
    call_id = call["call_id"]
    print(f"\n  call_id      : {call_id}")
    print(f"  access_token : {token[:16]}…")

    web_dir = STATE_FILE.parent / "web"
    page = web_dir / "index.html"
    payload_file = web_dir / "call.json"
    payload_file.write_text(json.dumps({"accessToken": token, "callId": call_id}))

    # The token goes in the URL fragment, so the page also works when opened
    # straight off the filesystem with no local server.
    url = f"{page}#token={token}"
    print(f"  token file   : {payload_file}")
    print("\n  Open this in a browser (Chrome/Edge/Safari) and allow the mic:")
    print(f"    open '{url}'")
    return 0


# --------------------------------------------------------------------------- #
# simulate
# --------------------------------------------------------------------------- #

def cmd_simulate(args: argparse.Namespace) -> int:
    settings = load_settings()
    state = load_state()
    flow_id = state.get("flow_id")
    if not flow_id:
        print("No flow deployed yet. Run: python -m frontdesk.cli deploy")
        return 1

    engine = conversation_flow_engine(flow_id)

    with RetellClient(settings.api_key) as client:
        _banner(f"Creating {len(SCENARIOS)} test case definitions")

        existing = {}
        try:
            for case in client.list_test_case_definitions(limit=100):
                existing[case.get("name")] = case.get("test_case_definition_id")
        except RetellError as exc:
            print(f"  (could not list existing cases: {exc})")

        case_ids: list[str] = []
        for payload in build_test_case_payloads(engine):
            name = payload["name"]
            try:
                if name in existing:
                    client.delete_test_case_definition(existing[name])
                created = client.create_test_case_definition(payload)
                case_ids.append(created["test_case_definition_id"])
                print(f"  + {name}")
            except RetellError as exc:
                print(f"  ! {name}: {exc}")

        if not case_ids:
            print("\nNo test cases could be created.")
            return 1

        _banner("Running batch test")
        batch = client.create_batch_test(case_ids, engine)
        batch_id = batch["test_case_batch_job_id"]
        print(f"  batch id: {batch_id}")

        deadline = time.time() + args.timeout
        last_status = None
        while time.time() < deadline:
            batch = client.get_batch_test(batch_id)
            status = batch["status"]
            if status != last_status:
                print(f"  status: {status} "
                      f"(pass {batch['pass_count']} / fail {batch['fail_count']} "
                      f"/ error {batch['error_count']} of {batch['total_count']})")
                last_status = status
            if status == "complete":
                break
            time.sleep(5)

        _banner("Result")
        total = batch.get("total_count", 0)
        passed = batch.get("pass_count", 0)
        rate = (passed / total * 100) if total else 0.0
        print(f"  pass rate: {passed}/{total} ({rate:.0f}%)")
        print(f"  fail: {batch.get('fail_count')}  error: {batch.get('error_count')}")
        if passed < total:
            print("\n  Inspect failures in the Retell dashboard, then add the")
            print("  failing conversation to the scenario list so it can never")
            print("  regress silently again.")

        state.setdefault("test_case_ids", [])
        state["test_case_ids"] = case_ids
        state.setdefault("batch_ids", [])
        state["batch_ids"] = [*state.get("batch_ids", []), batch_id]
        save_state(state)
    return 0


# --------------------------------------------------------------------------- #
# benchmark
# --------------------------------------------------------------------------- #

BENCHMARK_FILE = STATE_FILE.with_name(".frontdesk-benchmark.json")


def _benchmark_state() -> dict[str, Any]:
    if BENCHMARK_FILE.exists():
        try:
            return json.loads(BENCHMARK_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_benchmark(data: dict[str, Any]) -> None:
    BENCHMARK_FILE.write_text(json.dumps(data, indent=2) + "\n")


def _batch_case_ids(state: dict[str, Any]) -> dict[str, dict[str, str]]:
    """The batch-arm case ids, tolerating the older flat layout.

    Returns ``{architecture: {slug: case_id}}``. Reading the wrong arm here would
    grade one set of journeys and label the results with another, which no
    downstream check could detect.
    """
    cases = state.get("cases", {})
    if "batch" in cases or "voice" in cases:
        return cases.get("batch", {})
    # Written before cases were namespaced by arm.
    return cases


def _benchmark_plan(cap_usd: float) -> int:
    """Show what the suite covers and what it is expected to cost. Spends nothing."""
    from .benchmark import ARCHITECTURES, estimate_case_cost

    print("\n  journeys by arm")
    for arm in ("batch", "voice"):
        journeys = benchmark_journeys(arm)
        print(f"\n    {arm} arm ({len(journeys)})")
        for j in journeys:
            note = " [dealer_can_book=false]" if j.get("dynamic_variables") else ""
            print(f"      {j['slug']:<18} {len(j['metrics'])} metrics{note}")
        if arm == "batch":
            per = sum(estimate_case_cost(j) for j in journeys)
            print(f"      estimated batch cost: ${per:.2f} per architecture "
                  f"(per-message rates, not billed-verified)")
        else:
            print(f"      voice cost is billed per minute: measured, "
                  f"~$0.31/call historically")

    print(f"\n  budget : ${cap_usd:.2f} per architecture")
    for name, spec in ARCHITECTURES.items():
        print(f"    {name:<9} {spec['agent_id']}  {spec['label']}")
    print("\n  next   : python -m frontdesk.cli benchmark prepare")
    return 0


def _voice_script() -> str:
    """The manual call script: exactly what to say, and in what order.

    Written out rather than left to improvisation because the voice arm is the
    expensive one and a call that wanders is a wasted minute. Both architectures
    must hear the same caller, or the comparison measures the caller.
    """
    from .benchmark import ARCHITECTURES

    lines = [
        "# Benchmark calls — manual script",
        "",
        "Two journeys, on both architectures. Four calls in total, roughly",
        "$0.31 each. Read the caller's part as written: the architectures are",
        "being compared, so the caller has to be the same in both or the",
        "comparison measures the caller instead of the flow.",
        "",
        "Start each call with the command below (it tags the call so the report",
        "can pair it with the journey), then open the printed link, allow the",
        "mic, and speak the script. Hang up when the agent has finished; do not",
        "let it run on, because cost is billed per minute.",
        "",
        "## What the live system will actually say",
        "",
        "The tool mocks used by the batch arm do **not** apply to a real call, so",
        "the live DMS answers instead. It is seeded with a deliberately ambiguous",
        "plate, which is worth knowing before you dial — otherwise the agent asks",
        "a question the script has not prepared you for:",
        "",
        "- `AB12CDE` returns **two** vehicles, not one:",
        "  - 2021 BMW 3 Series 320i, **grey**",
        "  - 2019 Mercedes-Benz C 200 AMG Line, blue",
        "",
        "  When the agent asks which you mean, say **\"the grey BMW\"**. This is the",
        "  disambiguation branch, and both architectures should handle it the same",
        "  way — it is one of the behaviours being compared.",
        "",
        "- Availability for 1–7 October is six slots: 08:30, 11:00 and 14:30 on",
        "  both Thursday 1 and Friday 2. Ask for, and accept, **Thursday 1 October",
        "  at 08:30** in both calls, so the two architectures follow the same path.",
        "",
        "- The rating at the end writes to the tool server's `/feedback`, so what",
        "  you say is recorded and can be shown afterwards.",
        "",
        "- Registration speech: the agent will ask you to spell it. Say",
        "  \"A, B, one, two, C, D, E\" — letters as words, the digits as digits.",
        "",
    ]
    for journey in benchmark_journeys("voice"):
        lines += [f"## {journey['slug']}", "",
                  f"*{journey['name']}*", "",
                  "Command:", "", "```"]
        for arch, spec in ARCHITECTURES.items():
            lines.append(
                f"python -m frontdesk.cli webcall --agent-id {spec['agent_id']} "
                f"--journey {journey['slug']} --arch {arch}"
            )
        lines += ["```", "", "What to say:", "", "> "
                  + journey["user_prompt"].replace("\n", " "), "",
                  "Target: 2–3 minutes. Call it twice per architecture.", ""]
    return "\n".join(lines)


def cmd_benchmark(args: argparse.Namespace) -> int:
    from .benchmark import (ARCHITECTURES, Budget, BudgetExceeded, build_report,
                            call_stats, calls_for, estimate_case_cost,
                            measure_case_cost, prepare_cases, render,
                            resolve_architectures, run_batch,
                            transcripts_from_run)

    settings = load_settings()

    if args.action == "plan":
        return _benchmark_plan(args.cap_usd)

    with RetellClient(settings.api_key) as client:
        arches = resolve_architectures(client, args.arch)
        journeys = benchmark_journeys(args.arm)

        # ---------------------------------------------------------- prepare
        if args.action == "prepare":
            _banner(f"Creating test cases for the {args.arm} arm")
            state = _benchmark_state()
            for arch, spec in arches.items():
                prepared, _ = prepare_cases(client, arch, spec, journeys)
                for p in prepared:
                    print(f"  [{arch}] {p.slug} -> {p.case_id}")
                # Namespaced by arm. One flat map per architecture meant that
                # preparing the voice arm silently replaced the batch arm's
                # cases, so a later batch run would have graded the wrong
                # journeys — and still reported clean pass/fail.
                state.setdefault("cases", {}).setdefault(args.arm, {})[arch] = {
                    p.slug: p.case_id for p in prepared
                }
            _save_benchmark(state)

            if args.arm == "voice":
                path = STATE_FILE.parent / "scenarios" / "10-benchmark-calls.md"
                path.write_text(_voice_script())
                _banner("Manual call script written")
                print(f"  {path}")
                print("  Open it, then run each command printed there.")
            return 0

        # --------------------------------------------------------------- run
        if args.action == "run":
            if args.arm != "batch":
                print("  The voice arm is run by making calls, not by this "
                      "command.\n  Run `benchmark prepare --arm voice` for the "
                      "script, then `benchmark report`.")
                return 1
            if not args.yes:
                print("  Refusing to spend without --yes.")
                print("  Re-run: python -m frontdesk.cli benchmark run --arm "
                      "batch --yes")
                return 1

            state = _benchmark_state()
            case_ids = _batch_case_ids(state)
            if not case_ids:
                print("  No prepared cases. Run: benchmark prepare --arm batch")
                return 1

            # One budget *per architecture*, because that is how the cap was
            # specified. A single shared total would let a cheap first arm
            # starve the second, and would block a run that is well inside its
            # own ceiling.
            budgets: dict[str, Any] = {}
            results: dict[str, Any] = state.get("runs", {})

            for arch, spec in arches.items():
                prior = results.get(arch, {})
                budget = Budget(cap_usd=args.cap_usd, label=spec["label"])
                budgets[arch] = budget

                # Seed the guard with what this architecture has already cost.
                # A fresh budget per invocation is not a cap: the first run
                # spends up to the limit, and so does every run after it. That
                # is how the monolith reached $2.06 against a $2.00 ceiling.
                spent_before = float(prior.get("batch_cost_usd_total",
                                               prior.get("batch_cost_usd", 0.0)) or 0.0)
                if spent_before:
                    budget.charge(spent_before, f"{arch} earlier runs",
                                  measured=False)

                ids = list(case_ids.get(arch, {}).values())
                if not ids:
                    continue

                # Already done and paid for. Re-running would spend the money
                # again for the same answer, and a benchmark that quietly
                # double-bills itself is one nobody will trust with a budget.
                if prior.get("accuracy") and not getattr(args, "force", False):
                    print(f"\n  {arch}: already recorded "
                          f"({prior.get('batch_id')}) — skipping. "
                          f"Use --force to re-run and re-pay.")
                    continue

                estimate = sum(estimate_case_cost(j) for j in journeys)
                try:
                    budget.afford(estimate, f"{arch} batch ({len(ids)} cases)")
                except BudgetExceeded as exc:
                    print(f"\n  STOP: {exc}")
                    continue

                batch = run_batch(client, spec, ids, timeout_s=args.timeout)
                _banner(f"Batch test results — {spec['label']}")
                print(f"  batch {batch['test_case_batch_job_id']}: "
                      f"pass {batch.get('pass_count')} / "
                      f"fail {batch.get('fail_count')} / "
                      f"error {batch.get('error_count')}")

                runs = client.list_test_runs(
                    batch["test_case_batch_job_id"], limit=None)
                by_case = {r["test_case_definition_id"]: r for r in runs}
                accuracy: dict[str, tuple[str, str]] = {}
                spent = 0.0
                for slug, cid in case_ids[arch].items():
                    run = by_case.get(cid, {})
                    status = run.get("status", "error")
                    explanation = (run.get("result_explanation") or "")[:400]
                    accuracy[slug] = (status, explanation)
                    spent += measure_case_cost(transcripts_from_run(run))
                budget.charge(spent, f"{arch} batch", measured=False)
                # Accumulated, not replaced. A re-run adds to what this
                # architecture has cost; keeping only the latest figure would
                # make the spend look smaller on every retry, which is exactly
                # backwards for something used as a budget guard.
                spent_total = float(
                    prior.get("batch_cost_usd_total",
                              prior.get("batch_cost_usd", 0.0)) or 0.0) + spent
                results[arch] = {
                    "batch_id": batch["test_case_batch_job_id"],
                    "accuracy": {k: list(v) for k, v in accuracy.items()},
                    "batch_cost_usd": round(spent, 4),
                    "batch_cost_usd_total": round(spent_total, 4),
                    "batch_attempts": int(prior.get("batch_attempts", 0)) + 1,
                }

            state["runs"] = results
            state["cap_usd"] = args.cap_usd
            _save_benchmark(state)
            for arch, budget in budgets.items():
                print(f"  {arch:<10} batch spend ${budget.spent_usd:.2f} "
                      f"of ${budget.cap_usd:.2f}")

        # ------------------------------------------------------------ report
        reports = []
        state = _benchmark_state()
        for arch, spec in arches.items():
            run = state.get("runs", {}).get(arch, {})
            accuracy = {k: tuple(v) for k, v in run.get("accuracy", {}).items()}
            stats = [s for s in (call_stats(arch, c)
                                 for c in calls_for(client, spec["agent_id"]))
                     if s]
            # Only calls tagged for this benchmark count as benchmark spend.
            # The rest belong to the agent but not to this exercise.
            tagged = [s for s in stats if s.journey]
            reports.append(build_report(arch, spec, accuracy, tagged,
                                        run.get("batch_cost_usd_total",
                                                run.get("batch_cost_usd", 0.0)),
                                        other_calls=[s for s in stats if not s.journey]))

        print(render(reports))
        print(f"\n  cap: ${args.cap_usd:.2f} per architecture "
              f"(batch estimates are not billed-verified; voice cost is)")
        return 0


# --------------------------------------------------------------------------- #
# cleanup
# --------------------------------------------------------------------------- #

def cmd_cleanup(args: argparse.Namespace) -> int:
    settings = load_settings()
    state = load_state()
    if not state:
        print("Nothing to clean up.")
        return 0

    with RetellClient(settings.api_key) as client:
        for agent_id in filter(None, [state.get("agent_id")]):
            try:
                client.delete_agent(agent_id)
                print(f"  deleted agent {agent_id}")
            except RetellError as exc:
                print(f"  ! agent {agent_id}: {exc}")
        for flow_id in filter(None, [state.get("flow_id")]):
            try:
                client.delete_conversation_flow(flow_id)
                print(f"  deleted flow {flow_id}")
            except RetellError as exc:
                print(f"  ! flow {flow_id}: {exc}")
        for case_id in state.get("test_case_ids", []):
            try:
                client.delete_test_case_definition(case_id)
            except RetellError as exc:
                print(f"  ! test case {case_id}: {exc}")
        print(f"  deleted {len(state.get('test_case_ids', []))} test case definitions")

    STATE_FILE.unlink(missing_ok=True)
    print("  removed local state file")
    return 0


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m frontdesk.cli",
        description="Deploy and test a modular, guardrailed Retell voice agent.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="check credentials and connectivity")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("plan", help="print the flow plan and prompt budget")
    p.add_argument("--json", action="store_true", help="also dump token stats as JSON")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("deploy", help="create or update the flow and agent")
    p.add_argument("--dry-run", action="store_true", help="print the payload only")
    p.add_argument("--voice", default=None, help="Retell voice_id to use")
    p.add_argument("--model", default="gpt-4.1", help="LLM for the conversation flow")
    p.set_defaults(func=cmd_deploy)

    p = sub.add_parser("webcall", help="start a browser call and write the token")
    p.add_argument("--agent-id", default=None)
    p.add_argument("--customer-name", default=None)
    p.add_argument("--journey", default=None,
                   help="benchmark journey slug, recorded in call metadata")
    p.add_argument("--arch", default=None,
                   help="architecture label, recorded in call metadata")
    p.set_defaults(func=cmd_webcall)

    p = sub.add_parser("adapters",
                       help="show the DMS capability matrix and run conformance")
    p.add_argument("--conformance", action="store_true",
                   help="run the contract suite against every adapter")
    p.set_defaults(func=cmd_adapters)

    p = sub.add_parser("diagram",
                       help="serve the interactive flow diagram (client/business/technical)")
    p.add_argument("--agent-id", default=None,
                   help="which agent to inspect (default: the deployed one)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8100)
    p.add_argument("--calls-limit", type=int, default=50,
                   help="how many recent calls to sample for cost and latency")
    p.add_argument("--no-open", action="store_true", help="do not open a browser")
    p.set_defaults(func=cmd_diagram)

    p = sub.add_parser("simulate", help="create and run the regression suite")
    p.add_argument("--timeout", type=int, default=900, help="seconds to wait")
    p.set_defaults(func=cmd_simulate)

    p = sub.add_parser("benchmark",
                       help="compare the monolith and subflow architectures")
    p.add_argument("action", nargs="?", default="plan",
                   choices=["plan", "prepare", "run", "report"],
                   help="plan: show the suite and cost estimate (free); "
                        "prepare: create test cases and write the call scripts "
                        "(free); run: execute an arm (spends); "
                        "report: combine and print")
    p.add_argument("--arm", choices=["batch", "voice"], default="batch",
                   help="which arm to run (default: batch)")
    p.add_argument("--arch", choices=["monolith", "modular"], default=None,
                   help="restrict to one architecture")
    p.add_argument("--cap-usd", type=float, default=2.00,
                   help="hard spend cap per architecture (default: 2.00)")
    p.add_argument("--timeout", type=int, default=1200, help="seconds to wait")
    p.add_argument("--yes", action="store_true",
                   help="required to spend: without it, `run` only prints the plan")
    p.add_argument("--force", action="store_true",
                   help="re-run an architecture that already has results (re-pays)")
    p.set_defaults(func=cmd_benchmark)

    p = sub.add_parser("cleanup", help="delete deployed resources")
    p.set_defaults(func=cmd_cleanup)

    return parser


def cmd_diagram(args: argparse.Namespace) -> int:
    """Serve the flow diagram locally.

    Deliberately a *separate* process from `deploy` and the tool server: this is
    an operator view that holds the API key, not part of the agent's runtime.
    """
    settings = load_settings()  # fail early, with a useful message, on a bad key
    print(f"  api key : ...{settings.api_key[-6:]}")
    print(f"  dealer  : {settings.dealer_id}")

    from .viewserver import serve

    serve(
        args.agent_id,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
        calls_limit=args.calls_limit,
    )
    return 0


def cmd_adapters(args: argparse.Namespace) -> int:
    """Print what each dealer's DMS connection can actually do.

    This is the command to run *before* promising a dealer a feature, and the
    one to run after adding an adapter to find out whether it is safe.
    """
    from .dealer_profiles import all_dealers
    from .dms import get_dms, known_vendors, run_conformance, summarise

    adapters = [get_dms(dealer.dealer_id) for dealer in all_dealers()]

    print("\n\033[1mDMS capability matrix\033[0m")
    print("Capability is a property of the connection, not the product — two\n"
          "dealers on the same vendor can differ.\n")
    print(summarise(adapters))

    print("\n\033[1mRegistered vendors\033[0m")
    print("  " + ", ".join(known_vendors()))

    print("\n\033[1mPer dealer\033[0m")
    for dealer in all_dealers():
        adapter = get_dms(dealer.dealer_id)
        print(f"  {dealer.dealer_id}")
        print(f"    {dealer.display_name} ({dealer.country})")
        print(f"    vendor      : {adapter.vendor}")
        print(f"    integration : {adapter.integration}")
        print(f"    note        : {dealer.integration_note}")

    if args.conformance:
        print("\n\033[1mConformance\033[0m")
        failures = 0
        for adapter in adapters:
            try:
                checks = run_conformance(adapter, unique=f"cli-{adapter.vendor}")
            except AssertionError as exc:
                failures += 1
                print(f"  \033[31mFAIL\033[0m {adapter.vendor:22} {exc}")
            else:
                print(f"  \033[32mPASS\033[0m {adapter.vendor:22} {len(checks)} checks")
        print()
        if failures:
            print(f"\033[31m{failures} adapter(s) failed conformance.\033[0m "
                  f"Do not point a customer at these.")
            return 1
        print("Every adapter satisfies the contract.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RetellError as exc:
        print(f"\n\033[31mRetell API error:\033[0m {exc}")
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
