# Overview

`scietex.dac_service` is a [`scietex.service`](https://github.com/bond-anton/scietex.service)
worker connected to Valkey that owns one or more SPI DAC devices through
[`scietex.hal.dac`](https://github.com/bond-anton/scietex.hal.dac). It opens the
configured converters, exposes listing, readback, writes, configuration, and
status as `dac:*` tasks, and publishes a telemetry snapshot of the live devices
and their last written channel state.

## What it does

- **Owns the SPI bus and its converters.** Each configured device is built once
  from `dac/dac.yml` and opened at startup; a device that fails to open does not
  stop the worker.
- **Serves the DAC contract as tasks.** Clients submit `dac:list`, `dac:read`,
  `dac:write`, `dac:configure`, and `dac:status` over Valkey task lanes. Writes
  and readbacks reach the bus at request time.
- **Runs as a daemon** on the `scietex.service` framework, with signal handling,
  heartbeat, watchdog, and graceful shutdown inherited from `ValkeyWorker`.
- **Accepts remote configuration** through the framework's config channel — see
  [Remote configuration](remote-config.md).
- **Degrades per device.** A converter that will not open is reported as
  `available: false` with the recorded error, and the remaining devices keep
  working.
- **Never polls the bus for telemetry.** DAC state is the last written value per
  channel, held in memory, so the 2 s telemetry tick cannot disturb a write.

## How it fits together

```
client ──▶ Valkey task lanes ──▶ DacWorker ──▶ SPI bus ──▶ DAC parts
              (control, data)         │
                                      └──▶ telemetry key
```

`DacWorker` is a `ValkeyWorker` subclass. It owns a `DacDeviceRegistry`, which
maps each validated settings entry onto a live `scietex.hal.dac` object, and
five task handlers — three control-lane (`dac:list`, `dac:configure`,
`dac:status`) and two data-lane (`dac:read`, `dac:write`). The registry is
rebuilt whenever the effective settings change, so a remote config apply is
reflected in the live device set.

The HAL device classes are documented in
[`scietex.hal.dac`](https://github.com/bond-anton/scietex.hal.dac).

## Lifecycle

The worker starts whether or not any device opens. It listens on no port and
has no TCP or serial listener of its own: its only interface is the Valkey task
lanes plus the telemetry key.

Device state is per-entry. When the registry rebuilds, each configured device is
opened independently and a failure is recorded rather than raised. In the
degraded case where zero devices open:

- `dac:read`, `dac:write`, `dac:configure`, and `dac:status` against a configured
  but unopened id return `DEVICE_UNAVAILABLE` (retryable — the hardware may
  reappear).
- `dac:list` still succeeds and reports each device with `available: false` and
  the recorded `error`.
- Telemetry carries the same `available: false` and `error` fields.

An id that was never configured is a different outcome: `DEVICE_NOT_FOUND`,
which is a request error, not a hardware state.

## Requirements

- **Python** 3.10 or higher.
- An **SPI bus** reachable as `/dev/spidevX.Y`, or the gpiozero pin backend with
  `clock_pin`/`mosi_pin`/`miso_pin`/`select_pin` wired. The `spi` or `gpiozero`
  extra supplies the matching backend — see the [README](../README.md).
- Network access to a **Valkey** (Redis-compatible) instance for the worker
  framework's task transport.
- **Linux** (the container image targets Debian Bookworm).

## Configuration layers

Effective settings resolve through four layers, later layers winning: constructor
defaults < `dac/dac.yml` < the framework's `dac/config.yml` snapshot < the remote
`dac` section. The frame is a field-level patch; a `devices` entry is replaced
wholesale. See [Configuration](configuration.md) for the schema, the merge
rules, and validation behavior.

## Tasks

| Task | Lane | Request | Result |
|---|---|---|---|
| `dac:list` | control | `device_id` (optional; omit to list all) | `devices[]` with `device_id`, `type`, `available`, `error`, `channels`, `resolution`, `max_code`, `unit`, `reference`, `readable` |
| `dac:read` | data | `device_id`, `channel` (default `0`) | `device_id`, `channel`, `code`, `unit`, `reference`, `max_code` |
| `dac:write` | data | `device_id`, `channel` (default `0`), exactly one of `code` / `output` | `device_id`, `channel`, `code`, `output`, `unit`, `max_code` |
| `dac:configure` | control | `device_id`, optional `range`, `slew`, `sr_clock`, `sr_step`, `rext`, `dcen`, `output` | `device_id`, `range`, `slew`, `sr_clock`, `sr_step`, `rext`, `dcen`, `output_enabled` |
| `dac:status` | control | `device_id` | geometry, `last_written[]`, `status`, `control` |

`dac:write` accepts exactly one of `code` and `output`: a raw code is validated
against the part's range, an output value is scaled and clamped to the span.
A write-only part reports `output: null` in the write result because computing
it would need a register readback. `dac:read` on a write-only part returns
`DAC_READBACK_ERROR`.

`dac:configure` forwards only the fields present in the request, so an omitted
field keeps its previous value. The response is decoded from the control
register when the part exposes one, otherwise the applied values are echoed.
`dac:status` reports the decoded `status` and `control` registers only for parts
that expose them (the AD54x0 family).

## Telemetry

The worker publishes to
`scietex:DacService:<instance_id>:telemetry` — msgpack, every 2.0 s, with a 6 s
TTL (so a single missed beat does not expire the key). The snapshot lists every
configured device with its open state, `type`, and per-channel `code`, `output`,
and `unit`. Channel entries come from the in-memory last-written cache; the bus
is never touched during a telemetry tick, and a channel that was never written
has no entry.

## Documentation

| Page | Contents |
| --- | --- |
| [Configuration](configuration.md) | The `dac.yml` schema, parts, defaults, precedence, and validation |
| [Deployment](deployment.md) | Container, SPI device access, Quadlet |
| [Remote configuration](remote-config.md) | The declarative `dac` section |
