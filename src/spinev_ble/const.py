"""Protocol constants for Exicom Spin EV chargers.

No credentials or device identifiers are stored in this package.
"""

from __future__ import annotations

from enum import IntEnum

SERVICE_UUID = "49535343-fe7d-4ae5-8fa9-9fafd205e455"
"""Vendor service. The Microchip/ISSC "Transparent UART" service, a generic
serial over BLE tunnel. The charger protocol rides on top of it."""

CHARACTERISTIC_UUID = "49535343-1e4d-4bd9-ba61-23c647249616"
"""The only characteristic the charger uses, in BOTH directions: commands are
written to it and replies arrive as notifications on the same handle."""

FRAME_HEADER = b"\x10\xac"
"""Every protocol frame starts with these two bytes."""

FRAME_LENGTH = 8
"""Length of a standard request or reply frame, in bytes."""

ADVERTISED_NAME_PATTERN = r"^ ?\d{12}_[0-9a-fA-F]{4}$"
"""Charger advertising names: a leading space, a 12 digit serial, an underscore
and the last four hex digits of the MAC, for example " 000000000000_0000"."""


class Operation(IntEnum):
    """Value of the flag byte, which selects what a frame does."""

    READ = 0x00
    WRITE = 0x01
    BULK = 0x10


class Register(IntEnum):
    """Register ids this library knows how to use.

    The charger exposes more registers than are named here. Only those with a
    known meaning are listed.
    """

    VOLTAGE = 0x0A
    """Line voltage, in volts."""
    CURRENT = 0x14
    """Output current, in amps."""
    PASSWORD = 0x32
    """The charger's own Bluetooth password, in the low three bytes."""
    SESSION_ENERGY = 0x35
    """Energy delivered in the current session, in hundredths of a kWh."""
    ALARMS = 0x39
    """Active alarm bit field, see :data:`spinev_ble.protocol.ALARM_BITS`."""
    CONTROL = 0x3C
    """Start and stop commands, see :class:`Command`."""
    CURRENT_LIMIT = 0x4F
    """Charging current limit, in amps, as a float."""
    FIRMWARE_VERSION = 0x52
    """Four version components, one per byte."""
    SESSION_SECONDS = 0x59
    """Duration of the current session, in seconds."""
    OCPP_PORT = 0x5E
    """TCP port of the OCPP server, e.g. 80 for ws, 443 for wss."""
    OCPP_PATH = 0x60
    """Path or tenant, no leading slash, e.g. "myserver/ocpp" or "ocpp"."""
    WIFI_SSID = 0x61
    """SSID of the network the charger joins."""
    OCPP_CHARGE_POINT_ID = 0x62
    """Identifier the charger registers as, e.g. its serial number."""
    WIFI_PASSWORD = 0x63
    """WiFi key, stored and returned as plain text, not masked."""
    OCPP_HOST = 0x64
    """OCPP server host, e.g. "dns:ocpp.example.com" ("dns:" selects DNS)."""
    LIFETIME_ENERGY = 0x65
    """Total energy delivered, in hundredths of a kWh."""
    STATE = 0x67
    """Charger state, see :class:`ChargerState`."""
    HISTORY_SESSIONS = 0x68
    """Bulk read of stored charging sessions."""
    LIFETIME_SECONDS = 0x6A
    """Total charging time, in seconds."""
    HISTORY_EVENTS = 0x70
    """Bulk read of stored events."""
    POWER = 0x84
    """Active power, in watts."""


class ChargerState(IntEnum):
    """Values reported by :attr:`Register.STATE`.

    Values above 4 exist but have no confirmed meaning, so they are not named.
    Reading one through :meth:`SpinEvCharger.async_get_state` raises
    :class:`~spinev_ble.exceptions.SpinEvProtocolError`; use
    :meth:`SpinEvCharger.async_get_state_value` for the raw number instead.
    """

    AVAILABLE = 1
    """no vehicle connected"""
    IDLE = 2
    """vehicle plugged in, not charging"""
    STARTING = 3
    """transitional, just after a start command"""
    CHARGING = 4
    """delivering power"""

    @property
    def is_charging(self) -> bool:
        """True while the charger is delivering or about to deliver power."""
        return self in (ChargerState.STARTING, ChargerState.CHARGING)

    @property
    def has_vehicle(self) -> bool:
        """True when a vehicle is connected."""
        return self is not ChargerState.AVAILABLE


class Command(IntEnum):
    """Command byte written to :attr:`Register.CONTROL`."""

    START = 0x01
    STOP = 0x10


MAX_WIFI_FIELD_LEN = 32
"""Maximum length of the WiFi SSID and password. Neither may contain a double
quote."""

MAX_PORT = 65535
"""Highest valid TCP port, used to bound :attr:`Register.OCPP_PORT`."""

ENERGY_SCALE = 0.01
"""kWh per count. Energy registers are in hundredths of a kWh."""

MIN_CURRENT_A = 6.0
"""Lowest charging current the charger accepts, in amps. The usual EV floor:
IEC 61851 and SAE J1772 both stop pilot signalling below 6 A. Does not vary by
model."""

DEFAULT_MAX_CURRENT_A = 32.0
"""Default upper bound for the charging current, in amps. 32 A is the hardware
maximum of the top single phase Spin Air (model 7K230-2T32, 230 V, ~7.4 kW). The
real ceiling is model specific: an 11 kW or 22 kW three phase unit, or a lower
rated single phase one, will differ. Treat this as a safe default, override it
per model via the ``max_amps`` argument of
:meth:`SpinEvCharger.async_set_current_limit`, and read
:attr:`Register.CURRENT_LIMIT` back to confirm what the charger accepted."""

SESSION_RECORD_LENGTH = 26
"""Length of a charging session history record, in bytes."""

EVENT_RECORD_LENGTH = 20
"""Length of an event history record, in bytes."""

RECORD_START_MARKER = 0x7C
"""First byte of every history record."""

RECORD_END_MARKER = 0x25
"""Last byte of every history record."""

YEAR_EPOCH = 1900
"""History record years are stored as an offset from this."""

DEFAULT_TIMEOUT = 5.0
"""Seconds to wait for a reply. A charger usually answers within about 120 ms."""

BULK_IDLE_TIMEOUT = 0.4
"""Seconds of silence that end a bulk read, which has no end marker."""

DEFAULT_HISTORY_COUNT = 40
"""How many records :meth:`SpinEvCharger.async_get_history` asks for."""
