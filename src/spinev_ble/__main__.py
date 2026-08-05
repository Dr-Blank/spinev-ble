"""Command line tool for talking to a charger.

Run ``python -m spinev_ble --help`` for usage.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

from bleak import BleakScanner
from bleak.backends.device import BLEDevice

from .client import SpinEvCharger
from .const import ADVERTISED_NAME_PATTERN, DEFAULT_HISTORY_COUNT
from .exceptions import SpinEvError

_NAME_RE = re.compile(ADVERTISED_NAME_PATTERN)

#: Read instead of ``--password`` so the credential stays out of shell history
#: and out of the process list.
PASSWORD_ENV_VAR = "SPINEV_BLE_PASSWORD"


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
    device = await _resolve(args.address, args.timeout)
    async with SpinEvCharger(device, timeout=args.timeout) as charger:
        password = await charger.async_get_password()
    print(f"password 0x{password:06X} ({password})")
    print(
        "keep this private, it is what lets anyone start and stop your charger. "
        f"Pass it back through the {PASSWORD_ENV_VAR} environment variable "
        "rather than on the command line."
    )


async def _cmd_status(args: argparse.Namespace) -> None:
    device = await _resolve(args.address, args.timeout)
    async with SpinEvCharger(device, args.password, timeout=args.timeout) as charger:
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
    device = await _resolve(args.address, args.timeout)
    async with SpinEvCharger(device, args.password, timeout=args.timeout) as charger:
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
    device = await _resolve(args.address, args.timeout)
    action = "start" if start else "stop"
    async with SpinEvCharger(device, args.password, timeout=args.timeout) as charger:
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
