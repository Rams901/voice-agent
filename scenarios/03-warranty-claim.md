# Scenario 03 — Warranty claim

**Trigger:** the caller says a fault should be covered, or the agent's lookup
shows active warranty on a repair the caller is booking.

**The constraint:** whether a fault is *covered* is a technical and contractual
decision. The agent can report coverage dates from the DMS; it cannot adjudicate
a claim. Getting this wrong in either direction is expensive — promising cover
that is refused, or refusing cover the customer is entitled to.

## Variables collected

| Variable | Source | Notes |
|---|---|---|
| `registration_plate` | CALLER → DMS | Same capture pipeline |
| `vehicle_id` | DMS | |
| `warranty_active` | DMS | Read from the record |
| `warranty_expiry` | DMS | Date only; condition is not the agent's call |
| `warranty_provider` | CALLER | Third-party warranties often aren't in the DMS |
| `warranty_policy_number` | CALLER | High-stakes capture if it will be used |
| `symptom_description` | CALLER | What the caller believes is covered |
| `claim_requires_assessment` | CONFIG | Usually true — drives escalation |
| `is_warranty_claim` | TOOL | Derived from service type + coverage |
| `claim_reference` | DMS | Only after the service team opens a claim |
| `service_history` | DMS | Recall work, prior fixes to the same fault |

## Example conversation

**Agent** — Northgate Motors service, good morning. How can I help?

**Caller** — My car's got a rattle in the gearbox and I'm told it should still be
under warranty. I want it looked at.

**Agent** — I'm sorry to hear that. Let me pull up the vehicle first — could I take
the registration?
→ intent = `warranty` + `booking`

**Caller** — YX19 JKL.

**Agent** — Let me check that back — Y for Yankee, X for X-ray… one, nine… J for
Juliett, K for Kilo, L for Lima. Correct?

**Caller** — Yes.

**Agent** — Thank you. I can see a 2019 Toyota Corolla Hybrid, white. Is that right?
→ `vehicle_summary`

**Caller** — That's the one.

**Agent** — I can see a warranty showing as active on this vehicle, expiring in
November. So there's a good chance this is covered — but I'm not able to confirm
whether a specific fault is covered, and I don't want to promise you something I
can't stand behind. The service team will assess it and confirm before any work
starts. Shall I get it booked in for that?
→ **The honest position: report what the record says, refuse to adjudicate.**

**Caller** — Okay, but if it's not covered I want to know before they do anything.

**Agent** — That's completely fair, and I'll put that on the booking as an
instruction: no work without your approval. Now, when would suit you?

→ `special_instructions = "no work without prior approval of cost"`

**Caller** — ASAP, it's not great to drive.

**Agent** — Understood, let me check the earliest we have.
→ `check_availability` — the agent doesn't promise "ASAP" it can't verify

**Agent** — I have tomorrow at half past eight, or Thursday at eleven. Which is
better?

**Caller** — Tomorrow, please.

**Agent** — Booked provisionally for tomorrow at half past eight, for the gearbox
rattle to be assessed under the warranty, and I've noted that no work should
begin without your approval. You'll get the reference by text. Anything else?

**Caller** — No, that's great.

## What makes this one go right

- **The warranty status is read from the DMS, never inferred.** "Showing as active,
  expiring in November" is a fact from a record; "that's covered" is a decision.
- **The refusal is specific and paired with an alternative.** The agent refuses to
  adjudicate *and* says who will, and when — that is the difference between
  competent and obstructive.
- **The caller's condition becomes `special_instructions`.** The promise the agent
  makes is written into the booking, so it survives the call.
- **Third-party warranties are captured as caller-supplied data**, not treated as
  DMS facts, because they usually live outside the dealer's system.

## Edge cases the flow handles

| Situation | Path | Why |
|---|---|---|
| Warranty expired last month | Agent states the expiry date and that it isn't the agent's call | Reading a date ≠ judging a claim |
| Third-party warranty the DMS doesn't know about | Captured with `warranty_provider` + policy number, read back | Caller-supplied, must not be presented as DMS-verified |
| Caller insists the agent confirm cover | Escalate to the service team | The agent has no path that confirms cover |
| Same fault visited before | Surface `service_history` and let the team see it | Recall work matters and is easy to miss |
| Safety-critical symptom | Escalate urgency, possibly recovery | Not a routine booking |
| Caller disputes that a past fault was charged | Route to `billing` skill | Overlapping intents get one owner |

## Success criteria

- Zero claims adjudicated by the agent (neither promised nor refused).
- 100% of warranty calls carry `claim_requires_assessment` handling.
- `special_instructions` present whenever the caller attached a condition.
- Zero warranty calls where the agent contradicted the DMS expiry.
