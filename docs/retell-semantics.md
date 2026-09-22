# How Retell actually works — transitions, variables, and "fine-tuning"

Reference notes gathered while building the diagram, because the answers changed
decisions in this repo. Sources are Retell's own docs, and each claim is marked
as either **documented** or **community-reported** — the distinction matters,
because one of the most useful details below is not in the docs.

---

## 1. Transitions

A transition is an edge between two nodes. Each edge pairs a **condition** with a
**destination**, and the condition is one of two kinds.  *(documented)*

| Type | Evaluated by | Deterministic? |
|---|---|---|
| **Prompt** | The LLM, against the conversation | No — a probability |
| **Equation** | Hardcoded comparison over dynamic variables | **Yes** |

### The evaluation order

This is the part that matters, and Retell's canvas does not show it.
*(documented)*

1. **Always edge** — if present, the agent moves as soon as the user responds.
   Nothing else is checked.
2. **Equation conditions** — *all* of them, before *any* prompt condition.
   Among equation edges, top-to-bottom, and **first true wins**.
3. **Prompt conditions** — only if no equation matched. The LLM evaluates all
   prompt conditions together, alongside any global node entry conditions, and
   picks the single best match — or none.
4. **Else edge** — the fallback, so a node cannot get stuck.
5. **Stay** — otherwise the node keeps talking.

Two consequences worth internalising:

- **An equation can never be overridden by a prompt.** Equations are not "a
  kind of condition"; they are a higher tier that runs first. If you can express
  a rule as an equation, the model gets no vote on it.
- **Prompt conditions have no priority.** The docs say the LLM "evaluates all
  prompt conditions together ... and picks the single best match". A community
  investigation is more specific: they are collected into a **single multi-class
  classifier call at `temperature=0`** that returns the chosen edge id, with *no
  ordering between them*. *(community-reported)* That is consistent with the
  docs and with the practical advice to "break down complex conditions into
  multiple simpler ones" — reordering the list does not help, because order is
  not part of the decision.

### What the LLM actually receives

*(documented)*

- The current node's **instruction** is visible while it evaluates conditions.
  But each condition should stand on its own — the docs advise describing what
  the user said or the state reached, *without* leaning on the instruction text.
- For a function node, the condition may reference the **tool result**.
- Only **dynamic variables** can appear in equations. Anything the agent learns
  mid-call must go through a prompt condition or a **Extract DV node** first,
  because an equation cannot read something that has not been stored.

### The equation reference

*(documented)*

| Operator | Compares | Notes |
|---|---|---|
| `==` `!=` | Strings | **Case-insensitive.** No numeric coercion — `"18"` and `"18.0"` are different strings |
| `> >= < <=` | Numbers | If either side is empty or not a number the equation is **false** — it never throws |
| `Contains` / `Not Contains` | Strings | Substring, case-insensitive |
| `exists` / `does not exist` | Variable | **An empty string counts as set** |

Up to 50 equations per condition, combined with `ANY` or `ALL`.

That "never throws" behaviour is why defaulting every gate variable is worth it —
the flow is safe, but a missed value silently routes down the false branch rather
than raising anything.

### When transitions are checked

*(documented)*

- **Conversation / subagent nodes** — after each user turn.
- **Function, code, MCP, extract DV** — after the tool result arrives.
- **Call transfer** — after the transfer result comes back.
- **Logic split** — immediately on entry.

### The numbered-label trap

*(documented)*

If a prompt contains numbered labels like `PRIORITY 1`, `Step 2` or
`ATTEMPT 1`, the model can **confuse those numbers with its transition options**,
and a transition fires whose condition was never met. Retell's own advice is to
rename them to letters (`PRIORITY A`) or descriptions (`New service intent`).

This repo uses no numbered labels in node instructions — checked, and worth
keeping that way.

### One node, one question

*(observed in production — this one cost us a broken call path)*

Transitions on a conversation node are evaluated **after each user turn**, and a
node is considered done when its stated goal is met. That creates a trap when a
single node asks for two things.

Our front door asked for recording consent **and** asked what the caller wanted,
with transitions describing *intent*:

```
greet → "booking | rental | billing | human",  else → fallback
```

A caller who answered only *"yes"* — a complete answer to the consent question —
satisfied the node's goal. The intent transitions were then evaluated against a
turn containing no intent, matched nothing, and the `else` branch fired. The
caller landed in the catch-all, and from there could not reach booking or rental
at all, however clearly they asked afterwards.

The rule: **a node can only route on something the caller has already said.** If
a node has both a yes/no question and a classification to do, split it.

The current shape puts consent *inside* the greeting rather than after it, so the
caller's first turn is the only question it answers:

```
consent_gate   (START; branch on a dynamic variable, silent)
                 → greet_consent | greet_open
greet_consent  (greet + ask to record)   → services_intro | consent_refused
greet_open     (greet; consent not required) → services_intro
services_intro (judge intent only)        → booking | rental | billing | …
```

The gate is the start node precisely so the consent question is not a second
turn. It stays silent — it chooses which opening the caller hears, and the chosen
node does the talking — which keeps the market difference as *data* instead of
two flows. That matters here: `veldhoven-nl` sets
`requires_recording_consent=false`, so both branches are live.

Two further notes from the same fix:

- **The catch-all must route back to the menu.** An `else` branch that only
  leads to a transfer is a trap: any misheard request strands the caller away
  from every skill. Give the fallback a path back to the router.
- **Don't loop the unclear case back to the start node.** Re-entering `greet`
  replays the whole opening on every unclear turn, which on a bad line is a
  plausible infinite loop. A dedicated re-ask node bounds it.

---

## 2. Dynamic variables

*(documented)*

A placeholder written `{{variable_name}}` in a supported field. Retell
substitutes the value available when that field is used. Supported in prompts,
messages, tool URLs/headers/params, flow conditions, transfer numbers, SMS,
voicemail, webhooks, and more. In code tools and code nodes, read them as
`dv.variable_name` instead.

### Where values come from, and the precedence

Sources override **top to bottom** — a lower row wins.

| Source | Set where |
|---|---|
| Agent defaults | The agent's default dynamic variables |
| Environment values | The environment tag for the session |
| Contact fields | Automatic when a phone call matches a contact |
| Request values | The call/chat request, or the inbound call webhook |
| **Collected values** | Extracted during the conversation, or saved from tool responses |
| Overrides | `fields_to_override` in Update Live Call |

**This resolves the question about defaults.** Agent-level defaults are the
*lowest*-precedence source. They are fallbacks, not instructions.

### So does the AI try to extract default dynamic variables?

**No.** Nothing extracts a default. It is a value seeded before the call starts,
used only when nothing else supplies one. There is no description, no type and no
pattern to learn, because there is nothing to learn — the value is already set.

Actual extraction in this flow happens by a different mechanism entirely:

- `create_appointment` and the other tools declare **`response_variables`**, e.g.
  `booking_confirmed: data.confirmed`. Retell maps the tool's JSON response onto
  a dynamic variable. **This is how `booking_confirmed`, `identifier_valid` and
  friends get their real values** — the flow's `default_dynamic_variables` only
  seeds them to `"false"` so the equation has something to read before the tool
  has run.
- An **Extract DV node** extracts from the conversation. There you *do* define a
  name, a description, and a type (Text / Number / Enum / Boolean), and "the LLM
  analyzes the full conversation to capture each value". That is where patterns
  get described — in prose, per variable.

So if you want the model to actively pull a value out of the conversation, a
default will not do it; you need an Extract DV node or a tool response mapping.

### Is the scope node-level?

**No.** A dynamic variable belongs to the **conversation**, not to a node. The
variable picker shows "default system variables plus variables defined for the
agent or flow" — the scoping unit is the agent (agent defaults) or the flow
(`default_dynamic_variables`), and once set, any later node can read it.

What *is* node-level is the **write**: an Extract DV node runs on entry to that
node and stores its values for the rest of the call. Node-level extraction, but
conversation-level scope.

### Unset variables — the behaviour to know

*(documented)*

| Situation | Result |
|---|---|
| Key missing, or `null` | The placeholder **stays as literal `{{user_name}}`** |
| Empty string `""` | Replaced with **nothing** — an empty string counts as a value |
| A real value | Substituted |

A literal `{{user_name}}` reaching the model is the interesting failure: the
prompt then contains braces and a variable name, which the model may read aloud
or try to interpret. Retell's own mitigation is defensive prompting — e.g. *"If
`{{user_name}}` appears with curly braces, use a generic greeting."*

### Built-in system variables

*(documented)* — no configuration needed.

| Variable | Value |
|---|---|
| `{{current_time}}` | e.g. `"Thursday, March 28, 2024 at 11:46 PM PDT"` |
| `{{current_calendar}}` | **14 days**, dated, with today marked `(Today)` |
| `{{current_hour}}` | Current hour as a fraction, e.g. `"3.5"` |
| `{{current_time_ms}}` | Unix timestamp in ms |
| `{{system_timezone}}` | Timezone for the unqualified time variables — **defaults to the agent's timezone, or `America/Los_Angeles` if unset** |
| `{{current_node}}` / `{{previous_node}}` | Flow position |
| `{{session_type}}` / `{{session_duration}}` | Session metadata |
| `{{direction}}` / `{{user_number}}` / `{{agent_number}}` | Call metadata |

`{{current_time_[timezone]}}`, `{{current_hour_[timezone]}}` and
`{{current_calendar_[timezone]}}` override the timezone per use, and variables
can be **nested**: `{{current_time_{{my_timezone}} }}`.

The `system_timezone` default is the trap this repo hit. Retell supplies the date
— but not necessarily in the dealership's timezone, so "next Tuesday" can be
resolved against the wrong day. `dealer_dynamic_variables` therefore seeds the
dealer's own timezone under Retell's reserved name.

---

## 3. Tool parameters — what the model generates, and what Retell applies

*(documented, then observed the hard way)*

A parameter in a tool's `parameters.properties` gets its value one of two ways:

| Property has | Who supplies the value |
| --- | --- |
| a `description` | **The LLM.** Retell asks the model to produce a value, and it "does its best". |
| a `const` | **Retell, directly.** Applied verbatim, never offered to the model. |

Two consequences that are easy to miss:

- **`{{dynamic_variables}}` are substituted inside `const` values** (and in
  descriptions, headers, query parameters and URLs). So
  `{"const": "{{dealer_id}}"}` is a deterministic read of a variable, not a
  model guess.
- **`required` only governs LLM generation.** It is the list of properties the
  model is asked to produce. A `const` property should *not* also be listed,
  because that asks the model to invent a value for something already applied.

### The setting behind every 422 in this project

*(documented — and misdiagnosed three times before it was read)*

`args_at_root`. Omitting it is the single reason four separate 422s were
reported, and the reason the fixes for them did nothing.

With it false (the default), Retell nests the arguments:

```json
{"name": "validate_identifier", "args": {"value": "AB12CDE"}, "call": {...}}
```

Our endpoints describe a *flat* body, so nothing matched and FastAPI reported
every required field as missing:

```
"loc": ["body", "kind"],  "msg": "Field required"
"loc": ["body", "value"], "msg": "Field required"
```

That message is the trap. It says the model declined to supply the fields — while
the call log showed them present in the args. So it read as a prompt or schema
problem, and three rounds went into:

1. Making `kind` a `const` and dropping it from `required`.
2. Making `flag_conversation.reason` optional, with a tolerant default.
3. Rewriting the plate parameter's description with worked examples.

All three were plausible. None touched the cause. And (2) actively made things
worse: with `reason` optional the 422 stopped, so it looked fixed — while the
server went on reading an empty body and recording flags with blank reasons. A
required field going optional converted a loud failure into a silent wrong
answer, which is the more expensive kind.

**The rule: a request that arrives but matches nothing is a transport bug, not a
model bug.** When a required field is reported missing *and* the call log shows
it present in `args`, stop reading prompts.

`_tool()` sets `"args_at_root": True` on every tool now, and
`tests/test_flow.py` asserts it on all six so a new tool cannot be added without
it. `tool_server` also rejects the wrapped envelope and names the setting —
because the one thing worse than a confusing error is a silent one.

### Why `const` still matters — the real, separate failure

The rule below is not a fix for the above; it stands on its own, and it was
arrived at while chasing the wrong bug.

Ask the model for a value and you have created a place the call can fail.
`validate_identifier` originally declared three parameters, all with
descriptions, all required — so it asked the model for `kind` and `country`,
neither of which it had any business deciding.

### The rule

**If the value is known, it is a `const`.** Every parameter left to the model is
a place the call can fail, so the goal is to minimise that set — and to leave
only values the caller genuinely holds.

Applied across the six tools, 12 of 20 parameters became `const`, including a
side benefit: `lookup_vehicle.plate` is now `{{identifier_normalised}}`, so the
lookup reads the value the validator produced and the caller confirmed. Before,
the model recalled the plate a second time, which meant the read-back the caller
agreed to was not necessarily what was looked up.

### The related trap: function nodes and their arguments

A `wait_for_result` function node runs its tool on entry, and its arguments are
generated the same way — from the tool's schema. So a node whose instruction is
only *"say nothing while this runs"* gives the model no reason to think it must
produce a value. Either make the value a `const`, or say explicitly in the node
instruction what to pass.

### Retell silently drops most JSON Schema keywords

*(observed)*

A property is not passed through to the model as written. Retell keeps only
three keys and discards the rest:

| Kept | Dropped |
| --- | --- |
| `type`, `description`, `const` | everything else — notably `examples`, `enum`, `format`, `default`, `minimum`, `pattern` |

Verified by deploying a property with an `examples` array and reading the flow
back: `examples` was **absent**. No warning, no error — the deploy reported
success.

This matters because the failure is invisible in the worst place. An author who
writes `"examples": [...]` sees correct code, a green test suite, and a
successful deploy, while the model receives no examples at all.

**So: `description` is the only channel.** Any guidance that must reach the model
— examples, allowed values, a note about what *not* to do — has to be prose
inside `description`. `tests/test_flow.py` asserts this for the plate parameter
and rejects an `examples` key outright, so the trap cannot be reintroduced.

---

## 4. "Fine-tuning" — what it actually is

The word is doing a lot of work, and the marketing and the product mean different
things.

**What the product feature is** *(documented)*: **finetune examples** — you attach
transcript snippets to a node to correct a response or a transition. Nothing is
retrained.

| Node | Support |
|---|---|
| Conversation | Response **and** transition examples |
| Subagent | Response **and** transition examples |
| Function | **Transition only** |
| Global | Example conversations, each ending with a user utterance |

- You supply a transcript as context, choosing `user`, `agent` or `function` as
  the role. For `function` you also give the invocation and its result.
- **Response examples** need at least one `agent` turn, since they are steering
  the agent's reply.
- **Transition examples** need the transcript *and* the transition result — i.e.
  the correct destination node, as a label.
- The docs' tip is revealing: if the target nodes are hard to tell apart,
  **rename them** so the examples are easier to distinguish.

**So: zero-shot by default, few-shot once you add examples.** Not weight
fine-tuning.

The mechanics follow from the evaluation order above. Prompt conditions are
resolved by a single classifier call, so examples bolted onto a transition are
few-shot demonstrations inserted into that classifier's context, teaching it the
mapping from "things callers say" to "which edge". That is why the docs frame it
as "show the model real transcripts with the correct decision", and why an
example is *context plus a label* rather than a gradient update.

**What the marketing pages describe** is the other thing: training a model on
thousands of real transcripts, with annotation, evaluation and retraining. That
is weight-level fine-tuning, and it is a service engagement, not a toggle in the
flow builder. Nothing in the flow editor does it.

**Where knowledge bases fit**: for factual answers, Retell chunks and embeds
documents, retrieves passages per turn, and inserts them into context. That is
RAG — also not fine-tuning, and usually the right tool when the need is "know our
prices" rather than "phrase things like we do".

### Practical upshot

- If a transition misfires, the levers in order are: make the condition more
  specific, **use an equation if the variable is knowable**, then add a
  transition example. A prompt condition is a probability; an equation is not.
- Fine-tune examples are not free: they occupy context on every evaluation of
  that node. Use them for what the prompt cannot express economically — that is
  exactly the case the docs make for them ("keep this information from clunking
  up the prompt").
- Renaming nodes to be distinguishable is a legitimate reliability technique.

---

## Summary of what this changed in the repo

| Finding | Change |
|---|---|
| Equations run before prompts, first-true-wins, no LLM | The diagram's "deterministic spine" metric is counting the tier that genuinely cannot be overruled. The legend now states the order |
| Prompt conditions have no priority | Explains why the flow prefers `_eq_edge` wherever a value is validated |
| `{{current_calendar}}` exists and is dated | `capture_timing` now resolves "next week" against it instead of guessing |
| `system_timezone` defaults to `America/Los_Angeles` | The dealer's timezone is seeded under Retell's reserved name |
| Defaults are the lowest-precedence source and extract nothing | The gate-variable defaults are documented as *seeds*, with `response_variables` doing the real writing |
| `{{var}}` survives literally when unset | The flow declares every gate variable up front rather than relying on `exists` |
| Finetune examples are few-shot, not training | Worth knowing before anyone promises a "fine-tuned" agent on a call |
| A tool property with a `const` is applied directly; one with a `description` is generated by the LLM | Anything already known is a `const` with a `{{variable}}` in it. 12 of 20 tool parameters stopped being model guesses, and the plate lookup now reuses the validated value |
| A node cannot route on something the caller has not said | Consent and intent became separate nodes; the catch-all routes back to the menu |
