"""Exceptions raised by :mod:`spinev_ble`."""

from __future__ import annotations


class SpinEvError(Exception):
    """Base class for every error raised by this package."""


class SpinEvConnectionError(SpinEvError):
    """The charger could not be reached, or the link dropped."""


class SpinEvTimeoutError(SpinEvError):
    """The charger did not answer a request in time.

    A charger normally replies within about 120 ms. The usual cause of this
    error is that something else already holds the connection, since these
    chargers accept only one Bluetooth client at a time.
    """


class SpinEvProtocolError(SpinEvError):
    """A frame arrived that does not match the expected protocol."""


class SpinEvPasswordError(SpinEvError):
    """The Bluetooth password is not usable.

    Each charger has its own password, which must be an integer that fits in
    three bytes.
    """


class SpinEvValueError(SpinEvError, ValueError):
    """A value passed to a setter is outside the range the charger accepts."""
