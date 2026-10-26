# Contributing

## The one rule

**The model proposes; code disposes.**

Every design decision in this repo follows from it. A model may produce a
candidate value, a sentence, or an intent. It may not be the thing that decides a
value is valid, that a write happened, or that a caller was told the truth.

If you are adding a feature, ask: *is this probabilistic or deterministic?* Put
it on the correct side of the line.

| Probabilistic (prompt / model) | Deterministic (code / edge / tool) |
|---|---|
| Which skill a caller wants | Whether an identifier is valid |
| How to phrase a question | When a gate is satisfied |
| What the caller probably meant | Whether a booking exists |
| Repairing a misheard letter | Whether money moved |

Concretely, this means:

- **Use function nodes, not prompt instructions, for anything that must happen.**
  The model does not get to decide whether a plate was validated.
- **Use equation edges, not prompt conditions, for gates.** `identifier_valid ==
  "true"` is a fact. "The model thought it looked fine" is not.
- **Every split needs an `else` edge.** A flow that cannot get stuck is a flow
  that cannot strand a caller.
- **Errors are data, not exceptions.** Return a structured result the flow can
  branch on, so a caller never hears "error".

## Setup

```bash
git clone <repo-url> && cd frontdesk
make install
cp .env.example .env      # optional — only needed for API work
make check                # lint + tests; this is what CI runs
```

Python 3.10+. The tests need no credentials and no network.

```bash
make test        # pytest, whole suite
make lint        # ruff check
make fmt         # ruff --fix, then review the diff
make check       # lint + test — run before you push
make plan        # prompt budget, no API calls
make diagram     # local diagram server on :8100
```

## The gate

CI runs on every push and pull request: `ruff check .` and `pytest` on Python
3.10, 3.11 and 3.12.

A change is mergeable when:

1. `make check` passes locally.
2. New behaviour has a test that would **fail without the change**. Verify this
   by reverting the change and watching it go red — a test that passes both ways
   is decoration.
3. If you touched the flow, `test_journey_accounts_for_every_node_in_the_flow`
   still passes, or you updated `journey.py` deliberately.
4. If you touched the prompt, `make plan` still shows a budget in the expected
   range.

## Never commit

These are gitignored because they are per-account or per-machine. Do not
force-add them:

| Path | Why |
|---|---|
| `.env` | Credentials |
| `.frontdesk.json`, `.frontdesk-components.json` | Deployed resource ids from *your* Retell account |
| `.frontdesk-benchmark.json` | Run output, account-scoped ids and costs |
| `pricing.json` | Your account's real unit prices |
| `.cache/` | Cached API responses |
| `web/call.json` | A live call token |

If you think you have committed a secret: rotate the key first, then tell the
maintainer. Removing the commit does not un-leak it.

## Adding things

### A new DMS adapter

1. Read `dms/base.py` — the nine-method contract and the capability vocabulary.
2. Read `docs/dms-integration.md` §4 for the sequence.
3. Implement the adapter. Declare *only* the capabilities you actually support;
   the flow degrades honestly against the matrix.
4. `dms/conformance.py` runs automatically against every registered adapter. If
   you had to special-case the suite, the adapter is wrong, not the suite.
5. Register it in `dms/registry.py` and add a dealer profile if needed.
6. `python -m frontdesk.cli adapters --conformance` must stay green.

### A new scenario

Scenarios are the regression net. Add the conversation to `scenarios/NN-name.md`,
then the assertions to `scenarios.py`.

The rule: **assert the mechanism, not the wording.** "The agent did not confirm a
booking" is falsifiable. "The agent said exactly this sentence" is a maintenance
liability that fails on every prompt tweak.

### A change to the voice experience

See [docs/onboarding-voice.md](docs/onboarding-voice.md). Tuning lives in
`cli.py` (`responsiveness`, `interruption_sensitivity`, `reminder_trigger_ms`,
`reminder_max_count`) and in the node prompts in `flow.py`.

Change one knob at a time, and re-run the voice benchmark arm rather than
trusting one call.

## Style

- Comments explain **why**, not what. The most valuable comment in this repo is
  the one above `terminal`-looking code that says which failure mode the next
  line defends against.
- Prefer naming a real failure over describing a hypothetical. "Retell rejects
  the whole flow with `Duplicate node id: booking_done`" is worth more than
  "avoid collisions".
- Type hints on public functions. `from __future__ import annotations` at the top.
- No new dependency without a reason in the PR description. The current set is
  deliberately small.

## Reporting a problem

Include: what you ran, what you expected, what happened, and whether
`make check` passes. For anything involving the deployed flow, include
`python -m frontdesk.cli doctor` output — with the key redacted.
