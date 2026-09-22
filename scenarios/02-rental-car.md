# Scenario 02 — Rental / loan car

**Trigger:** the caller wants a car while theirs is in, either as part of a
service booking or as a standalone request. In the flow, this is a *skill* that
the booking or greeting can hand off to.

**The honest constraint:** a rental agreement cannot be completed over voice. It
needs a licence, a payment card, a signature and often an age and licence check.
An agent that pretends otherwise creates a customer standing at a desk being told
something different. So the flow **captures and validates the request, then hands
it to the rental desk**, and says plainly that nothing is confirmed until the desk
confirms it.

## Variables collected, in order

| # | Variable | Source | Notes |
|---|---|---|---|
| 1 | `loan_car_offered` | CONFIG | Some dealers don't offer one. Check before promising |
| 2 | `rental_required` | CALLER | |
| 3 | `rental_start_date` / `_end_date` | CALLER | Linked to the service slot where possible |
| 4 | `rental_days` | TOOL | Derived, never computed by the agent |
| 5 | `rental_vehicle_class` | CALLER | compact / estate / SUV / van |
| 6 | `rental_transmission` | CALLER | Eligibility and availability both depend on it |
| 7 | `rental_driver_age` | CALLER | Insurance eligibility |
| 8 | `rental_licence_number` | CALLER | Captured and read back, **not judged** |
| 9 | `rental_licence_country` | CALLER | Cross-border rules differ |
| 10 | `rental_licence_expiry` | CALLER | Must be valid at collection |
| 11 | `additional_drivers` | CALLER | Each needs their own licence |
| 12 | `rental_insurance_option` | CALLER | Excess reduction etc. |
| 13 | `rental_pickup_location` | CALLER | |
| 14 | `rental_desk_handoff` | TOOL | Flagged for the desk |
| 15 | `rental_price_per_day` / `_total` | DMS | **Never quoted by the agent** |

## Example conversation

**Agent** — …so you're booked in for Tuesday the sixth at half past eight.
While the car's with us, would you like us to arrange a loan car?

**Caller** — Yes please, that'd be really helpful.
→ `rental_required = true`

**Agent** — Of course. I'll need a few details, and I should say now — the rental
desk will confirm the vehicle and the price with you, as I can't reserve one from
here. Is that alright?

**Caller** — Yeah, that's fine.

**Agent** — How many days would you need it for?

**Caller** — Just the Tuesday, so one day. Actually, maybe two to be safe.

**Agent** — Let's say Tuesday and Wednesday then. Is an estate what you'd need, or
would something smaller do?
→ `rental_start_date`, `rental_end_date`, `rental_vehicle_class`

**Caller** — An estate ideally, I've got a lot of kit.

**Agent** — Noted. And would that be a manual or an automatic?

**Caller** — Automatic.

**Agent** — Can I take your age, just for the insurance?
→ `rental_transmission`, `rental_driver_age`

**Caller** — Forty-two.

**Agent** — Thank you. Could you read me your driving licence number? It's the
sixteen characters on the front.
→ `rental_licence_number` — **captured, validated for format, read back, but the
   agent makes no claim about entitlement**

**Caller** — It's M-O-R-G-A, eight-seven, six-five, four-three, two-one, zero.

**Agent** — Let me read that back — M for Mike, O for Oscar, R for Romeo,
G for Golf, A for Alpha… eight, seven… six, five… four, three… two, one, zero.
Is that right?
→ read-back uses the same phonetic discipline as the plate

**Caller** — That's right.

**Agent** — And which country issued it?
→ `rental_licence_country`

**Caller** — UK.

**Agent** — Will anyone else be driving?

**Caller** — No, just me.

**Agent** — Perfect. I've got a note to pass to the rental desk: an automatic
estate for Tuesday and Wednesday. They'll call you to confirm the vehicle and the
price before anything is reserved.
→ `rental_desk_handoff = true`

**Caller** — Sounds good.

## What makes this one go right

- **The agent states the limitation up front**, before collecting anything. That
  is a design decision, not a disclaimer: it prevents the caller building a plan
  on an unconfirmed vehicle.
- **No price is ever spoken** unless the DMS returned one. Rental pricing depends
  on class, days, age, excess and season — a model guessing at it is a complaint
  waiting to happen.
- **The licence number gets the full capture pipeline** but no judgement. The
  agent cannot know if a licence is valid or if the class covers the vehicle.
- **The handoff is recorded, not just spoken.** If the desk never calls back, the
  flag is the only thing that surfaces it.

## Edge cases the flow handles

| Situation | Path | Why |
|---|---|---|
| Dealer doesn't offer loan cars | `rental_intro` says so; offers collection instead | `offers_loan_car` is CONFIG, not a guess |
| Caller pushes hard for a price | Agent holds, offers desk callback | The flow has no path that produces a price |
| Caller wants the rental without a service | `rental_intro` still runs standalone | Same skill, different entry point |
| Licence number is 15 or 17 characters | Read back and re-captured | Format matters; the agent doesn't silently "fix" it |
| Rental dates don't overlap the service | Agent notes the mismatch and confirms both | Prevents a gap the caller didn't intend |
| Under-25 driver | Agent says the desk will check eligibility | Age affects insurance; not the agent's call |

## Success criteria

- Zero rental prices quoted by the agent that didn't come from the DMS.
- Zero "your loan car is booked" statements before the desk confirms.
- Every rental request produces a flag (i.e. none are quietly dropped).
- Licence-number read-back rate of 100% on this path.
