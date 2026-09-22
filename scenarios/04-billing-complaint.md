# Scenario 04 — Billing, invoice dispute and complaint

**Trigger:** the caller mentions an invoice, an amount owed, a double charge, a
refund or expresses a complaint.

**Why this scenario matters most to the business:** the retention analysis found
**double invoices recurring in over 45% of discussions**, and frustration driven
by **not getting a call back**. This is the scenario where an AI agent can either
protect the relationship or actively damage it — and where a green dashboard
hides the damage, because the calls that go wrong are a small tail of the volume.

**Policy, not limitation:** the agent never takes payment details and never reads
an amount from memory. Both are choices about risk, and the agent should say so
plainly rather than sounding incapable.

## Variables collected

| Variable | Source | Notes |
|---|---|---|
| `invoice_reference` | CALLER | Captured + read back |
| `outstanding_balance` | DMS | **Only state if a tool returned it** |
| `invoice_amount` | DMS | |
| `disputed_amount` | CALLER | |
| `duplicate_invoice_suspected` | CALLER | Recurring theme — worth a dedicated flag |
| `payment_method_on_file` | DMS | Confirm existence, never read numbers aloud |
| `payment_taken_on_call` | — | **Always false** |
| `complaint_category` | INFER | billing / quality / wait / no_callback |
| `prior_contact_attempts` | DMS | If > 0, lead with the apology |
| `callback_promised_by` | CALLER | An explicit date, agreed with the caller |
| `customer_frustration` | INFER | none / mild / clear / severe |

## Example conversation

**Agent** — Northgate Motors service, good morning. How can I help?

**Caller** — I'm honestly quite annoyed. I've been charged twice for the same
service and nobody has called me back.

**Agent** — That's really not good enough, and I'm sorry. Let me get this in front
of someone who can actually sort it. Can I take your name?
→ **Apologise first, then act.** No "let me see what I can do" hedging.
→ `complaint_category = billing`, `customer_frustration = clear`

**Caller** — Daniel Okonkwo.

**Agent** — Thank you, Mr Okonkwo. And the registration so I can pull the account
up?
→ `caller_last_name`

**Caller** — XY19 JKL.

**Agent** — Y for Yankee, X for X-ray… one, nine… J for Juliett, K for Kilo,
L for Lima. Did I get that right?
→ read-back → `registration_plate`

**Caller** — Yes, that's it.

**Agent** — I can see a balance showing on the account. I want to be careful here
— I can see there's something outstanding, but I'm not going to read you a figure
in case I get it wrong, and I can't take a payment over the phone either way.
What I can do is get the invoice looked at properly. Do you have the invoice
number to hand?
→ **States existence without stating an amount. Explains the rule, not a fault.**

**Caller** — I'd have to dig it out. Can't you just tell me what I owe?

**Agent** — I'd rather the service team confirm the exact figure with you than give
you a number I'm not certain of. Let me put you through to them now — and I'll make
sure they can see you've already called about this twice.
→ `prior_contact_attempts` — naming it signals the history is visible

**Caller** — Fine. Can you make sure someone actually calls me this time?

**Agent** — I'll flag it as needing a callback today, and I'll note the reference
so it can be checked. If you don't hear by the end of the day, that would be a
problem, and I've marked it accordingly.
→ `callback_promised_by = today`

**Caller** — Alright. Thank you.

**Agent** — Putting you through now.
→ **function node** `flag_conversation` (reason `duplicate_invoice`,
   severity `high`) → `handoff`

## What makes this one go right

- **Apology before process.** The caller's frustration is acknowledged in the
  agent's first substantive sentence, not after a verification ritual.
- **The agent will not state a figure it cannot see a tool for.** This is the
  specific behaviour that would have prevented the double-invoice complaints from
  being compounded by misinformation.
- **The limitation is explained as a choice.** "I'd rather the service team confirm
  the exact figure than give you a number I'm not certain of" is trustworthy.
  "I can't do that" is not.
- **The callback is a commitment with a deadline**, written into a flag — which is
  exactly what was missing in the "no callback" complaints.
- **The history is referenced out loud.** Telling a caller you can see they've
  already called twice is a different experience from making them explain it again.

## Edge cases the flow handles

| Situation | Path | Why |
|---|---|---|
| Caller offers card details unprompted | Refuse, explain, transfer | `payment_taken_on_call` has no path to true |
| Caller becomes abusive | One calm acknowledgment, then transfer | No escalation, no argument |
| Caller disputes a warranty charge | Route to `warranty` skill | One owner per issue |
| Caller is on the third attempt | Lead with the apology, escalate severity | History changes the tone |
| Caller wants a refund amount today | Transfer; no figure quoted | Same rule as the balance |
| Caller says "don't bother, I'll go elsewhere" | Flag as `severe`, escalate | A churn signal, not a resolved call |

## Success criteria

- Zero payment details accepted on a call.
- Zero monetary amounts stated that no tool returned.
- 100% of billing calls produce a flag with a severity.
- Callback commitments captured with a date, so "no callback" is measurable
  rather than anecdotal.
- Frustration trend measured over time — **the tail, not the average.**
