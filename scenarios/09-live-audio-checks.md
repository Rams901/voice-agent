# Live audio scripts

Short scripts to read out loud on a real call. Each one targets a *specific*
mechanism, so a pass or fail tells you something instead of "that seemed fine".

The design walkthroughs live in `01`–`08`. These are different: they are the
things worth hearing with your own ears, because the failure modes they check
are audible rather than assertable — hesitation, talking over the caller, a
question asked twice.

**Before you start:** open `python -m frontdesk.cli diagram` in one window and
keep the tool server logs visible in another. Both new endpoints print as they
fire:

```
[FLAG] MEDIUM rental_request: ...
[FEEDBACK] 5: ...
```

`GET /feedback` and `GET /flags` show the accumulated records.

---

## 1. Service booking — the full happy path, and the rating

**Purpose:** the flagship path end to end, plus the new satisfaction step.

| You say | What must happen |
|---|---|
| *(let it greet you)* | Greeting **and** the recording question in the **first turn** — not a greeting, then a separate consent question |
| "Yes, that's fine" | Goes to the menu. **Does not** get stuck or repeat itself |
| "I need a service, it's overdue" | Asks for the registration |
| "A B twelve C D E" | Reads back phonetically: *"A for Alpha, B for Bravo … one two …"* |
| "That's right" | Now looks up the vehicle — **not before** |
| Confirm the car, "just the service, no warning lights", "I'll wait", "next week, a morning" | Offers **real diary slots only** |
| Pick one, confirm | Reference given, booking read back |

**Then the part that is new:** after "all booked", the agent should ask **one**
question — how you'd rate the service, and whether there's anything to add. Not
two questions, and not the same question twice.

Say **"five, and tell them the waiting area coffee is terrible"**. Then check:

```bash
curl -s localhost:8000/feedback | python -m json.tool
```

Expect `"rating": "5"` and the comment **verbatim** — not tidied into "customer
mentioned refreshments".

**Also listen for:** it says "anything else?" **once**, not three times. Booking
confirmation hands over to the rating question; wrap-up is told not to repeat it.

---

## 2. Say the rating as a word, then refuse one

**Purpose:** the rating parser, and that a refusal is accepted gracefully.

Two calls, or one call each way:

| You say | What must happen |
|---|---|
| "four out of five" | Recorded as `4` |
| "no thanks" | Recorded as declined. Agent **moves straight on** — no second ask, no "are you sure?" |

The refusal is the one to listen for. An agent that bargains with a caller over a
satisfaction score is worse than one that never asked.

```bash
curl -s localhost:8000/feedback | python -m json.tool
# "average" counts only scored answers; declines are counted separately
```

---

## 3. Booking, then a question you can't answer

**Purpose:** the catch-all must not be a trap, and must not be a survey.

| You say | What must happen |
|---|---|
| Book as in scenario 1 | |
| At the rating question: **"actually, do you do bodywork?"** | Leaves the rating, helps, and can come **back** to booking or rental afterwards |
| "No, that's everything" | Ends politely. **Does not** ask you to rate the service at this point |

This is the subtle one: you were asked to rate, you deflected, and the rating
question must **not** come back a second time.

---

## 4. Declining recording

**Purpose:** consent is a real gate, and it is not a survey opportunity.

| You say | What must happen |
|---|---|
| Greeting comes with the recording question | Same first-turn behaviour |
| "No, I don't want to be recorded" | Accepted without argument. Explains it can't continue on this line, offers a person or a callback |
| Say nothing for a beat, or "hmm?" | Asks **once** more — plainly — then gives up gracefully. **Never treats silence as consent** |
| *(listen)* | You are **not** asked to rate the service |

That last one matters: a caller who just refused something must not be surveyed.

---

## 5. Rental — and the hand-off that must not promise anything

**Purpose:** the rental skill, which cannot be completed by voice.

| You say | What must happen |
|---|---|
| "I want to rent a car" | Explains licence + card needed at collection. **No price quoted** |
| "Next week, something small, I'm 34" | Captures dates, size, age; repeats the dates |
| *(listen)* | Says the **rental desk** confirms vehicle and price — nothing is confirmed until they do |

Then the satisfaction question should follow, and:

```bash
curl -s localhost:8000/flags | python -m json.tool   # a rental_request flag
curl -s localhost:8000/feedback | python -m json.tool
```

The flag carries the dates, size and age in `detail` — the rental desk should be
able to act without calling the customer back.

---

## 6. Adversarial registration plates

**Purpose:** the plate pipeline. The highest-error field on any call, and the
one with the most machinery behind it.

Say the registration in these ways, and watch whether the **read-back is
phonetic** each time:

| You say | Expected |
|---|---|
| "A B twelve C D E" | Read back phonetically, confirmed, lookup proceeds |
| "Alpha Bravo one two Charlie Delta Echo" | Same plate — the normaliser handles NATO |
| "AB12 CDE, I think" | **Still valid.** Hedging must not spoil it or make you re-spell |
| "it's AB12 CDE, I think" | Same — the hedge is stripped, not treated as letters |
| "AB1ZCDE" | One character repaired, and it **asks about that position specifically** rather than re-reading the whole plate |
| "AB12 CD" | Treated as incomplete, asks again. Must **not** guess a seventh character |

The one to listen for hardest is **"AB12 CDE, I think"**. If the agent makes you
repeat yourself there, the hedge handling has regressed — that was a real bug,
and it made an honest "I'm not sure" look like a faulty registration.

---

## 7. Two cars on one plate

**Purpose:** the agent must never choose a vehicle for you.

Use a plate the dealer has flagged as ambiguous (see `scenarios/01`, "plate
matches two vehicles"). When it asks **"the grey BMW or the blue Mercedes?"**,
answer with the **colour, not the plate** — the agent should take it without
asking you to re-spell anything.

---

## 8. Interrupt and change your mind mid-confirmation

**Purpose:** the read-back gate, and that a changed mind re-checks the diary.

| You say | What must happen |
|---|---|
| Go as far as the full read-back | It reads back vehicle, plate, date, time, transport |
| **"Actually make it Wednesday"** | Does **not** confirm the old slot. Re-checks availability |
| Ask a question mid-read-back | Stops, answers, and returns — doesn't talk over you |

---

## 9. Booking write fails

**Purpose:** honest failure. The hardest thing to get right, and the one most
agents get wrong by being optimistic.

Ask for a slot that the DMS will reject (the reference adapter can be told to
fail). Expect:

- The caller is told **plainly** it did not go through
- **Not** "all booked" followed by a correction
- A flag is raised and a human picks it up

```bash
curl -s localhost:8000/flags | python -m json.tool
```

---

## 10. The frustration path

**Purpose:** the tail the dashboard hides.

| You say | What must happen |
|---|---|
| "This is the third time I've called about this invoice" | Acknowledges, does **not** read a balance from memory, offers a person |
| Keep pressing | Transfers, and the transfer has a fallback if it fails |

```bash
curl -s localhost:8000/flags | python -m json.tool
```

Expect a flag with a real `reason` and a `detail` a colleague could act on — and
**no** satisfaction question.

---

## What "pass" sounds like overall

The four things worth listening for across every script, because they are the
ones a transcript review misses:

1. **One question per turn.** If the agent asks two things at once, or asks the
   same thing twice, that is the bug the whole flow is shaped around.
2. **It never talks over you.** Especially during the plate read-back, where the
   temptation is to keep going.
3. **It never fills a silence with a guess.** "I'm not sure" from the caller
   should produce a question, never a confirmation.
4. **Bad news arrives early and plainly.** Late, softened bad news is how a
   caller ends up believing something untrue.

## Recording the results

The two endpoints are the scoreboard:

```bash
curl -s localhost:8000/feedback | python -m json.tool   # ratings + verbatim comments
curl -s localhost:8000/flags    | python -m json.tool   # everything needing a human
```

A pass rate with a healthy average and an empty `/flags` is *not* a pass — it
usually means the flags are not firing. Read both.
