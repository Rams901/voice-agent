# Scenario 07 — Returning customer, outbound and retention

**Trigger:** outbound call or callback to a customer the dealer already knows —
service due, MOT expiring, declined work from a previous visit, or a customer who
hasn't been back.

**Why this scenario is the business case:** the framing here is retention, not
acquisition. *"Growth now depends on keeping customers longer. 10% better
retention ≈ 30% more revenue. A six-year-old vehicle customer is worth 3x a new
one. Don't measure how much you made — measure how much you lost."*

That reframing changes the agent's objective. An inbound booking agent optimises
for a confirmed appointment. A retention agent optimises for **prevented loss** —
and that means the successful outcome is sometimes "we found out why they left",
not "we booked them in".

## Variables collected

| Variable | Source | Notes |
|---|---|---|
| `customer_id`, `caller_first_name` | DMS | Known before dialling; use the name |
| `preferred_language` | DMS/CONFIG | Switch immediately if wrong |
| `callback_consent` | CALLER | **Required before outbound marketing/reminders** |
| `vehicle_summary`, `vehicle_mileage` | DMS | Context for why we're calling |
| `last_visit_date` | DMS | |
| `last_service_type` | DMS | |
| `declined_work` | DMS | Prior recommendations not taken up |
| `service_due_reason` | TOOL | mileage / time / MOT / recall / declined work |
| `is_returning_customer` | TOOL | Based on visit history |
| `days_since_last_visit` | TOOL | |
| `retention_risk` | TOOL | churn signal strength |
| `churn_reason` | CALLER | **The most valuable field in the whole model** |
| `wants_to_book` | CALLER | |
| `do_not_contact` | CALLER | Must be honoured immediately and permanently |
| `next_best_action` | INFER | book / callback / offer / no_contact |

## Example conversation

**Agent** — Good afternoon, could I speak with Sarah Whitfield?

**Caller** — Speaking.

**Agent** — Hello Mrs Whitfield, it's Northgate Motors service. I'm calling
because your BMW 3 Series is due its annual service — it's been about fourteen
months, and the mileage on record suggests it's due. Is now an alright time?
→ `service_due_reason = time + mileage`, `days_since_last_visit`
→ **States the reason immediately.** A retention call that starts with a pitch
   gets hung up on.

**Caller** — Oh. Actually, I've been meaning to call you about something.

**Agent** — Of course, go ahead.

**Caller** — I wasn't that happy last time. The invoice had two charges for the
same thing and it took three phone calls to sort out. I ended up taking it to a
garage near work instead.
→ `churn_reason = billing_dispute + no_callback`, `retention_risk = high`

**Agent** — That's a fair complaint, and I'm sorry — once is one time too many,
and three calls is not acceptable. You shouldn't have had to chase it. Can I ask
— was it resolved in the end, or is it still outstanding?
→ **Acknowledge, apologise, and ask, without defending.** The instinct to explain
   the process here loses the customer permanently.

**Caller** — It got sorted eventually. But it left a bad taste.

**Agent** — I understand, and I'd feel the same. Thank you for telling me — it's
genuinely useful, and I'll make sure it's recorded so it isn't something you have
to explain twice. I'm not going to try to talk you out of anything today. Would it
be alright if someone from our service team called you, or would you rather I took
you off our list?
→ **This is the retention move.** Offering the exit is what makes the offer
   credible; and `do_not_contact` is honoured immediately, not resisted.

**Caller** — No, it's okay. You can have someone call me. I'd rather use you if the
service is good.

**Agent** — I appreciate that. I'll ask them to call you this week, and I'll note
what happened so you don't have to start from the beginning.
→ `callback_consent = true`, `callback_promised_by`

**Caller** — Thanks. That's better than the last time, I'll say that.

**Agent** — That's kind of you. We'll speak this week.

## What makes this one go right

- **The reason for the call comes first.** No "how are you today", no pitch.
- **The complaint is not defended against.** The agent neither explains the
  double-invoice process nor promises it can't happen again. It apologises and asks.
- **The exit is offered.** Offering to stop contact increases the chance of
  continuing it, and it is the only honest way to handle `do_not_contact`.
- **`churn_reason` is captured as structured data.** A hundred of these is a
  backlog of fixable operational problems — and it is where the double-invoice
  pattern (`>45% of discussions`) becomes visible instead of anecdotal.
- **The promise is recorded, not spoken.** "Someone will call this week" is a
  flag with a date, so it is measurable.

## Edge cases the flow handles

| Situation | Path | Why |
|---|---|---|
| Wrong number / not the customer | Apologise, remove from list | Data quality, immediately |
| Customer says "don't call again" | Honour instantly, set `do_not_contact` | Legal and reputational |
| Customer wants to complain formally | Route to `billing` skill, flag `high` | Don't absorb a complaint inside a sales call |
| Customer has sold the vehicle | Update record, offer no_contact | Keeps the DMS clean |
| Customer is distressed by the call | Apologise, end politely, flag | Outbound has a higher duty of care |
| Answering machine | Leave a message or not, per CONFIG | Market rules differ |
| Customer asks how you got the number | Explain the service relationship honestly | Consent transparency |

## Success criteria

- `churn_reason` captured on 100% of at-risk conversations.
- `do_not_contact` honoured with zero repeat contact (a hard failure if violated).
- Callback commitments kept — measured, not assumed.
- Returning-customer rate and days-since-last-visit tracked by cohort.
- **Prevented loss is reportable**, not just bookings taken.
