# Scenario 06 — Out of hours, breakdown and safety-critical

**Trigger:** the call arrives outside opening hours, or the caller describes
something that is not a routine booking — a warning light, smoke, a brake
failure, or a car that won't start.

**Why this scenario needs its own design:** these calls are low-volume and
high-consequence. They are also exactly the calls that look fine in aggregate
metrics: "contained" and "resolved" both appear true when an agent politely
books a brake failure in for next Thursday.

**The rule:** safety-critical descriptions are a **routing decision**, not a
booking decision. The agent's job is to recognise the class, stop selling a slot,
and get the caller to the right place with the right urgency.

## Variables collected

| Variable | Source | Notes |
|---|---|---|
| `is_opening_hours` | CONFIG | Compare call time against dealer hours |
| `is_driveable` | CALLER | The single most important routing field |
| `warning_light_on` | CALLER | Which one matters; capture verbatim |
| `symptom_description` | CALLER | Verbatim. The agent does NOT diagnose |
| `safety_critical` | INFER | smoke / brakes / steering / fuel leak / overheating |
| `urgency` | INFER | routine / soon / safety_critical |
| `recovery_required` | CALLER | Does the car need collecting? |
| `recovery_address` | CALLER | Only if recovery is needed |
| `caller_safe` | CALLER | **Ask before anything else if the car is on a road** |
| `out_of_hours_route` | CONFIG | recovery / OEM hotline / next-day callback |
| `callback_promised_by` | CALLER | When the team will call |
| `escalation_reason` | INFER | safety / hours / capability |

## Example conversation

**Agent** — Northgate Motors service. We're closed at the moment, but I can still
help — what's happened?

**Caller** — My car's got a red brake warning on the dash and the pedal feels
soft. I'm on the A34 hard shoulder.

**Agent** — First — are you somewhere safe, away from the traffic?
→ **Safety of the person outranks every other objective.** No booking talk yet.
→ `caller_safe`

**Caller** — Yes, I'm behind the barrier.

**Agent** — Good. Keep well back from the carriageway. Soft brakes and a red
warning light aren't something to drive on, so this needs recovery rather than a
booking. I'm going to put you through to our recovery line now. Can you confirm
your registration so I can pass it on?
→ `safety_critical = true`, `is_driveable = false`, `recovery_required = true`

**Caller** — AB12 CDE.

**Agent** — A for Alpha, B for Bravo… one, two… C for Charlie, D for Delta,
E for Echo. Right?

**Caller** — Yes.

**Agent** — Thank you. And a number to reach you on, in case we get cut off?
→ `phone_number`

**Caller** — The one I'm calling from.

**Agent** — I'll use that. I'm flagging this as urgent and passing on the
details — recovery, possible brake failure, caller is safe behind the barrier.
Staying on the line, putting you through now.
→ `flag_conversation` (severity `high`) → `handoff`

## What makes this one go right

- **The agent asks about the person's safety first.** Not the vehicle, not the
  booking. This is the clearest signal that the system was designed around
  outcomes rather than task completion.
- **The agent refuses to book.** "This needs recovery rather than a booking" is
  the correct output, and it is only possible because `safety_critical` routes
  *before* the booking skill.
- **The agent does not diagnose.** It repeats the caller's own words — "soft
  brakes and a red warning light" — without adding a cause. Diagnosing a brake
  fault over the phone is a liability, not a service.
- **The plate still gets the full capture pipeline**, because recovery needs it
  to be right. Safety urgency does not mean skipping verification.
- **The flag carries the urgency**, so the review queue is ordered by severity
  rather than by arrival.

## Edge cases the flow handles

| Situation | Path | Why |
|---|---|---|
| Caller describes smoke or a fuel smell | Safety branch, recovery, no booking | Highest-consequence misroute |
| Caller is *on* a live carriageway | Safety instruction first, offer emergency services | The agent's priority is the person |
| Caller insists they'll drive it in | Agent states the risk once, then routes | Advise, don't argue |
| Outside hours, routine request | Offer next-day booking or callback | Don't block a simple booking |
| Red warning light, car parked safely at home | Diagnostics booking with urgency `soon` | Not every warning light is an emergency |
| Dealer has no recovery contract | Route to OEM hotline from CONFIG | Capability matrix again |
| Caller cannot speak freely | Offer SMS link instead | Channel fallback |

## Success criteria

- Zero safety-critical calls that ended in a routine booking.
- 100% of safety-critical calls flagged with severity `high`.
- `caller_safe` captured before any vehicle discussion on recovery calls.
- Zero diagnoses spoken by the agent (audited on transcripts).
- Out-of-hours calls never left unresolved without a callback commitment.
