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


class SpinEvCommandRejectedError(SpinEvError):
    """The charger received a start or stop command and refused it.

    The command did not take effect: the charger is still in whatever state it
    was in before. The usual cause is a wrong Bluetooth password, since the
    charger reports a bad password by refusing the command rather than by
    reporting a distinct error.
    """


class SpinEvBusyError(SpinEvError):
    """The charger is mid-session, and the operation is not allowed during one.

    Stop charging first, or wait for the session to end.
    """


class SpinEvValueError(SpinEvError, ValueError):
    """A value passed to a setter is outside the range the charger accepts."""
