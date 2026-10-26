# Voice onboarding — start here

You are picking up the voice side of a modular, guardrailed booking agent that
runs on Retell. This document is the map: what exists, where the voice surface
actually is, what to tune, what to listen for, and what is still open.

Read [CONTRIBUTING.md](../CONTRIBUTING.md) first for setup and the architectural
rule. This document assumes you have run `make test` and `make plan`.

---

## 1 · The one thing to understand before touching anything

The conversation is probabilistic. The transaction is not.

```
caller speech ──▶ ASR ──▶ router ──▶ skill nodes ──▶ [GATE] ──▶ tool ──▶ verify
                            │                          │
                     may mishear                  must be code
```

Everything to the left of a gate can be wrong, and the design assumes it will be.
Everything at and after a gate must be exact, and is therefore **not** the
model's decision.

This is why there are function nodes and equation edges in the flow rather than a
long prompt. If you find yourself writing "make sure the model doesn't…" in a
prompt, that is the signal that the control belongs in code instead.

The two failures that motivated the whole redesign were both violations of this
boundary in different directions:

| Failure | What actually happened |
|---|---|
| Long-call forgetting | One 14k-token prompt holding 190 nodes; rules competed, adherence drifted, facts lived only in the transcript |
| Misheard plate booked anyway | Eight missing controls in a row; no format mask, no read-back, no confirmation, no verification of the write |

Neither is fixed by a better model. Both are fixed by structure.

---

## 2 · Where the voice surface is

There are **two** places voice is configured, and they are deliberately separate.

### Agent level — how it sounds and what is measured

`build_agent_payload` in [`frontdesk/cli.py`](../frontdesk/cli.py). This is the
file you will spend most of your time in.

| Setting | Current | Why it is set there |
|---|---|---|
| `responsiveness` | `0.8` | High enough not to feel sluggish on a phone line, low enough not to talk over someone spelling a plate |
| `interruption_sensitivity` | `0.6` | Callers self-correct mid-sentence ("no, B not D"); cutting in early breaks that |
| `voice_speed` | `1.0` | Neutral by default |
| `reminder_trigger_ms` | `9000` | People go quiet while reading a plate off a document |
| `reminder_max_count` | `1` | One nudge, never nagging |
| `begin_message_delay_ms` | `400` | Gives the greeting room to land after pickup |
| `boosted_keywords` | dealer + domain vocabulary | ASR biasing toward words that actually appear on calls |
| `language` | per dealer | Set from the dealer profile, not hardcoded |

The design note in the code is worth keeping: **the flow owns _what to do_; the
agent owns _how it sounds and what we measure_**. That split is what lets ten
dealers share one flow but keep their own voice, language and metrics.

### Node level — what it says

`flow.py`. Prompts are short by design (~309 tokens global, largest node ~133,
worst-case turn ~442). The budget is enforced and printed by `make plan`.

If a prompt grows, the question is not "how do I fit it" but **"which node should
own this instead"**.

---

## 3 · First hour: listen before you change

Do not tune anything before you have heard the thing. Numbers in a config file
are hypotheses; a recording is evidence.

```bash
cp .env.example .env         # RETELL_API_KEY
./scripts/dev_tunnel.sh --deploy
python -m frontdesk.cli webcall
```

Then call it and deliberately try to break it:

| Try this | What you are testing |
|---|---|
| Spell a plate with ambiguous letters ("B as in Bravo, no, D") | Interruption handling and the repair loop |
| Give a plate that is *valid format but not in the DMS* | Reconcile step — it must not book |
| Go quiet for 10 seconds while "reading a document" | Reminder fires once, not twice |
| Ask for a booking on a dealer where booking is gated off | Capability degradation, not a failure |
| Talk over the greeting | `begin_message_delay_ms` and barge-in |
| Ask an out-of-scope question mid-flow | Handoff carries context, not just a transfer |
| Say a date, then change it two turns later | Whether the second value wins cleanly |

Write down **latency you can feel** and **moments you had to repeat yourself**.
Those two are the voice-specific quality signals that no unit test covers.

---

## 4 · The measurement loop

Three layers, cheapest first. Use them in this order — do not start with live
calls, they are the most expensive and the least repeatable.

```
make test                       # 235 tests, no network, ~1s
python -m frontdesk.cli simulate    # batch text sim against the deployed flow
python -m frontdesk.cli benchmark   # cost/latency/accuracy, budget-capped
```

### `simulate` — accuracy, cheap

Creates and runs a Retell test batch from the journeys in `scenarios.py`. Returns
pass/fail plus a transcript, **no latency and no cost fields**. This is where you
iterate on behaviour.

### `benchmark` — the arm that actually measures voice

Two arms, and they answer different questions:

| Arm | What it costs | What it returns |
|---|---|---|
| `batch` | Per message | Accuracy, transcript. **No latency, no cost.** |
| `voice` | Per minute | `latency.*` percentiles and itemised `call_cost` |

The voice arm is spent on two journeys only — `multi-skill` and `happy-control` —
because real calls are expensive and those two bracket a typical call length.

```bash
python -m frontdesk.cli benchmark --help     # modes and the budget cap
```

**The budget cap refuses to start work it cannot afford.** If you are iterating,
raise it deliberately rather than discovering the overrun afterwards.

### Reading the result

The last committed run is in
[`benchmarks/results/`](../benchmarks/results/), with the method in
[`benchmarks/README.md`](../benchmarks/README.md).

Two things to internalise from it:

1. **The batch arm's cost is an estimate**, derived from real message counts in
   the returned transcript at published rates. It is labelled an estimate
   everywhere, because a number that looks measured but is not is worse than no
   number.
2. **A green suite can be lying.** In the last run, the monolith passed
   `rating-declined` and `gate-booking-off` while failing `async-pending` — and
   the failure was a single word ("booked" used as if settled). Read the grader's
   reasoning, not just the pass column.

---

## 5 · Known gaps — the actual work

These are honest and documented, not hidden.

### Voice-specific

| Gap | Where | What good looks like |
|---|---|---|
| **No latency budget in CI** | — | A p50/p90 threshold from the voice arm that fails the build when exceeded |
| **Latency is modelled, not measured** | `insights.py` | One connected call is enough to replace the model with observed values; the account currently has none that connected |
| **Barge-in is untested** | — | A scenario that talks over the agent mid-plate and asserts the repair loop still lands |
| **No ASR error-rate measurement** | — | Plate capture accuracy across N calls, not one |
| **`boosted_keywords` is hand-picked** | `cli.py` | Derived from real transcripts once there is traffic |
| **Pronunciation of dealer names** | — | Per-dealer phonetic hints, not one global list |

### Structural

| Gap | Where |
|---|---|
| DMS is mocked (`memory.py` is the control case) | `dms/` |
| Tool server state is in-memory and vanishes on restart | `tool_server/app.py` |
| ~10 scenarios is a starting set, not a regression net | `scenarios.py` |
| Benchmark ids are account-scoped | `benchmark.py`, overridable via `FRONTDESK_*_AGENT_ID` |

**The most valuable thing you can build** is production transcript replay. The
simulation suite is bounded by the imagination of whoever wrote it; real traffic
is not. The design already returns `post_call_analysis_data` fields purpose-built
for this — `agent_failure`, `detail_lost`, `customer_frustration`,
`plate_read_back`, `booking_verified` — so collecting is a matter of listening.

---

## 6 · Tuning rules

1. **One knob at a time.** `responsiveness` and `interruption_sensitivity`
   interact; changing both means learning nothing.
2. **Re-run the voice arm, not one call.** A single call is an anecdote. Two
   journeys is still small — know that you are reading a signal, not a proof.
3. **Change the flow before the voice.** Most "the agent feels slow" reports are
   extra turns, not audio settings. Count the turns first.
4. **Never tune against the grader.** If you change a prompt to make a scenario
   pass, check whether the scenario was asserting the right thing in the first
   place.
5. **Write down what you heard.** "Felt sluggish" and "p90 went from 1.9s to
   2.4s" are different artefacts, and only one survives a week.

---

## 7 · Where to go next

- `flow.py` — the router, the skills, the deterministic spine
- `frontdesk/tools.py` — the tool definitions the voice model can call
- `scenarios/README.md` — the conversation catalogue and the variable model
- [`docs/retell-semantics.md`](retell-semantics.md) — what Retell's transitions,
  dynamic variables and "fine-tuning" actually do, with the undocumented parts
  called out
- [`README.md`](../README.md) — the design rationale and the benchmark
