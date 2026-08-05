"""Client tests, driven by the scripted transport in ``conftest``.

No hardware and no Bluetooth adapter are involved.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest

from spinev_ble import (
    ChargerState,
    OcppConfig,
    Operation,
    Register,
    SpinEvConnectionError,
    SpinEvProtocolError,
    SpinEvTimeoutError,
    SpinEvValueError,
)
from spinev_ble.client import BleakClientLike, SpinEvCharger

from .conftest import FAKE_DEVICE, FakeTransport, reply, string_reply

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
    client_class: Callable[..., Any],
    password: int | None = DUMMY_PASSWORD,
    timeout: float = 1.0,
) -> SpinEvCharger:
    return SpinEvCharger(
        FAKE_DEVICE, password, timeout=timeout, client_class=client_class
    )


class TestTransportContract:
    def test_bleak_client_satisfies_the_protocol(
        self, transport: FakeTransport
    ) -> None:
        assert isinstance(transport, BleakClientLike)

    async def test_context_manager_connects_and_disconnects(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        charger = make_charger(client_class)
        async with charger:
            assert charger.is_connected
            assert transport.connect_calls == 1
            assert transport.notify_callback is not None
        # Checked on the transport rather than the charger property, which mypy
        # keeps narrowed from the assertion above.
        assert not transport.is_connected
        assert transport.disconnect_calls == 1

    async def test_connect_is_idempotent(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        charger = make_charger(client_class)
        await charger.async_connect()
        await charger.async_connect()
        assert transport.connect_calls == 1
        await charger.async_disconnect()

    async def test_disconnect_without_connect_is_safe(
        self, client_class: Callable[..., Any]
    ) -> None:
        await make_charger(client_class).async_disconnect()

    async def test_reads_before_connecting_are_rejected(
        self, client_class: Callable[..., Any]
    ) -> None:
        charger = make_charger(client_class)
        with pytest.raises(SpinEvConnectionError, match="not connected"):
            await charger.async_get_power()

    async def test_timeout_names_the_register_and_the_likely_cause(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        transport.replies = {}
        charger = make_charger(client_class, timeout=0.05)
        async with charger:
            with pytest.raises(SpinEvTimeoutError) as excinfo:
                await charger.async_get_power()
        assert "0x84" in str(excinfo.value)
        assert "phone app" in str(excinfo.value)

    async def test_dropped_link_fails_fast_instead_of_waiting(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        """A disconnect mid request must not make the caller sit out the timeout."""
        charger = make_charger(client_class, timeout=30.0)
        async with charger:
            transport.drop_on_write = True
            with pytest.raises(SpinEvConnectionError, match="disconnected"):
                await charger.async_get_power()

    async def test_short_reply_is_reported_as_a_protocol_error(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        transport.replies = {Register.POWER: reply(Register.POWER, b"\x01\x02")}
        charger = make_charger(client_class)
        async with charger:
            with pytest.raises(SpinEvProtocolError, match="short reply"):
                await charger.async_get_power()

    async def test_unmatched_notifications_are_ignored(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        charger = make_charger(client_class)
        transport.replies = dict(STATUS_REPLIES)
        async with charger:
            transport.notify(b"garbage")
            transport.notify(reply(Register.VOLTAGE, bytes.fromhex("438003d7")))
            assert await charger.async_get_power() == pytest.approx(3859.84, abs=0.01)

    async def test_stray_write_echo_does_not_corrupt_a_pending_read(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        """A write's echo to a register must not answer an unrelated read of it.

        The charger echoes every accepted frame back, including the chunks
        of a fire-and-forget string write, whose sequence number sits in the
        byte a read reply would use for its flag. If a reply were matched on
        the register alone, a chunk echo left in flight from an earlier
        write could be handed to a later read waiting on the same register,
        silently returning the wrong value instead of the string just read.
        """
        async with make_charger(client_class) as charger:
            read_task = asyncio.ensure_future(charger.async_get_wifi_ssid())
            await asyncio.sleep(0.01)  # let the read register its waiter

            # A leftover echo from a WiFi SSID write chunk: same register,
            # but the flag byte holds a chunk sequence number, not 0x00.
            transport.notify(reply(Register.WIFI_SSID, b"Evil", flag=1))
            assert not read_task.done()

            transport.notify(string_reply(Register.WIFI_SSID, "ExampleNet"))
            assert await read_task == "ExampleNet"


class TestTelemetry:
    async def test_individual_reads(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        transport.replies = dict(STATUS_REPLIES)
        async with make_charger(client_class) as charger:
            assert await charger.async_get_state() is ChargerState.CHARGING
            assert await charger.async_get_power() == pytest.approx(3859.84, abs=0.01)
            assert await charger.async_get_voltage() == pytest.approx(256.03, abs=0.01)
            assert await charger.async_get_current() == pytest.approx(15.05, abs=0.01)
            assert await charger.async_get_current_limit() == pytest.approx(16.0)
            assert await charger.async_get_alarms() == ["Mains Fail", "Earth Leakage"]

    async def test_status_reads_every_field(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        transport.replies = dict(STATUS_REPLIES)
        async with make_charger(client_class) as charger:
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
        assert status.alarms == ("Mains Fail", "Earth Leakage")
        assert status.has_alarms

    async def test_status_survives_an_unknown_state(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        """A charger in a state this library does not name must not fail the read."""
        transport.replies = dict(STATUS_REPLIES)
        transport.replies[Register.STATE] = reply(
            Register.STATE, bytes.fromhex("00000063")
        )
        async with make_charger(client_class) as charger:
            status = await charger.async_get_status()
        assert status.state is None
        assert status.state_value == 0x63
        assert not status.is_charging
        assert status.power_w is not None

    async def test_unknown_state_raises_on_the_enum_accessor(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        transport.replies = {
            Register.STATE: reply(Register.STATE, bytes.fromhex("00000063"))
        }
        async with make_charger(client_class) as charger:
            with pytest.raises(SpinEvProtocolError, match="unknown charger state 99"):
                await charger.async_get_state()
            assert await charger.async_get_state_value() == 99

    async def test_status_is_immutable(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        transport.replies = dict(STATUS_REPLIES)
        async with make_charger(client_class) as charger:
            status = await charger.async_get_status()
        with pytest.raises(AttributeError):
            status.power_w = 0.0  # type: ignore[misc]


class TestControl:
    async def test_start_and_stop_send_the_password(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        transport.replies = {
            Register.CONTROL: reply(
                Register.CONTROL, bytes.fromhex("00000000"), flag=Operation.WRITE
            )
        }
        async with make_charger(client_class) as charger:
            await charger.async_start_charging()
            await charger.async_stop_charging()
        assert transport.writes[0].hex() == "10ac3c0101abcdef"
        assert transport.writes[1].hex() == "10ac3c0110abcdef"

    async def test_password_is_read_from_the_charger_when_not_supplied(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        transport.replies = {
            Register.PASSWORD: reply(Register.PASSWORD, bytes.fromhex("00abcdef")),
            Register.CONTROL: reply(
                Register.CONTROL, bytes.fromhex("00000000"), flag=Operation.WRITE
            ),
        }
        async with make_charger(client_class, password=None) as charger:
            assert await charger.async_get_password() == DUMMY_PASSWORD
            await charger.async_start_charging()
            await charger.async_stop_charging()
        # Read once on first use, then cached rather than re-read.
        assert sum(1 for w in transport.writes if w[2] == Register.PASSWORD) == 2
        assert transport.writes[-1].hex() == "10ac3c0110abcdef"

    async def test_password_high_byte_is_masked_off(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        transport.replies = {
            Register.PASSWORD: reply(Register.PASSWORD, bytes.fromhex("ffabcdef"))
        }
        async with make_charger(client_class, password=None) as charger:
            assert await charger.async_get_password() == DUMMY_PASSWORD

    @pytest.mark.parametrize("amps", [16.0, 6.0, 32.0])
    async def test_current_limit_accepts_the_supported_range(
        self, transport: FakeTransport, client_class: Callable[..., Any], amps: float
    ) -> None:
        transport.replies = {
            Register.CURRENT_LIMIT: reply(
                Register.CURRENT_LIMIT,
                bytes.fromhex("41800000"),
                flag=Operation.WRITE,
            )
        }
        async with make_charger(client_class) as charger:
            await charger.async_set_current_limit(amps)
        assert transport.writes[-1][:4].hex() == "10ac4f01"

    @pytest.mark.parametrize("amps", [5.9, 0.0, -1.0, 32.1, 100.0])
    async def test_current_limit_rejects_out_of_range(
        self, transport: FakeTransport, client_class: Callable[..., Any], amps: float
    ) -> None:
        async with make_charger(client_class) as charger:
            with pytest.raises(SpinEvValueError, match="out of range"):
                await charger.async_set_current_limit(amps)
        assert transport.writes == []

    async def test_current_limit_ceiling_is_adjustable_per_model(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        transport.replies = {
            Register.CURRENT_LIMIT: reply(
                Register.CURRENT_LIMIT,
                bytes.fromhex("41800000"),
                flag=Operation.WRITE,
            )
        }
        async with make_charger(client_class) as charger:
            await charger.async_set_current_limit(40.0, max_amps=63.0)
            with pytest.raises(SpinEvValueError):
                await charger.async_set_current_limit(40.0, max_amps=32.0)

    async def test_reboot_sends_the_commit_frame(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        async with make_charger(client_class) as charger:
            await charger.async_reboot()
        assert transport.writes[-1].hex() == "10ac3b0101000000"


class TestNetworkConfig:
    async def test_reads_wifi_settings(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        transport.replies = {
            Register.WIFI_SSID: string_reply(Register.WIFI_SSID, "ExampleNet"),
            Register.WIFI_PASSWORD: string_reply(Register.WIFI_PASSWORD, "hunter2"),
        }
        async with make_charger(client_class) as charger:
            assert await charger.async_get_wifi_ssid() == "ExampleNet"
            assert await charger.async_get_wifi_password() == "hunter2"

    async def test_writes_wifi_chunked_then_commits(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        async with make_charger(client_class) as charger:
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
        self, transport: FakeTransport, client_class: Callable[..., Any], field: str
    ) -> None:
        values = {"ssid": "a", "password": "b"}
        values[field] = "x" * 33
        async with make_charger(client_class) as charger:
            with pytest.raises(SpinEvValueError, match="too long"):
                await charger.async_set_wifi(values["ssid"], values["password"])
        assert transport.writes == []

    @pytest.mark.parametrize("field", ["ssid", "password"])
    async def test_wifi_rejects_double_quotes(
        self, transport: FakeTransport, client_class: Callable[..., Any], field: str
    ) -> None:
        values = {"ssid": "a", "password": "b"}
        values[field] = 'has"quote'
        async with make_charger(client_class) as charger:
            with pytest.raises(SpinEvValueError, match="double quote"):
                await charger.async_set_wifi(values["ssid"], values["password"])
        assert transport.writes == []

    async def test_reads_ocpp_config(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
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
        async with make_charger(client_class) as charger:
            config = await charger.async_get_ocpp_config()
        assert config == OcppConfig(
            host="dns:ocpp.example.com",
            port=443,
            path="ocpp",
            charge_point_id="CP0001",
        )

    async def test_writes_ocpp_config_in_full(
        self, transport: FakeTransport, client_class: Callable[..., Any]
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
        async with make_charger(client_class) as charger:
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
        self, transport: FakeTransport, client_class: Callable[..., Any], port: int
    ) -> None:
        config = OcppConfig(
            host="dns:example.com", port=port, path="ocpp", charge_point_id="CP0001"
        )
        async with make_charger(client_class) as charger:
            with pytest.raises(SpinEvValueError, match="port"):
                await charger.async_set_ocpp_config(config)
        assert transport.writes == []

    async def test_ocpp_rejects_an_empty_host(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        config = OcppConfig(host="", port=80, path="ocpp", charge_point_id="CP0001")
        async with make_charger(client_class) as charger:
            with pytest.raises(SpinEvValueError, match="host"):
                await charger.async_set_ocpp_config(config)
        assert transport.writes == []

    async def test_sets_and_reads_timezone(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        transport.replies = {
            Register.TIMEZONE: reply(Register.TIMEZONE, bytes.fromhex("0000051e")),
        }
        async with make_charger(client_class) as charger:
            await charger.async_set_timezone(5, 30)
            assert await charger.async_get_timezone() == (5, 30)
        assert transport.writes[0].hex() == "10ac78010000051e"
        assert transport.writes[1].hex() == "10ac3b0101000000"

    async def test_sets_random_delay_then_commits(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        async with make_charger(client_class) as charger:
            await charger.async_set_random_delay(600)
        assert transport.writes[0].hex() == "10acc20100000258"
        assert transport.writes[-1].hex() == "10ac3b0101000000"

    async def test_random_delay_rejects_out_of_range(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        async with make_charger(client_class) as charger:
            with pytest.raises(SpinEvValueError):
                await charger.async_set_random_delay(1801)
        assert transport.writes == []

    async def test_syncs_clock(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        async with make_charger(client_class) as charger:
            await charger.async_sync_clock(datetime(2026, 8, 5, 12, 39, 12))
        # time 00 SS MM HH, date 00 DD MM YY (month 0-based, year-1900)
        assert transport.writes[0].hex() == "10ac3f01000c270c"
        assert transport.writes[1].hex() == "10ac40010005077e"
        assert transport.writes[2].hex() == "10ac3b0101000000"


class TestHistory:
    RECORD_A = "7c03040500237801020023090f1e00237801020023000004d225"
    RECORD_B = "7c0102030023780103002304050600237801030023000002b725"

    async def test_decodes_and_deduplicates(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        transport.bulk = [
            bytes.fromhex(self.RECORD_A),
            bytes.fromhex(self.RECORD_B),
            bytes.fromhex(self.RECORD_A),  # the charger repeats records
        ]
        async with make_charger(client_class, timeout=0.2) as charger:
            sessions = await charger.async_get_history(2)
        assert len(sessions) == 2
        assert sessions[0].start == datetime(2020, 1, 2, 3, 4, 5)
        assert sessions[0].energy_kwh == pytest.approx(12.34)
        assert sessions[1].start == datetime(2020, 1, 3, 1, 2, 3)
        assert transport.writes[0].hex() == "10ac680000000002"

    async def test_malformed_records_are_skipped(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        transport.bulk = [
            bytes.fromhex(self.RECORD_A),
            b"\x7c" + b"\x00" * 24 + b"\x25",  # right shape, impossible date
        ]
        async with make_charger(client_class, timeout=0.2) as charger:
            sessions = await charger.async_get_history()
        assert len(sessions) == 1

    async def test_empty_history_is_not_an_error(
        self, transport: FakeTransport, client_class: Callable[..., Any]
    ) -> None:
        async with make_charger(client_class, timeout=0.2) as charger:
            assert await charger.async_get_history() == []

    async def test_history_before_connecting_is_rejected(
        self, client_class: Callable[..., Any]
    ) -> None:
        with pytest.raises(SpinEvConnectionError, match="not connected"):
            await make_charger(client_class).async_get_history()
