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
    COMMIT_VALUE,
    CONTROL_REJECTED,
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
from .exceptions import (
    SpinEvCommandRejectedError,
    SpinEvPasswordError,
    SpinEvProtocolError,
)
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


def build_string_write(register: int, value: str, field_bytes: int) -> list[bytes]:
    """Build the frames that write a text setting.

    Text settings (the WiFi and OCPP configuration) are written in four byte
    pieces, each in its own frame ``10 AC <reg> <seq>`` where ``<seq>`` counts
    from 1. The value is zero padded to ``field_bytes``, which must be a
    multiple of four, so the number of frames is fixed regardless of the value.

    :raises SpinEvProtocolError: if ``value`` is not ASCII, is longer than
        ``field_bytes``, or ``field_bytes`` is not a multiple of four.
    """
    if field_bytes <= 0 or field_bytes % 4:
        raise SpinEvProtocolError("field_bytes must be a positive multiple of 4")
    try:
        payload = value.encode("ascii")
    except UnicodeEncodeError as err:
        raise SpinEvProtocolError("value must be ASCII") from err
    if len(payload) > field_bytes:
        raise SpinEvProtocolError(
            f"value is {len(payload)} bytes, limit is {field_bytes}"
        )
    payload = payload.ljust(field_bytes, b"\x00")
    return [
        FRAME_HEADER + bytes([register, offset // 4 + 1]) + payload[offset : offset + 4]
        for offset in range(0, field_bytes, 4)
    ]


def build_clock_time(hour: int, minute: int, second: int) -> bytes:
    """Build a write of the clock time, packed as ``00 SS MM HH``.

    :raises SpinEvProtocolError: if any component is out of range.
    """
    if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
        raise SpinEvProtocolError("time component out of range")
    return build_write_uint(Register.CLOCK_TIME, (second << 16) | (minute << 8) | hour)


def build_clock_date(year: int, month: int, day: int) -> bytes:
    """Build a write of the clock date, packed as ``00 DD MM YY``.

    ``month`` is 1 to 12 and is stored zero based; ``year`` is a full year and
    is stored as an offset from 1900.

    :raises SpinEvProtocolError: if any component is out of range.
    """
    if not (1900 <= year <= 2155 and 1 <= month <= 12 and 1 <= day <= 31):
        raise SpinEvProtocolError("date component out of range")
    value = (day << 16) | ((month - 1) << 8) | (year - YEAR_EPOCH)
    return build_write_uint(Register.CLOCK_DATE, value)


def build_timezone(hours: int, minutes: int) -> bytes:
    """Build a write of the UTC offset, packed as ``00 00 HH MM``.

    :raises SpinEvProtocolError: if the offset is out of range.
    """
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise SpinEvProtocolError("timezone offset out of range")
    return build_write_uint(Register.TIMEZONE, (hours << 8) | minutes)


def build_commit() -> bytes:
    """Build the frame that applies a batch of configuration writes."""
    return build_write_uint(Register.COMMIT, COMMIT_VALUE)


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


def check_control_reply(sent: bytes, reply: bytes) -> None:
    """Confirm the charger accepted a start or stop command.

    A charger that acts on a control command echoes it back byte for byte. One
    that refuses replies on the same register with
    :data:`~spinev_ble.const.CONTROL_REJECTED` instead, so a reply arriving is
    not on its own proof that anything happened.

    :param sent: the frame built by :func:`build_control`.
    :param reply: the payload the charger answered with.
    :raises SpinEvCommandRejectedError: if the charger refused the command.
    :raises SpinEvProtocolError: if the reply is not a control reply at all.
    """
    frame = parse_frame(reply)
    if frame is None or frame.register != Register.CONTROL:
        raise SpinEvProtocolError("reply is not a control reply")
    if frame.raw == struct.pack(">I", CONTROL_REJECTED):
        raise SpinEvCommandRejectedError(
            "the charger refused the command. The Bluetooth password is probably wrong."
        )
    if frame.raw != sent[VALUE_OFFSET:]:
        raise SpinEvProtocolError(
            "the charger answered a control command with a different command"
        )


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
