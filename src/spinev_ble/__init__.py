"""Local control for Exicom Spin EV chargers.

The package has three layers, and only the transports have dependencies:

- **Codec** (:mod:`spinev_ble.protocol`): turns charger commands into frames
  and frames back into values. Pure, no dependencies::

      from spinev_ble import Command, build_control

      frame = build_control(Command.START, password=0xABCDEF)
      # send `frame` however you like

- **Charger client** (:class:`SpinEvCharger`): the high level API, one method
  per charger feature. Pure, no dependencies. It talks through a transport.

- **Transports** (:mod:`spinev_ble.transports`): what carries the frames.
  :class:`BleTransport` uses the Bluetooth link and needs
  ``pip install spinev-ble[bleak]``. :class:`OcppTunnelTransport` uses the
  charger's OCPP connection and needs nothing extra::

      async with SpinEvCharger(BleTransport(device)) as charger:
          ...
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

from .charger import SpinEvCharger
from .const import (
    ADVERTISED_NAME_PATTERN,
    ALARM_BANK2_FLAG,
    ALARM_BANKS,
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
    AlarmSeverity,
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
    SpinEvTypeError,
    SpinEvUnsupportedError,
    SpinEvValueError,
)
from .models import (
    AlarmDef,
    ChargerStatus,
    ChargingSession,
    Frame,
    LoadBalancingConfig,
    OcppConfig,
)
from .protocol import (
    ALARMS,
    build_alarm_read,
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
    decode_alarm_defs,
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
from .transports import (
    DataTransferCall,
    DisconnectCallback,
    FrameCallback,
    OcppTunnelTransport,
    SpinEvTransport,
)

if TYPE_CHECKING:
    from .transports.ble import BleakClientLike, BleTransport

try:
    __version__ = version("spinev-ble")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0"

__all__ = [
    "ADVERTISED_NAME_PATTERN",
    "ALARMS",
    "ALARM_BANK2_FLAG",
    "ALARM_BANKS",
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
    "AlarmDef",
    "AlarmSeverity",
    "BleTransport",
    "BleakClientLike",
    "ChargerState",
    "ChargerStatus",
    "ChargingSession",
    "Command",
    "DataTransferCall",
    "DisconnectCallback",
    "Frame",
    "FrameCallback",
    "LoadBalancingConfig",
    "OcppConfig",
    "OcppTunnelTransport",
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
    "SpinEvTransport",
    "SpinEvTypeError",
    "SpinEvUnsupportedError",
    "SpinEvValueError",
    "__version__",
    "build_alarm_read",
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
    "decode_alarm_defs",
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

_BLE = frozenset({"BleTransport", "BleakClientLike"})
"""Names that live in the optional bleak backed transport."""


def __getattr__(name: str) -> Any:
    """Import the Bluetooth transport only when it is actually asked for."""
    if name in _BLE:
        # Deliberate lazy import so the rest of the package needs no bleak.
        from . import transports  # pylint: disable=import-outside-toplevel

        return getattr(transports, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
