"""Transports that carry frames between a charger client and a charger.

:class:`~spinev_ble.charger.SpinEvCharger` talks to the charger through
exactly one transport, chosen by the caller:

- :class:`BleTransport`, the charger's Bluetooth link. Needs the ``bleak``
  extra: ``pip install spinev-ble[bleak]``.
- :class:`OcppTunnelTransport`, the charger's OCPP connection. No extra: the
  caller supplies the ``DataTransfer`` function from its own central system.

Anything matching :class:`SpinEvTransport` works in their place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import DisconnectCallback, FrameCallback, SpinEvTransport
from .ocpp import DataTransferCall, OcppTunnelTransport

if TYPE_CHECKING:
    from .ble import BleakClientLike, BleTransport, ClientFactory

__all__ = [
    "BleTransport",
    "BleakClientLike",
    "ClientFactory",
    "DataTransferCall",
    "DisconnectCallback",
    "FrameCallback",
    "OcppTunnelTransport",
    "SpinEvTransport",
]

_BLE = frozenset({"BleTransport", "BleakClientLike", "ClientFactory"})
"""Names that live in the optional bleak backed module."""


def __getattr__(name: str) -> Any:
    """Import the Bluetooth transport only when it is actually asked for."""
    if name in _BLE:
        try:
            # Deliberate lazy import so the rest of the package needs no bleak.
            from . import ble  # pylint: disable=import-outside-toplevel
        except ImportError as err:  # pragma: no cover
            raise ImportError(
                f"{name} needs bleak. Install it with 'pip install spinev-ble[bleak]'."
            ) from err
        return getattr(ble, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
