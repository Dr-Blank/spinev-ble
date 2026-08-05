## What this project is

Published PyPI lib: local Bluetooth LE control, Exicom Spin EV chargers. Core (`const`, `protocol`, `models`, `exceptions`) pure codec, **zero runtime deps**. `client` and `__main__` optional, behind `bleak` extra. People install this, build on it — public API contract, not draft.

## Never leak secrets

No real credential or device identifier goes into code, tests, docs, comments, commit messages, fixtures. Includes:

- Bluetooth passwords, WiFi passwords, OCPP credentials
- charger serial numbers, charge point ids, MAC addresses
- Bluetooth captures, HCI snoop logs, bytes extracted from them

Use obvious placeholders instead: `0xABCDEF` for password,
`AA:BB:CC:DD:EE:FF` for MAC, `example.com` for host, hand-built synthetic
values for record fixtures. Fixture mirrors real frame layout → keep layout, replace data.

Library never logs payload bytes. Several registers carry credentials —
log register id and length, never value.

## Comments and docs describe the library, not its history

Every comment, docstring, README line, error message says **what something is
and how to use it**. Nothing says how library came to exist.

Don't write:

- how protocol was worked out, or from what
- "verified against my charger", "unverified", "confirmed by traffic analysis"
- that AI wrote, generated, or reviewed any of it
- changelog-style narration in code ("previously this used…", "now fixed")

Write behaviour caller needs: what value means, what range, what happens on
failure, what's risky. Safety warnings stay — as statements about
consequences ("wrong value leaves charger unable to reach any network until
re-provisioned"), never as statements about provenance.

## Coding practices

- Public names carry docstrings. Constants use `#:` comments — self-documenting.
- Type everything. `mypy --strict` covers `src` and `tests`, must pass.
- Raise package's own exceptions from `exceptions.py`, never bare
  `ValueError` or `RuntimeError` — callers catch `SpinEvError`.
- No `assert` for runtime invariants in library code; `python -O` strips it.
  Raise instead.
- Validate inputs reaching charger. Guard preventing bricked device worth more
  than convenience of skipping it.
- Keep codec pure. `protocol.py` does no I/O, imports nothing optional.
- `bleak` import stays confined to `client.py` and `__main__.py`, reached
  lazily from `__init__.py`. Importing `spinev_ble` must work without bleak
  installed.
- New behaviour comes with tests. Client tested through fake transport in
  `tests/conftest.py`, never against real hardware.

## Gates

All must pass before work done:

```bash
uv run pytest
uv run prek run --all-files
uv run pylint src/spinev_ble
```

## Git

Don't commit, push, tag unless explicitly asked. Prep changes in working
tree, describe them.
