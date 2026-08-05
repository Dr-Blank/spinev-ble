"""Tests for the argument handling in the command line tool.

Nothing here touches Bluetooth. The commands themselves are covered by the
client tests.
"""

from __future__ import annotations

import argparse

import pytest

from spinev_ble.__main__ import (
    PASSWORD_ENV_VAR,
    _build_parser,
    _default_password,
    _format,
    _parse_password,
)
from spinev_ble.const import DEFAULT_HISTORY_COUNT

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
        "command", ["scan", "password", "status", "start", "stop", "history"]
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
