# Scenario 08 — Human handoff, general questions and dead ends

**Trigger:** anything that isn't a clean fit for the other scenarios — a general
question, a request for a person, or a request the agent cannot satisfy.

**Why this is a scenario and not a fallback:** in the monolith, "anything else"
was the dangerous branch — the place where an agent with a 14,000-token prompt
improvises. Here it is a designed destination with an explicit exit condition.

**The design principle:** a handoff is a *feature*, not a failure. An agent that
cannot transfer is an agent that must guess. Every node in the flow has a route
here, and the route carries context so the caller never has to start again.

## Variables collected

| Variable | Source | Notes |
|---|---|---|
| `handoff_reason` | INFER | request / tool_failure / policy / capability / frustration |
| `handoff_context_summary` | TOOL | What was established, so the human doesn't re-ask |
| `handoff_priority` | INFER | normal / high |
| `customer_frustration` | INFER | Drives priority |
| `escalation_number` | CONFIG | Per dealer and per market |
| `transfer_succeeded` | TOOL | A failed transfer must not dead-end |
| `callback_number` | CALLER | Fallback when the transfer fails |
| `callback_promised_by` | CALLER | |

## Example conversation — a general question that can be answered

**Agent** — Northgate Motors service, good morning. How can I help?

**Caller** — What time do you close on Saturdays?

**Agent** — We're open nine until one on Saturdays. Anything else I can help with?
→ `CONFIG` answer — no tool needed, no improvisation
→ intent = `info_only`

**Caller** — And do you do aircon regas?

**Agent** — We do, yes — it's usually about an hour. Would you like me to book
that in?

**Caller** — Not today, I'll call back.

**Agent** — No problem at all. Thanks for calling Northgate Motors.
→ Clean end. **No attempt to convert.** Recognising a low-intent call and ending
   it well is better service than a forced booking.

## Example conversation — a request the agent can't satisfy

**Agent** — Northgate Motors service, how can I help?

**Caller** — Can you tell me whether the recall work on my car was actually done?

**Agent** — I can see the vehicle and its service history, but I can't confirm
recall completion from here — that sits with the service team, and I don't want to
tell you something I can't verify. Let me put you through to them.
→ **The refusal is specific and honest**, and is paired with an action.
→ `handoff_reason = capability`

**Caller** — Fine.

**Agent** — I'll pass on what I've already confirmed so you don't have to repeat it.
→ `handoff_context_summary`

**Agent** — One moment, putting you through now.
→ `handoff` (predefined number from CONFIG)

## Example conversation — a failed transfer

**Agent** — One moment, putting you through now.
→ Transfer fails → `escalation_fallback`

**Agent** — I'm sorry, the line didn't connect. Rather than leave you hanging —
could I take a number to call you back on, and I'll make sure the team has your
details?
→ **A failed transfer is a designed state, not a dead end.**
→ `callback_number`

**Caller** — Yes, use the number I'm calling from.

**Agent** — I'll use that, and I've noted the recall question so they can look at
it before calling. Sorry again about that.

## What makes this go right

- **The agent knows what it cannot do and says so plainly.** "I can't confirm
  recall completion from here" is a stronger answer than a confident guess.
- **Context travels with the handoff.** The caller doesn't repeat themselves —
  which is the same complaint the retention scenario surfaced ("no call back" /
  "had to explain twice").
- **A failed transfer has its own path.** Telephony fails; the flow assumes it.
- **The dead-end node has an exit condition**, so the flow can never get stuck.
  In the monolith this is where conversations silently stall.

## Edge cases the flow handles

| Situation | Path | Why |
|---|---|---|
| Caller asks for a person immediately | Straight to `handoff` | Don't make them earn it |
| Question is answerable from CONFIG | Answer, don't escalate | Escalating everything is as bad as escalating nothing |
| Question needs data the agent can't see | Specific refusal + transfer | Honest about the boundary |
| Transfer fails | `escalation_fallback` → callback | No dead ends |
| Caller refuses to give a number | End politely, flag | Consent; no coercion |
| Caller is angry | Priority `high`, one apology, transfer | No arguing |
| Language the agent doesn't support | Transfer to the language line | Market requirement |

## Success criteria

- Zero conversations that end without either resolution or an explicit handoff
  (i.e. no silent stalls).
- 100% of handoffs carry a context summary.
- Failed-transfer recovery rate: every failed transfer ends with a callback
  commitment.
- "Repeat explanation" complaints trending down — the measurable effect of
  context travelling with the handoff.
