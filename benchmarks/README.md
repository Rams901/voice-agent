# Benchmarks

The comparison between the two flow architectures, and the result of the last run.

## What is being compared

The same conversation, built two ways:

| Architecture | Shape |
|---|---|
| **monolith** | one flow, 39 nodes, one large prompt |
| **modular** | 22-node orchestrator routing between six subflow components |

They are separate deployed agents, resolved by id and **validated by name**. That
check exists because a benchmark that silently measured the same agent twice
would produce a clean, plausible and completely wrong report.

## Two arms, because the platform bills and reports them differently

| Arm | How it runs | Bills | Returns | Used for |
|---|---|---|---|---|
| `batch` | text simulation | per message | pass/fail + transcript. **No latency, no cost** | accuracy |
| `voice` | real web calls | per minute | `latency.*` percentiles + itemised `call_cost` | latency and true cost |

The voice arm is expensive, so it is spent on two journeys whose length brackets
a typical call: `multi-skill` and `happy-control`.

Neither arm alone answers all three questions:

- **Accuracy** — does splitting a skill into a component lose context across the
  boundary, or route to the wrong outcome?
- **Latency** — a component boundary is an extra transition. Does it add an LLM
  round-trip per journey?
- **Cost** — more transitions mean more tokens, but smaller prompts mean fewer
  tokens per turn. The direction is genuinely unknown, which is why it is
  measured rather than argued.

## The budget is a control, not an estimate

The cap is the binding constraint on the whole design, so it is enforced in code
rather than trusted to arithmetic in someone's head. `Budget` refuses to start
work it cannot afford and accounts for every run afterwards.

Batch cost **cannot be read back** from the API. It is estimated from the real
message counts in the returned transcript, at published per-message rates, and
labelled as an estimate everywhere it appears. A number that looks measured but
is not would be worse than no number.

## Running it

```bash
python -m frontdesk.cli benchmark --help
```

The architecture ids are **account-scoped** and will not exist in a fresh Retell
account. Override them:

```bash
# .env
FRONTDESK_MONOLITH_AGENT_ID=agent_...
FRONTDESK_MONOLITH_FLOW_ID=conversation_flow_...
FRONTDESK_MODULAR_AGENT_ID=agent_...
FRONTDESK_MODULAR_FLOW_ID=conversation_flow_...
```

After `cli deploy`, the modular ids are in `.frontdesk.json`.

## Results

| File | What it is |
|---|---|
| [`results/2026-09-23-architecture-benchmark.json`](results/2026-09-23-architecture-benchmark.json) | One complete run: per-case verdicts, grader reasoning, batch ids, costs, and the cap that was enforced |

Run outputs are gitignored (`.frontdesk-benchmark.json`) because they contain
account-scoped ids. To commit one, copy it here with a dated filename.

### How to read a run

1. **Check the cap held.** `runs.*.batch_cost_usd_total` against `cap_usd`.
2. **Read the reasoning, not the pass column.** A verdict without its reasoning
   hides the interesting failures.
3. **Look for cases where both arms pass but for different reasons.** That is
   usually a grader that is passing on absence rather than on evidence.

Known caveat from the committed run: the monolith arm reported `pass` on
`rating-declined` and `gate-booking-off` while failing `async-pending` on a
single word — the agent used "booked" as if the write had settled. The accuracy
difference between the arms is one case out of four, which is too small to be a
claim. Report it as a direction, not a result.
