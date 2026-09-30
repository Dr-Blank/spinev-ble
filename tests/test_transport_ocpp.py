"""OCPP tunnel transport tests.

A fake central system answers each ``DataTransfer`` the way a charger does,
from the same script the other fakes use. No OCPP library is involved.
"""

from __future__ import annotations

import asyncio

import pytest

from spinev_ble import (
    OcppConfig,
    OcppTunnelTransport,
    Register,
    SpinEvCharger,
    SpinEvConnectionError,
    SpinEvProtocolError,
    SpinEvTimeoutError,
    SpinEvTransport,
    SpinEvUnsupportedError,
    SpinEvValueError,
    build_read,
    build_write_float,
)
from spinev_ble.transports.ocpp import MESSAGE_ID, VENDOR_ID

from .conftest import ScriptedCharger, reply, string_reply

DUMMY_PASSWORD = 0xABCDEF

NO_COMMAND = "4E_6F_20_43_6F_6D_6D_61"
"""What the charger answers for a register it does not implement: ASCII text,
not a frame."""


def tunnel_form(payload: bytes) -> str:
    """Format a payload the way the charger puts it in a reply's ``data``."""
    return "_".join(f"{byte:02X}" for byte in payload)


class FakeCentralSystem(ScriptedCharger):
    """Sends ``DataTransfer`` requests to a scripted charger.

    Call it as the transport's ``data_transfer`` function. Every request is
    recorded, and the charger's answer comes back in its underscore joined
    form.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str, str]] = []
        self.status = "Accepted"
        self.unsupported: set[int] = set()
        """Registers answered with ASCII text instead of a frame."""
        self.error: Exception | None = None
        """Raised by every request, when set."""
        self.delay = 0.0
        self._answers: list[bytes] = []

    async def __call__(
        self, vendor_id: str, message_id: str, data: str
    ) -> tuple[str, str | None]:
        self.calls.append((vendor_id, message_id, data))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        frame = bytes.fromhex(data)
        self.writes.append(frame)
        if frame[2] in self.unsupported:
            return self.status, NO_COMMAND
        self._answers.clear()
        self.answer(frame)
        if not self._answers:
            return self.status, None
        return self.status, tunnel_form(self._answers[0])

    def notify(self, payload: bytes) -> None:
        self._answers.append(payload)


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
def central() -> FakeCentralSystem:
    return FakeCentralSystem()


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


async def connected(
    central: FakeCentralSystem, recorder: Recorder
) -> OcppTunnelTransport:
    transport = OcppTunnelTransport(central)
    await transport.async_connect(recorder.on_frame, recorder.on_disconnect)
    return transport


class TestContract:
    def test_satisfies_the_transport_protocol(self, central: FakeCentralSystem) -> None:
        assert isinstance(OcppTunnelTransport(central), SpinEvTransport)

    def test_uses_the_charger_s_vendor_and_message_ids(self) -> None:
        assert VENDOR_ID == "CPV07"
        assert MESSAGE_ID == "CC_CONFIG"


class TestConnection:
    async def test_connecting_opens_nothing(
        self, central: FakeCentralSystem, recorder: Recorder
    ) -> None:
        """The central system owns the link; connecting only wires callbacks."""
        transport = OcppTunnelTransport(central)
        before = transport.is_connected
        await transport.async_connect(recorder.on_frame, recorder.on_disconnect)
        assert not before
        assert transport.is_connected
        assert central.calls == []

    async def test_disconnect_stops_delivery(
        self, central: FakeCentralSystem, recorder: Recorder
    ) -> None:
        transport = await connected(central, recorder)
        await transport.async_disconnect()
        assert not transport.is_connected
        assert recorder.disconnects == 0
        with pytest.raises(SpinEvConnectionError, match="not connected"):
            await transport.async_send(build_read(Register.POWER))

    async def test_notify_disconnected_reports_the_drop_once(
        self, central: FakeCentralSystem, recorder: Recorder
    ) -> None:
        transport = await connected(central, recorder)
        transport.notify_disconnected()
        transport.notify_disconnected()
        assert recorder.disconnects == 1
        assert not transport.is_connected

    def test_notify_disconnected_before_connecting_is_safe(
        self, central: FakeCentralSystem
    ) -> None:
        OcppTunnelTransport(central).notify_disconnected()


class TestFrames:
    async def test_a_frame_travels_as_lowercase_hex(
        self, central: FakeCentralSystem, recorder: Recorder
    ) -> None:
        transport = await connected(central, recorder)
        await transport.async_send(build_write_float(Register.CURRENT_LIMIT, 24.0))
        assert central.calls == [(VENDOR_ID, MESSAGE_ID, "10ac4f0141c00000")]

    @pytest.mark.parametrize(
        "data",
        [
            "10_AC_84_00_45_71_3D_71",
            "10_ac_84_00_45_71_3d_71",
            "10ac840045713d71",
            " 10_AC_84_00 45_71_3D_71\n",
        ],
    )
    async def test_the_reply_reaches_the_client_as_frame_bytes(
        self, data: str, recorder: Recorder
    ) -> None:
        async def answer(_vendor: str, _message: str, _data: str) -> tuple[str, str]:
            return "Accepted", data

        transport = OcppTunnelTransport(answer)
        await transport.async_connect(recorder.on_frame, recorder.on_disconnect)
        await transport.async_send(build_read(Register.POWER))
        assert recorder.frames == [bytes.fromhex("10ac840045713d71")]

    @pytest.mark.parametrize("data", [None, ""])
    async def test_a_reply_without_data_delivers_nothing(
        self, data: str | None, recorder: Recorder
    ) -> None:
        async def answer(
            _vendor: str, _message: str, _data: str
        ) -> tuple[str, str | None]:
            return "Accepted", data

        transport = OcppTunnelTransport(answer)
        await transport.async_connect(recorder.on_frame, recorder.on_disconnect)
        await transport.async_send(build_read(Register.POWER))
        assert recorder.frames == []

    async def test_a_reply_that_is_not_hex_is_a_protocol_error(
        self, recorder: Recorder
    ) -> None:
        """The reply text stays out of the error: registers can hold secrets."""
        secret = "hunter2-not-hex"

        async def answer(_vendor: str, _message: str, _data: str) -> tuple[str, str]:
            return "Accepted", secret

        transport = OcppTunnelTransport(answer)
        await transport.async_connect(recorder.on_frame, recorder.on_disconnect)
        with pytest.raises(SpinEvProtocolError) as excinfo:
            await transport.async_send(build_read(Register.POWER))
        assert secret not in str(excinfo.value)

    async def test_an_unimplemented_register_is_a_protocol_error(
        self, central: FakeCentralSystem, recorder: Recorder
    ) -> None:
        central.unsupported = {0xD1}
        transport = await connected(central, recorder)
        with pytest.raises(SpinEvProtocolError, match="0xD1"):
            await transport.async_send(build_read(0xD1))
        assert recorder.frames == []

    @pytest.mark.parametrize("status", ["Rejected", "UnknownVendorId"])
    async def test_a_refused_request_is_a_protocol_error(
        self, status: str, central: FakeCentralSystem, recorder: Recorder
    ) -> None:
        central.status = status
        transport = await connected(central, recorder)
        with pytest.raises(SpinEvProtocolError, match=status):
            await transport.async_send(build_read(Register.POWER))

    @pytest.mark.parametrize(
        "register", [Register.HISTORY_SESSIONS, Register.HISTORY_EVENTS]
    )
    async def test_streamed_reads_are_unsupported(
        self, register: Register, central: FakeCentralSystem, recorder: Recorder
    ) -> None:
        transport = await connected(central, recorder)
        with pytest.raises(SpinEvUnsupportedError, match="streams records"):
            await transport.async_send(build_read(register, 40))
        assert central.calls == []

    @pytest.mark.parametrize("frame", [b"", b"\x10\xac", b"\x00\x00\x84\x00\x00"])
    async def test_something_that_is_not_a_frame_is_refused(
        self, frame: bytes, central: FakeCentralSystem, recorder: Recorder
    ) -> None:
        transport = await connected(central, recorder)
        with pytest.raises(SpinEvValueError, match="not a register frame"):
            await transport.async_send(frame)
        assert central.calls == []

    async def test_a_timeout_from_the_central_system_is_a_timeout_error(
        self, central: FakeCentralSystem, recorder: Recorder
    ) -> None:
        central.error = TimeoutError()
        transport = await connected(central, recorder)
        with pytest.raises(SpinEvTimeoutError, match="no DataTransfer reply"):
            await transport.async_send(build_read(Register.POWER))

    @pytest.mark.parametrize("data", [{"value": 1}, 1234, ["10", "AC"]])
    async def test_reply_data_that_is_not_text_is_a_protocol_error(
        self, data: object, recorder: Recorder
    ) -> None:
        """A central system handing back parsed JSON gets a clear error."""

        async def answer(_vendor: str, _message: str, _data: str) -> tuple[str, str]:
            return "Accepted", data  # type: ignore[return-value]

        transport = OcppTunnelTransport(answer)
        await transport.async_connect(recorder.on_frame, recorder.on_disconnect)
        with pytest.raises(SpinEvProtocolError, match="expected text"):
            await transport.async_send(build_read(Register.POWER))

    async def test_a_generic_failure_is_a_connection_error(
        self, central: FakeCentralSystem, recorder: Recorder
    ) -> None:
        central.error = OSError("websocket closed")
        transport = await connected(central, recorder)
        with pytest.raises(SpinEvConnectionError, match="websocket closed"):
            await transport.async_send(build_read(Register.POWER))

    async def test_a_package_error_passes_through_unchanged(
        self, central: FakeCentralSystem, recorder: Recorder
    ) -> None:
        central.error = SpinEvTimeoutError("central system gave up")
        transport = await connected(central, recorder)
        with pytest.raises(SpinEvTimeoutError, match="central system gave up"):
            await transport.async_send(build_read(Register.POWER))

    async def test_a_reply_after_a_disconnect_is_dropped(
        self, recorder: Recorder
    ) -> None:
        holder: list[OcppTunnelTransport] = []

        async def answer(_vendor: str, _message: str, _data: str) -> tuple[str, str]:
            holder[0].notify_disconnected()
            return "Accepted", "10_AC_84_00_45_71_3D_71"

        transport = OcppTunnelTransport(answer)
        holder.append(transport)
        await transport.async_connect(recorder.on_frame, recorder.on_disconnect)
        await transport.async_send(build_read(Register.POWER))
        assert recorder.frames == []
        assert recorder.disconnects == 1


class TestWithTheCharger:
    """The charger client over the tunnel, end to end."""

    @pytest.fixture
    def charger(self, central: FakeCentralSystem) -> SpinEvCharger:
        return SpinEvCharger(
            OcppTunnelTransport(central), password=DUMMY_PASSWORD, timeout=1.0
        )

    async def test_reads_the_current_limit(
        self, central: FakeCentralSystem, charger: SpinEvCharger
    ) -> None:
        central.replies = {
            Register.CURRENT_LIMIT: reply(
                Register.CURRENT_LIMIT, bytes.fromhex("41c00000")
            )
        }
        async with charger:
            assert await charger.async_get_current_limit() == 24.0

    async def test_sets_the_current_limit(
        self, central: FakeCentralSystem, charger: SpinEvCharger
    ) -> None:
        central.replies = {
            Register.STATE: reply(Register.STATE, bytes.fromhex("00000001")),
            Register.CURRENT_LIMIT: reply(
                Register.CURRENT_LIMIT, bytes.fromhex("41c00000"), flag=1
            ),
        }
        async with charger:
            await charger.async_set_current_limit(24)
        assert [data for _v, _m, data in central.calls] == [
            "10ac670000000000",
            "10ac4f0141c00000",
        ]

    async def test_reads_text_registers(
        self, central: FakeCentralSystem, charger: SpinEvCharger
    ) -> None:
        central.replies = {
            Register.OCPP_HOST: string_reply(Register.OCPP_HOST, "ocpp.example.com"),
            Register.OCPP_PORT: reply(Register.OCPP_PORT, bytes.fromhex("00002328")),
            Register.OCPP_PATH: string_reply(Register.OCPP_PATH, "ocpp"),
            Register.OCPP_CHARGE_POINT_ID: string_reply(
                Register.OCPP_CHARGE_POINT_ID, "CP0001"
            ),
        }
        async with charger:
            config = await charger.async_get_ocpp_config()
        assert config == OcppConfig(
            host="ocpp.example.com", port=9000, path="ocpp", charge_point_id="CP0001"
        )

    async def test_starts_charging(
        self, central: FakeCentralSystem, charger: SpinEvCharger
    ) -> None:
        async with charger:
            await charger.async_start_charging()
        assert central.calls[-1][2] == "10ac3c0101abcdef"

    async def test_history_is_unsupported(
        self, central: FakeCentralSystem, charger: SpinEvCharger
    ) -> None:
        async with charger:
            with pytest.raises(SpinEvUnsupportedError):
                await charger.async_get_history()
        assert central.calls == []

    async def test_an_unimplemented_register_fails_without_waiting(
        self, central: FakeCentralSystem
    ) -> None:
        central.unsupported = {Register.POWER}
        charger = SpinEvCharger(OcppTunnelTransport(central), timeout=30.0)
        async with charger:
            with pytest.raises(SpinEvProtocolError):
                await asyncio.wait_for(charger.async_get_power(), timeout=1.0)

    async def test_the_charger_timeout_bounds_a_slow_round_trip(
        self, central: FakeCentralSystem
    ) -> None:
        """The reply arrives inside the send, so the one deadline must cover
        both."""
        central.delay = 5.0
        charger = SpinEvCharger(OcppTunnelTransport(central), timeout=0.05)
        async with charger:
            with pytest.raises(SpinEvTimeoutError, match="0x84"):
                await asyncio.wait_for(charger.async_get_power(), timeout=1.0)

    async def test_the_charger_timeout_bounds_a_write(
        self, central: FakeCentralSystem
    ) -> None:
        central.delay = 5.0
        charger = SpinEvCharger(OcppTunnelTransport(central), timeout=0.05)
        async with charger:
            with pytest.raises(SpinEvTimeoutError, match="0xC2"):
                await asyncio.wait_for(
                    charger.async_set_random_delay(0, commit=False), timeout=1.0
                )

    async def test_a_register_that_never_answers_times_out(
        self, central: FakeCentralSystem
    ) -> None:
        charger = SpinEvCharger(OcppTunnelTransport(central), timeout=0.05)
        async with charger:
            with pytest.raises(SpinEvTimeoutError, match="0x84"):
                await charger.async_get_power()

    async def test_a_dropped_connection_disconnects_the_charger(
        self, central: FakeCentralSystem, charger: SpinEvCharger
    ) -> None:
        async with charger:
            transport = charger.transport
            assert isinstance(transport, OcppTunnelTransport)
            transport.notify_disconnected()
            assert not charger.is_connected
            with pytest.raises(SpinEvConnectionError, match="not connected"):
                await charger.async_get_power()
            await charger.async_connect()
            assert charger.is_connected
