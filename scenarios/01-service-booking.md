# Scenario 01 — Service booking

**Trigger:** caller wants to book, move or cancel a service, MOT, repair or
diagnostic appointment. The highest-volume scenario, and the one most calls in
production are actually about.

## Variables collected, in order

| # | Variable | Source | Why here |
|---|---|---|---|
| 1 | `recording_consent` | CALLER | Legal gate. Nothing else happens first. |
| 2 | `from_number` | CALL | Silent identity; no need to ask |
| 3 | `registration_plate` | CALLER → DMS | Highest-error field on the call |
| 4 | `plate_confirmed_by_caller` | CALLER | **Gate** — no lookup before `true` |
| 5 | `vehicle_id`, `vehicle_summary` | DMS | Personalises everything after |
| 6 | `symptom_description` | CALLER | Captured verbatim; not diagnosed |
| 7 | `service_type` | CALLER → DMS | Mapped to a DMS service product |
| 8 | `warning_light_on` | CALLER | Routes to diagnostics if true |
| 9 | `transport_option` | CALLER | Drives duration and courtesy-car need |
| 10 | `date_range_from` / `_to` | TOOL | Widened automatically if empty |
| 11 | `offered_slots` | DMS | **Only offerable times** |
| 12 | `chosen_slot` | CALLER | Must be in `offered_slots` |
| 13 | `booking_confirmed` | CALLER | **Commit gate** |
| 14 | `booking_reference` | DMS | Read from the write, not composed |
| 15 | `write_verified` | TOOL | Read back from the DMS |

## Example conversation

> Line breaks are the agent's actual speaking turns — short, because this is a
> phone call. `→` marks a variable the turn is establishing.

**Agent** — Good morning, Northgate Motors service department. This call is
recorded for quality and training. Is that alright?
→ `recording_consent`

**Caller** — Yeah, fine.

**Agent** — Thank you. What can I help you with today?

**Caller** — Hi, I need to get my car serviced, it's overdue.

**Agent** — Happy to help with that. Could I take your registration number?
→ intent = `booking`

**Caller** — Yeah, it's A B twelve C D E.

**Agent** — Let me make sure I've got that right. A for Alpha, B for Bravo… one,
two… C for Charlie, D for Delta, E for Echo. Is that correct?
→ `registration_plate = "AB12CDE"` (validated by tool first, then read back)

**Caller** — That's it, yes.
→ `plate_confirmed_by_caller = true` ← **the gate. Only now can we look up.**

**Agent** — Thank you. I can see a 2021 BMW 3 Series, grey — is that the one?
→ `vehicle_summary`, `vehicle_id`

**Caller** — That's the one.

**Agent** — Great. And is anything else you've noticed, or is it just the service?

**Caller** — There's a squeak from the passenger door, and the aircon's a bit weak.

**Agent** — I'll note both down. And is any warning light showing on the dash?
→ `symptom_description`, `warning_light_on`

**Caller** — No, nothing like that.

**Agent** — Perfect. Will you be waiting with us, or would you like a loan car?
→ `transport_option`

**Caller** — I'll just wait, it's nearby.

**Agent** — And when would suit you?

**Caller** — Sometime next week, ideally a morning.
→ `date_range_from` / `_to`, `preferred_time_window`

**Agent** — Let me look at the diary.
→ **function node** `check_availability` — the agent does not offer a time it
   has not been given

**Agent** — I've got Tuesday the sixth at half past eight in the morning, or
Wednesday the seventh at eleven. Would either of those work?
→ `offered_slots`

**Caller** — Tuesday morning's good.

**Agent** — Let me read that back to you. A 2021 BMW 3 Series, registration
A B one two C D E, in for a service on Tuesday the sixth of October at half past
eight, and you'll be waiting on site. Have I got all of that right?
→ **the full read-back, including the vehicle again** — this is the turn that
   answers "it forgets earlier details"

**Caller** — Yes, that's right.
→ `booking_confirmed = true`

**Agent** — One moment, I'm putting it in the diary.
→ **function node** `create_appointment` (idempotent) → **function node**
   `verify_write`

**Agent** — All booked. Your reference is NM1001, and you'll get a text
confirmation shortly. Anything else I can help with?
→ `booking_reference`, `write_verified`

**Caller** — No, that's everything. Thanks.

**Agent** — Thanks for calling Northgate Motors. Goodbye.

## What makes this one go right

- **The plate is validated before it is read back**, so the read-back is of a
  *structurally plausible* value rather than of whatever the ASR guessed.
- **`plate_confirmed_by_caller` gates the lookup.** If the caller had said
  "yeah, I think so", the flow returns to read-back instead of proceeding.
- **`chosen_slot` can only be a value from `offered_slots`.** The agent has no
  mechanism to invent a time.
- **The write is verified.** If `verify_write` had failed, the flow goes to
  `booking_failed` → flag → handoff, and the caller is never told it worked.

## Edge cases the flow handles

| Situation | Path | Why |
|---|---|---|
| Heard "AB1ZCDE" (Z where a digit belongs) | `plate_repair` → targeted question about position 4 | One repaired character, one question, not a full re-spell |
| Three characters need repair | `plate_repair` | Past `MAX_COERCIONS` this is a guess, not a validation |
| Plate matches two vehicles | `disambiguate_vehicle` | "The grey BMW or the blue Mercedes?" — the agent must not pick |
| Plate not on record | `vehicle_not_found` | Offer re-spell, proceed as new customer, or transfer |
| No slots in the requested range | `no_slots` | Widened range or callback. Never a made-up time |
| Caller changes the day mid-confirmation | `confirm_booking` → `capture_timing` | Re-check availability; the old slot may be gone |
| Booking write fails | `booking_failed` → `flag_and_handoff` | Honest failure beats a false confirmation |
| Caller hesitates when confirming | `confirm_booking` re-asks | An unsure "yes" is not a yes |

## Success criteria

- Registration exact-match rate on the first attempt (not word error rate).
- Zero bookings written against an unconfirmed plate.
- Zero bookings where the DMS record disagrees with what the caller was told.
- Zero slots offered that did not come from `offered_slots`.
