"""Bluetooth LE client for Exicom Spin EV chargers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from types import TracebackType
from typing import Any, Protocol, runtime_checkable

from bleak import BleakClient
from bleak.backends.device import BLEDevice

from .const import (
    BULK_IDLE_TIMEOUT,
    CHARACTERISTIC_UUID,
    DEFAULT_HISTORY_COUNT,
    DEFAULT_MAX_CURRENT_A,
    DEFAULT_TIMEOUT,
    MAX_PORT,
    MAX_WIFI_FIELD_LEN,
    MIN_CURRENT_A,
    ChargerState,
    Command,
    Register,
)
from .exceptions import (
    SpinEvConnectionError,
    SpinEvError,
    SpinEvProtocolError,
    SpinEvTimeoutError,
    SpinEvValueError,
)
from .models import ChargerStatus, ChargingSession, OcppConfig
from .protocol import (
    VALUE_LENGTH,
    VALUE_OFFSET,
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
)

_LOGGER = logging.getLogger(__name__)


@runtime_checkable
class BleakClientLike(Protocol):
    """The part of :class:`bleak.BleakClient` this library uses.

    Any object with these members works as a transport, which is what lets the
    charger be reached through something other than the local adapter. The
    characteristic and payload arguments are positional only, so an
    implementation is free to name them whatever suits it.
    """

    @property
    def is_connected(self) -> bool:
        """True while the link is up."""

    async def connect(self) -> Any:
        """Open the link."""

    async def disconnect(self) -> Any:
        """Close the link."""

    async def start_notify(self, char_specifier: Any, callback: Any, /) -> Any:
        """Subscribe to notifications on a characteristic."""

    async def write_gatt_char(
        self, char_specifier: Any, data: Any, /, *, response: bool | None = None
    ) -> Any:
        """Write bytes to a characteristic."""


ClientFactory = Callable[..., BleakClientLike]
"""Builds the transport. Called with the device positionally plus ``timeout``
and ``disconnected_callback`` keywords, so any replacement must accept those.
:class:`bleak.BleakClient` and ``habluetooth.HaBleakClientWrapper`` both do."""

# Registers read by async_get_status, in the order they are fetched, as
# (attribute, register, decoder).
_STATUS_READS: tuple[tuple[str, Register, Callable[[bytes], Any]], ...] = (
    ("power_w", Register.POWER, decode_float),
    ("voltage_v", Register.VOLTAGE, decode_float),
    ("current_a", Register.CURRENT, decode_float),
    ("current_limit_a", Register.CURRENT_LIMIT, decode_float),
    ("session_energy_kwh", Register.SESSION_ENERGY, decode_energy),
    ("session_seconds", Register.SESSION_SECONDS, decode_uint),
    ("lifetime_energy_kwh", Register.LIFETIME_ENERGY, decode_energy),
    ("lifetime_seconds", Register.LIFETIME_SECONDS, decode_uint),
    ("firmware_version", Register.FIRMWARE_VERSION, decode_firmware_version),
)


class SpinEvCharger:
    """Talk to one charger.

    The charger accepts a single Bluetooth client at a time. If the official
    phone app is connected, this will not be able to connect, and the reverse
    is also true.

    Usage::

        async with SpinEvCharger(device, password=my_password) as charger:
            status = await charger.async_get_status()
            print(status.power_w)

    ``device`` should be a :class:`bleak.backends.device.BLEDevice`.
    """

    def __init__(
        self,
        device: BLEDevice,
        password: int | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        client_class: ClientFactory | None = None,
    ) -> None:
        """Create a client.

        ``password`` may be omitted, in which case it is read from the charger
        on first use via :meth:`async_get_password`.

        ``client_class`` builds the transport. It defaults to
        :class:`bleak.BleakClient`, which uses the local adapter. Pass a
        different one to route elsewhere, for example
        ``habluetooth.HaBleakClientWrapper`` to reach the charger through an
        ESPHome Bluetooth proxy. Anything matching :class:`BleakClientLike`
        works.
        """
        self._device = device
        self._password = password
        self._timeout = timeout
        self._client_class: ClientFactory = client_class or BleakClient
        self._client: BleakClientLike | None = None
        self._lock = asyncio.Lock()
        self._waiters: dict[int, asyncio.Future[bytes]] = {}
        self._bulk: list[bytes] = []
        self._bulk_event = asyncio.Event()

    @property
    def is_connected(self) -> bool:
        """True while the BLE link is up."""
        return self._client is not None and self._client.is_connected

    async def async_connect(self) -> None:
        """Connect and subscribe to notifications."""
        if self.is_connected:
            return
        try:
            client = self._client_class(
                self._device,
                timeout=self._timeout,
                disconnected_callback=self._on_disconnect,
            )
            await client.connect()
            await client.start_notify(CHARACTERISTIC_UUID, self._on_notify)
        except Exception as err:
            raise SpinEvConnectionError(f"could not connect: {err}") from err
        self._client = client

    async def async_disconnect(self) -> None:
        """Drop the link. Safe to call when already disconnected."""
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception as err:  # pylint: disable=broad-exception-caught
            _LOGGER.debug("error while disconnecting: %s", err)

    async def __aenter__(self) -> SpinEvCharger:
        await self.async_connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.async_disconnect()

    def _require_client(self) -> BleakClientLike:
        """Return the live transport, or raise if there is not one."""
        client = self._client
        if client is None or not client.is_connected:
            raise SpinEvConnectionError("not connected")
        return client

    def _on_disconnect(self, _client: object) -> None:
        """Fail anything in flight instead of letting it wait for the timeout."""
        for future in self._waiters.values():
            if not future.done():
                future.set_exception(SpinEvConnectionError("charger disconnected"))
        self._waiters.clear()
        self._bulk_event.set()

    def _on_notify(self, _sender: object, data: bytearray) -> None:
        # Payloads are never logged. Several registers carry credentials, and a
        # debug log is not the place for them.
        payload = bytes(data)
        if is_history_record(payload):
            self._bulk.append(payload)
            self._bulk_event.set()
            return
        if is_reply(payload):
            # Replies echo the register in byte 2. Match on that and hand back
            # the whole payload, so both fixed 8-byte replies and the longer
            # variable-length text replies (WiFi, OCPP) go to the right waiter.
            register = payload[2]
            waiter = self._waiters.pop(register, None)
            if waiter is not None and not waiter.done():
                waiter.set_result(payload)
            else:
                _LOGGER.debug(
                    "no waiter for reply to register 0x%02X, %d bytes",
                    register,
                    len(payload),
                )
            return
        _LOGGER.debug("ignoring unexpected payload of %d bytes", len(payload))

    async def _request(self, frame: bytes, register: int) -> bytes:
        """Send a frame and wait for the matching reply.

        Replies are matched on the register byte because the charger does not
        guarantee ordering when several requests are in flight. Requests are
        serialised anyway.
        """
        client = self._require_client()
        async with self._lock:
            loop = asyncio.get_running_loop()
            future: asyncio.Future[bytes] = loop.create_future()
            self._waiters[register] = future
            try:
                await client.write_gatt_char(CHARACTERISTIC_UUID, frame, response=True)
                payload = await asyncio.wait_for(future, timeout=self._timeout)
            except TimeoutError as err:
                raise SpinEvTimeoutError(
                    f"no reply for register 0x{register:02X}. Another client, such as "
                    "the phone app, may be holding the connection."
                ) from err
            except SpinEvError:
                raise
            except Exception as err:
                raise SpinEvConnectionError(f"write failed: {err}") from err
            finally:
                self._waiters.pop(register, None)
            return payload

    async def async_read_raw(self, register: int, parameter: int = 0) -> bytes:
        """Read a register and return its four raw value bytes."""
        payload = await self._request(build_read(register, parameter), register)
        value = payload[VALUE_OFFSET : VALUE_OFFSET + VALUE_LENGTH]
        if len(value) != VALUE_LENGTH:
            raise SpinEvProtocolError(
                f"short reply for register 0x{register:02X}: "
                f"{len(value)} value bytes, expected {VALUE_LENGTH}"
            )
        return value

    async def async_read_string(self, register: int) -> str:
        """Read a text register and return it decoded, padding stripped.

        Used for the WiFi and OCPP settings, whose replies are the value's
        ASCII bytes rather than the usual fixed four.
        """
        payload = await self._request(build_read(register), register)
        return decode_string(payload[VALUE_OFFSET:])

    async def async_get_state(self) -> ChargerState:
        """Read the charger state.

        Raises :class:`SpinEvProtocolError` if the charger reports a value this
        library does not recognise. Use :meth:`async_get_state_value` if you want
        the raw number without the enum lookup.
        """
        value = await self.async_get_state_value()
        try:
            return ChargerState(value)
        except ValueError as err:
            raise SpinEvProtocolError(f"unknown charger state {value}") from err

    async def async_get_state_value(self) -> int:
        """Read the charger state as a plain integer, never raising on unknowns."""
        return decode_uint(await self.async_read_raw(Register.STATE))

    async def async_get_power(self) -> float:
        """Read active power in watts."""
        return decode_float(await self.async_read_raw(Register.POWER))

    async def async_get_voltage(self) -> float:
        """Read voltage in volts."""
        return decode_float(await self.async_read_raw(Register.VOLTAGE))

    async def async_get_current(self) -> float:
        """Read current in amps."""
        return decode_float(await self.async_read_raw(Register.CURRENT))

    async def async_get_current_limit(self) -> float:
        """Read the charging current limit in amps."""
        return decode_float(await self.async_read_raw(Register.CURRENT_LIMIT))

    async def async_get_alarms(self) -> list[str]:
        """Read active alarms.

        Only the first alarm bank is read. A second bank exists, but the bit to
        bank assignment for it is not known.
        """
        return decode_alarms(decode_uint(await self.async_read_raw(Register.ALARMS)))

    async def async_get_status(self) -> ChargerStatus:
        """Read everything useful in one pass.

        Each register is a separate round trip, so this takes roughly one
        second.

        An unrecognised state does not fail the whole read: it leaves
        :attr:`ChargerStatus.state` as ``None`` while
        :attr:`ChargerStatus.state_value` keeps the raw number.
        """
        state_value = await self.async_get_state_value()
        try:
            state: ChargerState | None = ChargerState(state_value)
        except ValueError:
            _LOGGER.debug("unknown charger state %d", state_value)
            state = None

        values: dict[str, Any] = {}
        for attribute, register, decode in _STATUS_READS:
            values[attribute] = decode(await self.async_read_raw(register))

        return ChargerStatus(
            state=state,
            state_value=state_value,
            alarms=tuple(await self.async_get_alarms()),
            **values,
        )

    async def async_get_password(self) -> int:
        """Read the charger's own Bluetooth password.

        Treat the result as a credential: it is what lets a caller start and
        stop the charger.
        """
        return decode_uint(await self.async_read_raw(Register.PASSWORD)) & 0xFFFFFF

    async def _async_require_password(self) -> int:
        """Return the configured password, reading it from the charger if needed."""
        if self._password is None:
            self._password = await self.async_get_password()
        return self._password

    async def async_start_charging(self) -> None:
        """Start charging.

        The charger acknowledges immediately but takes about one second to
        report the new state and about two seconds before power actually flows.
        """
        password = await self._async_require_password()
        await self._request(build_control(Command.START, password), Register.CONTROL)

    async def async_stop_charging(self) -> None:
        """Stop charging."""
        password = await self._async_require_password()
        await self._request(build_control(Command.STOP, password), Register.CONTROL)

    async def async_set_current_limit(
        self, amps: float, *, max_amps: float = DEFAULT_MAX_CURRENT_A
    ) -> None:
        """Set the charging current limit, in amps.

        This changes how much power the charger will deliver: single phase at
        230 V, 6 A is about 1.4 kW and 32 A is about 7.4 kW. The new limit is
        applied to the current session as well as future ones.

        ``amps`` must be at least :data:`~spinev_ble.const.MIN_CURRENT_A` (the
        6 A EV floor, which does not vary by model) and at most ``max_amps``.
        The maximum a charger will honour is model specific: the top single
        phase Spin Air allows 32 A, but an 11 kW or 22 kW three phase unit, or
        a lower rated one, will differ, so ``max_amps`` defaults to
        :data:`~spinev_ble.const.DEFAULT_MAX_CURRENT_A` (32 A) and can be
        raised or lowered to match the unit. This bound is only a client side
        guard: always read :meth:`async_get_current_limit` back to confirm what
        the charger actually accepted.

        :raises SpinEvValueError: if ``amps`` is outside the accepted range.
        """
        if not MIN_CURRENT_A <= amps <= max_amps:
            raise SpinEvValueError(
                f"current limit {amps} A is out of range "
                f"{MIN_CURRENT_A} to {max_amps} A"
            )
        await self._request(
            build_write_float(Register.CURRENT_LIMIT, amps),
            Register.CURRENT_LIMIT,
        )

    async def async_get_wifi_ssid(self) -> str:
        """Read the SSID of the WiFi network the charger is set to join."""
        return await self.async_read_string(Register.WIFI_SSID)

    async def async_get_wifi_password(self) -> str:
        """Read the stored WiFi password.

        Returned as plain text. Handle the result as the credential it is.
        """
        return await self.async_read_string(Register.WIFI_PASSWORD)

    async def async_set_wifi(self, ssid: str, password: str) -> None:
        """Point the charger at a WiFi network.

        .. warning::
           A wrong value here can leave the charger unable to reach any network
           until it is re-provisioned. Read the values back afterwards, and keep
           the phone app available to restore the originals.

        Both fields must be ASCII, at most
        :data:`~spinev_ble.const.MAX_WIFI_FIELD_LEN` characters, and contain no
        double quote.

        :raises SpinEvValueError: if either field breaks those rules.
        """
        self._check_wifi_field("ssid", ssid)
        self._check_wifi_field("password", password)
        await self._request(
            build_write_string(Register.WIFI_SSID, ssid), Register.WIFI_SSID
        )
        await self._request(
            build_write_string(Register.WIFI_PASSWORD, password),
            Register.WIFI_PASSWORD,
        )

    @staticmethod
    def _check_wifi_field(name: str, value: str) -> None:
        if len(value) > MAX_WIFI_FIELD_LEN:
            raise SpinEvValueError(
                f"wifi {name} is too long, max {MAX_WIFI_FIELD_LEN} characters"
            )
        if '"' in value:
            raise SpinEvValueError(f"wifi {name} must not contain a double quote")

    async def async_get_ocpp_config(self) -> OcppConfig:
        """Read the OCPP central-system settings.

        The charger uses these to reach its management server. Read them to see
        where it points, or as a template before writing your own with
        :meth:`async_set_ocpp_config`.
        """
        host = await self.async_read_string(Register.OCPP_HOST)
        port = decode_uint(await self.async_read_raw(Register.OCPP_PORT))
        path = await self.async_read_string(Register.OCPP_PATH)
        charge_point_id = await self.async_read_string(Register.OCPP_CHARGE_POINT_ID)
        return OcppConfig(
            host=host,
            port=port,
            path=path,
            charge_point_id=charge_point_id,
        )

    async def async_set_ocpp_config(self, config: OcppConfig) -> None:
        """Point the charger at an OCPP central system.

        .. warning::
           A wrong value here leaves the charger unable to reach a central
           system. Read the values back afterwards to confirm it accepted them,
           and keep the phone app available to restore the originals.

        :raises SpinEvValueError: if the host is empty or the port is not a
            valid TCP port.
        """
        if not config.host:
            raise SpinEvValueError("ocpp host must not be empty")
        if not 1 <= config.port <= MAX_PORT:
            raise SpinEvValueError(
                f"ocpp port {config.port} is out of range 1 to {MAX_PORT}"
            )
        await self._request(
            build_write_string(Register.OCPP_HOST, config.host),
            Register.OCPP_HOST,
        )
        await self._request(
            build_write_uint(Register.OCPP_PORT, config.port),
            Register.OCPP_PORT,
        )
        await self._request(
            build_write_string(Register.OCPP_PATH, config.path),
            Register.OCPP_PATH,
        )
        await self._request(
            build_write_string(Register.OCPP_CHARGE_POINT_ID, config.charge_point_id),
            Register.OCPP_CHARGE_POINT_ID,
        )

    async def async_get_history(
        self, count: int = DEFAULT_HISTORY_COUNT
    ) -> list[ChargingSession]:
        """Fetch recent charging sessions, most recent first.

        The charger streams records with no end marker, so collection stops on
        an idle gap. Duplicate records are removed while preserving order.
        """
        client = self._require_client()
        async with self._lock:
            self._bulk.clear()
            self._bulk_event.clear()
            frame = build_read(Register.HISTORY_SESSIONS, count)
            try:
                await client.write_gatt_char(CHARACTERISTIC_UUID, frame, response=True)
            except Exception as err:
                raise SpinEvConnectionError(f"write failed: {err}") from err
            await self._collect_bulk()
            records = list(self._bulk)
            self._bulk.clear()

        sessions: list[ChargingSession] = []
        seen: set[tuple[object, ...]] = set()
        for raw in records:
            session = decode_session_record(raw)
            if session is None:
                continue
            key = (session.start, session.end, session.energy_kwh)
            if key in seen:
                continue
            seen.add(key)
            sessions.append(session)
        return sessions

    async def _collect_bulk(self) -> None:
        """Wait for the record stream to go quiet."""
        deadline = asyncio.get_running_loop().time() + self._timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(
                    self._bulk_event.wait(),
                    timeout=min(BULK_IDLE_TIMEOUT, remaining),
                )
            except TimeoutError:
                return
            self._bulk_event.clear()
