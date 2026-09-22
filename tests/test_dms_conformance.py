"""Conformance and configuration-drift tests for the DMS layer.

Two jobs:

1. **Conformance** — run `dms.conformance.run_conformance` against every
   registered adapter, so a new vendor is provably safe before a customer sees it.

2. **Drift** — assert that dealer configuration and adapter capability cannot
   silently disagree. This is the failure that hurts most at scale: a dealer
   profile that claims booking while the adapter cannot write produces an agent
   that promises appointments nobody can keep. It is invisible when testing one
   dealer and near-guaranteed when testing ten.
"""

from __future__ import annotations

import pytest

from frontdesk.dealer_profiles import all_dealers, dealer_dynamic_variables, get_dealer
from frontdesk.dms import (
    CAP_ASYNC_WRITES,
    CAP_AVAILABILITY_SEARCH,
    CAP_CREATE_APPOINTMENT,
    DmsAdapter,
    DmsCapabilityError,
    ExportOnlyDms,
    FortellisDms,
    KeyloopCredentials,
    KeyloopDms,
    ReadOnlyDms,
    ReferenceDms,
    build_adapter,
    known_vendors,
    run_conformance,
    summarise,
)

# Every adapter, including the two same-vendor/different-integration cases that
# prove capability is a property of the connection rather than the product.
ADAPTERS = [
    pytest.param(ReferenceDms, "northgate-uk", id="reference"),
    pytest.param(KeyloopDms, "northgate-uk", id="keyloop"),
    pytest.param(FortellisDms, "northgate-uk", id="fortellis-sync"),
    pytest.param(lambda d: FortellisDms(d, async_writes=True), "rheinmain-de",
                 id="fortellis-async"),
    pytest.param(ReadOnlyDms, "midlands-us", id="reynolds-read-only"),
    pytest.param(ExportOnlyDms, "rural-fr", id="export-only"),
]


@pytest.mark.parametrize("factory,dealer_id", ADAPTERS)
def test_adapter_satisfies_the_contract(factory, dealer_id: str) -> None:
    """Every adapter must pass the same suite. This is the gate a new DMS hits."""
    adapter = factory(dealer_id)
    passed = run_conformance(adapter, unique=f"conformance-{dealer_id}")
    assert passed, "conformance ran no checks — the adapter declares nothing?"


def test_adapter_protocol_is_structurally_satisfied() -> None:
    for factory, dealer_id in [(p.values[0], p.values[1]) for p in ADAPTERS]:
        adapter = factory(dealer_id)
        assert isinstance(adapter, DmsAdapter), f"{adapter.vendor} is not a DmsAdapter"


# --------------------------------------------------------------------------- #
# Adapter-specific behaviour worth asserting explicitly
# --------------------------------------------------------------------------- #

def test_ambiguous_plate_is_surfaced_never_picked() -> None:
    """Two vehicles on one plate is a real outcome; a guess here books the wrong
    car into the workshop."""
    for adapter in (ReferenceDms("northgate-uk"), KeyloopDms("northgate-uk"),
                    FortellisDms("northgate-uk"), ReadOnlyDms("midlands-us")):
        assert len(adapter.lookup_vehicle("AB12CDE")) == 2, adapter.vendor
        assert len(adapter.lookup_vehicle("XY19JKL")) == 1, adapter.vendor


def test_read_only_integration_refuses_writes_it_cannot_make() -> None:
    """A certified read-only partner is a supported configuration, not a broken
    one — and it must say so rather than appear to succeed."""
    adapter = ReadOnlyDms("midlands-us")
    assert CAP_CREATE_APPOINTMENT not in adapter.capabilities()
    with pytest.raises(DmsCapabilityError):
        adapter.create_appointment(
            vehicle_id="V-1001", customer_id="C-5001",
            service_type="oil_change", start="2026-10-07T08:30",
            duration_minutes=45, transport_option="none", notes="",
            idempotency_key="readonly-1",
        )


def test_export_only_refuses_availability_rather_than_returning_empty() -> None:
    """Empty means 'nothing free'. Refused means 'I cannot know'.

    The distinction is the entire reason capability refusal is a typed error
    rather than an empty result — and it holds even when the export is fresh,
    because the problem is the absence of a live diary, not stale data.
    """
    adapter = ExportOnlyDms("rural-fr")
    assert not adapter.snapshot.is_stale, "fixture models a normally-fresh export"

    with pytest.raises(DmsCapabilityError) as availability_error:
        adapter.check_availability("revision", "2026-10-01", "2026-10-31")
    assert "live" in str(availability_error.value).lower()

    # Booking is refused regardless of freshness: there is no write path at all,
    # so no amount of fresh data would make a promise keepable.
    with pytest.raises(DmsCapabilityError):
        adapter.create_appointment(
            vehicle_id="V-1001", customer_id="C-5001", service_type="revision",
            start="2026-10-07T08:30", duration_minutes=90,
            transport_option="none", notes="", idempotency_key="export-1",
        )


def test_async_write_reports_pending_not_success() -> None:
    """The CDK Async Service Appointment case.

    The provider returns 202 Accepted and the appointment does not exist yet. An
    adapter that reports success here is the exact mechanism by which an agent
    confirms a booking that was never made.
    """
    adapter = FortellisDms("rheinmain-de", async_writes=True)
    assert CAP_ASYNC_WRITES in adapter.capabilities()

    slots = adapter.check_availability("inspektion", "2026-10-01", "2026-10-31")
    assert slots, "need a slot to attempt a booking"

    from frontdesk.dms import DmsAsyncPendingError

    with pytest.raises(DmsAsyncPendingError) as excinfo:
        adapter.create_appointment(
            vehicle_id="V-1001", customer_id="C-5001",
            service_type="inspektion", start=slots[0].start,
            duration_minutes=90, transport_option="none", notes="",
            idempotency_key="async-1",
        )
    assert excinfo.value.job_id, "the flow needs a job handle to poll"
    assert not isinstance(excinfo.value, DmsCapabilityError)

    # And the adapter provides the vendor-specific way to resolve it.
    appointment = adapter.await_appointment(excinfo.value.job_id)
    assert appointment.appointment_id


def test_missing_partner_key_is_an_auth_error_not_a_silent_success() -> None:
    """Keyloop access is gated behind a partner key. A missing key must fail
    loudly and as the right class, because the flow's response differs."""
    from frontdesk.dms import DmsAuthError

    adapter = KeyloopDms("northgate-uk", credentials=KeyloopCredentials(partner_key=""))
    with pytest.raises(DmsAuthError):
        adapter.lookup_vehicle("AB12CDE")


# --------------------------------------------------------------------------- #
# Configuration drift — the tests that protect the scaling claim
# --------------------------------------------------------------------------- #

def test_every_dealer_names_a_registered_vendor() -> None:
    """A typo in a profile should fail CI, not a customer call."""
    for dealer in all_dealers():
        assert dealer.dms_vendor in known_vendors(), (
            f"{dealer.dealer_id} names vendor {dealer.dms_vendor!r}, "
            f"which is not registered in the DMS registry"
        )


def test_every_dealer_resolves_to_an_adapter() -> None:
    for dealer in all_dealers():
        adapter = build_adapter(dealer.dealer_id, dealer.dms_vendor)
        assert isinstance(adapter, DmsAdapter)


def test_profile_capabilities_are_a_subset_of_adapter_capabilities() -> None:
    """The drift check.

    A profile must never claim a capability its adapter cannot deliver. If it
    does, the flow offers the caller a feature that fails at the last moment —
    the worst possible time to discover a configuration mistake.
    """
    for dealer in all_dealers():
        adapter = build_adapter(dealer.dealer_id, dealer.dms_vendor)
        claimed = dealer.capabilities
        delivered = adapter.capabilities()
        missing = claimed - delivered
        assert not missing, (
            f"{dealer.dealer_id} claims {sorted(missing)} but its "
            f"{adapter.vendor} adapter does not provide it"
        )


def test_flow_gates_agree_with_capabilities() -> None:
    """The dynamic variables the flow branches on must match reality.

    These are the values that decide whether the caller is asked for a booking
    or routed to a callback. If `dealer_can_book` says 'true' while the adapter
    cannot write, the bug is invisible until a customer is lied to.
    """
    for dealer in all_dealers():
        adapter = build_adapter(dealer.dealer_id, dealer.dms_vendor)
        variables = dealer_dynamic_variables(dealer)
        caps = adapter.capabilities()

        assert variables["dealer_can_book"] == (
            "true" if CAP_CREATE_APPOINTMENT in caps else "false"
        ), dealer.dealer_id
        assert variables["dealer_can_check_availability"] == (
            "true" if CAP_AVAILABILITY_SEARCH in caps else "false"
        ), dealer.dealer_id
        assert variables["dealer_async_writes"] == (
            "true" if CAP_ASYNC_WRITES in caps else "false"
        ), dealer.dealer_id


def test_every_dealer_has_a_honest_integration_note() -> None:
    """The note is what an operator reads before promising a dealer a feature.
    An empty one is how 'we assumed it worked like the last one' happens."""
    for dealer in all_dealers():
        assert dealer.integration_note.strip(), (
            f"{dealer.dealer_id} has no integration note"
        )


def test_capability_matrix_renders_for_every_dealer() -> None:
    adapters = [build_adapter(d.dealer_id, d.dms_vendor) for d in all_dealers()]
    matrix = summarise(adapters)
    assert "yes" in matrix
    for adapter in adapters:
        assert adapter.vendor in matrix


def test_same_vendor_can_have_different_capabilities() -> None:
    """The central claim: capability is per-connection, not per-product.

    Keyloop's own documentation says endpoint availability varies between the
    DMSs behind their Open Platform. Two dealers, one vendor, different feature
    sets — and the flow must handle both without a fork.
    """
    sync = FortellisDms("northgate-uk", async_writes=False)
    async_ = FortellisDms("rheinmain-de", async_writes=True)
    assert sync.vendor == async_.vendor
    assert sync.capabilities() != async_.capabilities()


def test_export_only_dealer_is_never_told_it_can_book() -> None:
    """The long-tail dealer. If this ever flips to true, the agent starts
    promising bookings into a system with no write path at all."""
    dealer = get_dealer("rural-fr")
    variables = dealer_dynamic_variables(dealer)
    assert variables["dealer_can_book"] == "false"
    assert variables["dealer_can_check_availability"] == "false"
