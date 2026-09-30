"""Bluetooth transport tests, driven by the fake bleak client in ``conftest``.

No hardware and no Bluetooth adapter are involved.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from bleak_retry_connector import MAX_CONNECT_ATTEMPTS

from spinev_ble import (
    CHARACTERISTIC_UUID,
    Register,
    SpinEvCharger,
    SpinEvConnectionError,
    SpinEvTransport,
    build_read,
)
from spinev_ble.transports import BleakClientLike, BleTransport

from .conftest import FAKE_DEVICE, ConnectAttempt, FakeBleakClient, reply


class Recorder:
    """Collects what a transport hands back to its client."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.disconnects = 0

    def on_frame(self, payload: bytes) -> None:
        self.frames.append(payload)

    def on_disconnect(self) -> None:
        self.disconnects += 1


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


def make_transport(client_class: Callable[..., Any], **kwargs: Any) -> BleTransport:
    return BleTransport(FAKE_DEVICE, client_class=client_class, **kwargs)


class TestContract:
    def test_fake_client_satisfies_the_bleak_protocol(
        self, bleak_client: FakeBleakClient
    ) -> None:
        assert isinstance(bleak_client, BleakClientLike)

    def test_satisfies_the_transport_protocol(
        self, client_class: Callable[..., Any]
    ) -> None:
        assert isinstance(make_transport(client_class), SpinEvTransport)

    def test_is_exported_lazily_from_the_package(self) -> None:
        import spinev_ble  # pylint: disable=import-outside-toplevel

        assert spinev_ble.BleTransport is BleTransport


class TestConnection:
    async def test_connecting_goes_through_bleak_retry_connector(
        self,
        client_class: Callable[..., Any],
        connect_attempts: list[ConnectAttempt],
        recorder: Recorder,
    ) -> None:
        """Retries and adapter handling belong to bleak-retry-connector."""
        transport = make_transport(client_class, timeout=7.5)
        await transport.async_connect(recorder.on_frame, recorder.on_disconnect)
        await transport.async_disconnect()

        assert len(connect_attempts) == 1
        attempt = connect_attempts[0]
        assert attempt.client_class is client_class
        assert attempt.device is FAKE_DEVICE
        assert attempt.name == FAKE_DEVICE.name
        assert attempt.max_attempts == MAX_CONNECT_ATTEMPTS
        assert attempt.kwargs["timeout"] == 7.5

    async def test_max_attempts_is_the_caller_s_to_set(
        self,
        client_class: Callable[..., Any],
        connect_attempts: list[ConnectAttempt],
        recorder: Recorder,
    ) -> None:
        """One attempt is what a caller wanting a fast answer asks for."""
        transport = make_transport(client_class, max_attempts=1)
        await transport.async_connect(recorder.on_frame, recorder.on_disconnect)
        await transport.async_disconnect()

        assert connect_attempts[0].max_attempts == 1

    async def test_subscribes_to_the_charger_characteristic(
        self,
        bleak_client: FakeBleakClient,
        client_class: Callable[..., Any],
        recorder: Recorder,
    ) -> None:
        transport = make_transport(client_class)
        await transport.async_connect(recorder.on_frame, recorder.on_disconnect)
        assert transport.is_connected
        assert bleak_client.notify_char == CHARACTERISTIC_UUID

    async def test_a_failed_connection_is_a_connection_error(
        self,
        client_class: Callable[..., Any],
        monkeypatch: pytest.MonkeyPatch,
        recorder: Recorder,
    ) -> None:
        async def refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise TimeoutError("no advertisement")

        monkeypatch.setattr("spinev_ble.transports.ble.establish_connection", refuse)
        transport = make_transport(client_class)
        with pytest.raises(SpinEvConnectionError, match="no advertisement"):
            await transport.async_connect(recorder.on_frame, recorder.on_disconnect)
        assert not transport.is_connected

    async def test_a_failed_subscription_hands_the_link_back(
        self,
        bleak_client: FakeBleakClient,
        client_class: Callable[..., Any],
        recorder: Recorder,
    ) -> None:
        """A connected link the transport cannot use must not keep the slot."""
        bleak_client.notify_error = RuntimeError("no such characteristic")
        transport = make_transport(client_class)

        with pytest.raises(SpinEvConnectionError, match="no such characteristic"):
            await transport.async_connect(recorder.on_frame, recorder.on_disconnect)

        assert not bleak_client.is_connected
        assert bleak_client.disconnect_calls == 1
        assert not transport.is_connected

    async def test_connect_is_idempotent(
        self,
        bleak_client: FakeBleakClient,
        client_class: Callable[..., Any],
        recorder: Recorder,
    ) -> None:
        transport = make_transport(client_class)
        await transport.async_connect(recorder.on_frame, recorder.on_disconnect)
        await transport.async_connect(recorder.on_frame, recorder.on_disconnect)
        assert bleak_client.connect_calls == 1

    async def test_disconnect_is_safe_before_connecting_and_twice(
        self,
        bleak_client: FakeBleakClient,
        client_class: Callable[..., Any],
        recorder: Recorder,
    ) -> None:
        transport = make_transport(client_class)
        await transport.async_disconnect()
        await transport.async_connect(recorder.on_frame, recorder.on_disconnect)
        await transport.async_disconnect()
        await transport.async_disconnect()
        assert bleak_client.disconnect_calls == 1
        assert not transport.is_connected

    async def test_a_failing_disconnect_is_swallowed(
        self,
        bleak_client: FakeBleakClient,
        client_class: Callable[..., Any],
        recorder: Recorder,
    ) -> None:
        """Closing a link that is already gone must not raise."""
        transport = make_transport(client_class)
        await transport.async_connect(recorder.on_frame, recorder.on_disconnect)

        async def broken(**_kwargs: Any) -> None:
            raise OSError("already gone")

        bleak_client.disconnect = broken  # type: ignore[method-assign]
        await transport.async_disconnect()
        assert not transport.is_connected

    async def test_a_requested_disconnect_is_not_reported_as_a_drop(
        self, client_class: Callable[..., Any], recorder: Recorder
    ) -> None:
        """bleak signals a requested disconnect like a lost link; the client
        must hear only about the lost ones."""
        transport = make_transport(client_class)
        await transport.async_connect(recorder.on_frame, recorder.on_disconnect)
        await transport.async_disconnect()
        assert recorder.disconnects == 0

    async def test_a_dropped_link_is_reported(
        self,
        bleak_client: FakeBleakClient,
        client_class: Callable[..., Any],
        recorder: Recorder,
    ) -> None:
        transport = make_transport(client_class)
        await transport.async_connect(recorder.on_frame, recorder.on_disconnect)
        bleak_client.drop_on_write = True
        await transport.async_send(build_read(Register.POWER))
        assert recorder.disconnects == 1
        assert not transport.is_connected


class TestFrames:
    async def test_frames_are_written_with_response_to_the_characteristic(
        self,
        bleak_client: FakeBleakClient,
        client_class: Callable[..., Any],
        recorder: Recorder,
    ) -> None:
        transport = make_transport(client_class)
        await transport.async_connect(recorder.on_frame, recorder.on_disconnect)
        frame = build_read(Register.POWER)
        await transport.async_send(frame)
        assert bleak_client.writes == [frame]
        assert bleak_client.written_to == [CHARACTERISTIC_UUID]

    async def test_notifications_reach_the_client_as_bytes(
        self,
        bleak_client: FakeBleakClient,
        client_class: Callable[..., Any],
        recorder: Recorder,
    ) -> None:
        transport = make_transport(client_class)
        await transport.async_connect(recorder.on_frame, recorder.on_disconnect)
        payload = reply(Register.POWER, bytes.fromhex("45713d71"))
        bleak_client.notify(payload)
        assert recorder.frames == [payload]
        assert type(recorder.frames[0]) is bytes

    async def test_sending_before_connecting_is_refused(
        self, client_class: Callable[..., Any]
    ) -> None:
        with pytest.raises(SpinEvConnectionError, match="not connected"):
            await make_transport(client_class).async_send(build_read(Register.POWER))

    async def test_a_failed_write_is_a_connection_error(
        self,
        bleak_client: FakeBleakClient,
        client_class: Callable[..., Any],
        recorder: Recorder,
    ) -> None:
        transport = make_transport(client_class)
        await transport.async_connect(recorder.on_frame, recorder.on_disconnect)
        bleak_client.write_error = OSError("gatt error")
        with pytest.raises(SpinEvConnectionError, match="gatt error"):
            await transport.async_send(build_read(Register.POWER))


class TestWithTheCharger:
    """The charger client over Bluetooth, end to end."""

    async def test_reads_a_value(
        self, bleak_client: FakeBleakClient, client_class: Callable[..., Any]
    ) -> None:
        bleak_client.replies = {
            Register.POWER: reply(Register.POWER, bytes.fromhex("45713d71"))
        }
        charger = SpinEvCharger(make_transport(client_class), timeout=1.0)
        async with charger:
            assert await charger.async_get_power() == pytest.approx(3859.84, abs=0.01)
        assert not bleak_client.is_connected

    async def test_a_drop_mid_request_fails_at_once(
        self, bleak_client: FakeBleakClient, client_class: Callable[..., Any]
    ) -> None:
        """A disconnect mid request must not make the caller sit out the timeout."""
        charger = SpinEvCharger(make_transport(client_class), timeout=30.0)
        async with charger:
            bleak_client.drop_on_write = True
            with pytest.raises(SpinEvConnectionError, match="disconnected"):
                await charger.async_get_power()
