"""Frontdesk — reference implementation of the redesigned voice-agent platform.

This package is a small, runnable implementation of the redesign:

    one monolithic flow, ~190 nodes, 14k-token system prompt, long-call
    hallucination, and a mis-booked car caused by a misheard license plate.

The design it encodes:

    a probabilistic conversation  wrapped around  a deterministic transaction

Layout:
    identifiers.py     deterministic validation of high-stakes fields (plates, VIN, ...)
    dealer_profiles.py dealer/market configuration as *data* (config over code)
    dms/               canonical domain model + adapters, one contract per vendor
    tools.py           Retell tool definitions that expose the deterministic layer
    flow.py            the modular conversation flow (router + skills + guardrails)
    retell_client.py   thin Retell REST client
    scenarios.py       eval scenarios for the failure classes above
    cli.py             deploy / call / simulate / cleanup
"""

__version__ = "0.1.0"
