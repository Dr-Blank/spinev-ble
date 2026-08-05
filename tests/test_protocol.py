"""Codec tests.

Every byte sequence here is synthetic. Never commit a real charger password,
serial number or capture.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from spinev_ble import (
    ChargerState,
    Command,
    Register,
    SpinEvPasswordError,
    SpinEvProtocolError,
    build_control,
    build_read,
    build_write_float,
    build_write_string,
    build_write_uint,
    decode_alarms,
    decode_energy,
    decode_firmware_version,
    decode_float,
    decode_session_record,
    decode_string,
    decode_uint,
    is_history_record,
    is_reply,
    parse_frame,
)

DUMMY_PASSWORD = 0xABCDEF


class TestBuildFrames:
    def test_read_frame(self) -> None:
        assert build_read(Register.POWER).hex() == "10ac840000000000"
        assert build_read(Register.STATE).hex() == "10ac670000000000"

    def test_read_frame_with_parameter(self) -> None:
        # history fetch asking for 40 records
        assert build_read(Register.HISTORY_SESSIONS, 40).hex() == "10ac680000000028"

    def test_control_frames_use_password(self) -> None:
        start = build_control(Command.START, DUMMY_PASSWORD)
        stop = build_control(Command.STOP, DUMMY_PASSWORD)
        assert start.hex() == "10ac3c0101abcdef"
        assert stop.hex() == "10ac3c0110abcdef"

    def test_control_frames_differ_only_in_command_byte(self) -> None:
        start = build_control(Command.START, DUMMY_PASSWORD)
        stop = build_control(Command.STOP, DUMMY_PASSWORD)
        assert start[:4] == stop[:4]
        assert start[5:] == stop[5:]
        assert start[4] != stop[4]

    def test_write_helpers(self) -> None:
        assert build_write_uint(0x4E, 1).hex() == "10ac4e0100000001"
        assert (
            build_write_float(Register.CURRENT_LIMIT, 16.0).hex() == "10ac4f0141800000"
        )

    @pytest.mark.parametrize("bad", [-1, 0x1000000, "abcdef", None, True])
    def test_bad_password_rejected(self, bad: object) -> None:
        with pytest.raises(SpinEvPasswordError):
            build_control(Command.START, bad)  # type: ignore[arg-type]

    def test_password_error_does_not_echo_a_valid_secret(self) -> None:
        """A rejected value is out of range, so it is safe to show. A valid one
        never reaches the message."""
        with pytest.raises(SpinEvPasswordError) as excinfo:
            build_control(Command.START, 0x1000000)
        assert "three bytes" in str(excinfo.value)


class TestParseFrames:
    def test_parse_valid_frame(self) -> None:
        frame = parse_frame(bytes.fromhex("10ac840045713d71"))
        assert frame is not None
        assert frame.register == Register.POWER
        assert frame.flag == 0x00

    def test_repr_is_readable(self) -> None:
        frame = parse_frame(bytes.fromhex("10ac840045713d71"))
        assert repr(frame) == "Frame(register=0x84, flag=0x00, raw=45713d71)"

    @pytest.mark.parametrize(
        "bad",
        ["", "10ac8400", "10ac840045713d7100", "ffff840045713d71"],
    )
    def test_parse_rejects_non_frames(self, bad: str) -> None:
        assert parse_frame(bytes.fromhex(bad)) is None

    def test_is_reply_accepts_both_lengths(self) -> None:
        assert is_reply(bytes.fromhex("10ac840045713d71"))
        # a text register reply, longer than 8 bytes
        assert is_reply(b"\x10\xac\x61\x00" + b"MyNetwork")

    @pytest.mark.parametrize(
        "bad", [b"", b"\x10\xac", b"\x10\xac\x61\x00", b"\xff\xff"]
    )
    def test_is_reply_rejects_short_and_unheaded(self, bad: bytes) -> None:
        assert not is_reply(bad)


class TestDecodeValues:
    """Values consistent with a charger delivering about 3.86 kW."""

    def test_power(self) -> None:
        assert decode_float(bytes.fromhex("45713d71")) == pytest.approx(
            3859.84, abs=0.01
        )

    def test_voltage(self) -> None:
        assert decode_float(bytes.fromhex("438003d7")) == pytest.approx(
            256.03, abs=0.01
        )

    def test_current(self) -> None:
        assert decode_float(bytes.fromhex("4170b852")) == pytest.approx(15.05, abs=0.01)

    def test_voltage_times_current_matches_power(self) -> None:
        volts = decode_float(bytes.fromhex("438003d7"))
        amps = decode_float(bytes.fromhex("4170b852"))
        watts = decode_float(bytes.fromhex("45713d71"))
        assert volts * amps == pytest.approx(watts, rel=0.01)

    def test_uint(self) -> None:
        assert decode_uint(bytes.fromhex("0001e240")) == 123456

    def test_energy_scaling(self) -> None:
        assert decode_energy(bytes.fromhex("0001e240")) == pytest.approx(1234.56)
        assert decode_energy(bytes.fromhex("00000002")) == pytest.approx(0.02)

    def test_firmware_version(self) -> None:
        assert decode_firmware_version(bytes.fromhex("23180420")) == "35.24.4.32"

    @pytest.mark.parametrize(
        "decode", [decode_float, decode_uint, decode_energy, decode_firmware_version]
    )
    @pytest.mark.parametrize("raw", [b"", b"\x00", b"\x00" * 3, b"\x00" * 5])
    def test_wrong_value_length_rejected(self, decode: object, raw: bytes) -> None:
        with pytest.raises(SpinEvProtocolError):
            decode(raw)  # type: ignore[operator]


class TestChargerState:
    def test_known_states(self) -> None:
        assert ChargerState(2) is ChargerState.IDLE
        assert ChargerState(3) is ChargerState.STARTING
        assert ChargerState(4) is ChargerState.CHARGING

    def test_is_charging(self) -> None:
        assert not ChargerState.AVAILABLE.is_charging
        assert not ChargerState.IDLE.is_charging
        assert ChargerState.STARTING.is_charging
        assert ChargerState.CHARGING.is_charging

    def test_available_state(self) -> None:
        assert ChargerState(1) is ChargerState.AVAILABLE
        assert not ChargerState.AVAILABLE.has_vehicle
        assert ChargerState.IDLE.has_vehicle
        assert ChargerState.CHARGING.has_vehicle

    def test_unknown_state_is_not_named(self) -> None:
        with pytest.raises(ValueError, match="99"):
            ChargerState(99)


class TestHistory:
    # A synthetic session: 2020-01-02 03:04:05 to 09:15:30, 12.34 kWh.
    RECORD = "7c03040500237801020023090f1e00237801020023000004d225"

    def test_decode_session(self) -> None:
        session = decode_session_record(bytes.fromhex(self.RECORD))
        assert session is not None
        assert session.start == datetime(2020, 1, 2, 3, 4, 5)
        assert session.end == datetime(2020, 1, 2, 9, 15, 30)
        assert session.energy_kwh == pytest.approx(12.34)
        assert session.duration.total_seconds() == pytest.approx(22285)

    def test_rejects_wrong_length(self) -> None:
        assert decode_session_record(bytes.fromhex("7c172a0e25")) is None

    def test_rejects_bad_markers(self) -> None:
        bad = "00" + self.RECORD[2:]
        assert decode_session_record(bytes.fromhex(bad)) is None

    def test_rejects_impossible_date(self) -> None:
        # month 0x63 is not a valid month
        bad = bytearray.fromhex(self.RECORD)
        bad[7] = 0x63
        assert decode_session_record(bytes(bad)) is None

    def test_is_history_record(self) -> None:
        assert is_history_record(bytes.fromhex(self.RECORD))
        assert not is_history_record(bytes.fromhex("10ac840045713d71"))


class TestAlarms:
    def test_no_alarms(self) -> None:
        assert decode_alarms(0) == []

    def test_single_alarm(self) -> None:
        assert decode_alarms(0x000001) == ["Mains Fail"]
        assert decode_alarms(0x020000) == ["Temperature High"]

    def test_multiple_alarms(self) -> None:
        alarms = decode_alarms(0x000021)
        assert "Mains Fail" in alarms
        assert "Earth Leakage" in alarms
        assert len(alarms) == 2

    def test_reserved_bit_ignored(self) -> None:
        assert decode_alarms(0x000800) == []


class TestStringRegisters:
    def test_build_write_string(self) -> None:
        # 10 AC <reg> 01 then the raw ASCII bytes.
        frame = build_write_string(Register.WIFI_SSID, "MyNet")
        assert frame.hex() == "10ac6101" + b"MyNet".hex()
        assert frame[:4].hex() == "10ac6101"
        assert frame[4:] == b"MyNet"

    def test_build_write_string_ocpp_host(self) -> None:
        frame = build_write_string(Register.OCPP_HOST, "ws.example.org")
        assert frame[:4].hex() == "10ac6401"
        assert frame[4:] == b"ws.example.org"

    def test_build_write_string_rejects_non_ascii(self) -> None:
        with pytest.raises(SpinEvProtocolError):
            build_write_string(Register.WIFI_SSID, "café")

    def test_decode_string_strips_padding(self) -> None:
        # Value bytes as they arrive after the 4 byte header, zero padded.
        raw = b"HomeWiFi" + b"\x00" * 12
        assert decode_string(raw) == "HomeWiFi"

    def test_decode_string_trims_whitespace(self) -> None:
        assert decode_string(b"  edge  \x00\x00") == "edge"

    def test_decode_string_empty(self) -> None:
        assert decode_string(b"\x00\x00\x00\x00") == ""
