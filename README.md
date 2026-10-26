# Frontdesk — a modular, guardrailed voice booking agent

Frontdesk answers the phone for a dealership service department: it books, moves
and cancels service appointments, and hands the caller to a person when a person
should take over.

The whole design fits in one sentence:

> **A call is a probabilistic conversation wrapped around a deterministic transaction.**

Everything below is about protecting the transaction.

---

## Start here

**New to the repo?** Read in this order:

| # | Document | Why |
|---|---|---|
| 1 | [CONTRIBUTING.md](CONTRIBUTING.md) | Set up, run the tests, and the one architectural rule that governs every change |
| 2 | [docs/onboarding-voice.md](docs/onboarding-voice.md) | The voice surface: what to tune, what to listen for, what is still open |
| 3 | [Run it](#quick-start) below | Tool server → tunnel → deploy → call |
| 4 | [Architecture](#architecture) | Why the flow looks the way it does |
| 5 | [docs/dms-integration.md](docs/dms-integration.md) | Adding the next DMS |

Status: **235 tests, no network** (~1s). `ruff check .` is clean. CI runs both on
every push.

```
make install   # dependencies
make test      # the suite, no credentials needed
make check     # lint + tests, the same gate CI runs
make plan      # prompt budget and architecture, no API calls
```

---

## What's here

```
frontdesk/
  identifiers.py       deterministic validation: plates, VINs, phones, mileage
  dealer_profiles.py   dealer/market differences as DATA (config over code)
  dms/                 canonical DMS layer — one contract, many vendor adapters
    model.py             domain types, capability vocabulary, typed errors
    base.py              the nine-method adapter contract
    memory.py            in-process reference adapter (the control case)
    keyloop.py           Keyloop Service Appointment Booking
    fortellis.py         CDK via Fortellis — including the async-write case
    partners.py          certified read-only, and export-only (no API at all)
    registry.py          vendor -> adapter, and per-dealer capability scoping
    conformance.py       the reusable contract suite every adapter must pass
  tools.py             Retell tool definitions exposing the deterministic layer
  flow.py              the redesigned flow: router + skills + deterministic spine
  scenarios.py         the failure classes above turned into regression tests
  retell_client.py     thin Retell REST client
  snapshot.py          Retell -> one canonical graph model (the IR) + contraction
  annotations.py       the curated "why" overlay Retell cannot know
  insights.py          cost and latency: modelled, and corrected by real calls
  journey.py           the client story, authored by hand and dereferenced
                       against the graph so it cannot quietly go stale
  viewserver.py        local diagram server (deliberately not the tool server)
  cli.py               doctor / plan / adapters / deploy / webcall / diagram /
                       simulate / cleanup
tool_server/app.py     mock DMS tool server (the deterministic half, no vendor names)
scenarios/*.md         example conversations + the full variable model
benchmarks/            architecture comparison: method, cost cap, and the
                       result of the last run
docs/dms-integration.md  the DMS landscape, the contract, and how to add the 10th
docs/retell-semantics.md what transitions, dynamic variables and "fine-tuning"
                       actually do — sourced, with the parts Retell does not
                       document called out
docs/onboarding-voice.md the voice surface: tuning, listening, open questions
tests/                 guardrail, flow-structure, adapter-conformance and
                       diagram-normalizer tests
web/index.html         browser call UI
web/diagram/index.html zero-build React diagram (client / business / technical)
.github/workflows/ci.yml  lint + tests on every push and pull request
```

## Quick start

```bash
make install                  # or: pip install -r requirements.txt
cp .env.example .env          # then paste your RETELL_API_KEY

make test                     # 235 tests, no network
python -m frontdesk.cli plan  # the architecture + prompt budget, no API calls
```

The test suite needs **no credentials and no network**. You can contribute fully
without a Retell account; only `deploy`, `webcall`, `simulate` and `benchmark`
talk to the API.

Deploy and talk to it:

```bash
# 1. Serve the tool server and expose it, then deploy against that URL
./scripts/dev_tunnel.sh --deploy

# 2. Call it from the browser
python -m frontdesk.cli webcall     # prints a URL, opens web/index.html

# 3. Look at the flow as a diagram, for whoever is asking
python -m frontdesk.cli diagram     # opens http://127.0.0.1:8100

# 4. Run the regression suite against the deployed flow
python -m frontdesk.cli simulate

# 5. Remove everything
python -m frontdesk.cli cleanup
```

`scripts/dev_tunnel.sh` exists because the manual version has three ways to bite
you, all of which it handles:

1. **A printed hostname is not a reachable one — and an *answering* one is not a
   working one.** cloudflared announces the URL before the tunnel answers, and
   Retell will 404 until it does. The script therefore waits for a real `2xx`.
   That qualifier earned its place: the check originally treated *any* HTTP
   response as healthy, and Cloudflare's edge will happily answer **`530` /
   error 1033** for a hostname it routes but cannot serve — over a valid TLS
   connection. So the script concluded a tunnel was already running, skipped
   starting cloudflared entirely, and deployed a URL with nothing behind it. The
   gate has to mean `2xx` or it is not a gate. See `frontdesk/net.py` and
   `tests/test_net.py`.
2. **The local resolver lies.** macOS negative-caches new `*.trycloudflare.com`
   names, so a tunnel that works fine for Retell looks dead from the machine
   doing the deploy. The script (and `doctor`) therefore fall back to resolving
   over DNS-over-HTTPS and connecting to that address directly — keeping the real
   hostname for SNI and certificate checks, so TLS still verifies. See
   `frontdesk/net.py`.
3. **A re-deploy can silently strip every tool.** This is the dangerous one:
   `update_conversation_flow` is a `PATCH`, and `build_flow_payload` only builds
   tools `if tools_base_url:`, so an unset variable sends `"tools": []` — a
   present key, which *clears* the tools. The function nodes keep their `tool_id`
   references and point at nothing. Two guards: the script reads the deployed
   model and voice out of `.frontdesk.json` rather than trusting defaults, and
   `config.load_settings` falls back to `TOOLS_PUBLIC_URL` when `TOOLS_BASE_URL`
   is unset, so a machine with a named tunnel configured can never resolve to an
   empty URL by accident.

A quick tunnel gets a random hostname and dies with the script, so the deployed
flow keeps pointing at a stale URL afterwards. Re-run the script when you want
live tools again.

#### A named tunnel removes the whole class

Every failure above is a symptom of the hostname being **random and short-lived**.
A named tunnel makes it fixed, which is why it is worth the ten minutes:

```bash
# 1. Zero Trust -> Networks -> Tunnels -> Create a tunnel -> Cloudflared
#    Copy the token.
# 2. On that tunnel, add a Public Hostname route:
#      tools.yourdomain.com  ->  http://localhost:8000
# 3. Put both in .env:
#      CLOUDFLARE_TUNNEL_TOKEN=eyJh...
#      TOOLS_PUBLIC_URL=https://tools.yourdomain.com
./scripts/dev_tunnel.sh --deploy     # deploy once; it stays valid
```

With that, the flow is deployed **once** and keeps working across restarts. If
the tunnel is already up (a launchd service, say), the script uses it and leaves
it running rather than starting a second one — it only tears down what it
started itself.

`CLOUDFLARE_TUNNEL_TOKEN` without `TOOLS_PUBLIC_URL` is refused rather than
guessed: a named tunnel's hostname is configured in the dashboard and is *not*
reported by cloudflared, so there is nothing to read it from.

`TOOLS_BASE_URL` is optional *for a first deploy*: without it the flow still
deploys (validation, branch and conversation nodes are all code or prompts) and
the simulation suite still runs, because the tests pin the tools with mocks.

It is **not** safe to omit on a re-deploy. `update_conversation_flow` is a
`PATCH`, and `build_flow_payload` only builds tools `if tools_base_url:`, so an
unset variable sends `"tools": []` — which is a present key, and therefore
*clears* every tool already on the flow. The function nodes would keep their
`tool_id` references and silently point at nothing. Re-deploying an existing flow
means passing the same base URL it was built with, which is why `deploy` records
it in `.frontdesk.json`:

```bash
# re-deploy without changing the tool wiring
TOOLS_BASE_URL="$(python -c 'import json;print(json.load(open(".frontdesk.json"))["tools_base_url"])')" \
  python -m frontdesk.cli deploy --model "$(python -c 'import json;print(json.load(open(".frontdesk.json"))["model"])')" \
                                 --voice "$(python -c 'import json;print(json.load(open(".frontdesk.json"))["voice_id"])')"
```

The same applies to `--model`, which defaults to `gpt-4.1` — pass the deployed
value explicitly rather than assuming the default is what is running.

---

## The two failures, and what actually fixes them

### Failure A — "on long calls it hallucinates / forgets earlier details"

The tempting answer is "the prompt is too long". The real mechanisms are:

| Mechanism | What it looks like | Fix in this repo |
|---|---|---|
| **Context rot** — recall degrades as the window fills; middle-position facts are worst | Forgetting the vehicle discussed mid-call | Context isolation: each node sees a small, current slice (`flow.py`) |
| **Instruction drift** — a long system prompt is followed less faithfully over time | Behaviour quietly degrading on long calls | 309-token global prompt + short per-node prompts |
| **No structured state** — facts live only in the transcript | Nothing to *retrieve*, so it must re-derive | Externalised state + dynamic variables |
| **One prompt holds 190 nodes** | Conflicting rules, wrong transitions | Router + per-skill subflows |
| **Long-horizon meltdown** — accumulated subtle errors, invented tool output | Contradicting itself, hallucinated availability | Function nodes, equation edges, write verification |

The measured claim, from `cli plan`:

```
global prompt            : ~309 tokens
largest node prompt      : ~133 tokens  ('plate_repair')
worst-case turn context  : ~442 tokens
baseline monolith        : ~14,000 tokens in ONE prompt across 190 nodes
```

The number matters less than the shape: **no node needs to know about any other
node.** That is what stops drift and forgetting — not the smaller count.

### Failure B — "a license plate letter was misheard and nothing caught it"

This one is more revealing, because it is **not an ASR problem**. Replacing the
speech model changes the error *rate*; it does nothing about an architecture with
no control in it. The plate was wrong because eight controls were missing in a
row:

```
constrain  → format mask for the country       (identifiers.PLATE_MASKS)
capture    → phonetic spelling, custom vocab   (flow: capture_plate)
read back  → grouped, character by character   (identifiers.readback_grouped)
validate   → mask + repair known confusions    (identifiers.validate_plate)
confirm    → explicit caller confirmation gate (flow: readback_plate)
reconcile  → match against the vehicle record  (tool: lookup_vehicle)
dispose    → commit in code, idempotently      (tool: create_appointment)
verify     → read the record back out          (tool: verify_appointment)
```

Two ideas carry most of the weight:

- **A format mask can never prove a plate is correct — only plausible.** Two real
  plates can both match the mask, so the format check must be followed by a
  record lookup and a caller confirmation. Neither alone is enough.
- **The agent proposes; code disposes.** The model's role ends at proposing a
  structured value. Validation, commitment and verification are code.

The repair logic is deliberately conservative — it only changes characters the
mask *proves* are wrong, reports every change, and refuses when too much of the
string would have to be guessed:

```
"AB1ZCDE" → normalised "AB12CDE", coercions ["Z->2 at position 4"],
            valid=false, uncertain_positions=[4]     ← needs confirmation
"0BQDCDE" → refused: 3 repairs is too many to be trustworthy
"QQQQQQQ" → …never guessed, asked again
```

In SQL terms: a repair is not a guess when the mask leaves only one possibility.
It *is* a guess past a couple of characters, so the flow asks a targeted question
about those positions instead of re-reading the whole plate.

#### Who interprets the plate — and the bug that found

The division of labour is deliberate: **the model transcribes, the server
interprets.** The model is told to pass the plate *exactly* as the caller said or
spelled it and to leave the understanding to code. It is not asked to know what a
UK plate looks like, because a formatting rule that lives in a prompt is a rule
nobody can test and that varies between runs. Move it into `identifiers.py` and
it becomes a function with cases.

The catch is that "pass it verbatim" is abstract, and an abstract instruction is
where a helpful model starts improvising — completing a partial plate, or
correcting one it thinks is wrong. So the parameter carries five real ways a
caller says the same plate:

```
'AB12CDE'  'A B one two C D E'  'Alpha Bravo twelve Charlie Delta Echo'
'a for alpha, b for bravo, one, two…'  'AB12CDE, I think'
```

Those are not decoration. Each is asserted to still normalise to `AB12CDE`
(`test_the_plate_examples_are_actually_accepted`), because an example shown to a
model is a **claim about a deterministic function** — and claims rot. Change the
normaliser and a prompt can quietly start teaching the model a form that fails.

#### What the examples exposed

Writing out how callers actually speak — rather than how a mask suggests they
speak — turned up a live bug:

```
"AB12CDE, I think"       → AB12CDEITHINK     invalid
"it's AB12CDE, I think"  → ITSAB12CDEITHINK  invalid
"I think it's AB12CDE"   → ITHINKITSAB12CDE  invalid
```

A caller who has just spelled a plate out and says "I think" is the *normal*
case. The flow was sending them back to re-spell a plate they had said correctly,
and an honest "I'm not sure" was being read as a faulty registration.

The cause is an ordering trap. Hedges were added to the filler list, but
tokenised, `"I think"` is a bare `"I"` followed by `"THINK"` — and a
single-character token is claimed as a letter by `_char_token` *before* the
filler check runs. So `"I"` was kept, and the naive fix (adding `"I"` to the
filler set) does nothing at all. Multi-word hedges have to be removed **before**
tokenising, while they are still phrases:

```python
text = re.sub(r"\b[A-Z0-9]\s+(?:FOR|IS|AS|LIKE)\s+([A-Z]+)\b", r"\1", text)
if strip_filler:
    text = _HEDGE_RE.sub(" ", text)   # must happen before tokenising
```

Both passes stay as they were otherwise: the faithful reading is tried first and
the cleaned one is only a rescue, so a plate that genuinely contains those
letters is still matched.

The general lesson is worth more than the fix. Every existing test had been
written from the mask's point of view — valid plates, invalid plates, known ASR
confusions — and all of them passed while the ordinary path failed. Cases drawn
from the *specification* confirm the specification. Cases drawn from what
callers **say** are what find the bug.

### Failure C — the 422 that four fixes failed to fix

Worth recording because of how long it looked like something else.

Retell nests tool arguments under `args` unless the tool sets `args_at_root`, so
every request actually arrived as:

```json
{"name": "validate_identifier", "args": {"value": "AB12CDE"}, "call": {...}}
```

Our endpoints describe a **flat** body, so nothing matched and FastAPI reported
each required field as missing:

```
"loc": ["body", "kind"],  "msg": "Field required"
"loc": ["body", "value"], "msg": "Field required"
```

Read that message and the conclusion is obvious: the model didn't supply the
fields. Except the call log showed them right there in `args`. Three rounds of
prompt and schema work followed — making `kind` a `const`, rewriting the plate
description, relaxing `flag_conversation` — none of which touched the cause.

One of them made it worse. Making `reason` optional stopped the 422 and *looked*
like a fix, while the server went on reading an empty body and recording flags
with blank reasons. **A required field going optional converted a loud failure
into a silent wrong answer.** That is the failure mode this project is built to
avoid, arriving through the front door.

The rule that came out of it: **a request that arrives but matches nothing is a
transport bug, not a model bug.** When a field is reported missing and the log
shows it present, stop reading prompts.

Fixed in `tools.py` (`"args_at_root": True` on every tool), pinned for all six by
`test_every_tool_sends_its_arguments_at_the_root`, and enforced on the server
side — `tool_server` rejects the wrapped envelope and names the setting, because
the only thing worse than a confusing error is a silent one.

### Failure classes that follow from these two

`scenarios.py` turns the taxonomy into tests. Beyond the two reported failures:
wrong vehicle matched, ambiguous plate, tool timeout, duplicate booking on retry,
stale availability, invented price, adjudicated warranty claim, payment details
taken over voice, safety-critical symptom booked routinely, failed transfer
dead-ending, and silent stalls where nothing resolves and nobody is told.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Channels        voice │ chat │ SMS │ WhatsApp               │
├──────────────────────────────────────────────────────────────┤
│  Orchestration   router → stage graph (FSM + flex dialogue)  │
├──────────────────────────────────────────────────────────────┤
│  Skills          booking │ rental │ billing │ handoff        │
├──────────────────────────────────────────────────────────────┤
│  GUARDRAILS      validators · confirmation gates ·           │  ← deterministic
│  (the spine)     idempotency · policy · consent              │
├──────────────────────────────────────────────────────────────┤
│  Domain services availability │ pricing │ eligibility        │  ← code, not prompts
├──────────────────────────────────────────────────────────────┤
│  Adapters        DMS-A │ DMS-B │ CRM │ OEM │ payments        │  ← anti-corruption
├──────────────────────────────────────────────────────────────┤
│  State           session state · event log · pinned facts    │
├──────────────────────────────────────────────────────────────┤
│  Observability   traces · evals in CI · replay · flagged     │
└──────────────────────────────────────────────────────────────┘
```

### What I broke apart, and what I deliberately kept together

**Broke apart:** intent routing · each business domain · the 14k prompt ·
deterministic logic out of prompts · identifier capture into one reusable
component · verification into a guardrail layer · per-DMS integration into
adapters · consent/identity/handoff into shared components.

**Kept together:** persona and voice · a single front door · the canonical domain
model and tool contracts · one global guardrail policy · the session state record ·
the compliance/identity subflow · the evaluation suite.

The distinction is deliberate, not fence-sitting. Fully independent autonomous
micro-agents would add latency, coordination failures and un-debuggable emergent
behaviour — a *worse* failure mode than the one being fixed. This is a **modular
monolith with a deterministic spine**.

### Why the flow looks the way it does

- **Function nodes, not subagent nodes, for anything that must happen.** The
  model does not get to decide whether a plate is validated. Contrast Retell's
  own guidance: use function nodes for guaranteed execution.
- **Equation edges, not prompt conditions, for the gates.** `identifier_valid ==
  "true"` is a fact; "did the model think the plate looked fine" is not.
- **An `else` edge on every split.** A flow that cannot get stuck is a flow that
  cannot strand a caller.
- **Errors are data, not exceptions.** Every validator returns a structured
  result the flow branches on, so a caller never hears "error".

---

## Scaling to 10 dealers and several countries

| Mechanism | Where |
|---|---|
| Dealer/market config as data | `dealer_profiles.py` |
| Canonical model + vendor adapters | `dms/` |
| **Capability matrix** — degrade honestly when a DMS can't do something | `dms/base.py`, `dms/registry.py` |
| **Adapter conformance suite** — prove a new DMS is safe before shipping it | `dms/conformance.py` |
| Reusable components (capture, confirm, handoff) | shared nodes in `flow.py` |
| Market formats and compliance | `PLATE_MASKS`, `requires_recording_consent` |
| Per-tenant observability | `post_call_analysis_data` |
| Regression suite + replay | `scenarios.py`, `cli simulate` |

**Dealer and DMS are kept as separate dimensions** on purpose: several dealers can
share one DMS, and one dealer group can span several. A third dimension sits
underneath them — *what this dealer's connection to that DMS can do* — because
capability is a property of the connection, not the product.

Try it:

```bash
python -m frontdesk.cli adapters --conformance   # the matrix, and the contract gate
DEALER_ID=midlands-us python -m frontdesk.cli plan
```

See [docs/dms-integration.md](docs/dms-integration.md) for the full picture: the
real vendor landscape (Keyloop, Fortellis/CDK, Reynolds RCI, Tekion APC), the
four axes on which integrations actually differ, and the sequence for adding the
tenth one.

### What the matrix exposes

Five dealers, four distinct integration modes:

```
adapter                          async   avail  cancel    book    appt  customer   price   RO  history transport  vehicle
reynolds-rci / RCI (read-only)        -       -       -       -     yes       yes     yes  yes      yes         -      yes
keyloop / SAB v2 (full)               -     yes     yes     yes     yes       yes       -    -      yes       yes      yes
fortellis / CDK (async writes)      yes     yes       -     yes     yes       yes     yes    -      yes       yes      yes
export-only / CSV export (no API)     -       -       -       -       -       yes       -  yes      yes         -      yes
```

Note `northgate-uk` and `veldhoven-nl` are both Keyloop but *not* equivalent: the
Dutch DMS behind the Open Platform does not expose booking options, so no loan
car is offered there. Keyloop's own documentation warns that endpoint
availability varies per DMS — this is that warning made concrete, and the test
suite asserts it stays true.

### The async case, which is the one that bites

CDK ships both a synchronous appointment API and an **Async Service Appointment**
API. The write returns `202 Accepted` with a job id, and *the appointment does not
exist yet*. An adapter that treats `202` as success produces an agent that
confidently confirms bookings nobody has — and no test catches it, because the
adapter is behaving exactly as written.

So the taxonomy has a dedicated class for it, the flow has a dedicated node, and
the caller hears the truth:

```
booking_confirmed == true   →  verify the write, then confirm to the caller
booking_pending   == true   →  "it's in, but not confirmed yet" + flag for follow-up
otherwise                   →  booking failed, apologise and hand off
```

---

## Observability — measuring the tail, not the average

The `post_call_analysis_data` fields on the agent are chosen so the unhappy tail
is visible:

`call_outcome` · `primary_intent` · `plate_read_back` ·
`plate_confirmed_by_caller` · `booking_verified` · `booking_reference` ·
`customer_frustration` · `escalated_to_human` · **`agent_failure`** ·
**`detail_lost`**

The last two are the ones that matter most. `detail_lost` exists specifically to
catch Failure A in production rather than relying on someone noticing, and
`agent_failure` turns each bad call into a description of what went wrong — which
is the raw material for the next regression test.

This is the design principle: **accuracy can look above 70% while a tail of
customers is severely frustrated at the very end of the distribution.** With
review sites in play, every one of those counts. So fixes get prioritised by
exposure × cost-to-fix — *while* the flagged-call queue is watched continuously.

---

## Benchmarking the two architectures

The modular flow exists to be turned on and off per market, which is a claim
about structure. Nothing about that claim is visible in a unit test, so the two
architectures are measured against each other:

```bash
python -m frontdesk.cli benchmark plan      # the suite and its cost, spends nothing
python -m frontdesk.cli benchmark prepare --arm batch
python -m frontdesk.cli benchmark run --arm batch --yes --cap-usd 2.00
python -m frontdesk.cli benchmark prepare --arm voice   # writes the call script
python -m frontdesk.cli benchmark report
```

**Six journeys, two arms, because the platform bills and reports them
differently.** Batch runs bill per message and return a graded pass/fail *and
the transcript*, but no latency and no cost. Voice calls bill per minute and
return `latency.*` percentiles and an itemised `call_cost`, but cost a few
hundred times more per run. So accuracy is bought where it is cheap and
repeatable, and voice minutes are spent on two journeys whose length brackets a
typical call — `multi-skill`, which crosses three subflows, and the happy path
as a control.

Five journeys were added for this. The existing scenarios test the flow's
*judgement*; none of them crosses three skills and none can be switched off, so
none of them would notice a broken boundary:

| Journey | Arm | What it pins |
|---|---|---|
| `multi-skill` | voice | plate → booking → rental → rating in one call |
| `happy-control` | voice | the unchanged happy path, as a control |
| `plate-handoff` | batch | the plate → handoff boundary |
| `async-pending` | batch | the third booking outcome, distinct from success |
| `gate-booking-off` | batch | a skill switched off for a market |
| `rating-declined` | batch | refusing the rating, and not being pressed |

### The budget is a control, not an estimate

The cap is enforced in code. `Budget` refuses to start work it cannot afford
rather than warning afterwards, the guard is **seeded with what each
architecture has already cost**, and batch cost is derived from real transcript
message counts at published per-message rates — labelled as an estimate wherever
it appears, because a figure that looks measured but is not is worse than none.
Voice cost is the platform's own itemised number.

### What the first run found

The benchmark earned its keep immediately, and not in the way it was meant to.
It caught a bug that **no test in this repository noticed and that three of four
graded journeys still reported as a pass.**

The component exit router returned `{ result: 'ok' }` while declaring a
`response_variables` path of `data.result`. Retell resolves those paths against
the node's result using the same convention as tool results — which are wrapped
as `{"status": …, "data": {…}}`. So the path resolved to nothing, the variable
was never set, and every equation edge reading it was false. Every `ok` exit
fell through to its fallback, and **the entire modular flow collapsed into
handoff at the first opportunity on every call**: no booking, no availability
check, no rating.

The graded journeys did not catch it because the failures were graded as passes.
`rating-declined` asserted "asked for a rating at most once" and "did not invent
a rating" — every one of those is an assertion that something *did not happen*,
so all of them pass when the rating is never reached at all. The journey never
got near the rating and scored 100%.

Both problems are now pinned by tests:

- `tests/test_components.py` checks every router's declared path against the
  literal its code returns, so the two can never drift apart again.
- `rating-declined` gained a positive assertion — the booking must have been
  completed and a reference given *before* the rating — making it unfakeable.

After the fix, the modular transcript shows the real path
(`validate → exit(ok) → check_availability → create_appointment → verify →
record_feedback`), where before it showed `validate → exit(ok) → flag →
transfer`.

### What the numbers do and do not say

Post-fix, the two architectures are **equivalent on the graded set** — and that
is the honest limit of what one run per journey can tell you. `async-pending`
failed on the monolith in the second run having passed in the first, and passed
on the modular in the second having failed in the first. **n=1 per journey is
noise.** A journey that measures a real difference between the architectures
cannot be distinguished from one that is merely flaky at this sample size, and
the report says so rather than picking the flattering run.

The voice arm is where cost and latency come from, and those calls have to be
made by hand: Retell's simulation is text-only and a web call expects a person
on a microphone. `benchmark prepare --arm voice` writes
`scenarios/10-benchmark-calls.md` with the exact command and script per journey,
so both architectures hear the same caller.

The architecture-relevant part of latency is small and known: measured calls put
`latency.e2e` at ~1.8s while `latency.llm` is ~0.6s, so ASR, TTS and network
dominate and are identical across both. Structure can only move the LLM portion
and the number of round-trips per journey — which is why the transcript turn
count matters more here than the total call duration.

---

## Limitations and honest caveats

- **The DMS is mocked.** `dms/memory.py` implements the canonical interface with
  in-memory data, including a deliberately ambiguous plate. Real Keyloop and
  CDK/Fortellis adapters would sit behind the same interface; the point is that
  nothing above the adapter changes.
- **Plate masks cover a useful subset.** GB, NL, DE, FR, ES, IT, BE. Production
  should read these from the country spec and allow a per-dealer override.
- **VIN check digits are informational.** European VINs often don't honour the
  North-American check digit, so it is reported as a warning, not a rejection.
- **The tool server is in-memory.** Flags and appointments vanish on restart;
  that is deliberate so the demo is safe to run.
- **The simulation suite is a starting set, not the goal.** ~10 scenarios catch
  the cases you thought of. The real regression net is **replaying production
  transcripts** — synthetic tests are bounded by the imagination of whoever wrote
  them, and production traffic is not.
- **Retell-specific.** The structure (router, skills, deterministic spine,
  verify-the-write) is platform-independent, but the implementation uses Retell's
  node and tool primitives.

---

## The diagram — one model, three audiences

```bash
python -m frontdesk.cli diagram            # summarised, at 100% zoom
python -m frontdesk.cli diagram --refresh  # ignore the 5-minute cache
python -m frontdesk.snapshot --out snap.json   # the IR alone, no server
```

The Retell canvas is hard to read, and the reason is arithmetic: this flow's
booking spine is 14 steps deep, so drawing it at a legible size needs ~3300px of
width. Any "fit to screen" view therefore lands around **0.35 zoom and the text
becomes 4px** — the diagram is unreadable exactly because it is complete.

So the default view is the graph **contracted** into skill clusters, which fits
at 100%. The full node graph is one toggle away, and is the right thing to look
at *after* the summary has told you where to look.

That is one canonical model and three projections of it, not three diagrams:

| Audience | What they get | Where it comes from |
|---|---|---|
| **Client** | a plain-language journey, no graph, plus what the agent will never do | `journey.py` |
| **Business** | cost per conversation and at volume, p90 latency, what the flow does | `insights.py` |
| **Technical** | edge semantics, determinism, tools, prompt budgets | `snapshot.py` |

The audience tabs change the **sidebar only** — same snapshot, same clusters, same
canvas — with one deliberate exception. The client tab is not a panel at all: it
is a single readable column with no graph, because a graph is the wrong artefact
for the question that audience is asking. Business and technical differ only in
the order the numbers arrive in, because the reader's question decides the
sequence and nothing else does.

Three things the Retell API does not expose, and the diagram computes instead:

- **Edge semantics.** Retell stores `edges`, `edge`, `else_edge` and
  `always_edge`. Whether a transition was *decided by code* or *guessed by the
  model* is the most important fact about this graph, and it is not a field. The
  tool measures it: **8 code nodes, 15 of 63 transitions decided by code** — the
  "deterministic spine" claim, counted rather than asserted.
- **Skill clusters and the shared spine.** `flag_and_handoff`, `wrap_up` and
  `end_call` are reached from several skills. Those are the nodes you must
  re-test whenever you touch *any* skill, and the canvas gives no hint of it.
  Note the rule that identifies them: a node is shared when several skills route
  *into* it — not when it is *reachable from* several, which wrongly marked the
  entire rental subtree as shared.
- **The "why".** `annotations.py` carries the reasoning from `flow.py` — which
  failure mode each node defends against, which guardrail stage it belongs to.
  It is an overlay, so a node added in the dashboard still renders.

### Cost and latency, honestly labelled

Cost and latency come in two kinds, and
conflating them is how an estimate becomes a commitment:

- **MODELLED** — from the flow's own prompt budget and a pricing table
  (`pricing.json`). Available immediately. Duration dominates the answer, so it
  is a range, not a number.
- **OBSERVED** — read from Retell's `call_cost` and `latency`. Precise, but only
  exists once there is traffic. This account currently has one call and it failed
  (`error_user_not_joined`), so `call_cost` is zeroed and `latency` is empty —
  which is the *normal* state of a new deployment, not an edge case.

Calls that never connected are reported separately rather than averaged in,
because counting a $0 call as a cheap call drags every estimate toward zero.
Once real traffic exists, `call_cost.product_costs` supplies the unit prices
your account is actually charged, and the tool reports how far its own model is
off — so the pricing table stops being a guess.

#### The client story is the one authored layer, and it is checked

Everything else in this tool is *derived* from the graph, which is what stops
the views from disagreeing. The client journey cannot be: "Readback plate" ->
"the agent asks you to confirm what it heard" is an act of translation, and
generating it from node ids produces word salad. A dealer principal deciding
whether to trust the agent with their customers is the wrong place to be clever,
so the narrative in `journey.py` is written by hand.

An authored layer is a fourth thing that can drift — so every step declares the
nodes that implement it, and `build_journey` checks them against the graph it is
given:

- A declared node that no longer exists is reported as `missing`, the step is
  marked **stale**, and it still renders. Dropping the step silently would hide
  the drift, which is the exact failure worth guarding against: a page that reads
  perfectly and describes software that is no longer running.
- A graph node no step mentions is reported as **uncovered**.
- The page says which of those it is, out loud, at the top: *"Checked against the
  system that is running — all 32 steps accounted for."*

Both directions are tested, and the tests are not vacuous — delete a node from
`flow.py` and `test_journey_accounts_for_every_node_in_the_flow` goes red until
somebody decides how to describe it. Two other properties are pinned for the same
reason: every guarantee must be enforced by a node that actually exists (a
promise with no live check behind it is a marketing claim), and no snake_case
node id may leak into the prose.

The client tab also carries the guardrails stated as promises rather than
defects — "it will not invent an appointment time" instead of "invented or stale
availability". Same facts, read from the same overlay the technical tab uses, so
the two cannot disagree about where the controls are.

#### The Business view

Money first, and the assumptions next to it rather than behind a tooltip. Four
things are doing the work:

- **A volume ladder.** 100 conversations per day is the default assumption,
  but it is never the only number anybody wants — the first follow-up is always
  "and at double that?". The model is linear in volume, so the table is
  arithmetic rather than a second guess. The configured rung is marked, and it is
  asserted to agree with the headline *to the cent*: a table that contradicts the
  number above it costs more trust than the table buys. The rungs come from
  `volume_ladder` in `pricing.json`, not from code.
- **Drivers, stated as a share.** At placeholder rates, **95% of a conversation is
  duration** and 5% is tokens. Saying that out loud is the difference between a
  business audience arguing about the *duration assumption* — the thing that
  moves the total — and arguing about token pricing, which does not.
- **The inputs, visible and editable.** Duration bands, seconds per turn, tokens
  per turn, volume, unit prices. A cost model that cannot be argued with is not a
  business case, and the useful conversation is "our calls are nearer 2 minutes
  than 3".
- **A placeholder warning that can be cleared.** The default unit prices produce a
  number with the right shape and the wrong magnitude, so the panel says so —
  and stops saying so the moment `pricing.json` exists. A warning that cannot be
  cleared is just noise.

#### The diagram shows the deployed flow, the tests assert the local one

Worth knowing, because the two can disagree: `tests/` builds its graph from
`flow.build_flow_payload`, while the diagram reads the live flow from Retell. A
green test suite therefore does **not** mean the diagram matches what you just
wrote — it means the payload builds.

This is not hypothetical. On first building this, the deployed flow was three
things behind `flow.py`, and the diagram disagreed with the local tests on the
prompt budget (7,345 vs 7,365 input tokens) — which is what surfaced it:

- `booking_pending` was missing, along with its three edges. That node is not
  decoration: CDK Drive's Async Service Appointment API returns `202 Accepted`
  and the appointment *does not exist yet*, so the flow has a deliberate
  three-way split. Without it, `create_appointment` falls through to
  `booking_failed` and **the caller is told a booking failed when it was
  accepted**.
- `create_appointment` lacked the `booking_pending: data.pending` response
  variable that the middle state reads.
- Eleven `default_dynamic_variables` were absent — including the runtime result
  variables (`identifier_valid`, `vehicle_found`, `slots_available`,
  `booking_confirmed`) that the branch equations in production were already
  reading. They resolved as *undefined* rather than as a declared `false`.

Deploying with an explicit `TOOLS_BASE_URL` closed all three; the flow, tools and
dynamic variables now match `flow.py` exactly. The lesson is the one the tool
exists to make cheap: the diagram is the only artefact that shows what is
*actually running*, so it is the thing to check before trusting a green suite.
Automating that comparison is phase 4.

#### Remaining phases

| Phase | What | Status |
|---|---|---|
| 0 | the IR, a JSON dump, one rough render | **done** |
| 1 | business view — cost and latency, labelled | **done** |
| 2 | technical annotated view + deep link to the Retell canvas | annotations and the agent link are in; the *flow* deep link still needs its URL pattern confirmed against a real browser tab |
| 3 | client journey — plain language, no graph | **done** |
| 4 | drift check — compare the live flow against `build_flow_payload` and warn when they diverge | |

---

## Where this would go next

1. **Replay production calls** against the flow to catch the long tail the
   synthetic suite can't reach.
2. **Automated diagnosis of recurring failures** — cluster `agent_failure` values
   and open a ticket per cluster rather than per call.
3. **Self-healing for well-understood modes** — e.g. a failing DMS call that
   automatically falls back to callback capture.
4. **A second real DMS adapter**, to prove the interface holds before there are ten.
5. **Cross-channel verification** — when a plate is low-confidence, send an SMS
   link so the caller can *type* it. Typed input removes the ASR failure class
   entirely, using a capability the company already has.
