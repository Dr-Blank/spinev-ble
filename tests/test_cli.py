"""Tests for the argument handling in the command line tool.

Nothing here touches Bluetooth. The commands themselves are covered by the
charger client and transport tests.
"""

from __future__ import annotations

import argparse

import pytest

from spinev_ble.__main__ import (
    PASSWORD_ENV_VAR,
    _build_parser,
    _charger_host,
    _default_password,
    _format,
    _ocpp_url,
    _parse_password,
    _wanted_ocpp,
)
from spinev_ble.const import DEFAULT_HISTORY_COUNT
from spinev_ble.models import OcppConfig

DUMMY_PASSWORD = 0xABCDEF


class TestPasswordArgument:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [("0xABCDEF", 0xABCDEF), ("0xabcdef", 0xABCDEF), ("11259375", 11259375)],
    )
    def test_accepts_hex_and_decimal(self, text: str, expected: int) -> None:
        assert _parse_password(text) == expected

    @pytest.mark.parametrize("text", ["", "nonsense", "12.5", "0xZZ"])
    def test_rejects_non_integers(self, text: str) -> None:
        with pytest.raises(argparse.ArgumentTypeError, match="must be an integer"):
            _parse_password(text)

    def test_environment_variable_supplies_a_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keeping the credential out of argv keeps it out of shell history."""
        monkeypatch.setenv(PASSWORD_ENV_VAR, "0xABCDEF")
        assert _default_password() == DUMMY_PASSWORD

    def test_no_environment_variable_means_no_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(PASSWORD_ENV_VAR, raising=False)
        assert _default_password() is None

    def test_explicit_flag_wins_over_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(PASSWORD_ENV_VAR, "1")
        args = _build_parser().parse_args(["--password", "0xABCDEF", "status"])
        assert args.password == DUMMY_PASSWORD


class TestParser:
    def test_subcommand_is_required(self) -> None:
        with pytest.raises(SystemExit):
            _build_parser().parse_args([])

    @pytest.mark.parametrize(
        "command", ["scan", "password", "status", "start", "stop", "history", "ocpp"]
    )
    def test_every_command_parses(self, command: str) -> None:
        args = _build_parser().parse_args([command])
        assert args.command == command

    def test_history_count_defaults_to_the_shared_constant(self) -> None:
        args = _build_parser().parse_args(["history"])
        assert args.count == DEFAULT_HISTORY_COUNT

    def test_address_and_timeout_are_optional(self) -> None:
        args = _build_parser().parse_args(
            ["--address", "AA:BB:CC:DD:EE:FF", "--timeout", "5", "status"]
        )
        assert args.address == "AA:BB:CC:DD:EE:FF"
        assert args.timeout == 5.0


class TestFormatting:
    def test_missing_readings_are_named_rather_than_crashing(self) -> None:
        assert _format(None) == "unknown"
        assert _format(None, ".2f") == "unknown"

    def test_values_use_the_given_spec(self) -> None:
        assert _format(3.14159, ".2f") == "3.14"
        assert _format(3600) == "3600"
        assert _format("35.24.4.32") == "35.24.4.32"


VENDOR_OCPP = OcppConfig(
    host="dns:ocpp.example.com", port=9000, path="ocpp", charge_point_id="XY2123"
)


class TestOcpp:
    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            ("192.0.2.10", "192.0.2.10"),
            (" 192.0.2.10 ", "192.0.2.10"),
            ("2001:db8::1", "2001:db8::1"),
            ("homeassistant.local", "dns:homeassistant.local"),
            ("dns:homeassistant.local", "dns:homeassistant.local"),
        ],
    )
    def test_names_are_marked_for_the_charger(self, host: str, expected: str) -> None:
        assert _charger_host(host) == expected

    def test_only_the_host_given_keeps_the_rest(self) -> None:
        args = _build_parser().parse_args(["ocpp", "--host", "192.0.2.10"])
        assert _wanted_ocpp(VENDOR_OCPP, args) == OcppConfig(
            host="192.0.2.10", port=9000, path="ocpp", charge_point_id="XY2123"
        )

    def test_every_setting_can_be_given(self) -> None:
        args = _build_parser().parse_args(
            [
                "ocpp",
                "--host",
                "192.0.2.10",
                "--port",
                "9321",
                "--path",
                "central",
                "--charge-point-id",
                "garage",
            ]
        )
        assert _wanted_ocpp(VENDOR_OCPP, args) == OcppConfig(
            host="192.0.2.10", port=9321, path="central", charge_point_id="garage"
        )

    def test_an_empty_path_gets_the_default(self) -> None:
        args = _build_parser().parse_args(["ocpp", "--host", "192.0.2.10"])
        current = OcppConfig(host="", port=9000, path="", charge_point_id="XY2123")
        assert _wanted_ocpp(current, args).path == "ocpp"

    def test_a_charge_point_id_is_needed(self) -> None:
        args = _build_parser().parse_args(["ocpp", "--host", "192.0.2.10"])
        current = OcppConfig(host="", port=9000, path="", charge_point_id="")
        with pytest.raises(SystemExit, match="--charge-point-id"):
            _wanted_ocpp(current, args)

    def test_reading_needs_no_host(self) -> None:
        args = _build_parser().parse_args(["ocpp"])
        assert args.host is None

    def test_url_shows_the_name_without_its_mark(self) -> None:
        assert _ocpp_url(VENDOR_OCPP) == "ws://ocpp.example.com:9000/ocpp/XY2123"
