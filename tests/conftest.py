"""Shared fixtures.

The fake transport below stands in for a real Bluetooth link so the client can
be tested without hardware. It implements only the members the client uses,
which is what :class:`spinev_ble.client.BleakClientLike` describes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
from bleak.backends.device import BLEDevice

from spinev_ble.const import FRAME_HEADER, Operation, Register

#: Stand-in for a scanned device. The client only ever hands it to the
#: transport, so nothing about it needs to be real.
FAKE_DEVICE = cast(BLEDevice, object())


def reply(register: int, value: bytes, flag: int = Operation.READ) -> bytes:
    """Build a reply frame the way a charger would."""
    return FRAME_HEADER + bytes([register, flag]) + value


def string_reply(register: int, value: str, padding: int = 4) -> bytes:
    """Build a text register reply, zero padded like the charger's."""
    return reply(register, value.encode("ascii") + b"\x00" * padding)


class FakeTransport:
    """A scripted stand-in for :class:`bleak.BleakClient`.

    ``replies`` maps a register id to the payload the charger answers with. A
    register with no entry never answers, which is how timeouts are exercised.
    ``bulk`` is streamed in response to any bulk read.
    """

    def __init__(
        self,
        device: object,
        *,
        timeout: float = 5.0,
        disconnected_callback: Callable[[object], None] | None = None,
    ) -> None:
        self.device = device
        self.timeout = timeout
        self.disconnected_callback = disconnected_callback
        self.is_connected = False
        self.writes: list[bytes] = []
        self.notify_callback: Callable[[object, bytearray], None] | None = None
        self.connect_calls = 0
        self.disconnect_calls = 0
        #: Filled in by tests before use.
        self.replies: dict[int, bytes] = {}
        self.bulk: list[bytes] = []
        #: Set to drop the link instead of answering the next write.
        self.drop_on_write = False

    async def connect(self, **_kwargs: Any) -> None:
        self.connect_calls += 1
        self.is_connected = True

    async def disconnect(self, **_kwargs: Any) -> None:
        self.disconnect_calls += 1
        self.is_connected = False

    async def start_notify(
        self, _char: object, callback: Callable[[object, bytearray], None], **_kw: Any
    ) -> None:
        self.notify_callback = callback

    async def write_gatt_char(
        self, _char: object, data: Any, response: bool | None = None
    ) -> None:
        assert response is True, "the charger needs acknowledged writes"
        frame = bytes(data)
        self.writes.append(frame)
        if self.drop_on_write:
            self.is_connected = False
            if self.disconnected_callback is not None:
                self.disconnected_callback(self)
            return
        self._answer(frame)

    def _answer(self, frame: bytes) -> None:
        """Push whatever the scripted charger would send back."""
        register = frame[2]
        if self.bulk and frame[3] == Operation.READ and register in _BULK_REGISTERS:
            for record in self.bulk:
                self.notify(record)
            return
        payload = self.replies.get(register)
        if payload is not None:
            self.notify(payload)
            return
        if register in _ECHO_REGISTERS:
            self.notify(frame)

    def notify(self, payload: bytes) -> None:
        """Deliver a notification exactly as bleak would."""
        assert self.notify_callback is not None, "not subscribed"
        self.notify_callback(self, bytearray(payload))


#: Registers whose reads stream records rather than answering with one frame.
_BULK_REGISTERS = frozenset({0x68, 0x70})

#: Registers a charger answers by echoing the written frame back unchanged.
#: A test that wants a refusal scripts an entry in ``replies`` instead.
_ECHO_REGISTERS = frozenset({Register.CONTROL})


@pytest.fixture
def transport() -> FakeTransport:
    """The transport instance the client under test will be given."""
    return FakeTransport(FAKE_DEVICE)


@pytest.fixture
def client_class(transport: FakeTransport) -> Callable[..., Any]:
    """A factory handing back the one shared transport, so tests can inspect it."""

    def factory(device: object, **kwargs: Any) -> FakeTransport:
        transport.device = device
        transport.timeout = kwargs.get("timeout", transport.timeout)
        transport.disconnected_callback = kwargs.get("disconnected_callback")
        return transport

    return factory
