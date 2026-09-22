"""The adapter contract.

Everything above this line is canonical; everything below is vendor-specific.
That boundary is the entire scaling argument: **a new DMS is a new class
implementing this protocol, plus a row of dealer config.**

The contract is deliberately narrow (nine methods), because the cost of adding a
DMS is the cost of implementing everything you declare. A wide interface means
every new integration is a small project; a narrow one that degrades honestly
means most dealers can go live against a subset.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .model import (
    Appointment, Customer, Slot, Vehicle,
)


@runtime_checkable
class DmsAdapter(Protocol):
    """What every DMS integration must satisfy.

    Implementations may declare fewer capabilities than this interface exposes.
    The rule is: declare what you can actually do, and raise
    `DmsCapabilityError` for what you cannot. Never silently no-op, and never
    return an empty list where the honest answer is "I can't tell you".
    """

    #: Which brand of DMS this talks to. Used for logs and support.
    vendor: str

    #: Human-readable integration name, e.g. "Keyloop SAB v2".
    integration: str

    def capabilities(self) -> frozenset[str]:
        """What this *dealer's* integration can do.

        Note per-dealer, not per-vendor: Keyloop's own docs warn that endpoint
        availability varies between the DMSs behind their Open Platform, and
        Reynolds partners ship read-only integrations. So this is a property of
        the connection, not of the product.
        """
        ...

    # -- reads ------------------------------------------------------------- #
    def lookup_vehicle(self, plate: str) -> list[Vehicle]:
        """Vehicles matching a plate. Returns a list because a plate can be
        transferred: more than one match is a real outcome, and the caller must
        be asked rather than a guess being made."""
        ...

    def lookup_customer(self, phone: str) -> Customer | None: ...

    def check_availability(self, service_type: str, date_from: str,
                           date_to: str) -> list[Slot]: ...

    def get_appointment(self, appointment_id: str) -> Appointment | None:
        """Read an appointment back. This is the method that makes
        verify-the-write possible, and it is the one most worth insisting on."""
        ...

    # -- writes ------------------------------------------------------------ #
    def create_appointment(self, *, vehicle_id: str, customer_id: str,
                           service_type: str, start: str,
                           duration_minutes: int, transport_option: str,
                           notes: str, idempotency_key: str) -> Appointment:
        """Commit a booking.

        Contract requirements an implementation must honour:

        1. **Idempotent** on `idempotency_key`. A retry returns the original
           appointment, never a duplicate.
        2. **Re-check availability at commit time.** A slot offered a minute ago
           may be gone.
        3. Raise `DmsAsyncPendingError` if the write is accepted but not yet
           committed, rather than returning a fake success.

        Requirement 3 is the one teams skip, and it is the one that produces
        confident agents telling callers about appointments that don't exist.
        """
        ...

    def cancel_appointment(self, appointment_id: str) -> Appointment: ...


class DeclaredCapabilities:
    """Mixin for adapters whose capability set can be narrowed by dealer config.

    Two different questions get conflated easily, and keeping them separate is
    the reason this exists:

    * **What can this product do?** — a property of the vendor and the APIs the
      dealer subscribes to. Declared by the adapter.
    * **What is this dealer configured for?** — phased rollout, contractual
      scope, a feature the dealer does not want. Declared by the profile.

    The second can only ever remove capabilities. An adapter is never widened
    past what the vendor supports, which is why the operation is an intersection
    and not an assignment.
    """

    _declared: frozenset[str]

    def restrict_to(self, caps: frozenset[str]) -> None:
        self._declared = self._declared & frozenset(caps)


