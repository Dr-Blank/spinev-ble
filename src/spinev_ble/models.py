"""Data structures returned by the client."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .const import ChargerState


@dataclass(frozen=True, slots=True)
class Frame:
    """A decoded 8 byte protocol frame."""

    register: int
    flag: int
    raw: bytes

    def __repr__(self) -> str:
        return (
            f"Frame(register=0x{self.register:02X}, "
            f"flag=0x{self.flag:02X}, raw={self.raw.hex()})"
        )


@dataclass(frozen=True, slots=True)
class ChargingSession:
    """One record from the charger's on device history.

    The charger stores a rolling window of recent sessions, not a complete
    ledger.

    Be careful with :attr:`duration`. The end timestamp marks unplug rather than
    the moment charging stopped, so a session can span days while only drawing
    power for part of it. Do not derive average power from energy divided by
    duration.
    """

    start: datetime
    end: datetime
    energy_kwh: float

    @property
    def duration(self) -> timedelta:
        """Wall clock period covered by the record, which may be zero."""
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class OcppConfig:
    """The charger's OCPP central-system settings.

    Together these form the WebSocket endpoint the charger connects to,
    roughly ``ws://<host>:<port>/<path>/<charge_point_id>``. Repoint them at a
    self-hosted server for local control without the vendor cloud.
    """

    host: str
    port: int
    path: str
    charge_point_id: str


@dataclass(frozen=True, slots=True)
class LoadBalancingConfig:
    """How the charger shares a supply with the rest of the installation.

    Load balancing exists to keep the whole installation inside what the grid
    connection can carry, so these settings describe the supply and the
    protection around it, not the charger's own rating. When
    :attr:`enabled` is false the charger ignores them and simply charges up to
    its current limit.

    This library reads these but does not write them. Raising
    :attr:`grid_current_limit_a` or :attr:`max_grid_power_w` past what the
    supply actually is removes the protection that stops the charger
    overloading it.
    """

    enabled: bool
    grid_current_limit_a: float
    safe_current_offset_a: float
    reduce_current_offset_a: float
    max_grid_power_w: float
    source: int
    priority: int


@dataclass(frozen=True, slots=True)
class ChargerStatus:
    """An immutable snapshot of the charger.

    Fields are ``None`` when the corresponding register was not read.
    :attr:`state` is also ``None`` when the charger reported a value this
    library does not recognise; :attr:`state_value` always holds the raw number.
    """

    state: ChargerState | None = None
    state_value: int | None = None
    power_w: float | None = None
    voltage_v: float | None = None
    current_a: float | None = None
    current_limit_a: float | None = None
    session_energy_kwh: float | None = None
    session_seconds: int | None = None
    lifetime_energy_kwh: float | None = None
    lifetime_seconds: int | None = None
    firmware_version: str | None = None
    alarms: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_charging(self) -> bool:
        """True while the charger is delivering or about to deliver power."""
        return self.state is not None and self.state.is_charging

    @property
    def has_alarms(self) -> bool:
        """True if the charger is reporting at least one active alarm."""
        return bool(self.alarms)
