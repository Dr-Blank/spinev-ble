"""Local Bluetooth LE control for Exicom Spin EV chargers.

The core of this package is a dependency free codec. It turns charger commands
into bytes and bytes back into values, and never touches a radio. How those
bytes reach the charger is up to you: bleak, an ESPHome proxy, a serial bridge,
or nothing at all if you only want to inspect frames.

Pure codec, no dependencies::

    from spinev_ble import Command, build_control

    frame = build_control(Command.START, password=0xABCDEF)
    # send `frame` however you like

Optional convenience client, needs ``pip install spinev-ble[bleak]``::

    from spinev_ble import SpinEvCharger
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

from .const import (
    ADVERTISED_NAME_PATTERN,
    CHARACTERISTIC_UUID,
    CONTROL_REJECTED,
    DEFAULT_HISTORY_COUNT,
    DEFAULT_MAX_CURRENT_A,
    DEFAULT_TIMEOUT,
    MAX_PORT,
    MAX_RANDOM_DELAY_S,
    MAX_WIFI_FIELD_LEN,
    MIN_CURRENT_A,
    OCPP_ID_FIELD_BYTES,
    OCPP_TEXT_FIELD_BYTES,
    SERVICE_UUID,
    WIFI_FIELD_BYTES,
    ChargerState,
    Command,
    Operation,
    Register,
)
from .exceptions import (
    SpinEvBusyError,
    SpinEvCommandRejectedError,
    SpinEvConnectionError,
    SpinEvError,
    SpinEvPasswordError,
    SpinEvProtocolError,
    SpinEvTimeoutError,
    SpinEvValueError,
)
from .models import (
    ChargerStatus,
    ChargingSession,
    Frame,
    LoadBalancingConfig,
    OcppConfig,
)
from .protocol import (
    ALARM_BITS,
    build_clock_date,
    build_clock_time,
    build_commit,
    build_control,
    build_read,
    build_string_write,
    build_timezone,
    build_write_float,
    build_write_uint,
    check_control_reply,
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

if TYPE_CHECKING:
    from .client import BleakClientLike, SpinEvCharger

try:
    __version__ = version("spinev-ble")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0"

__all__ = [
    "ADVERTISED_NAME_PATTERN",
    "ALARM_BITS",
    "CHARACTERISTIC_UUID",
    "CONTROL_REJECTED",
    "DEFAULT_HISTORY_COUNT",
    "DEFAULT_MAX_CURRENT_A",
    "DEFAULT_TIMEOUT",
    "MAX_PORT",
    "MAX_RANDOM_DELAY_S",
    "MAX_WIFI_FIELD_LEN",
    "MIN_CURRENT_A",
    "OCPP_ID_FIELD_BYTES",
    "OCPP_TEXT_FIELD_BYTES",
    "SERVICE_UUID",
    "WIFI_FIELD_BYTES",
    "BleakClientLike",
    "ChargerState",
    "ChargerStatus",
    "ChargingSession",
    "Command",
    "Frame",
    "LoadBalancingConfig",
    "OcppConfig",
    "Operation",
    "Register",
    "SpinEvBusyError",
    "SpinEvCharger",
    "SpinEvCommandRejectedError",
    "SpinEvConnectionError",
    "SpinEvError",
    "SpinEvPasswordError",
    "SpinEvProtocolError",
    "SpinEvTimeoutError",
    "SpinEvValueError",
    "__version__",
    "build_clock_date",
    "build_clock_time",
    "build_commit",
    "build_control",
    "build_read",
    "build_string_write",
    "build_timezone",
    "build_write_float",
    "build_write_uint",
    "check_control_reply",
    "decode_alarms",
    "decode_energy",
    "decode_firmware_version",
    "decode_float",
    "decode_session_record",
    "decode_string",
    "decode_uint",
    "is_history_record",
    "is_reply",
    "parse_frame",
]

#: Names that live in the optional bleak backed client module.
_LAZY = frozenset({"BleakClientLike", "SpinEvCharger"})


def __getattr__(name: str) -> Any:
    """Import the optional bleak client only when it is actually asked for."""
    if name in _LAZY:
        try:
            # Deliberate lazy import so the core codec needs no bleak.
            from . import client  # pylint: disable=import-outside-toplevel
        except ImportError as err:  # pragma: no cover
            raise ImportError(
                f"{name} needs bleak. Install it with "
                "'pip install spinev-ble[bleak]', or use the dependency free "
                "codec in spinev_ble.protocol instead."
            ) from err
        return getattr(client, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
