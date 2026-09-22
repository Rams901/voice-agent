# Scenarios — example conversations and the variable model

Worked examples of every scenario the voice agent handles in a dealership, with
the **variables** collected at each step and **who is allowed to be the authority**
for each one.

These files are the design source for `frontdesk/flow.py`. The flow is generated
from the same reasoning, and `frontdesk/scenarios.py` turns the failure modes into
executable regression tests.

| # | Scenario | Primary skill | Key risk it addresses |
|---|---|---|---|
| 00 | [Master variable bank](00-variable-bank.md) | — | The full field model and source-of-truth rules |
| 01 | [Service booking](01-service-booking.md) | booking | Mis-booked car (failure class B) |
| 02 | [Rental / loan car](02-rental-car.md) | rental | Promising a price or vehicle the desk won't honour |
| 03 | [Warranty claim](03-warranty-claim.md) | warranty | Adjudicating a claim the agent can't decide |
| 04 | [Billing and complaints](04-billing-complaint.md) | billing | Double invoices, no callback, churn |
| 05 | [Reschedule and cancel](05-reschedule-cancel.md) | booking | Partial success — the old booking survives |
| 06 | [Out of hours and safety](06-out-of-hours-emergency.md) | routing | Booking a brake failure for next Thursday |
| 07 | [Retention and outbound](07-returning-customer-retention.md) | routing | Losing a customer without ever knowing why |
| 08 | [Handoff and general](08-handoff-and-general.md) | routing | Improvising instead of transferring |

## Conventions used in these files

**Variable tables** list, per scenario: the variable, its source of truth, and
why it sits at that point in the call. Source codes are defined in the
[master variable bank](00-variable-bank.md):

`CALLER` · `DMS` · `TOOL` · `CONFIG` · `CALL` · `INFER`

**Example conversations** show the agent's actual speaking turns. They are short
on purpose — this is a phone call, and a three-sentence reply is already long.
`→` annotates which variable a turn establishes, or which design rule is at work.

**Edge case tables** show the branch the flow takes and the reason it exists. If
a branch can't be justified in one line, it probably shouldn't be a branch.

## The four rules every scenario obeys

1. **The agent proposes; deterministic code disposes.**
   Anything irreversible — a booking, a cancellation, a payment — is validated and
   committed by code, never by the model's judgement.

2. **A format check is not a correctness check.**
   A mask can prove a plate is *plausible*. Only a record lookup can show it is
   *right*. Every high-stakes identifier needs both, plus the caller's confirmation.

3. **Ambiguity is a question, not a choice.**
   Two vehicles on one plate, two appointments for one customer, two candidate
   characters. The agent asks; it never picks the most likely one.

4. **Every path has an exit, and every exit has an owner.**
   No node is terminal without either a resolution or a handoff carrying context.
   A failed transfer still ends with a callback commitment.

## Reading order

If you are new to the problem, read `00` then `01`. The rest follow the same
shape. If you are looking for the flagship scenario, `01` is the
worked example and `../README.md` explains how it is implemented and tested.
