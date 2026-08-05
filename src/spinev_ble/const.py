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
    COMMIT = 0x3B
    """Applies a batch of configuration writes when written with 0x01000000."""
    CONTROL = 0x3C
    """Start and stop commands, see :class:`Command`."""
    CLOCK_TIME = 0x3F
    """Real-time clock time, packed as ``00 SS MM HH``."""
    CLOCK_DATE = 0x40
    """Real-time clock date, packed as ``00 DD MM YY``, month 0-based, year-1900."""
    CURRENT_LIMIT = 0x4F
    """Charging current limit, in amps, as a float."""
    FIRMWARE_VERSION = 0x52
    """Four version components, one per byte."""
    INTERNET_CONNECTIVITY = 0x56
    """Internet connectivity preference, 1 enabled, 0 disabled."""
    SESSION_SECONDS = 0x59
    """Duration of the current session, in seconds."""
    OCPP_PORT = 0x5E
    """TCP port of the OCPP server, e.g. 80 for ws, 443 for wss."""
    OCPP_PATH = 0x60
    """Path or tenant, no leading slash, e.g. "myserver/ocpp" or "ocpp"."""
    WIFI_PASSWORD = 0x61
    """WiFi key, stored and returned as plain text, not masked."""
    OCPP_CHARGE_POINT_ID = 0x62
    """Identifier the charger registers as, e.g. its serial number."""
    WIFI_SSID = 0x63
    """SSID of the network the charger joins."""
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
    """Bulk read of stored events.

    Unlike :attr:`HISTORY_SESSIONS`, this does not use the standard read
    framing: the charger expects flag :attr:`Operation.BULK`, not
    :attr:`Operation.READ`, and the value is not a plain record count.
    :func:`~spinev_ble.protocol.build_read` cannot build a working request
    for it, and nothing in this package reads it yet.
    """
    TIMEZONE = 0x78
    """UTC offset, packed as ``00 00 HH MM``."""
    POWER = 0x84
    """Active power, in watts, as a float."""
    GRID_CURRENT_LIMIT = 0x99
    """Current the grid supply can deliver, in amps, as a float.

    Load balancing holds the whole installation under this figure, so it
    describes the supply feeding the charger rather than the charger itself.
    """
    LOAD_BALANCING_ENABLED = 0x9A
    """Whether load balancing is active, 1 enabled, 0 disabled."""
    SAFE_CURRENT_OFFSET = 0x9C
    """Headroom kept below :attr:`GRID_CURRENT_LIMIT`, in amps."""
    REDUCE_CURRENT_OFFSET = 0x9D
    """Step by which charging current is cut when the grid limit is neared, in
    amps."""
    LOAD_BALANCING_SOURCE = 0xBD
    """Where load balancing reads grid load from, as a small enum."""
    RANDOM_DELAY = 0xC2
    """Delay before charging starts, in seconds, 0 to 1800, 0 disables it."""
    MAX_GRID_POWER = 0xD1
    """Ceiling on total grid power for the installation, as a float."""
    LOAD_BALANCING_PRIORITY = 0xD2
    """Priority mode used when several chargers share one supply."""


class ChargerState(IntEnum):
    """Values reported by :attr:`Register.STATE`.

    A charger may report a value outside this set. Reading one through
    :meth:`SpinEvCharger.async_get_state` raises
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
    FAULT = 5
    """a protection alarm has tripped; see :meth:`SpinEvCharger.async_get_alarms`"""
    FINISHING = 6
    """wrapping up after charging stops, before returning to available or idle"""
    EVSE_SUSPENDED = 7
    """the charger is holding off, with the session still open"""
    EV_SUSPENDED = 8
    """the vehicle has stopped drawing, with the session still open

    Power falls to roughly zero while the session timer keeps running and the
    energy total stops climbing. Charging resumes on its own, with no new start
    command, so this is an ordinary part of a session rather than a failure.
    """
    BOOTING = 9
    """starting up, and not yet answering commands

    Control commands sent now are refused. The charger passes through this
    after a restart, including the one applying a configuration change
    triggers.
    """
    UNAVAILABLE = 10
    """not offering charging at all"""

    @property
    def is_charging(self) -> bool:
        """True while the charger is delivering or about to deliver power."""
        return self in (ChargerState.STARTING, ChargerState.CHARGING)

    @property
    def is_suspended(self) -> bool:
        """True while a session is open but paused, by either end."""
        return self in (ChargerState.EVSE_SUSPENDED, ChargerState.EV_SUSPENDED)

    @property
    def has_vehicle(self) -> bool:
        """True when a vehicle is connected.

        False whenever the charger is not in a position to say, which covers
        :attr:`BOOTING` and :attr:`UNAVAILABLE` as well as :attr:`AVAILABLE`.
        """
        return self not in (
            ChargerState.AVAILABLE,
            ChargerState.BOOTING,
            ChargerState.UNAVAILABLE,
        )

    @property
    def is_fault(self) -> bool:
        """True when a protection alarm has tripped and needs clearing."""
        return self is ChargerState.FAULT


class Command(IntEnum):
    """Command byte written to :attr:`Register.CONTROL`."""

    START = 0x01
    STOP = 0x10


MAX_WIFI_FIELD_LEN = 32
"""Maximum length of the WiFi SSID and password. Neither may contain a double
quote."""

MAX_PORT = 65535
"""Highest valid TCP port, used to bound :attr:`Register.OCPP_PORT`."""

WIFI_FIELD_BYTES = 32
"""Byte width the WiFi SSID and password fields occupy on the wire."""

OCPP_ID_FIELD_BYTES = 32
"""Byte width the OCPP charge point id field occupies on the wire."""

OCPP_TEXT_FIELD_BYTES = 64
"""Byte width the OCPP host and path fields occupy on the wire."""

MAX_RANDOM_DELAY_S = 1800
"""Longest start delay the charger accepts, in seconds. 0 disables it."""

COMMIT_VALUE = 0x01000000
"""Value written to :attr:`Register.COMMIT` to apply pending configuration."""

CONTROL_REJECTED = 0xFFFFFFFF
"""Value the charger echoes back from :attr:`Register.CONTROL` when it refuses
a start or stop command. An accepted command is echoed back unchanged instead,
so a reply carrying this value means the charger did nothing."""

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
