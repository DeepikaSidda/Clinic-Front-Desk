"""In-process pub/sub channel for the Dashboard BFF (task 12.1, Req 9.6, 14.5,
14.8, 15.4).

The design's real-time section says: *the Data_Layer emits a ``ChangeEvent`` on
every successful mutation; the BFF fans these out to connected dashboard clients
over a WebSocket/SSE channel.* This module models that channel as a small,
transport-agnostic **publish/subscribe** abstraction so the fan-out can be
exercised without a real socket:

- :class:`DashboardChannel` is the hub. It *is* a
  :class:`~clinic_front_desk.data_layer.events.ChangeEmitter` — the seven stores
  are constructed with it, so every successful mutation calls
  :meth:`DashboardChannel.emit`, which immediately broadcasts to every connected
  client.
- :meth:`DashboardChannel.subscribe` / :meth:`DashboardChannel.unsubscribe`
  register and remove raw callbacks; :meth:`DashboardChannel.connect` wraps that
  in a :class:`DashboardConnection` that models one connected client and records
  the events it receives (what a WebSocket/SSE session would push to the
  browser).

**Propagation budget (Req 9.6, 14.5, 14.8, 15.4).** Fan-out is *synchronous and
immediate*: :meth:`emit` delivers to all subscribers before returning, on the
same thread that performed the mutation. There is no queue, no polling, and no
scheduled flush, so the wall-clock delay between a successful store mutation and
a connected client observing the ``ChangeEvent`` is bounded by the in-process
call itself — comfortably inside the decision-add ≤ 5 s, decision-removal ≤ 2 s,
schedule/activity ≤ 5 s, and escalation ≤ 5 s budgets.

**Delivery isolation (Req 16.6 emitter contract).** A subscriber callback that
raises must never turn a store's successful mutation into a failure. Each
delivery is therefore isolated: an exception from one subscriber is swallowed
(surfaced via :attr:`DashboardChannel.last_delivery_errors`) and does not stop
delivery to the remaining subscribers, and :meth:`emit` never propagates it back
to the store.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from clinic_front_desk.data_layer.events import ChangeEvent

# A subscriber is any callable that accepts a ChangeEvent. A real deployment
# would push the event onto a WebSocket/SSE connection; tests use a recording
# callback (see DashboardConnection).
Subscriber = Callable[[ChangeEvent], None]


class Subscription:
    """A handle to one registered subscriber, used to unsubscribe.

    Returned by :meth:`DashboardChannel.subscribe`. Calling
    :meth:`unsubscribe` (or the channel's ``unsubscribe``) detaches the callback
    so it receives no further events. Unsubscribing more than once is a no-op.
    """

    __slots__ = ("_channel", "_callback", "active")

    def __init__(self, channel: DashboardChannel, callback: Subscriber) -> None:
        self._channel = channel
        self._callback = callback
        self.active = True

    def unsubscribe(self) -> None:
        """Detach this subscriber; safe to call repeatedly."""
        self._channel.unsubscribe(self)


@dataclass
class DashboardConnection:
    """A connected dashboard client that records the events it receives.

    Models a single WebSocket/SSE session: while connected it accumulates every
    :class:`ChangeEvent` the channel broadcasts, in arrival order, which a real
    client would render as live updates. :meth:`close` unsubscribes it.
    """

    subscription: Subscription = field(init=False)
    events: list[ChangeEvent] = field(default_factory=list)

    def _receive(self, event: ChangeEvent) -> None:
        self.events.append(event)

    def close(self) -> None:
        """Disconnect the client from the channel."""
        self.subscription.unsubscribe()


class DashboardChannel:
    """In-process pub/sub hub that fans ``ChangeEvent``\\ s out to clients.

    Implements the :class:`~clinic_front_desk.data_layer.events.ChangeEmitter`
    protocol, so the Data_Layer stores can be constructed with it directly. On
    every successful mutation the store calls :meth:`emit`, which synchronously
    broadcasts the event to all connected clients.
    """

    def __init__(self) -> None:
        self._subscriptions: list[Subscription] = []
        # Errors raised by subscriber callbacks during the most recent broadcast,
        # retained for observability; never re-raised into the mutating store.
        self.last_delivery_errors: list[Exception] = []

    # -- subscription management ------------------------------------------

    def subscribe(self, callback: Subscriber) -> Subscription:
        """Register ``callback`` to receive every future broadcast event."""
        subscription = Subscription(self, callback)
        self._subscriptions.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """Remove a subscription; safe to call for an already-removed one."""
        subscription.active = False
        try:
            self._subscriptions.remove(subscription)
        except ValueError:
            pass

    def connect(self) -> DashboardConnection:
        """Attach a new :class:`DashboardConnection` and return it.

        Convenience over :meth:`subscribe` that models a connected dashboard
        client which records received events.
        """
        connection = DashboardConnection()
        connection.subscription = self.subscribe(connection._receive)
        return connection

    @property
    def subscriber_count(self) -> int:
        """Number of currently connected subscribers."""
        return len(self._subscriptions)

    # -- fan-out ----------------------------------------------------------

    def broadcast(self, event: ChangeEvent) -> None:
        """Deliver ``event`` to every current subscriber, synchronously.

        Delivery is isolated per subscriber: a callback that raises is recorded
        in :attr:`last_delivery_errors` and skipped, so one faulty client cannot
        block delivery to the others or fail the caller.
        """
        self.last_delivery_errors = []
        # Iterate a snapshot so a subscriber that unsubscribes (or a new
        # subscription added) during delivery does not disturb this broadcast.
        for subscription in list(self._subscriptions):
            if not subscription.active:
                continue
            try:
                subscription._callback(event)
            except Exception as exc:  # noqa: BLE001 - isolate faulty subscribers
                self.last_delivery_errors.append(exc)

    def emit(self, event: ChangeEvent) -> None:
        """:class:`ChangeEmitter` hook: broadcast a successful-mutation event.

        Invoked by the stores after a mutation commits (Req 16.6). Fan-out is
        immediate and synchronous, keeping propagation inside the dashboard
        budgets (Req 9.6, 14.5, 14.8, 15.4). Never raises for a well-formed
        event.
        """
        self.broadcast(event)


__all__ = [
    "Subscriber",
    "Subscription",
    "DashboardConnection",
    "DashboardChannel",
]
