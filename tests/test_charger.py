"""Charger client tests, driven by the scripted transport in ``conftest``.

No hardware and no Bluetooth are involved: the client is exercised through a
plain :class:`~spinev_ble.transports.SpinEvTransport`, which is all it needs.
"""

from __future__ import annotations

import asyncio
import gc
import subprocess
import sys
from datetime import datetime

import pytest

from spinev_ble import (
    ALARM_BANK2_FLAG,
    AlarmSeverity,
    ChargerState,
    LoadBalancingConfig,
    OcppConfig,
    Operation,
    Register,
    SpinEvBusyError,
    SpinEvCharger,
    SpinEvCommandRejectedError,
    SpinEvConnectionError,
    SpinEvProtocolError,
    SpinEvTimeoutError,
    SpinEvTransport,
    SpinEvTypeError,
    SpinEvValueError,
)
from spinev_ble.transports import BleTransport, OcppTunnelTransport

from .conftest import (
    FAKE_DEVICE,
    ScriptedTransport,
    reply,
    string_reply,
)

DUMMY_PASSWORD = 0xABCDEF

#: A charger part way through a session, covering every register a full status
#: read asks for.
STATUS_REPLIES: dict[int, bytes] = {
    Register.STATE: reply(Register.STATE, bytes.fromhex("00000004")),
    Register.POWER: reply(Register.POWER, bytes.fromhex("45713d71")),
    Register.VOLTAGE: reply(Register.VOLTAGE, bytes.fromhex("438003d7")),
    Register.CURRENT: reply(Register.CURRENT, bytes.fromhex("4170b852")),
    Register.CURRENT_LIMIT: reply(Register.CURRENT_LIMIT, bytes.fromhex("41800000")),
    Register.SESSION_ENERGY: reply(Register.SESSION_ENERGY, bytes.fromhex("000004d2")),
    Register.SESSION_SECONDS: reply(
        Register.SESSION_SECONDS, bytes.fromhex("00000e10")
    ),
    Register.LIFETIME_ENERGY: reply(
        Register.LIFETIME_ENERGY, bytes.fromhex("0001e240")
    ),
    Register.LIFETIME_SECONDS: reply(
        Register.LIFETIME_SECONDS, bytes.fromhex("00015180")
    ),
    Register.FIRMWARE_VERSION: reply(
        Register.FIRMWARE_VERSION, bytes.fromhex("23180420")
    ),
    Register.ALARMS: reply(Register.ALARMS, bytes.fromhex("00000021")),
}


def make_charger(
    transport: SpinEvTransport,
    password: int | None = DUMMY_PASSWORD,
    timeout: float = 1.0,
) -> SpinEvCharger:
    return SpinEvCharger(transport, password=password, timeout=timeout)


def test_the_core_imports_without_bleak() -> None:
    """Only the Bluetooth transport may need bleak."""
    code = (
        "import sys\n"
        "sys.modules['bleak'] = None\n"
        "sys.modules['bleak_retry_connector'] = None\n"
        "from spinev_ble import OcppTunnelTransport, SpinEvCharger\n"
        "try:\n"
        "    from spinev_ble import BleTransport\n"
        "except ImportError as err:\n"
        "    assert 'spinev-ble[bleak]' in str(err)\n"
        "else:\n"
        "    raise SystemExit('BleTransport imported without bleak')\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


class TestConstruction:
    def test_scripted_transport_satisfies_the_protocol(
        self, transport: ScriptedTransport
    ) -> None:
        assert isinstance(transport, SpinEvTransport)

    def test_the_transport_is_kept_as_given(self, transport: ScriptedTransport) -> None:
        assert make_charger(transport).transport is transport

    @pytest.mark.parametrize("not_a_transport", [FAKE_DEVICE, object(), None])
    def test_anything_but_a_transport_is_refused(self, not_a_transport: object) -> None:
        """A bare device is the likely mistake, so the error names the fix."""
        with pytest.raises(SpinEvTypeError, match="BleTransport") as excinfo:
            SpinEvCharger(not_a_transport)  # type: ignore[arg-type]
        assert isinstance(excinfo.value, TypeError)

    def test_password_and_timeout_are_keyword_only(
        self, transport: ScriptedTransport
    ) -> None:
        with pytest.raises(TypeError):
            SpinEvCharger(transport, DUMMY_PASSWORD)  # type: ignore[misc]


class TestConnection:
    async def test_context_manager_connects_and_disconnects(
        self, transport: ScriptedTransport
    ) -> None:
        charger = make_charger(transport)
        async with charger:
            assert charger.is_connected
            assert transport.connect_calls == 1
            assert transport.on_frame is not None
            assert transport.on_disconnect is not None
        # Checked on the transport rather than the charger property, which mypy
        # keeps narrowed from the assertion above.
        assert not transport.is_connected
        assert transport.disconnect_calls == 1

    async def test_a_generic_connect_failure_becomes_a_connection_error(
        self, transport: ScriptedTransport
    ) -> None:
        transport.connect_error = RuntimeError("adapter gone")
        with pytest.raises(SpinEvConnectionError, match="adapter gone"):
            await make_charger(transport).async_connect()

    async def test_a_package_error_from_connect_passes_through(
        self, transport: ScriptedTransport
    ) -> None:
        transport.connect_error = SpinEvTimeoutError("slot taken")
        with pytest.raises(SpinEvTimeoutError, match="slot taken"):
            await make_charger(transport).async_connect()

    async def test_a_generic_send_failure_becomes_a_connection_error(
        self, transport: ScriptedTransport
    ) -> None:
        transport.send_error = OSError("link reset")
        async with make_charger(transport) as charger:
            with pytest.raises(SpinEvConnectionError, match="link reset"):
                await charger.async_get_power()
            with pytest.raises(SpinEvConnectionError, match="link reset"):
                await charger.async_set_random_delay(0, commit=False)

    async def test_connect_is_idempotent(self, transport: ScriptedTransport) -> None:
        charger = make_charger(transport)
        await charger.async_connect()
        await charger.async_connect()
        assert transport.connect_calls == 1
        await charger.async_disconnect()

    async def test_disconnect_without_connect_is_safe(
        self, transport: ScriptedTransport
    ) -> None:
        await make_charger(transport).async_disconnect()

    async def test_reads_before_connecting_are_rejected(
        self, transport: ScriptedTransport
    ) -> None:
        charger = make_charger(transport)
        with pytest.raises(SpinEvConnectionError, match="not connected"):
            await charger.async_get_power()

    async def test_timeout_names_the_register_and_the_transport_s_hint(
        self, transport: ScriptedTransport
    ) -> None:
        transport.replies = {}
        transport.timeout_hint = "The test link is down."
        charger = make_charger(transport, timeout=0.05)
        async with charger:
            with pytest.raises(SpinEvTimeoutError) as excinfo:
                await charger.async_get_power()
        assert (
            str(excinfo.value) == "no reply for register 0x84. The test link is down."
        )

    async def test_timeout_without_a_hint_names_only_the_register(
        self, transport: ScriptedTransport
    ) -> None:
        transport.replies = {}
        charger = make_charger(transport, timeout=0.05)
        async with charger:
            with pytest.raises(SpinEvTimeoutError) as excinfo:
                await charger.async_get_power()
        assert str(excinfo.value) == "no reply for register 0x84"

    def test_each_bundled_transport_names_its_own_likely_cause(self) -> None:
        assert "phone app" in BleTransport.timeout_hint
        assert "OCPP" in OcppTunnelTransport.timeout_hint
        assert "phone app" not in OcppTunnelTransport.timeout_hint

    async def test_cancelled_send_that_fails_while_stopping_is_not_reported(
        self, transport: ScriptedTransport
    ) -> None:
        """A send that errors on its way out after a drop must not warn."""

        async def fails_when_cancelled(frame: bytes) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise OSError("link torn down") from None

        transport.async_send = fails_when_cancelled  # type: ignore[method-assign]
        loop = asyncio.get_running_loop()
        unhandled: list[dict[str, object]] = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        charger = make_charger(transport, timeout=30.0)
        async with charger:
            loop.call_later(0.01, transport.drop)
            with pytest.raises(SpinEvConnectionError, match="disconnected"):
                await charger.async_get_power()
        for _ in range(3):
            await asyncio.sleep(0)
        gc.collect()
        assert unhandled == []

    async def test_dropped_link_fails_fast_instead_of_waiting(
        self, transport: ScriptedTransport
    ) -> None:
        """A disconnect mid request must not make the caller sit out the timeout."""
        charger = make_charger(transport, timeout=30.0)
        async with charger:
            transport.drop_on_write = True
            with pytest.raises(SpinEvConnectionError, match="disconnected"):
                await charger.async_get_power()

    async def test_drop_during_a_held_send_fails_the_request_fast(
        self, transport: ScriptedTransport
    ) -> None:
        """A transport that holds its send for the round trip is cut short."""
        charger = make_charger(transport, timeout=30.0)
        async with charger:
            transport.hold_writes = True
            asyncio.get_running_loop().call_later(0.01, transport.drop)
            async with asyncio.timeout(1):
                with pytest.raises(SpinEvConnectionError, match="disconnected"):
                    await charger.async_get_power()
        await asyncio.sleep(0)
        assert transport.send_cancelled

    async def test_drop_during_a_held_send_fails_a_write_fast(
        self, transport: ScriptedTransport
    ) -> None:
        """Writes that wait for no reply, such as a commit, are cut short too."""
        charger = make_charger(transport, timeout=30.0)
        async with charger:
            transport.hold_writes = True
            asyncio.get_running_loop().call_later(0.01, transport.drop)
            async with asyncio.timeout(1):
                with pytest.raises(SpinEvConnectionError, match="disconnected"):
                    await charger.async_commit()
        await asyncio.sleep(0)
        assert transport.send_cancelled

    async def test_short_reply_is_reported_as_a_protocol_error(
        self, transport: ScriptedTransport
    ) -> None:
        transport.replies = {Register.POWER: reply(Register.POWER, b"\x01\x02")}
        charger = make_charger(transport)
        async with charger:
            with pytest.raises(SpinEvProtocolError, match="short reply"):
                await charger.async_get_power()

    async def test_unmatched_notifications_are_ignored(
        self, transport: ScriptedTransport
    ) -> None:
        charger = make_charger(transport)
        transport.replies = dict(STATUS_REPLIES)
        async with charger:
            transport.notify(b"garbage")
            transport.notify(reply(Register.VOLTAGE, bytes.fromhex("438003d7")))
            assert await charger.async_get_power() == pytest.approx(3859.84, abs=0.01)

    async def test_stray_write_echo_does_not_corrupt_a_pending_read(
        self, transport: ScriptedTransport
    ) -> None:
        """A write's echo to a register must not answer an unrelated read of it.

        The charger echoes every accepted frame back, including the chunks
        of a fire-and-forget string write, whose sequence number sits in the
        byte a read reply would use for its flag. If a reply were matched on
        the register alone, a chunk echo left in flight from an earlier
        write could be handed to a later read waiting on the same register,
        silently returning the wrong value instead of the string just read.
        """
        async with make_charger(transport) as charger:
            read_task = asyncio.ensure_future(charger.async_get_wifi_ssid())
            await asyncio.sleep(0.01)  # let the read register its waiter

            # A leftover echo from a WiFi SSID write chunk: same register,
            # but the flag byte holds a chunk sequence number, not 0x00.
            transport.notify(reply(Register.WIFI_SSID, b"Evil", flag=1))
            assert not read_task.done()

            transport.notify(string_reply(Register.WIFI_SSID, "ExampleNet"))
            assert await read_task == "ExampleNet"


class TestTelemetry:
    async def test_individual_reads(self, transport: ScriptedTransport) -> None:
        transport.replies = dict(STATUS_REPLIES)
        async with make_charger(transport) as charger:
            assert await charger.async_get_state() is ChargerState.CHARGING
            assert await charger.async_get_power() == pytest.approx(3859.84, abs=0.01)
            assert await charger.async_get_voltage() == pytest.approx(256.03, abs=0.01)
            assert await charger.async_get_current() == pytest.approx(15.05, abs=0.01)
            assert await charger.async_get_current_limit() == pytest.approx(16.0)
            assert await charger.async_get_alarms() == [
                "Mains Fail",
                "DC Fault/Internal RCD",
            ]

    async def test_alarms_span_both_banks(self, transport: ScriptedTransport) -> None:
        """Bits from the second alarm word reach the caller too.

        Bit 15 is set in both words, and decodes to a different name in each,
        so this also pins each word to its own half of the table.
        """
        transport.replies = dict(STATUS_REPLIES)
        transport.replies_by_flag[(Register.ALARMS, ALARM_BANK2_FLAG)] = reply(
            Register.ALARMS, bytes.fromhex("00008200"), flag=ALARM_BANK2_FLAG
        )
        async with make_charger(transport) as charger:
            assert await charger.async_get_alarms() == [
                "Mains Fail",
                "DC Fault/Internal RCD",
                "L1 Overcurrent",
                "Unexpected CP Voltage",
            ]

    async def test_alarm_defs_reach_the_caller_with_bank_and_severity(
        self, transport: ScriptedTransport
    ) -> None:
        transport.replies = dict(STATUS_REPLIES)
        transport.replies_by_flag[(Register.ALARMS, ALARM_BANK2_FLAG)] = reply(
            Register.ALARMS, bytes.fromhex("00000200"), flag=ALARM_BANK2_FLAG
        )
        async with make_charger(transport) as charger:
            defs = await charger.async_get_alarm_defs()
        assert [(d.bank, d.bit) for d in defs] == [(1, 0), (1, 5), (2, 9)]
        assert defs[-1].name == "L1 Overcurrent"
        assert defs[-1].severity is AlarmSeverity.CRITICAL
        assert defs[-1].code == "302"

    async def test_alarms_ignore_an_all_clear_second_bank(
        self, transport: ScriptedTransport
    ) -> None:
        """The unscripted bank 2 the fake answers with reads as no alarms."""
        transport.replies = dict(STATUS_REPLIES)
        async with make_charger(transport) as charger:
            assert await charger.async_get_alarms() == [
                "Mains Fail",
                "DC Fault/Internal RCD",
            ]

    async def test_short_alarm_reply_is_rejected(
        self, transport: ScriptedTransport
    ) -> None:
        transport.replies = dict(STATUS_REPLIES)
        transport.replies_by_flag[(Register.ALARMS, ALARM_BANK2_FLAG)] = reply(
            Register.ALARMS, b"\x00\x00", flag=ALARM_BANK2_FLAG
        )
        async with make_charger(transport) as charger:
            with pytest.raises(SpinEvProtocolError, match="short reply for alarm bank"):
                await charger.async_get_alarms()

    async def test_silent_second_bank_still_yields_bank_one(
        self, transport: ScriptedTransport
    ) -> None:
        """Firmware that ignores the bank 2 flag must not break the read.

        The bank 2 read times out. Bank 1's faults still reach the caller
        rather than the whole call failing.
        """
        transport.replies = dict(STATUS_REPLIES)
        transport.silent.add((Register.ALARMS, ALARM_BANK2_FLAG))
        async with make_charger(transport, timeout=0.05) as charger:
            assert await charger.async_get_alarms() == [
                "Mains Fail",
                "DC Fault/Internal RCD",
            ]

    async def test_silent_first_bank_still_raises(
        self, transport: ScriptedTransport
    ) -> None:
        """Only bank 2 is treated as optional."""
        transport.replies = dict(STATUS_REPLIES)
        transport.silent.add((Register.ALARMS, Operation.READ))
        async with make_charger(transport, timeout=0.05) as charger:
            with pytest.raises(SpinEvTimeoutError):
                await charger.async_get_alarms()

    async def test_status_survives_a_silent_second_bank(
        self, transport: ScriptedTransport
    ) -> None:
        """The whole status read keeps working on such firmware."""
        transport.replies = dict(STATUS_REPLIES)
        transport.silent.add((Register.ALARMS, ALARM_BANK2_FLAG))
        async with make_charger(transport, timeout=0.05) as charger:
            status = await charger.async_get_status()
        assert status.alarms == ("Mains Fail", "DC Fault/Internal RCD")

    async def test_status_reads_every_field(self, transport: ScriptedTransport) -> None:
        transport.replies = dict(STATUS_REPLIES)
        async with make_charger(transport) as charger:
            status = await charger.async_get_status()
        assert status.state is ChargerState.CHARGING
        assert status.state_value == 4
        assert status.is_charging
        assert status.power_w == pytest.approx(3859.84, abs=0.01)
        assert status.voltage_v == pytest.approx(256.03, abs=0.01)
        assert status.current_a == pytest.approx(15.05, abs=0.01)
        assert status.current_limit_a == pytest.approx(16.0)
        assert status.session_energy_kwh == pytest.approx(12.34)
        assert status.session_seconds == 3600
        assert status.lifetime_energy_kwh == pytest.approx(1234.56)
        assert status.lifetime_seconds == 86400
        assert status.firmware_version == "35.24.4.32"
        assert status.alarms == ("Mains Fail", "DC Fault/Internal RCD")
        assert status.has_alarms

    async def test_status_survives_an_unknown_state(
        self, transport: ScriptedTransport
    ) -> None:
        """A charger in a state this library does not name must not fail the read."""
        transport.replies = dict(STATUS_REPLIES)
        transport.replies[Register.STATE] = reply(
            Register.STATE, bytes.fromhex("00000063")
        )
        async with make_charger(transport) as charger:
            status = await charger.async_get_status()
        assert status.state is None
        assert status.state_value == 0x63
        assert not status.is_charging
        assert status.power_w is not None

    async def test_unknown_state_raises_on_the_enum_accessor(
        self, transport: ScriptedTransport
    ) -> None:
        transport.replies = {
            Register.STATE: reply(Register.STATE, bytes.fromhex("00000063"))
        }
        async with make_charger(transport) as charger:
            with pytest.raises(SpinEvProtocolError, match="unknown charger state 99"):
                await charger.async_get_state()
            assert await charger.async_get_state_value() == 99

    async def test_status_is_immutable(self, transport: ScriptedTransport) -> None:
        transport.replies = dict(STATUS_REPLIES)
        async with make_charger(transport) as charger:
            status = await charger.async_get_status()
        with pytest.raises(AttributeError):
            status.power_w = 0.0  # type: ignore[misc]


class TestControl:
    async def test_start_and_stop_send_the_password(
        self, transport: ScriptedTransport
    ) -> None:
        async with make_charger(transport) as charger:
            await charger.async_start_charging()
            await charger.async_stop_charging()
        assert transport.writes[0].hex() == "10ac3c0101abcdef"
        assert transport.writes[1].hex() == "10ac3c0110abcdef"

    async def test_password_is_read_from_the_charger_when_not_supplied(
        self, transport: ScriptedTransport
    ) -> None:
        transport.replies = {
            Register.PASSWORD: reply(Register.PASSWORD, bytes.fromhex("00abcdef")),
        }
        async with make_charger(transport, password=None) as charger:
            assert await charger.async_get_password() == DUMMY_PASSWORD
            await charger.async_start_charging()
            await charger.async_stop_charging()
        # Read once on first use, then cached rather than re-read.
        assert sum(1 for w in transport.writes if w[2] == Register.PASSWORD) == 2
        assert transport.writes[-1].hex() == "10ac3c0110abcdef"

    @pytest.mark.parametrize("command", ["async_start_charging", "async_stop_charging"])
    async def test_a_refused_command_raises_instead_of_reporting_success(
        self,
        transport: ScriptedTransport,
        command: str,
    ) -> None:
        transport.replies = {
            Register.CONTROL: reply(
                Register.CONTROL, bytes.fromhex("ffffffff"), flag=Operation.WRITE
            )
        }
        async with make_charger(transport) as charger:
            with pytest.raises(SpinEvCommandRejectedError):
                await getattr(charger, command)()

    async def test_a_command_echoed_back_altered_is_not_taken_as_success(
        self, transport: ScriptedTransport
    ) -> None:
        # A stop echoed back where a start was sent means the charger did
        # something other than what was asked, so it cannot be reported as done.
        transport.replies = {
            Register.CONTROL: reply(
                Register.CONTROL, bytes.fromhex("10abcdef"), flag=Operation.WRITE
            )
        }
        async with make_charger(transport) as charger:
            with pytest.raises(SpinEvProtocolError):
                await charger.async_start_charging()

    async def test_password_high_byte_is_masked_off(
        self, transport: ScriptedTransport
    ) -> None:
        transport.replies = {
            Register.PASSWORD: reply(Register.PASSWORD, bytes.fromhex("ffabcdef"))
        }
        async with make_charger(transport, password=None) as charger:
            assert await charger.async_get_password() == DUMMY_PASSWORD

    @pytest.mark.parametrize("amps", [16.0, 6.0, 32.0])
    async def test_current_limit_accepts_the_supported_range(
        self, transport: ScriptedTransport, amps: float
    ) -> None:
        transport.replies = {
            Register.STATE: reply(Register.STATE, bytes.fromhex("00000002")),
            Register.CURRENT_LIMIT: reply(
                Register.CURRENT_LIMIT,
                bytes.fromhex("41800000"),
                flag=Operation.WRITE,
            ),
        }
        async with make_charger(transport) as charger:
            await charger.async_set_current_limit(amps)
        assert transport.writes[-1][:4].hex() == "10ac4f01"
        # Deliberately not committed: a commit restarts the charger, which
        # would drop a session in progress just to change its current.
        assert not any(w.hex() == "10ac3b0101000000" for w in transport.writes)

    @pytest.mark.parametrize("amps", [5.9, 0.0, -1.0, 32.1, 100.0])
    async def test_current_limit_rejects_out_of_range(
        self, transport: ScriptedTransport, amps: float
    ) -> None:
        async with make_charger(transport) as charger:
            with pytest.raises(SpinEvValueError, match="out of range"):
                await charger.async_set_current_limit(amps)
        assert transport.writes == []

    async def test_current_limit_ceiling_is_adjustable_per_model(
        self, transport: ScriptedTransport
    ) -> None:
        transport.replies = {
            Register.STATE: reply(Register.STATE, bytes.fromhex("00000002")),
            Register.CURRENT_LIMIT: reply(
                Register.CURRENT_LIMIT,
                bytes.fromhex("41800000"),
                flag=Operation.WRITE,
            ),
        }
        async with make_charger(transport) as charger:
            await charger.async_set_current_limit(40.0, max_amps=63.0)
            with pytest.raises(SpinEvValueError):
                await charger.async_set_current_limit(40.0, max_amps=32.0)

    async def test_reboot_sends_the_commit_frame(
        self, transport: ScriptedTransport
    ) -> None:
        async with make_charger(transport) as charger:
            await charger.async_reboot()
        assert transport.writes[-1].hex() == "10ac3b0101000000"


class TestNetworkConfig:
    async def test_reads_wifi_settings(self, transport: ScriptedTransport) -> None:
        transport.replies = {
            Register.WIFI_SSID: string_reply(Register.WIFI_SSID, "ExampleNet"),
            Register.WIFI_PASSWORD: string_reply(Register.WIFI_PASSWORD, "hunter2"),
        }
        async with make_charger(transport) as charger:
            assert await charger.async_get_wifi_ssid() == "ExampleNet"
            assert await charger.async_get_wifi_password() == "hunter2"

    async def test_writes_wifi_chunked_then_commits(
        self, transport: ScriptedTransport
    ) -> None:
        async with make_charger(transport) as charger:
            await charger.async_set_wifi("ExampleNet", "secret")
        writes = transport.writes
        # 8 SSID chunks (0x63), 8 password chunks (0x61), 1 commit (0x3B)
        assert len(writes) == 8 + 8 + 1
        assert writes[0] == b"\x10\xac\x63\x01Exam"
        assert writes[8] == b"\x10\xac\x61\x01secr"
        assert writes[-1].hex() == "10ac3b0101000000"
        ssid = b"".join(w[4:] for w in writes[0:8])
        assert ssid == b"ExampleNet".ljust(32, b"\x00")

    @pytest.mark.parametrize("field", ["ssid", "password"])
    async def test_wifi_rejects_over_long_fields(
        self, transport: ScriptedTransport, field: str
    ) -> None:
        values = {"ssid": "a", "password": "b"}
        values[field] = "x" * 33
        async with make_charger(transport) as charger:
            with pytest.raises(SpinEvValueError, match="too long"):
                await charger.async_set_wifi(values["ssid"], values["password"])
        assert transport.writes == []

    @pytest.mark.parametrize("field", ["ssid", "password"])
    async def test_wifi_rejects_double_quotes(
        self, transport: ScriptedTransport, field: str
    ) -> None:
        values = {"ssid": "a", "password": "b"}
        values[field] = 'has"quote'
        async with make_charger(transport) as charger:
            with pytest.raises(SpinEvValueError, match="double quote"):
                await charger.async_set_wifi(values["ssid"], values["password"])
        assert transport.writes == []

    async def test_reads_load_balancing_config(
        self, transport: ScriptedTransport
    ) -> None:
        transport.replies = {
            Register.LOAD_BALANCING_ENABLED: reply(
                Register.LOAD_BALANCING_ENABLED, bytes.fromhex("00000001")
            ),
            Register.GRID_CURRENT_LIMIT: reply(
                Register.GRID_CURRENT_LIMIT, bytes.fromhex("42480000")
            ),
            Register.SAFE_CURRENT_OFFSET: reply(
                Register.SAFE_CURRENT_OFFSET, bytes.fromhex("40a00000")
            ),
            Register.REDUCE_CURRENT_OFFSET: reply(
                Register.REDUCE_CURRENT_OFFSET, bytes.fromhex("40000000")
            ),
            Register.MAX_GRID_POWER: reply(
                Register.MAX_GRID_POWER, bytes.fromhex("45895440")
            ),
            Register.LOAD_BALANCING_SOURCE: reply(
                Register.LOAD_BALANCING_SOURCE, bytes.fromhex("00000001")
            ),
            Register.LOAD_BALANCING_PRIORITY: reply(
                Register.LOAD_BALANCING_PRIORITY, bytes.fromhex("00000002")
            ),
        }
        async with make_charger(transport) as charger:
            config = await charger.async_get_load_balancing()
        assert config == LoadBalancingConfig(
            enabled=True,
            grid_current_limit_a=50.0,
            safe_current_offset_a=5.0,
            reduce_current_offset_a=2.0,
            max_grid_power_w=4394.53125,
            source=1,
            priority=2,
        )

    async def test_load_balancing_reports_when_it_is_off(
        self, transport: ScriptedTransport
    ) -> None:
        zero = bytes.fromhex("00000000")
        transport.replies = {
            register: reply(register, zero)
            for register in (
                Register.LOAD_BALANCING_ENABLED,
                Register.GRID_CURRENT_LIMIT,
                Register.SAFE_CURRENT_OFFSET,
                Register.REDUCE_CURRENT_OFFSET,
                Register.MAX_GRID_POWER,
                Register.LOAD_BALANCING_SOURCE,
                Register.LOAD_BALANCING_PRIORITY,
            )
        }
        async with make_charger(transport) as charger:
            config = await charger.async_get_load_balancing()
        assert config.enabled is False

    async def test_reads_ocpp_config(self, transport: ScriptedTransport) -> None:
        transport.replies = {
            Register.OCPP_HOST: string_reply(
                Register.OCPP_HOST, "dns:ocpp.example.com"
            ),
            Register.OCPP_PORT: reply(Register.OCPP_PORT, bytes.fromhex("000001bb")),
            Register.OCPP_PATH: string_reply(Register.OCPP_PATH, "ocpp"),
            Register.OCPP_CHARGE_POINT_ID: string_reply(
                Register.OCPP_CHARGE_POINT_ID, "CP0001"
            ),
        }
        async with make_charger(transport) as charger:
            config = await charger.async_get_ocpp_config()
        assert config == OcppConfig(
            host="dns:ocpp.example.com",
            port=443,
            path="ocpp",
            charge_point_id="CP0001",
        )

    async def test_writes_ocpp_config_in_full(
        self, transport: ScriptedTransport
    ) -> None:
        transport.replies = {
            Register.OCPP_HOST: string_reply(Register.OCPP_HOST, "dns:example.com"),
            Register.OCPP_PORT: reply(Register.OCPP_PORT, bytes.fromhex("00000050")),
            Register.OCPP_PATH: string_reply(Register.OCPP_PATH, "ocpp"),
            Register.OCPP_CHARGE_POINT_ID: string_reply(
                Register.OCPP_CHARGE_POINT_ID, "CP0001"
            ),
        }
        config = OcppConfig(
            host="dns:example.com", port=80, path="ocpp", charge_point_id="CP0001"
        )
        async with make_charger(transport) as charger:
            await charger.async_set_ocpp_config(config)
        writes = transport.writes
        registers = [w[2] for w in writes]
        assert registers == (
            [Register.OCPP_HOST] * 16
            + [Register.OCPP_PORT]
            + [Register.OCPP_PATH] * 16
            + [Register.OCPP_CHARGE_POINT_ID] * 8
            + [Register.COMMIT]
        )
        assert writes[16].hex() == "10ac5e0100000050"
        host = b"".join(w[4:] for w in writes[0:16])
        assert host == b"dns:example.com".ljust(64, b"\x00")

    @pytest.mark.parametrize("port", [0, -1, 65536, 999999])
    async def test_ocpp_rejects_impossible_ports(
        self, transport: ScriptedTransport, port: int
    ) -> None:
        config = OcppConfig(
            host="dns:example.com", port=port, path="ocpp", charge_point_id="CP0001"
        )
        async with make_charger(transport) as charger:
            with pytest.raises(SpinEvValueError, match="port"):
                await charger.async_set_ocpp_config(config)
        assert transport.writes == []

    async def test_ocpp_rejects_an_empty_host(
        self, transport: ScriptedTransport
    ) -> None:
        config = OcppConfig(host="", port=80, path="ocpp", charge_point_id="CP0001")
        async with make_charger(transport) as charger:
            with pytest.raises(SpinEvValueError, match="host"):
                await charger.async_set_ocpp_config(config)
        assert transport.writes == []

    async def test_sets_and_reads_timezone(self, transport: ScriptedTransport) -> None:
        transport.replies = {
            Register.TIMEZONE: reply(Register.TIMEZONE, bytes.fromhex("0000051e")),
        }
        async with make_charger(transport) as charger:
            await charger.async_set_timezone(5, 30)
            assert await charger.async_get_timezone() == (5, 30)
        assert transport.writes[0].hex() == "10ac78010000051e"
        assert transport.writes[1].hex() == "10ac3b0101000000"

    async def test_sets_random_delay_then_commits(
        self, transport: ScriptedTransport
    ) -> None:
        async with make_charger(transport) as charger:
            await charger.async_set_random_delay(600)
        assert transport.writes[0].hex() == "10acc20100000258"
        assert transport.writes[-1].hex() == "10ac3b0101000000"

    async def test_random_delay_rejects_out_of_range(
        self, transport: ScriptedTransport
    ) -> None:
        async with make_charger(transport) as charger:
            with pytest.raises(SpinEvValueError):
                await charger.async_set_random_delay(1801)
        assert transport.writes == []

    async def test_syncs_clock(self, transport: ScriptedTransport) -> None:
        async with make_charger(transport) as charger:
            await charger.async_sync_clock(datetime(2026, 8, 5, 12, 39, 12))
        # time 00 SS MM HH, date 00 DD MM YY (month 0-based, year-1900)
        assert transport.writes[0].hex() == "10ac3f01000c270c"
        assert transport.writes[1].hex() == "10ac40010005077e"
        assert transport.writes[2].hex() == "10ac3b0101000000"


class TestHistory:
    RECORD_A = "7c03040500237801020023090f1e00237801020023000004d225"
    RECORD_B = "7c0102030023780103002304050600237801030023000002b725"

    async def test_decodes_and_deduplicates(self, transport: ScriptedTransport) -> None:
        transport.bulk = [
            bytes.fromhex(self.RECORD_A),
            bytes.fromhex(self.RECORD_B),
            bytes.fromhex(self.RECORD_A),  # the charger repeats records
        ]
        async with make_charger(transport, timeout=0.2) as charger:
            sessions = await charger.async_get_history(2)
        assert len(sessions) == 2
        assert sessions[0].start == datetime(2020, 1, 2, 3, 4, 5)
        assert sessions[0].energy_kwh == pytest.approx(12.34)
        assert sessions[1].start == datetime(2020, 1, 3, 1, 2, 3)
        assert transport.writes[0].hex() == "10ac680000000002"

    async def test_malformed_records_are_skipped(
        self, transport: ScriptedTransport
    ) -> None:
        transport.bulk = [
            bytes.fromhex(self.RECORD_A),
            b"\x7c" + b"\x00" * 24 + b"\x25",  # right shape, impossible date
        ]
        async with make_charger(transport, timeout=0.2) as charger:
            sessions = await charger.async_get_history()
        assert len(sessions) == 1

    async def test_empty_history_is_not_an_error(
        self, transport: ScriptedTransport
    ) -> None:
        async with make_charger(transport, timeout=0.2) as charger:
            assert await charger.async_get_history() == []

    async def test_history_before_connecting_is_rejected(
        self, transport: ScriptedTransport
    ) -> None:
        with pytest.raises(SpinEvConnectionError, match="not connected"):
            await make_charger(transport).async_get_history()


class TestCommitBatching:
    """Applying configuration restarts the charger, so it is controllable."""

    async def test_setters_commit_by_default(
        self, transport: ScriptedTransport
    ) -> None:
        async with make_charger(transport) as charger:
            await charger.async_set_timezone(1, 30)
        assert transport.writes[-1].hex() == "10ac3b0101000000"

    async def test_commit_can_be_deferred_and_batched(
        self, transport: ScriptedTransport
    ) -> None:
        async with make_charger(transport) as charger:
            await charger.async_set_timezone(1, 30, commit=False)
            await charger.async_set_random_delay(600, commit=False)
            commits_before = [
                w for w in transport.writes if w.hex() == "10ac3b0101000000"
            ]
            await charger.async_commit()
        # Two settings, one restart, rather than one restart each.
        assert commits_before == []
        assert [w for w in transport.writes if w.hex() == "10ac3b0101000000"] == [
            bytes.fromhex("10ac3b0101000000")
        ]

    async def test_current_limit_does_not_commit_unless_asked(
        self, transport: ScriptedTransport
    ) -> None:
        transport.replies = {
            Register.STATE: reply(Register.STATE, bytes.fromhex("00000002")),
            Register.CURRENT_LIMIT: reply(
                Register.CURRENT_LIMIT,
                bytes.fromhex("41800000"),
                flag=Operation.WRITE,
            ),
        }
        async with make_charger(transport) as charger:
            await charger.async_set_current_limit(16.0)
            assert not any(w.hex() == "10ac3b0101000000" for w in transport.writes)
            await charger.async_set_current_limit(16.0, commit=True)
        assert transport.writes[-1].hex() == "10ac3b0101000000"


class TestCurrentLimitDuringASession:
    """The charger refuses a limit change while a session is open."""

    LIMIT_REPLY = reply(
        Register.CURRENT_LIMIT, bytes.fromhex("41800000"), flag=Operation.WRITE
    )

    @pytest.mark.parametrize(
        "state",
        [
            ChargerState.STARTING,
            ChargerState.CHARGING,
            ChargerState.EVSE_SUSPENDED,
            ChargerState.EV_SUSPENDED,
        ],
    )
    async def test_refused_while_a_session_is_open(
        self,
        transport: ScriptedTransport,
        state: ChargerState,
    ) -> None:
        transport.replies = {
            Register.STATE: reply(Register.STATE, int(state).to_bytes(4, "big")),
            Register.CURRENT_LIMIT: self.LIMIT_REPLY,
        }
        async with make_charger(transport) as charger:
            with pytest.raises(SpinEvBusyError, match="during a session"):
                await charger.async_set_current_limit(16.0)
        # Nothing reached the charger.
        assert not any(w[2] == Register.CURRENT_LIMIT for w in transport.writes)

    @pytest.mark.parametrize(
        "state", [ChargerState.AVAILABLE, ChargerState.IDLE, ChargerState.FINISHING]
    )
    async def test_allowed_between_sessions(
        self,
        transport: ScriptedTransport,
        state: ChargerState,
    ) -> None:
        transport.replies = {
            Register.STATE: reply(Register.STATE, int(state).to_bytes(4, "big")),
            Register.CURRENT_LIMIT: self.LIMIT_REPLY,
        }
        async with make_charger(transport) as charger:
            await charger.async_set_current_limit(16.0)
        assert transport.writes[-1][:4].hex() == "10ac4f01"

    async def test_override_writes_anyway(self, transport: ScriptedTransport) -> None:
        transport.replies = {
            Register.STATE: reply(Register.STATE, bytes.fromhex("00000004")),
            Register.CURRENT_LIMIT: self.LIMIT_REPLY,
        }
        async with make_charger(transport) as charger:
            await charger.async_set_current_limit(16.0, allow_while_charging=True)
        assert transport.writes[-1][:4].hex() == "10ac4f01"
        # The override also skips the state read the check would have done.
        assert not any(w[2] == Register.STATE for w in transport.writes)

    async def test_an_unknown_state_does_not_block_the_write(
        self, transport: ScriptedTransport
    ) -> None:
        """A state this library does not name must not become an exception."""
        transport.replies = {
            Register.STATE: reply(Register.STATE, bytes.fromhex("000000ff")),
            Register.CURRENT_LIMIT: self.LIMIT_REPLY,
        }
        async with make_charger(transport) as charger:
            await charger.async_set_current_limit(16.0)
        assert transport.writes[-1][:4].hex() == "10ac4f01"
