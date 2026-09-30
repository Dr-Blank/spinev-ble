"""OCPP transport: register frames through the charger's central system.

This module is pure. It depends on no OCPP library: the caller supplies the
function that sends a ``DataTransfer``, using whatever central system it runs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ..const import BULK_REGISTERS
from ..exceptions import (
    SpinEvConnectionError,
    SpinEvError,
    SpinEvProtocolError,
    SpinEvTimeoutError,
    SpinEvUnsupportedError,
    SpinEvValueError,
)
from ..protocol import is_reply
from .base import DisconnectCallback, FrameCallback

VENDOR_ID = "CPV07"
"""``DataTransfer`` vendor id under which the charger accepts register frames
from its central system."""

MESSAGE_ID = "CC_CONFIG"
"""``DataTransfer`` message id for register frames. The request ``data`` is
the frame as hex, for example ``'10ac4f0141c00000'``. The reply ``data`` is
the charger's reply frame as hex bytes joined by underscores, for example
``'10_AC_4F_01_41_C0_00_00'``."""

DataTransferCall = Callable[[str, str, str], Awaitable[tuple[str, str | None]]]
"""Sends one OCPP 1.6 ``DataTransfer`` request to the charger and waits for
the answer.

Called with ``vendor_id``, ``message_id`` and ``data``, in that order. Returns
the reply's ``status`` and its ``data``, which is ``None`` when the reply has
none. Any exception it raises is reported as :class:`SpinEvConnectionError`,
unless it is already a :class:`SpinEvError`."""

_ACCEPTED = "Accepted"
"""``DataTransfer`` reply status for a request the charger acted on."""


def _parse_reply(data: str) -> bytes:
    """Turn a reply's ``data`` into the payload Bluetooth would have carried.

    Accepts the charger's underscore joined form and plain hex, in either
    case.

    :raises SpinEvProtocolError: if ``data`` is not hex.
    """
    try:
        return bytes.fromhex("".join(data.replace("_", "").split()))
    except ValueError as err:
        # The text is left out of the message: some registers carry
        # credentials.
        raise SpinEvProtocolError(
            f"DataTransfer reply of {len(data)} characters is not hex"
        ) from err


class OcppTunnelTransport:
    """Carry frames through the charger's OCPP connection.

    The charger accepts register frames from its OCPP 1.6 central system as a
    ``DataTransfer`` with vendor id :data:`VENDOR_ID` and message id
    :data:`MESSAGE_ID`, and answers
    with its reply frame. This transport turns each frame into one such
    request and hands the reply to the charger client, so every
    :class:`~spinev_ble.charger.SpinEvCharger` method works the same way it
    does over Bluetooth.

    The central system owns the connection, so this class opens none of its
    own. Pass it the central system's way of sending a ``DataTransfer`` to
    this charger, for example with the ``ocpp`` package::

        async def data_transfer(vendor_id: str, message_id: str, data: str):
            result = await charge_point.call(
                call.DataTransfer(vendor_id=vendor_id, message_id=message_id, data=data)
            )
            return result.status, result.data

        async with SpinEvCharger(OcppTunnelTransport(data_transfer)) as charger:
            ...

    The charger's JSON parser does not accept whitespace between tokens: a
    ``DataTransfer`` serialised with spaces after ``,`` and ``:`` is refused.
    Send compact JSON.

    Limits of the tunnel:

    - One request carries one reply, so the streamed history reads
      (:attr:`~spinev_ble.const.Register.HISTORY_SESSIONS` and
      :attr:`~spinev_ble.const.Register.HISTORY_EVENTS`) raise
      :class:`SpinEvUnsupportedError`. The central system sees each session
      as an OCPP transaction instead.
    - A commit restarts the charger, which drops the OCPP connection. The
      reply to the commit may be lost with it, in which case the commit
      raises :class:`SpinEvConnectionError` once :meth:`notify_disconnected`
      is called, or :class:`SpinEvTimeoutError` if the drop is never
      reported, although the charger acted on it.

    Call :meth:`notify_disconnected` when the charger's OCPP connection
    drops, so requests in flight fail at once instead of waiting out their
    timeout.

    The transport keeps no deadline of its own: the charger client's
    ``timeout`` bounds each round trip.
    """

    timeout_hint = (
        "The charger's OCPP connection may be down, or its OCPP stack may have "
        "stopped answering."
    )
    """Likely cause of a request getting no reply over the tunnel."""

    def __init__(self, data_transfer: DataTransferCall) -> None:
        """Create a transport around ``data_transfer``."""
        self._data_transfer = data_transfer
        self._on_frame: FrameCallback | None = None
        self._on_disconnect: DisconnectCallback | None = None

    @property
    def is_connected(self) -> bool:
        """True between :meth:`async_connect` and a disconnect."""
        return self._on_frame is not None

    async def async_connect(
        self, on_frame: FrameCallback, on_disconnect: DisconnectCallback
    ) -> None:
        """Start handing replies to ``on_frame``. Opens no connection."""
        self._on_frame = on_frame
        self._on_disconnect = on_disconnect

    async def async_disconnect(self) -> None:
        """Stop handing replies to the client. Closes no connection."""
        self._on_frame = None
        self._on_disconnect = None

    def notify_disconnected(self) -> None:
        """Report that the charger's OCPP connection has dropped.

        The charger client fails anything in flight and must connect again
        before its next request.
        """
        on_disconnect = self._on_disconnect
        self._on_frame = None
        self._on_disconnect = None
        if on_disconnect is not None:
            on_disconnect()

    async def async_send(self, frame: bytes) -> None:
        """Send ``frame`` as a ``DataTransfer`` and deliver the reply.

        :raises SpinEvConnectionError: if not connected, or if
            ``data_transfer`` fails.
        :raises SpinEvValueError: if ``frame`` is not a register frame.
        :raises SpinEvUnsupportedError: for a streamed history read.
        :raises SpinEvTimeoutError: if ``data_transfer`` raises
            :class:`TimeoutError`.
        :raises SpinEvProtocolError: if the charger refuses the request, or
            answers with data that is not text or not a register frame, which
            is how it answers a register it does not implement.
        """
        if self._on_frame is None:
            raise SpinEvConnectionError("not connected")
        if not is_reply(frame):
            raise SpinEvValueError(f"{len(frame)} bytes are not a register frame")
        register = frame[2]
        if register in BULK_REGISTERS:
            raise SpinEvUnsupportedError(
                f"register 0x{register:02X} streams records, which the OCPP "
                "tunnel cannot carry"
            )
        try:
            status, data = await self._data_transfer(VENDOR_ID, MESSAGE_ID, frame.hex())
        except TimeoutError as err:
            raise SpinEvTimeoutError("no DataTransfer reply from the charger") from err
        except SpinEvError:
            raise
        except Exception as err:
            raise SpinEvConnectionError(f"DataTransfer failed: {err}") from err
        if status != _ACCEPTED:
            raise SpinEvProtocolError(
                f"charger answered the DataTransfer with {status}"
            )
        if not data:
            # Not an error here: a transport cannot tell whether a reply was
            # due. A write the client sends without waiting needs none, and a
            # request that waits for one times out in the client.
            return
        if not isinstance(data, str):
            raise SpinEvProtocolError(
                f"DataTransfer reply data is {type(data).__name__}, expected text"
            )
        payload = _parse_reply(data)
        if not is_reply(payload):
            raise SpinEvProtocolError(
                f"charger answered register 0x{register:02X} with {len(payload)} "
                "bytes that are not a register frame. The register may not exist "
                "on this firmware."
            )
        # The client may have disconnected while the request was in flight.
        on_frame = self._on_frame
        if on_frame is not None:
            on_frame(payload)
