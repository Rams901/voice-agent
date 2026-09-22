"""Dealer/market configuration as *data*.

Scaling from one dealer to many is a requirement, not an afterthought. The failure mode
to avoid is "one flow per dealer", because a bug then has to be fixed in ten
places. So everything dealer-specific lives here as configuration, and the flow
is built once and shared.

Three dimensions are kept separate on purpose, because they are not the same
thing:

    dealer       -- a commercial location (hours, tone, phone number, brands)
    dms          -- the system of record (what product it is)
    integration  -- what *this dealer's* connection to that product can do

The third is the one teams miss. Keyloop's own Open Platform documentation warns
that "not all endpoints ... are available or relevant for every dealer management
system", and Reynolds partners ship read-only connections. So capability is a
property of the connection, not of the product — which means it belongs here, per
dealer, and not in an adapter's assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .dms.model import (
    CAP_ASYNC_WRITES, CAP_AVAILABILITY_SEARCH, CAP_CANCEL_APPOINTMENT,
    CAP_CREATE_APPOINTMENT, CAP_READ_APPOINTMENT, CAP_READ_CUSTOMER,
    CAP_READ_PRICING, CAP_READ_REPAIR_ORDER, CAP_READ_SERVICE_HISTORY,
    CAP_READ_TRANSPORT, CAP_READ_VEHICLE,
    FULL_FIXED_OPS, READ_ONLY,
)


@dataclass(frozen=True)
class DealerProfile:
    dealer_id: str
    display_name: str
    country: str
    language: str
    timezone: str
    #: Vendor key, resolved through `dms.registry` to an adapter.
    dms_vendor: str
    currency: str
    phone_number: str
    services: list[str]
    opening_hours: dict[str, str]
    #: Overrides the built-in country masks when a market has odd formats.
    plate_masks: list[str] | None = None
    escalation_target: str = "+441134960000"
    tone: str = "warm, concise, British English"
    #: Whether a loan/courtesy car can be offered at all.
    offers_loan_car: bool = True
    #: Consent rules differ per market (e.g. recording consent, GDPR).
    requires_recording_consent: bool = True
    max_identifier_attempts: int = 3
    #: What this dealer's connection can do. Defaults to the vendor's full set;
    #: override where the real connection is narrower.
    capabilities: frozenset[str] = field(default_factory=lambda: FULL_FIXED_OPS)
    #: Skills this market offers, after capability. Opt-out, not opt-in: every
    #: skill defaults on, so a market declares only what it lacks. This is the
    #: switch an operator flips to turn a skill off for one market without
    #: touching the flow — see `feature_flags` for how it combines with the
    #: capability matrix.
    features: frozenset[str] = field(default_factory=lambda: ALL_FEATURES)
    #: Free-text note shown in `cli plan`, so an operator can see *why* a dealer
    #: behaves differently without reading code.
    integration_note: str = ""
    extra: dict[str, str] = field(default_factory=dict)


#: The skills and sub-skills a market can switch off. Naming them here rather
#: than in the flow is the point: turning a skill off for a market is a change to
#: one data file, not to the graph.
ALL_FEATURES: frozenset[str] = frozenset({"booking", "rental", "billing", "rating"})

#: Human-readable labels, so `cli plan` can explain a disabled skill without the
#: reader having to trace a variable back to a node.
FEATURE_LABELS: dict[str, str] = {
    "booking": "service booking",
    "rental": "hire car",
    "billing": "invoice and payment questions",
    "rating": "end-of-call rating question",
}

#: Dynamic variable name per skill, read by the orchestrator's gates.
FEATURE_VARIABLES: dict[str, str] = {
    "booking": "feature_booking",
    "rental": "feature_rental",
    "billing": "feature_billing",
    "rating": "feature_rating",
}


def feature_flags(dealer: DealerProfile) -> dict[str, bool]:
    """The on/off switches the flow's gates read.

    Two sources, combined here rather than in the graph: what this connection
    *can* do (the capability matrix, loan cars) and what this market *chooses* to
    offer. Both are real reasons to turn a skill off, and the flow should not
    have to know which applies — it gets one variable per skill either way.

    Keeping the combination in one function is what makes the switch trustworthy:
    there is exactly one place where "off" is decided, so a gate can never
    disagree with what the plan output claims.
    """
    return {
        # Booking needs both: a writable appointment API *and* the market wanting
        # the skill. Note `CAP_CREATE_APPOINTMENT`, not `dealer_can_book` — the
        # variable is derived from the capability here, as it always was.
        "booking": (CAP_CREATE_APPOINTMENT in dealer.capabilities
                    and "booking" in dealer.features),
        # A loan car needs a fleet to exist, the DMS to expose transport options,
        # *and* the market to want it. The capability half is not optional: this
        # dealer's profile note says the flow never offers a loan car because the
        # connection does not expose booking options, and before this flag the
        # note was true in prose and false in the graph — nothing read the
        # capability, so `offers_loan_car` alone would have offered one anyway.
        "rental": (dealer.offers_loan_car
                   and CAP_READ_TRANSPORT in dealer.capabilities
                   and "rental" in dealer.features),
        "billing": "billing" in dealer.features,
        "rating": "rating" in dealer.features,
    }


def disabled_features(dealer: DealerProfile) -> list[str]:
    """Labels of skills switched off for this market, for operator output."""
    return [FEATURE_LABELS[name] for name, on in feature_flags(dealer).items() if not on]


def _hours(weekday: str = "08:30–18:00", saturday: str = "09:00–13:00") -> dict[str, str]:
    return {
        "monday": weekday, "tuesday": weekday, "wednesday": weekday,
        "thursday": weekday, "friday": weekday,
        "saturday": saturday, "sunday": "closed",
    }


DEALERS: dict[str, DealerProfile] = {
    "northgate-uk": DealerProfile(
        dealer_id="northgate-uk",
        display_name="Northgate Motors",
        country="GB",
        language="en-GB",
        timezone="Europe/London",
        dms_vendor="keyloop",
        currency="GBP",
        # Note what is *absent*: warranty claim write-back. That is a separate
        # Keyloop product, not part of Service Appointment Booking, so the
        # profile must not claim it. The drift test caught this claim when it
        # was there — which is the entire reason that test exists.
        capabilities=FULL_FIXED_OPS,
        phone_number="+441134960100",
        services=["annual_service", "mot", "diagnostic", "brakes", "tyres", "aircon"],
        opening_hours=_hours(),
        escalation_target="+441134960000",
        tone="warm, concise, British English",
        integration_note="Keyloop SAB v2 with full read/write; partner key issued.",
    ),
    "veldhoven-nl": DealerProfile(
        dealer_id="veldhoven-nl",
        display_name="Veldhoven Auto Groep",
        country="NL",
        language="nl-NL",
        timezone="Europe/Amsterdam",
        dms_vendor="keyloop",
        currency="EUR",
        phone_number="+31401234567",
        services=["onderhoudsbeurt", "apk", "diagnose", "remmen", "banden", "airco"],
        opening_hours=_hours("08:00–17:30", "09:00–12:00"),
        escalation_target="+31401234500",
        tone="direct, friendly, Dutch",
        requires_recording_consent=False,
        # Same platform as Northgate, narrower connection: this DMS behind the
        # Open Platform does not expose transport options, so the flow never
        # offers a loan car here. Keyloop's own documentation warns that endpoint
        # availability varies per DMS — this is that warning made concrete.
        capabilities=FULL_FIXED_OPS - {CAP_READ_TRANSPORT},
        # Opted out of the end-of-call rating. A market choice, not a capability
        # one, which is why it lives here and not in the capability matrix — the
        # same distinction the two switches are meant to keep visible.
        features=ALL_FEATURES - {"rating"},
        integration_note="Same Keyloop Open Platform as Northgate, but this DMS "
                          "does not expose booking options, so no loan car can be "
                          "offered. Capability differs per dealer, not per vendor.",
    ),
    "rheinmain-de": DealerProfile(
        dealer_id="rheinmain-de",
        display_name="Rhein-Main Automobile",
        country="DE",
        language="de-DE",
        timezone="Europe/Berlin",
        # CDK Drive reached through Fortellis, and configured for asynchronous
        # writes. Two capability consequences the flow must handle: the write is
        # not immediate, and there is no cancel endpoint on this subscription.
        dms_vendor="fortellis-async",
        currency="EUR",
        capabilities=frozenset({
            CAP_AVAILABILITY_SEARCH, CAP_CREATE_APPOINTMENT, CAP_READ_APPOINTMENT,
            CAP_READ_VEHICLE, CAP_READ_CUSTOMER, CAP_READ_TRANSPORT,
            CAP_READ_PRICING, CAP_READ_SERVICE_HISTORY, CAP_ASYNC_WRITES,
        }),
        phone_number="+496912345678",
        services=["inspektion", "hu_au", "diagnose", "bremsen", "reifen", "klimaanlage"],
        opening_hours=_hours("07:30–17:00", "09:00–13:00"),
        escalation_target="+496912345600",
        tone="precise, courteous, German",
        offers_loan_car=False,
        integration_note="CDK Drive via Fortellis. Async Service Appointment API: "
                          "writes return 202 Accepted and must be polled. No cancel.",
    ),
    "midlands-us": DealerProfile(
        dealer_id="midlands-us",
        display_name="Midlands Chevrolet",
        country="US",
        language="en-US",
        timezone="America/Chicago",
        dms_vendor="reynolds-rci",
        currency="USD",
        capabilities=READ_ONLY,
        phone_number="+15125550100",
        services=["oil_change", "tire_rotation", "diagnostic", "brakes",
                  "state_inspection", "recall"],
        opening_hours=_hours("07:00–18:00", "08:00–14:00"),
        escalation_target="+15125550100",
        tone="friendly, efficient, American English",
        # Reynolds hands out access through a certification programme, not a
        # self-serve key, and most certified partners are read-only: they read
        # repair orders and appointments but push nothing back.
        integration_note="Reynolds RCI certified, read-only. Appointments are read "
                          "from the DMS, not written to it — so the agent captures "
                          "and the service team commits.",
    ),
    "rural-fr": DealerProfile(
        dealer_id="rural-fr",
        display_name="Garage du Bocage",
        country="FR",
        language="fr-FR",
        timezone="Europe/Paris",
        # The long tail: no API at all, data by scheduled export.
        dms_vendor="export-only",
        currency="EUR",
        capabilities=frozenset({
            CAP_READ_VEHICLE, CAP_READ_CUSTOMER, CAP_READ_REPAIR_ORDER,
            CAP_READ_SERVICE_HISTORY,
        }),
        phone_number="+33212345678",
        services=["revision", "controle_technique", "diagnostic", "freins",
                  "pneus", "climatisation"],
        opening_hours=_hours("08:00–12:00, 14:00–18:00", "closed"),
        escalation_target="+33212345600",
        tone="courteous, patient, French",
        requires_recording_consent=True,
        # No API and a nightly export: there is no live invoice position, so
        # billing questions cannot be answered from this connection and the
        # skill is switched off rather than answered by guesswork. Booking is
        # already off for the same reason, via the capability matrix.
        features=frozenset({"rating"}),
        max_identifier_attempts=4,
        integration_note="No API. Data arrives by nightly CSV export, so there is "
                          "no live diary. Booking is impossible; capture and callback.",
    ),
}


def get_dealer(dealer_id: str) -> DealerProfile:
    try:
        return DEALERS[dealer_id]
    except KeyError:
        known = ", ".join(sorted(DEALERS))
        raise SystemExit(f"Unknown DEALER_ID {dealer_id!r}. Known dealers: {known}") from None


def all_dealers() -> list[DealerProfile]:
    return [DEALERS[key] for key in sorted(DEALERS)]


def dealer_dynamic_variables(dealer: DealerProfile) -> dict[str, str]:
    """Flatten a profile into the dynamic variables the flow references.

    These are injected at deploy time (agent-level defaults) and can be
    overridden per call, which is how one flow serves every dealer.
    """
    return {
        "dealer_id": dealer.dealer_id,
        "dealer_name": dealer.display_name,
        "country": dealer.country,
        "language": dealer.language,
        "timezone": dealer.timezone,
        # Retell's built-in `{{current_time}}` / `{{current_calendar}}` /
        # `{{current_hour}}` resolve in `system_timezone`, and that defaults to
        # America/Los_Angeles when unset. For a UK dealership that is not a
        # cosmetic difference: "next Tuesday" computed in California can land on
        # the wrong day, and the agent would then check the diary for a date the
        # caller never asked for. This is why the dealer's own timezone is seeded
        # under Retell's reserved name rather than only our own `timezone`.
        "system_timezone": dealer.timezone,
        "dms_vendor": dealer.dms_vendor,
        "currency": dealer.currency,
        "dealer_phone": dealer.phone_number,
        "escalation_number": dealer.escalation_target,
        "services_list": ", ".join(dealer.services),
        "opening_hours": "; ".join(f"{d} {h}" for d, h in dealer.opening_hours.items()),
        "tone": dealer.tone,
        "offers_loan_car": "true" if dealer.offers_loan_car else "false",
        # Whether the *flow* asks for recording consent. A market difference
        # expressed as data, read by the front-door branch, so every dealer
        # still shares one node graph.
        "requires_recording_consent": (
            "true" if dealer.requires_recording_consent else "false"
        ),
        "max_identifier_attempts": str(dealer.max_identifier_attempts),
        # The capability matrix, exposed to the flow as data. Branch nodes read
        # these *before* a skill runs, so a dealer whose integration is narrower
        # never wastes the caller's time collecting details it cannot commit.
        "dealer_can_book": "true" if CAP_CREATE_APPOINTMENT in dealer.capabilities else "false",        "dealer_can_cancel": "true" if CAP_CANCEL_APPOINTMENT in dealer.capabilities else "false",
        "dealer_can_check_availability": (
            "true" if CAP_AVAILABILITY_SEARCH in dealer.capabilities else "false"
        ),
        "dealer_async_writes": (
            "true" if CAP_ASYNC_WRITES in dealer.capabilities else "false"
        ),
        # The skill switches, as data. The orchestrator's gates read these before
        # a skill runs, so a market that does not offer something never reaches
        # the nodes for it. Emitted as "true"/"false" strings because a dynamic
        # variable is a string, and an equation over an undefined variable is a
        # silent routing bug rather than an error.
        **{
            FEATURE_VARIABLES[name]: ("true" if on else "false")
            for name, on in feature_flags(dealer).items()
        },
        # Defaults for tool-populated gate variables. Every equation edge below
        # reads one of these, and an equation over an undefined variable is a
        # silent routing bug — so they are all defined up front and overwritten
        # as tools report back.
        "identifier_valid": "false",
        "identifier_confidence": "low",
        "vehicle_found": "false",
        "vehicle_ambiguous": "false",
        "slots_available": "0",
        "booking_confirmed": "false",
        "booking_pending": "false",
        "write_verified": "false",
    }
