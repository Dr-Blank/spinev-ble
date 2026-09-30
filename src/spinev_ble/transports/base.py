"""The interface every transport implements.

This module is pure. It imports nothing optional and does no I/O.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

FrameCallback = Callable[[bytes], None]
"""Receives each payload the charger sends, exactly as it arrived."""

DisconnectCallback = Callable[[], None]
"""Called when the link drops without the charger client asking for it."""


@runtime_checkable
class SpinEvTransport(Protocol):
    """Carries raw frames between :class:`~spinev_ble.charger.SpinEvCharger`
    and a charger.

    A transport moves bytes and nothing else. The charger client builds
    requests, matches replies and decodes values, so the same client works
    over any transport. The client serialises its requests, so a transport
    never has two :meth:`async_send` calls in flight at once.

    A transport may also define a ``timeout_hint`` string: the likely cause
    of a request getting no reply over that link. The client appends it to
    :class:`~spinev_ble.exceptions.SpinEvTimeoutError`. It is optional and
    not part of the checked protocol.
    """

    @property
    def is_connected(self) -> bool:
        """True while frames can be sent."""

    async def async_connect(
        self, on_frame: FrameCallback, on_disconnect: DisconnectCallback
    ) -> None:
        """Open the link.

        From then on, every payload the charger sends goes to ``on_frame``,
        and ``on_disconnect`` is called if the link drops on its own.
        Connecting an already connected transport does nothing.
        """

    async def async_disconnect(self) -> None:
        """Close the link. Safe to call when it is already closed."""

    async def async_send(self, frame: bytes) -> None:
        """Deliver one frame to the charger.

        Returns once the frame is delivered. Replies arrive through
        ``on_frame``, before or after this returns.
        """
