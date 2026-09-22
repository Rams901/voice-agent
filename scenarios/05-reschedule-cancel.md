# Scenario 05 — Reschedule and cancellation

**Trigger:** the caller wants to move or cancel an existing appointment.

**Why it deserves its own scenario:** it is the same booking data touched in a
different direction, and it is where "I never got a confirmation" complaints come
from. The failure mode is subtler than a mis-booked car: the *old* appointment
survives. The customer thinks they've moved it; the dealer has two bookings and
one empty bay.

**The rule this scenario exists to enforce:** moving an appointment is a
**two-part transaction** — cancel the old, create the new — and both halves must
be verified. A partial success is worse than a failure, because the caller has no
reason to suspect anything went wrong.

## Variables collected

| Variable | Source | Notes |
|---|---|---|
| `registration_plate` | CALLER → DMS | Same capture pipeline |
| `booking_reference` | CALLER | Or found via the plate |
| `existing_appointment_id` | DMS | Required — the old record must be identified, not assumed |
| `existing_appointment_datetime` | DMS | Read back so the caller confirms it's the right one |
| `cancellation_reason` | CALLER | Feeds the retention signal |
| `cancel_confirmed` | CALLER | Gate |
| `cancellation_verified` | TOOL | The old record is actually gone |
| `new_date_range` | CALLER | Only if rescheduling |
| `chosen_slot` | CALLER | Must be in `offered_slots` |
| `create_verified` | TOOL | New record actually exists |
| `retention_signal` | INFER | cancelled_by_us / cost / time / sold_vehicle / other |

## Example conversation

**Agent** — Northgate Motors service, how can I help?

**Caller** — I need to move my appointment, something's come up.

**Agent** — No problem at all. Could I take your registration so I can find it?
→ intent = `reschedule`

**Caller** — AB12 CDE.

**Agent** — A for Alpha, B for Bravo… one, two… C for Charlie, D for Delta,
E for Echo. Right?
→ `registration_plate`

**Caller** — Yes.

**Agent** — Thank you. I can see the 2021 BMW 3 Series. And I can see an
appointment for Tuesday the sixth of October at half past eight. Is that the one
you'd like to move?
→ `existing_appointment_id`, `existing_appointment_datetime`

**Caller** — That's the one.

**Agent** — Understood. Would you rather move it to another day, or cancel it
altogether?

**Caller** — Move it. Can we do the week after?

**Agent** — Of course. I'll cancel the Tuesday and find you something in the
following week — I don't want to hold both, so let me sort the new one first and
I'll confirm before I release the old one. Let me check the diary.
→ **The agent explains the sequencing.** This is a design decision: it never
   leaves the caller in a state where they have no booking.

**Caller** — Fine.

**Agent** — I've got Monday the twelfth at half past eight, or Wednesday the
fourteenth at eleven. Either work?
→ `offered_slots`

**Caller** — Wednesday at eleven.

**Agent** — Let me read it back. I'm cancelling Tuesday the sixth at half past
eight, and booking Wednesday the fourteenth of October at eleven, same vehicle,
same service. Shall I go ahead?
→ `cancel_confirmed` gate — **one confirmation covering both halves**

**Caller** — Yes, please.

**Agent** — One moment.
→ **function node** `create_appointment` (new) → **function node**
   `verify_write` → **function node** `cancel_appointment` (old) →
   **function node** `verify_cancellation`

**Agent** — Done. The new booking is Wednesday the fourteenth at eleven, reference
NM1004, and the Tuesday slot is released. You'll get a text confirming the change.
→ Both halves named explicitly. "Done" alone is not enough.

**Caller** — Great, thanks.

## What makes this one go right

- **The old appointment is identified from the DMS, not inferred from what the
  caller said.** If the DMS shows no such appointment, the flow says so rather
  than creating a duplicate.
- **Sequenced so the caller is never uncovered.** New booking first, verify, then
  release the old one.
- **Both halves are verified.** A single "booking created" response cannot prove
  the cancellation worked.
- **One confirmation, both actions named.** Two separate yes/no questions about
  two bookings is how callers get confused and agree to the wrong thing.
- **The cancellation reason is captured.** Over time this is a retention dataset:
  cancellations by reason are far more actionable than a single churn number.

## Edge cases the flow handles

| Situation | Path | Why |
|---|---|---|
| No appointment found for the plate | Say so; offer to book fresh | Never assume, never duplicate |
| Multiple appointments | Read each one back; caller picks | Ambiguity again — ask, don't pick |
| Caller cancels entirely | Confirm, then cancel, then *verify* | A silent failure looks like success |
| New slot taken mid-conversation | Re-offer from fresh availability | `check_availability` runs again |
| Caller says "just cancel, I've sold the car" | Cancel + record `retention_signal` | Churn reason is the valuable part |
| Caller reschedules twice in one call | Loop is bounded; second time is a fresh capture | Prevents fee/charge disputes |
| Cancellation is inside the fee window | State the policy, offer transfer | Policy decisions are not the agent's |

## Success criteria

- Zero reschedules where the old appointment survives.
- Zero reschedules where the new appointment is missing but the caller was told
  it worked.
- Cancellation reason captured on 100% of cancellations.
- `create_verified` **and** `cancellation_verified` both true before any success
  language is spoken.
