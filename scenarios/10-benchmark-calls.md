# Benchmark calls — manual script

Two journeys, on both architectures. Four calls in total, roughly
$0.31 each. Read the caller's part as written: the architectures are
being compared, so the caller has to be the same in both or the
comparison measures the caller instead of the flow.

Start each call with the command below (it tags the call so the report
can pair it with the journey), then open the printed link, allow the
mic, and speak the script. Hang up when the agent has finished; do not
let it run on, because cost is billed per minute.

## What the live system will actually say

The tool mocks used by the batch arm do **not** apply to a real call, so
the live DMS answers instead. It is seeded with a deliberately ambiguous
plate, which is worth knowing before you dial — otherwise the agent asks
a question the script has not prepared you for:

- `AB12CDE` returns **two** vehicles, not one:
  - 2021 BMW 3 Series 320i, **grey**
  - 2019 Mercedes-Benz C 200 AMG Line, blue

  When the agent asks which you mean, say **"the grey BMW"**. This is the
  disambiguation branch, and both architectures should handle it the same
  way — it is one of the behaviours being compared.

- Availability for 1–7 October is six slots: 08:30, 11:00 and 14:30 on
  both Thursday 1 and Friday 2. Ask for, and accept, **Thursday 1 October
  at 08:30** in both calls, so the two architectures follow the same path.

- The rating at the end writes to the tool server's `/feedback`, so what
  you say is recorded and can be shown afterwards.

- Registration speech: the agent will ask you to spell it. Say
  "A, B, one, two, C, D, E" — letters as words, the digits as digits.

## multi-skill

*Benchmark — one call across three skills*

Command:

```
python -m frontdesk.cli webcall --agent-id agent_e633333f5fae1f46ea35053778 --journey multi-skill --arch monolith
python -m frontdesk.cli webcall --agent-id agent_59e06efc2efc96e4030bf62518 --journey multi-skill --arch modular
```

What to say:

> You are Sarah Whitfield. Book an annual service for AB12CDE — spell it out: A, B, one, two, C, D, E. You want a morning appointment in the first week of October and you will wait on site. Confirm every detail clearly when asked. Once the booking is done, ask about a courtesy car for the day. Finally, if you are asked to rate the service, give it five out of five and say the team were great. Do not volunteer the rating before you are asked.

Target: 2–3 minutes. Call it twice per architecture.

## happy-control

*Booking — happy path, single vehicle match*

Command:

```
python -m frontdesk.cli webcall --agent-id agent_e633333f5fae1f46ea35053778 --journey happy-control --arch monolith
python -m frontdesk.cli webcall --agent-id agent_59e06efc2efc96e4030bf62518 --journey happy-control --arch modular
```

What to say:

> You are Sarah Whitfield, an existing customer. You want to book an annual service for your BMW. Your registration is AB12CDE — spell it out clearly as A, B, one, two, C, D, E. You are happy to wait on site. You want a morning appointment in the first week of October. When the agent reads details back, confirm them clearly. Accept the first morning slot offered.

Target: 2–3 minutes. Call it twice per architecture.
