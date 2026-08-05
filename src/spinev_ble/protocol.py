"""Frame encoding and decoding.

This module is pure. It has no bluetooth dependency and no I/O, so frames can be
built and decoded without a radio.

Frame layout, 8 bytes, all multi byte values big endian::

    byte:   0    1    2     3      4  5  6  7
          0x10 0xAC  REG  FLAG   <-- VALUE -->

The flag byte selects the operation, see :class:`spinev_ble.const.Operation`.
The charger replies on the same characteristic with the register and flag
echoed back.

Text registers are the exception: their replies carry the value's ASCII bytes
after the same 4 byte header, so they are longer than 8 bytes.
"""

from __future__ import annotations

import struct
from datetime import datetime

from .const import (
    ENERGY_SCALE,
    EVENT_RECORD_LENGTH,
    FRAME_HEADER,
    FRAME_LENGTH,
    RECORD_END_MARKER,
    RECORD_START_MARKER,
    SESSION_RECORD_LENGTH,
    YEAR_EPOCH,
    Command,
    Operation,
    Register,
)
from .exceptions import SpinEvPasswordError, SpinEvProtocolError
from .models import ChargingSession, Frame

VALUE_LENGTH = 4
"""Number of bytes carrying the value in a standard frame."""

VALUE_OFFSET = 4
"""Offset at which a reply's value starts, standard and text frames alike."""

# The password occupies the low three bytes of the control value.
_PASSWORD_MAX = 0xFFFFFF

ALARM_BITS: dict[int, str] = {
    0x000001: "Mains Fail",
    0x000002: "Mains Low",
    0x000004: "Mains High",
    0x000008: "Mains Output Current High",
    0x000010: "Earth Detect",
    0x000020: "Earth Leakage",
    0x000040: "PWM Fault",
    0x000080: "EM Comm Fault",
    0x000100: "RFID Comm Fault",
    0x000200: "Connectivity Fault",
    0x000400: "LED LCD Fault",
    0x001000: "NE Volt High",
    0x002000: "Output Current Very High",
    0x004000: "Emergency",
    0x008000: "SPD Detect",
    0x010000: "Mains Very High",
    0x020000: "Temperature High",
}
"""Alarm bit to name. Bit ``0x000800`` is reserved and never reported."""


def _check_password(password: int) -> None:
    if not isinstance(password, int) or isinstance(password, bool):
        raise SpinEvPasswordError("password must be an integer")
    if not 0 <= password <= _PASSWORD_MAX:
        raise SpinEvPasswordError(f"password must fit in three bytes, got {password!r}")


def build_read(register: int, parameter: int = 0) -> bytes:
    """Build a read request.

    ``parameter`` is zero for ordinary registers. The bulk history registers
    use it as a record count.
    """
    return (
        FRAME_HEADER + bytes([register, Operation.READ]) + struct.pack(">I", parameter)
    )


def build_write_uint(register: int, value: int) -> bytes:
    """Build a write request carrying an unsigned 32 bit value."""
    return FRAME_HEADER + bytes([register, Operation.WRITE]) + struct.pack(">I", value)


def build_write_float(register: int, value: float) -> bytes:
    """Build a write request carrying a 32 bit float."""
    return FRAME_HEADER + bytes([register, Operation.WRITE]) + struct.pack(">f", value)


def build_write_string(register: int, value: str) -> bytes:
    """Build a write request carrying an ASCII string.

    Text settings (the WiFi and OCPP configuration) are sent as their raw ASCII
    bytes after the ``10 AC <reg> 01`` header.

    :raises SpinEvProtocolError: if ``value`` is not pure ASCII.
    """
    try:
        payload = value.encode("ascii")
    except UnicodeEncodeError as err:
        raise SpinEvProtocolError("value must be ASCII") from err
    return FRAME_HEADER + bytes([register, Operation.WRITE]) + payload


def decode_string(raw: bytes) -> str:
    """Decode an ASCII string reply, dropping trailing padding.

    String registers reply with the value followed by zero padding. Any
    trailing NUL bytes and surrounding whitespace are stripped.
    """
    return raw.split(b"\x00", 1)[0].decode("ascii", "replace").strip()


def build_control(command: Command, password: int) -> bytes:
    """Build a start or stop command.

    Each charger has its own Bluetooth password. There is no default and no
    fallback: passing the wrong one means the charger ignores the command.

    :raises SpinEvPasswordError: if ``password`` is not an integer that fits in
        three bytes.
    """
    _check_password(password)
    value = (int(command) << 24) | password
    return build_write_uint(Register.CONTROL, value)


def parse_frame(data: bytes) -> Frame | None:
    """Parse an 8 byte reply.

    Returns ``None`` for anything that is not a standard frame, which is how
    bulk history records are told apart from register replies.
    """
    if len(data) != FRAME_LENGTH or not data.startswith(FRAME_HEADER):
        return None
    return Frame(register=data[2], flag=data[3], raw=bytes(data[VALUE_OFFSET:]))


def is_reply(data: bytes) -> bool:
    """True if the payload is a register reply, of either length.

    Text registers reply with a variable length payload, so this accepts
    anything long enough to carry a header and a register byte.
    """
    return len(data) > VALUE_OFFSET and data.startswith(FRAME_HEADER)


def decode_float(raw: bytes) -> float:
    """Decode a 4 byte big endian float."""
    _check_value_length(raw)
    return float(struct.unpack(">f", raw)[0])


def decode_uint(raw: bytes) -> int:
    """Decode a 4 byte big endian unsigned integer."""
    _check_value_length(raw)
    return int(struct.unpack(">I", raw)[0])


def decode_energy(raw: bytes) -> float:
    """Decode an energy register into kWh."""
    return decode_uint(raw) * ENERGY_SCALE


def decode_firmware_version(raw: bytes) -> str:
    """Decode the firmware version register.

    Each byte is one decimal component, so ``0x23180420`` becomes
    ``35.24.4.32``.
    """
    _check_value_length(raw)
    return ".".join(str(b) for b in raw)


def _check_value_length(raw: bytes) -> None:
    if len(raw) != VALUE_LENGTH:
        raise SpinEvProtocolError(f"expected {VALUE_LENGTH} bytes, got {len(raw)}")


def _decode_timestamp(time_part: bytes, date_part: bytes) -> datetime:
    """Decode the split timestamp used by history records.

    Time is ``HH MM SS`` and date is ``YY MM DD`` where the year is an offset
    from 1900.
    """
    return datetime(
        YEAR_EPOCH + date_part[0],
        date_part[1],
        date_part[2],
        time_part[0],
        time_part[1],
        time_part[2],
    )


def decode_session_record(data: bytes) -> ChargingSession | None:
    """Decode a 26 byte charging session record.

    Returns ``None`` if the record is malformed. Chargers do emit records with
    equal start and end timestamps, so callers must not assume the period is
    positive.
    """
    if (
        len(data) != SESSION_RECORD_LENGTH
        or data[0] != RECORD_START_MARKER
        or data[-1] != RECORD_END_MARKER
    ):
        return None
    try:
        start = _decode_timestamp(data[1:4], data[6:9])
        end = _decode_timestamp(data[11:14], data[16:19])
    except ValueError:
        return None
    return ChargingSession(
        start=start,
        end=end,
        energy_kwh=decode_uint(data[21:25]) * ENERGY_SCALE,
    )


def is_history_record(data: bytes) -> bool:
    """True if the payload looks like a bulk history record rather than a frame."""
    return (
        len(data) in (SESSION_RECORD_LENGTH, EVENT_RECORD_LENGTH)
        and data[0] == RECORD_START_MARKER
        and data[-1] == RECORD_END_MARKER
    )


def decode_alarms(word: int) -> list[str]:
    """Turn an alarm word into a list of active alarm names."""
    return [name for mask, name in ALARM_BITS.items() if word & mask]
