# spinev-ble

Local Bluetooth LE control for Exicom Spin EV chargers.

The core is a **dependency free codec**. It turns commands into bytes and bytes back into values, and never touches a radio. How those bytes reach the charger is your choice: bleak, an ESPHome Bluetooth proxy, a serial bridge, or nothing at all if you only want to inspect frames.

No cloud, no vendor app, no account, no internet. Not affiliated with Exicom.

## Install

```bash
pip install spinev-ble            # codec only, zero dependencies
pip install spinev-ble[bleak]     # adds the optional BLE client and CLI
```

Python 3.11 or newer.

## Just the protocol

Nothing here needs a Bluetooth stack. Build a frame, send it however you like, decode what comes back.

```python
from spinev_ble import Command, Register, build_control, build_read
from spinev_ble import parse_frame, decode_float, decode_uint, ChargerState

build_control(Command.START, password=0xABCDEF).hex()  # '10ac3c0101abcdef'
build_control(Command.STOP, password=0xABCDEF).hex()  # '10ac3c0110abcdef'
build_read(Register.POWER).hex()  # '10ac840000000000'

reply = parse_frame(bytes.fromhex("10ac840045713d71"))
decode_float(reply.raw)  # 3859.84 watts

state = decode_uint(bytes.fromhex("00000004"))
ChargerState(state)  # ChargerState.CHARGING
```

Write the bytes to characteristic `49535343-1e4d-4bd9-ba61-23c647249616` with response, and read replies as notifications on that same characteristic. That is the whole transport contract.

## Optional BLE client

Needs the `bleak` extra. Convenience only, the codec above is the real library.

```python
import asyncio

from bleak import BleakScanner
from spinev_ble import SpinEvCharger


async def main() -> None:
    device = await BleakScanner.find_device_by_address("AA:BB:CC:DD:EE:FF")
    if device is None:
        raise SystemExit("charger not found, is the phone app connected to it?")

    async with SpinEvCharger(device, password=0xABCDEF) as charger:
        status = await charger.async_get_status()
        print(status.state.name, status.power_w, "W")

        await charger.async_start_charging()
        await asyncio.sleep(3)
        await charger.async_stop_charging()


asyncio.run(main())
```

`ChargerStatus` is an immutable snapshot. Every reading is `None` if the charger did not supply it, so check before formatting. When the charger reports a state this library does not name, `status.state` is `None` and `status.state_value` holds the raw number, rather than the whole read failing.

Register reads are logged as an id and length, never the decoded value, since some registers hold credentials.

### Custom transports

`SpinEvCharger` does not care what carries the bytes. Pass `client_class` to swap the transport for anything matching `BleakClientLike`, which is the handful of members the client actually uses:

```python
from spinev_ble import SpinEvCharger

charger = SpinEvCharger(device, password, client_class=MyTransport)
```

It is called as `client_class(device, timeout=..., disconnected_callback=...)`.

## Command line

Installed with the `bleak` extra.

```bash
spinev-ble scan                  # find chargers nearby
spinev-ble password              # ask the charger for its own password
spinev-ble status                # live telemetry
spinev-ble history               # stored charging sessions
spinev-ble start                 # start charging
spinev-ble stop                  # stop charging
```

Or `python -m spinev_ble ...` if you prefer.

Pass the password through the environment rather than `--password`, so it stays out of your shell history and out of the process list:

```bash
export SPINEV_BLE_PASSWORD=0xABCDEF
spinev-ble start
```

## Your charger's Bluetooth password

Start and stop commands carry a per charger password. Reads do not need it, so telemetry works without one.

Ask the charger for it:

```bash
spinev-ble password
```

That reads register `0x32` on your own charger.

Treat it as a credential: do not commit it, and do not paste it into an issue.

## What is supported

| Feature | Register | API |
|---|---|---|
| Start and stop charging | `0x3C` | `async_start_charging`, `async_stop_charging` |
| Charger state | `0x67` | `async_get_state`, `async_get_state_value` |
| Active power, voltage, current | `0x84`, `0x0A`, `0x14` | `async_get_power`, `async_get_voltage`, `async_get_current` |
| Session energy and duration | `0x35`, `0x59` | `async_get_status` |
| Lifetime energy and duration | `0x65`, `0x6A` | `async_get_status` |
| Charging history | `0x68` | `async_get_history` |
| Firmware version | `0x52` | `async_get_status` |
| Active alarms | `0x39` | `async_get_alarms` |
| Charging current limit | `0x4F` | `async_get_current_limit`, `async_set_current_limit` |
| Charger password | `0x32` | `async_get_password` |
| WiFi settings | `0x61`, `0x63` | `async_get_wifi_ssid`, `async_set_wifi` |
| OCPP settings | `0x5E`, `0x60`, `0x62`, `0x64` | `async_get_ocpp_config`, `async_set_ocpp_config` |

Only the first alarm bank is decoded. A second bank exists, but its bit assignments are not known.

## Things worth knowing

**Writing network settings can strand the charger.** `async_set_wifi` and `async_set_ocpp_config` change how the charger reaches the outside world. A wrong value leaves it unable to connect until it is re-provisioned. Read the values back afterwards, and keep the phone app available to restore the originals.

**Current limits are model specific.** `async_set_current_limit` guards against anything below 6 A or above 32 A. 32 A is the ceiling of the top single phase unit; three phase and lower rated models differ, so pass `max_amps` to match yours and read the limit back to confirm what the charger accepted.

**One connection at a time.** These chargers accept a single Bluetooth client. While the phone app is connected you cannot connect, and vice versa. Close the app fully, not just to the background.

**Commands are not instant.** The charger acknowledges immediately, updates its state register after about a second, and starts delivering power about two seconds later. Do not treat a missing state change in the first second as a failure.

**History timestamps are not charging duration.** A record's end timestamp lines up with the next record's start, so it marks unplug time rather than the moment charging stopped. Sessions can span days while only drawing power for part of that. Do not compute average power from energy divided by duration.

**History is a rolling window.** The charger keeps only recent sessions, not everything.

**Do not poll hard.** Once every thirty seconds is plenty for monitoring, and five seconds is enough for a live power graph.

## Development

```bash
uv sync
uv run pytest
uv run prek run --all-files
uv run pylint src/spinev_ble
```

Install the git hook so `prek` runs automatically on commit:

```bash
uv run prek install
```

## Disclaimer

Independent, unofficial project providing Python API bindings for local access. Not affiliated with, endorsed by, or supported by Exicom.

Provided "as is", without warranty of any kind, express or implied, as stated in the Licence below. Use of this library, and of your charger's Bluetooth interface, is at your own risk and subject to your charger's own terms of use and warranty.

## Licence

MIT
