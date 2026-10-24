"""Command line tool for talking to a charger.

Run ``python -m spinev_ble --help`` for usage.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from ipaddress import ip_address

from bleak import BleakScanner
from bleak.backends.device import BLEDevice

from .charger import SpinEvCharger
from .const import ADVERTISED_NAME_PATTERN, DEFAULT_HISTORY_COUNT
from .exceptions import SpinEvError
from .models import OcppConfig
from .transports.ble import BleTransport

_NAME_RE = re.compile(ADVERTISED_NAME_PATTERN)

DNS_PREFIX = "dns:"
"""Marks a host the charger has to resolve, rather than an IP address."""

DEFAULT_OCPP_PATH = "ocpp"

PASSWORD_ENV_VAR = "SPINEV_BLE_PASSWORD"
"""Read instead of ``--password`` so the credential stays out of shell history
and out of the process list."""


async def _scan(timeout: float) -> list[tuple[BLEDevice, str]]:  # noqa: ASYNC109
    """Find likely chargers by advertised name."""
    found: list[tuple[BLEDevice, str]] = []
    devices = await BleakScanner.discover(timeout=timeout)
    for device in devices:
        name = device.name or ""
        if _NAME_RE.match(name):
            found.append((device, name.strip()))
    return found


async def _resolve(address: str | None, timeout: float) -> BLEDevice:  # noqa: ASYNC109
    if address:
        device = await BleakScanner.find_device_by_address(address, timeout=timeout)
        if device is None:
            raise SystemExit(
                f"no device at {address}. Is it in range, and is the phone app closed?"
            )
        return device
    matches = await _scan(timeout)
    if not matches:
        raise SystemExit(
            "no charger found. Move closer, and make sure the phone app is fully "
            "closed, since the charger accepts only one connection at a time."
        )
    if len(matches) > 1:
        print("several chargers found, pass --address to pick one:")
        for device, name in matches:
            print(f"  {device.address}  {name}")
        raise SystemExit(1)
    device, name = matches[0]
    print(f"using {device.address}  {name}")
    return device


async def _open(args: argparse.Namespace) -> SpinEvCharger:
    """Find the charger and build a Bluetooth client for it."""
    device = await _resolve(args.address, args.timeout)
    return SpinEvCharger(
        BleTransport(device, timeout=args.timeout),
        password=args.password,
        timeout=args.timeout,
    )


def _format(value: float | int | str | None, spec: str = "") -> str:
    """Format a reading, or say so when the charger did not supply one."""
    if value is None:
        return "unknown"
    return format(value, spec)


async def _cmd_scan(args: argparse.Namespace) -> None:
    matches = await _scan(args.timeout)
    if not matches:
        print("no chargers found")
        return
    for device, name in matches:
        serial = name.split("_")[0]
        print(f"{device.address}  serial {serial}  name {name!r}")


async def _cmd_password(args: argparse.Namespace) -> None:
    async with await _open(args) as charger:
        password = await charger.async_get_password()
    print(f"password 0x{password:06X} ({password})")
    print(
        "keep this private, it is what lets anyone start and stop your charger. "
        f"Pass it back through the {PASSWORD_ENV_VAR} environment variable "
        "rather than on the command line."
    )


async def _cmd_status(args: argparse.Namespace) -> None:
    async with await _open(args) as charger:
        status = await charger.async_get_status()
    state = status.state.name if status.state else f"unknown ({status.state_value})"
    print(f"state            {state}")
    print(f"power            {_format(status.power_w, '.0f')} W")
    print(f"voltage          {_format(status.voltage_v, '.1f')} V")
    print(f"current          {_format(status.current_a, '.2f')} A")
    print(f"current limit    {_format(status.current_limit_a, '.1f')} A")
    print(f"session energy   {_format(status.session_energy_kwh, '.2f')} kWh")
    print(f"session time     {_format(status.session_seconds)} s")
    print(f"lifetime energy  {_format(status.lifetime_energy_kwh, '.2f')} kWh")
    print(f"lifetime time    {_format(status.lifetime_seconds)} s")
    print(f"firmware         {_format(status.firmware_version)}")
    print(f"alarms           {', '.join(status.alarms) if status.alarms else 'none'}")


async def _cmd_history(args: argparse.Namespace) -> None:
    async with await _open(args) as charger:
        sessions = await charger.async_get_history(args.count)
    if not sessions:
        print("no history returned")
        return
    total = 0.0
    for session in sessions:
        total += session.energy_kwh
        print(
            f"{session.start:%Y-%m-%d %H:%M:%S} to "
            f"{session.end:%Y-%m-%d %H:%M:%S}  {session.energy_kwh:7.2f} kWh"
        )
    print(f"\n{len(sessions)} sessions, {total:.2f} kWh total")


async def _run_control(args: argparse.Namespace, start: bool) -> None:
    action = "start" if start else "stop"
    async with await _open(args) as charger:
        if start:
            await charger.async_start_charging()
        else:
            await charger.async_stop_charging()
        print(f"{action} sent, waiting for the charger to settle")
        await asyncio.sleep(3)
        print(f"state now {(await charger.async_get_state()).name}")


async def _cmd_start(args: argparse.Namespace) -> None:
    await _run_control(args, start=True)


async def _cmd_stop(args: argparse.Namespace) -> None:
    await _run_control(args, start=False)


def _charger_host(host: str) -> str:
    """Return ``host`` the way the charger stores it: an IP as is, a name marked."""
    host = host.strip()
    try:
        ip_address(host)
    except ValueError:
        return host if host.startswith(DNS_PREFIX) else f"{DNS_PREFIX}{host}"
    return host


def _ocpp_url(config: OcppConfig) -> str:
    """Return the address the charger connects to for OCPP."""
    host = config.host.removeprefix(DNS_PREFIX)
    path = f"/{config.path.strip('/')}" if config.path.strip("/") else ""
    return f"ws://{host}:{config.port}{path}/{config.charge_point_id}"


def _print_ocpp(config: OcppConfig) -> None:
    print(f"host             {config.host or 'not set'}")
    print(f"port             {config.port}")
    print(f"path             {config.path or 'not set'}")
    print(f"charge point id  {config.charge_point_id or 'not set'}")
    if config.host:
        print(f"url              {_ocpp_url(config)}")


def _wanted_ocpp(current: OcppConfig, args: argparse.Namespace) -> OcppConfig:
    """Return the settings to write: what was asked for, the rest as they are."""
    charge_point_id = args.charge_point_id or current.charge_point_id
    if not charge_point_id:
        raise SystemExit(
            "the charger has no charge point id, pass one with --charge-point-id"
        )
    return OcppConfig(
        host=_charger_host(args.host),
        port=args.port if args.port is not None else current.port,
        path=args.path if args.path is not None else current.path or DEFAULT_OCPP_PATH,
        charge_point_id=charge_point_id,
    )


async def _cmd_ocpp(args: argparse.Namespace) -> None:
    async with await _open(args) as charger:
        current = await charger.async_get_ocpp_config()
        if args.host is None:
            _print_ocpp(current)
            return
        wanted = _wanted_ocpp(current, args)
        if wanted == current:
            print("the charger already connects there, nothing to change")
            return
        print("current settings, note them to put them back later:")
        _print_ocpp(current)
        await charger.async_set_ocpp_config(wanted)
    print("\nnew settings written, the charger restarts to apply them:")
    _print_ocpp(wanted)


def _parse_password(value: str) -> int:
    """Accept a password as decimal or as ``0x`` hex."""
    try:
        return int(value, 0)
    except ValueError as err:
        raise argparse.ArgumentTypeError(
            f"password must be an integer, decimal or 0x hex, not {value!r}"
        ) from err


def _default_password() -> int | None:
    raw = os.environ.get(PASSWORD_ENV_VAR)
    return _parse_password(raw) if raw else None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spinev-ble",
        description="talk to an Exicom Spin EV charger over Bluetooth",
    )
    parser.add_argument("--address", help="charger MAC, otherwise it is auto detected")
    parser.add_argument("--timeout", type=float, default=20.0, help="seconds")
    parser.add_argument(
        "--password",
        type=_parse_password,
        default=_default_password(),
        help="charger Bluetooth password, decimal or 0x hex. Prefer the "
        f"{PASSWORD_ENV_VAR} environment variable, which keeps it out of shell "
        "history. Omitted means read it from the charger.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scan", help="list nearby chargers")
    sub.add_parser("password", help="read the charger's Bluetooth password")
    sub.add_parser("status", help="read live telemetry")
    sub.add_parser("start", help="start charging")
    sub.add_parser("stop", help="stop charging")
    history = sub.add_parser("history", help="read stored charging sessions")
    history.add_argument("--count", type=int, default=DEFAULT_HISTORY_COUNT)
    ocpp = sub.add_parser(
        "ocpp",
        help="read the OCPP server the charger connects to, or point it elsewhere",
        description="Without --host, print where the charger connects for OCPP. "
        "With --host, point it there, keeping any setting not given, and "
        "restart it. This ends a charging session in progress, and a charger "
        "pointed away from the vendor's server stops using the vendor cloud.",
    )
    ocpp.add_argument("--host", help="IP address or name of the OCPP server")
    ocpp.add_argument("--port", type=int, help="TCP port of the OCPP server")
    ocpp.add_argument("--path", help=f"WebSocket path, {DEFAULT_OCPP_PATH!r} if unset")
    ocpp.add_argument(
        "--charge-point-id", help="id the charger connects as, kept if omitted"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = _build_parser().parse_args(argv)
    handlers = {
        "scan": _cmd_scan,
        "password": _cmd_password,
        "status": _cmd_status,
        "history": _cmd_history,
        "start": _cmd_start,
        "stop": _cmd_stop,
        "ocpp": _cmd_ocpp,
    }
    try:
        asyncio.run(handlers[args.command](args))
    except SpinEvError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
