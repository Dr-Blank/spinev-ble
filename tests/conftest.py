"""Shared fixtures.

A scripted charger stands in for real hardware. It answers each frame it is
sent the way a charger would, and sits behind two fakes:

- :class:`ScriptedTransport`, a plain :class:`~spinev_ble.transports.SpinEvTransport`
  that the charger client tests run over, with no Bluetooth involved.
- :class:`FakeBleakClient`, a stand-in for :class:`bleak.BleakClient` that the
  Bluetooth transport tests run over.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from bleak.backends.device import BLEDevice

from spinev_ble.const import ALARM_BANK2_FLAG, FRAME_HEADER, Operation, Register
from spinev_ble.transports import DisconnectCallback, FrameCallback

FAKE_DEVICE = BLEDevice("AA:BB:CC:DD:EE:FF", "000000000000_ABCD", None)
"""Stand-in for a scanned device. Its name and address are read when a
connection is opened; nothing else about it needs to be real."""


def reply(register: int, value: bytes, flag: int = Operation.READ) -> bytes:
    """Build a reply frame the way a charger would."""
    return FRAME_HEADER + bytes([register, flag]) + value


def string_reply(register: int, value: str, padding: int = 4) -> bytes:
    """Build a text register reply, zero padded like the charger's."""
    return reply(register, value.encode("ascii") + b"\x00" * padding)


_BULK_REGISTERS = frozenset({0x68, 0x70})
"""Registers whose reads stream records rather than answering with one frame."""

_ECHO_REGISTERS = frozenset({Register.CONTROL})
"""Registers a charger answers by echoing the written frame back unchanged.
A test that wants a refusal scripts an entry in ``replies`` instead."""


class ScriptedCharger:
    """Answers frames the way a charger would, from a script.

    ``replies`` maps a register id to the payload the charger answers with. A
    register with no entry never answers, which is how timeouts are exercised.
    ``bulk`` is streamed in response to any bulk read. Subclasses decide how
    an answer reaches the client by implementing :meth:`notify`.
    """

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.replies: dict[int, bytes] = {}
        """Filled in by tests before use."""
        self.replies_by_flag: dict[tuple[int, int], bytes] = {}
        """Replies keyed by (register, flag), for when the flag matters, such
        as the two alarm banks that share one register."""
        self.bulk: list[bytes] = []
        self.silent: set[tuple[int, int]] = set()
        """(register, flag) pairs the charger never answers, standing in for
        firmware that does not implement a read."""

    def notify(self, payload: bytes) -> None:
        """Deliver a payload to the client."""
        raise NotImplementedError

    def answer(self, frame: bytes) -> None:
        """Push whatever the scripted charger would send back."""
        register = frame[2]
        flag = frame[3]
        if (register, flag) in self.silent:
            return
        if self.bulk and flag == Operation.READ and register in _BULK_REGISTERS:
            for record in self.bulk:
                self.notify(record)
            return
        flagged = self.replies_by_flag.get((register, flag))
        if flagged is not None:
            self.notify(flagged)
            return
        if register == Register.ALARMS and flag == ALARM_BANK2_FLAG:
            # An unscripted second alarm bank reads as all clear.
            self.notify(reply(register, b"\x00\x00\x00\x00", flag=ALARM_BANK2_FLAG))
            return
        payload = self.replies.get(register)
        if payload is not None:
            self.notify(payload)
            return
        if register in _ECHO_REGISTERS:
            self.notify(frame)


class ScriptedTransport(ScriptedCharger):
    """A transport wired straight to a scripted charger."""

    def __init__(self) -> None:
        super().__init__()
        self.is_connected = False
        self.on_frame: FrameCallback | None = None
        self.on_disconnect: DisconnectCallback | None = None
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.connect_error: Exception | None = None
        """Raised by the next connect, when set."""
        self.send_error: Exception | None = None
        """Raised by every send, when set."""
        self.drop_on_write = False
        """Set to drop the link instead of answering the next write."""
        self.hold_writes = False
        """Set to keep every send open until it is cancelled, the way a
        transport that waits for the whole round trip does."""
        self.send_cancelled = False
        """True once a held send has been cancelled."""
        self.timeout_hint: str | None = None
        """Appended to timeout errors by the charger client, when set."""

    async def async_connect(
        self, on_frame: FrameCallback, on_disconnect: DisconnectCallback
    ) -> None:
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error
        self.on_frame = on_frame
        self.on_disconnect = on_disconnect
        self.is_connected = True

    async def async_disconnect(self) -> None:
        self.disconnect_calls += 1
        self.is_connected = False
        self.on_frame = None
        self.on_disconnect = None

    async def async_send(self, frame: bytes) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.writes.append(frame)
        if self.hold_writes:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.send_cancelled = True
                raise
        if self.drop_on_write:
            self.drop()
            return
        self.answer(frame)

    def drop(self) -> None:
        """Lose the link the way a charger going away would."""
        self.is_connected = False
        if self.on_disconnect is not None:
            self.on_disconnect()

    def notify(self, payload: bytes) -> None:
        assert self.on_frame is not None, "not connected"
        self.on_frame(payload)


class FakeBleakClient(ScriptedCharger):
    """A scripted stand-in for :class:`bleak.BleakClient`.

    It implements only the members the Bluetooth transport uses, which is what
    :class:`spinev_ble.transports.BleakClientLike` describes.
    """

    def __init__(
        self,
        device: object,
        *,
        timeout: float = 5.0,
        disconnected_callback: Callable[[object], None] | None = None,
    ) -> None:
        super().__init__()
        self.device = device
        self.timeout = timeout
        self.disconnected_callback = disconnected_callback
        self.is_connected = False
        self.notify_callback: Callable[[object, bytearray], None] | None = None
        self.notify_char: object = None
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.drop_on_write = False
        """Set to drop the link instead of answering the next write."""
        self.notify_error: Exception | None = None
        """Set to fail the notification subscription that follows a connect."""
        self.write_error: Exception | None = None
        """Set to fail every write."""
        self.written_to: list[object] = []

    async def connect(self, **_kwargs: Any) -> None:
        self.connect_calls += 1
        self.is_connected = True

    async def disconnect(self, **_kwargs: Any) -> None:
        """Close the link. Like bleak, this fires the disconnected callback
        even though the disconnect was asked for."""
        self.disconnect_calls += 1
        was_connected, self.is_connected = self.is_connected, False
        if was_connected and self.disconnected_callback is not None:
            self.disconnected_callback(self)

    async def start_notify(
        self, char: object, callback: Callable[[object, bytearray], None], **_kw: Any
    ) -> None:
        if self.notify_error is not None:
            raise self.notify_error
        self.notify_char = char
        self.notify_callback = callback

    async def write_gatt_char(
        self, char: object, data: Any, response: bool | None = None
    ) -> None:
        assert response is True, "the charger needs acknowledged writes"
        if self.write_error is not None:
            raise self.write_error
        frame = bytes(data)
        self.writes.append(frame)
        self.written_to.append(char)
        if self.drop_on_write:
            self.is_connected = False
            if self.disconnected_callback is not None:
                self.disconnected_callback(self)
            return
        self.answer(frame)

    def notify(self, payload: bytes) -> None:
        """Deliver a notification exactly as bleak would."""
        assert self.notify_callback is not None, "not subscribed"
        self.notify_callback(self, bytearray(payload))


@pytest.fixture
def transport() -> ScriptedTransport:
    """The transport the charger client under test talks through."""
    return ScriptedTransport()


@pytest.fixture
def bleak_client() -> FakeBleakClient:
    """The Bluetooth client the transport under test will be given."""
    return FakeBleakClient(FAKE_DEVICE)


@pytest.fixture
def client_class(bleak_client: FakeBleakClient) -> Callable[..., Any]:
    """A factory handing back the one shared Bluetooth client, so tests can
    inspect it."""

    def factory(device: object, **kwargs: Any) -> FakeBleakClient:
        bleak_client.device = device
        bleak_client.timeout = kwargs.get("timeout", bleak_client.timeout)
        bleak_client.disconnected_callback = kwargs.get("disconnected_callback")
        return bleak_client

    return factory


@dataclass
class ConnectAttempt:
    """What the transport asked bleak-retry-connector to open."""

    client_class: Callable[..., Any]
    device: object
    name: str
    max_attempts: int
    kwargs: dict[str, Any]


@pytest.fixture(autouse=True)
def connect_attempts(monkeypatch: pytest.MonkeyPatch) -> list[ConnectAttempt]:
    """Stand in for bleak-retry-connector, which reaches for D-Bus on Linux.

    Builds the client through the injected factory and connects it, the way
    the real one does, and records the call so a test can check the arguments.
    """
    attempts: list[ConnectAttempt] = []

    async def establish_connection(
        client_class: Callable[..., Any],
        device: object,
        name: str,
        *,
        disconnected_callback: Callable[[Any], None] | None = None,
        max_attempts: int = 0,
        **kwargs: Any,
    ) -> Any:
        attempts.append(
            ConnectAttempt(client_class, device, name, max_attempts, dict(kwargs))
        )
        client = client_class(
            device, disconnected_callback=disconnected_callback, **kwargs
        )
        await client.connect()
        return client

    monkeypatch.setattr(
        "spinev_ble.transports.ble.establish_connection", establish_connection
    )
    return attempts
