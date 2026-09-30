"""Bluetooth LE transport, built on bleak.

Needs the ``bleak`` extra: ``pip install spinev-ble[bleak]``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol, cast, runtime_checkable

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import MAX_CONNECT_ATTEMPTS, establish_connection

from ..const import CHARACTERISTIC_UUID, DEFAULT_TIMEOUT
from ..exceptions import SpinEvConnectionError
from .base import DisconnectCallback, FrameCallback

_LOGGER = logging.getLogger(__name__)


@runtime_checkable
class BleakClientLike(Protocol):
    """The part of :class:`bleak.BleakClient` this transport uses.

    Any object with these members can stand in for the bleak client, which is
    what lets the charger be reached through something other than the local
    adapter. The characteristic and payload arguments are positional only, so
    an implementation is free to name them whatever suits it.
    """

    @property
    def is_connected(self) -> bool:
        """True while the link is up."""

    async def connect(self, **kwargs: Any) -> Any:
        """Open the link."""

    async def disconnect(self) -> Any:
        """Close the link."""

    async def start_notify(self, char_specifier: Any, callback: Any, /) -> Any:
        """Subscribe to notifications on a characteristic."""

    async def write_gatt_char(
        self, char_specifier: Any, data: Any, /, *, response: bool | None = None
    ) -> Any:
        """Write bytes to a characteristic."""


ClientFactory = Callable[..., BleakClientLike]
"""Builds the Bluetooth client. Connections go through
:func:`bleak_retry_connector.establish_connection`, which calls this with the
device positionally and passes ``disconnected_callback``, ``timeout``, ``pair``
and its own markers as keywords, so any replacement must accept ``**kwargs``.
:class:`bleak.BleakClient` and ``habluetooth.HaBleakClientWrapper`` both do."""


async def _async_close(client: BleakClientLike) -> None:
    """Drop a link, ignoring a failure to close one that is already gone."""
    try:
        await client.disconnect()
    except Exception as err:  # pylint: disable=broad-exception-caught
        _LOGGER.debug("error while disconnecting: %s", err)


class BleTransport:
    """Carry frames over the charger's Bluetooth link.

    Frames are written to :data:`~spinev_ble.const.CHARACTERISTIC_UUID` with
    response, and replies arrive as notifications on the same characteristic.

    The charger accepts a single Bluetooth client at a time. If the official
    phone app is connected, this will not be able to connect, and the reverse
    is also true.

    Usage::

        device = await BleakScanner.find_device_by_address("AA:BB:CC:DD:EE:FF")
        async with SpinEvCharger(BleTransport(device), password=0xABCDEF) as charger:
            ...
    """

    timeout_hint = (
        "Another Bluetooth client, such as the phone app, may be holding the "
        "connection."
    )
    """Likely cause of a request getting no reply over Bluetooth."""

    def __init__(
        self,
        device: BLEDevice,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        client_class: ClientFactory | None = None,
        max_attempts: int = MAX_CONNECT_ATTEMPTS,
    ) -> None:
        """Create a transport for ``device``.

        ``timeout`` bounds each connection attempt, in seconds.

        ``client_class`` builds the Bluetooth client. It defaults to
        :class:`bleak.BleakClient`, which uses the local adapter. Pass a
        different one to route elsewhere, for example
        ``habluetooth.HaBleakClientWrapper`` to reach the charger through an
        ESPHome Bluetooth proxy. Anything matching :class:`BleakClientLike`
        works.

        ``max_attempts`` caps the connection attempts made per
        :meth:`async_connect`. The charger takes one client at a time, so a
        caller that would rather hear straight away that the slot is taken,
        such as a setup form, can pass 1.
        """
        self._device = device
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._client_class: ClientFactory = client_class or BleakClient
        self._client: BleakClientLike | None = None
        self._on_frame: FrameCallback | None = None
        self._on_disconnect: DisconnectCallback | None = None

    @property
    def is_connected(self) -> bool:
        """True while the Bluetooth link is up."""
        return self._client is not None and self._client.is_connected

    async def async_connect(
        self, on_frame: FrameCallback, on_disconnect: DisconnectCallback
    ) -> None:
        """Connect and subscribe to notifications.

        :raises SpinEvConnectionError: if the link cannot be opened.
        """
        if self.is_connected:
            return
        self._on_frame = on_frame
        self._on_disconnect = on_disconnect
        try:
            # The factory is typed by what this library calls on it, which is
            # looser than the class establish_connection asks for.
            client = await establish_connection(
                cast(type[BleakClient], self._client_class),
                self._device,
                self._device.name or self._device.address,
                disconnected_callback=self._handle_disconnect,
                max_attempts=self._max_attempts,
                timeout=self._timeout,
            )
        except Exception as err:
            raise SpinEvConnectionError(f"could not connect: {err}") from err
        try:
            await client.start_notify(CHARACTERISTIC_UUID, self._handle_notify)
        except Exception as err:
            # The link is up but unusable, and the charger has only the one
            # slot, so it goes back before the failure is reported.
            await _async_close(client)
            raise SpinEvConnectionError(f"could not connect: {err}") from err
        self._client = client

    async def async_disconnect(self) -> None:
        """Drop the link. Safe to call when already disconnected.

        The client's ``on_disconnect`` is not called: it reports only drops
        the client did not ask for, and bleak signals a requested disconnect
        the same way it signals a lost link.
        """
        self._on_frame = None
        self._on_disconnect = None
        client, self._client = self._client, None
        if client is not None:
            await _async_close(client)

    async def async_send(self, frame: bytes) -> None:
        """Write ``frame`` to the charger, waiting for the write to be acked.

        :raises SpinEvConnectionError: if not connected, or the write fails.
        """
        client = self._client
        if client is None or not client.is_connected:
            raise SpinEvConnectionError("not connected")
        try:
            await client.write_gatt_char(CHARACTERISTIC_UUID, frame, response=True)
        except Exception as err:
            raise SpinEvConnectionError(f"write failed: {err}") from err

    def _handle_disconnect(self, _client: object) -> None:
        if self._on_disconnect is not None:
            self._on_disconnect()

    def _handle_notify(self, _sender: object, data: bytearray) -> None:
        if self._on_frame is not None:
            self._on_frame(bytes(data))
