# scietex.dac_service

**scietex.dac_service** is a [`scietex.service`](https://github.com/bond-anton/scietex.service)
worker connected to Valkey that owns one or more SPI DAC devices through
[`scietex.hal.dac`](https://github.com/bond-anton/scietex.hal.dac) (23 supported
parts: voltage converters, current-loop transmitters, and digital
potentiometers). It opens the configured devices at startup, exposes writes,
reads, and configuration as `dac:*` tasks, and publishes the last-written state
as telemetry without touching the bus. A device that fails to open does not stop
the worker: it is recorded, reported as unavailable, and the remaining devices
keep working. Configuration can also be pushed at runtime through the
framework's remote-config channel.

**Python ≥ 3.10** · **License: MIT**

## Documentation

- [Overview](docs/index.md) — what the service does and how it fits together
- [Configuration](docs/configuration.md) — the `dac.yml` schema and precedence
- [Deployment](docs/deployment.md) — container, SPI device access, Quadlet
- [Remote configuration](docs/remote-config.md) — the declarative `dac` section

## Installation

```bash
pip install "scietex.dac_service[spi]"      # spidev backend for /dev/spidev*
pip install "scietex.dac_service[gpiozero]" # plain-GPIO backend
```

The core package pulls in `scietex.hal.dac`, `scietex.service[valkey]`, and
`msgspec`. The backend extras are optional: a core install can still build a
device from a caller-supplied transport, but an operator normally installs the
extra that matches the board's wiring.

## Quick start

### 1. Write a configuration file

The service reads `dac/dac.yml` under its config directory. Create it by hand,
or let the service generate a default on first run. The file is the bootstrap
layer of a four-layer merge: constructor defaults < `dac.yml` < the framework's
`config.yml` snapshot < the remote `dac` section.

### 2. Run the service

```bash
start-dac-service --conf-dir /etc/scietex
```

The config directory resolves automatically when `--conf-dir` is omitted. The
service runs in the foreground until `SIGINT` or `SIGTERM`.

### 3. Write a channel

Submit a `dac:write` task to the worker with payload
`{"device_id": "dac0", "channel": 0, "output": 1.5}` — or use `code` for a raw
value. Exactly one of `code` and `output` must be set. The result carries the
code the channel holds and, for a write-only part, the scaled output.

## Configuration at a glance

Devices are keyed by a **string** id, which names the device in task payloads
and telemetry:

```yaml
devices:
  dac0:
    type: MCP4822
    port: 0
    device: 0
    reference: 2.048
  dac1:
    type: AD420
    range: 4-20mA
    latch_pin: 27
  dac2:
    type: AD5420
    port: 0
    device: 1
```

`MCP4822` is a dual 12-bit voltage converter with a fixed 2.048 V full scale.
`AD420` is a single 16-bit current transmitter: its `range` only declares the
RANGE SELECT strap, and `latch_pin` is the GPIO line that latches each frame.
`AD5420` is a 16-bit current source that needs `dac:configure` to select a range
and enable its output before a write reaches the loop.

Per-device fields: `type` (required), `port`/`device` for the SPI wiring, the
gpiozero pins (`clock_pin`/`mosi_pin`/`miso_pin`/`select_pin`), `reference`,
`label`, `log_level`, and the part-specific `range`, `load_pin`, `latch_pin`,
`r_ab`, `grounded`. Unknown fields are rejected. `port` is the SPI bus number
and `device` is the chip-select.

## Supported parts

| Parts | Channels | Resolution | Readable | Required extra |
|---|---|---|---|---|
| MCP4231, MCP4241 | 2 | 7 | yes | — |
| MCP4251, MCP4261 | 2 | 8 | yes | — |
| MCP4802, MCP4812, MCP4822 | 2 | 8 / 10 / 12 | no | — |
| MCP4902, MCP4912, MCP4922 | 2 | 8 / 10 / 12 | no | — |
| DAC8531, DAC8551 | 1 | 16 | no | — |
| DAC8552 | 2 | 16 | no | — |
| DAC121S101 | 1 | 12 | no | — |
| AD5302, AD5312, AD5322 | 2 | 8 / 10 / 12 | no | — |
| DAC8512 | 1 | 12 | no | `load_pin` |
| AD7390, AD7391 | 1 | 12 / 10 | no | `load_pin` |
| AD5410, AD5420 | 1 | 12 / 16 | yes | — |
| AD420 | 1 | 16 | no | `range`, `latch_pin` |

"Readable" means the part can report its registers back, so `dac:read` and
register-level `dac:status` work. A write-only part still accepts `dac:write`
and reports the cached code. The potentiometers additionally take `r_ab` and
`grounded`; the current-loop parts take `range`.

## Tasks

| Task | Lane | Request | Result |
|---|---|---|---|
| `dac:list` | control | optional `device_id` | `devices[]` with `device_id`, `type`, `available`, `channels`, `resolution`, `max_code`, `unit`, `reference`, `readable`, `error` |
| `dac:status` | control | `device_id` | `device_id`, `type`, `available`, `error`, `readable`, `channels`, `resolution`, `max_code`, `unit`, `reference`, `last_written[]`, `status`, `control` |
| `dac:configure` | control | `device_id`, optional `range`, `slew`, `sr_clock`, `sr_step`, `rext`, `dcen`, `output` | `device_id`, `range`, `slew`, `sr_clock`, `sr_step`, `rext`, `dcen`, `output_enabled` |
| `dac:read` | data | `device_id`, optional `channel` | `device_id`, `channel`, `code`, `unit`, `reference`, `max_code` |
| `dac:write` | data | `device_id`, optional `channel`, exactly one of `code`/`output` | `device_id`, `channel`, `code`, `output`, `unit`, `max_code` |

`dac:configure` is only supported by parts that expose a control register; a
part without one answers `DAC_CONFIGURE_UNSUPPORTED`. A configured device that
failed to open answers `DEVICE_UNAVAILABLE` (retryable) from `dac:status`,
`dac:read`, and `dac:write`, while `dac:list` reports it as
`available: false` instead of failing the listing.

## Telemetry

The worker publishes to `scietex:DacService:<instance_id>:telemetry` every 2 s
with a 6 s TTL. The snapshot lists every configured device with its open state,
part type, and the channel state left by the last successful write. It is built
from the registry's last-written cache and live channel objects only: a
telemetry tick never touches the bus, so it cannot disturb an in-flight write,
and a write-only part still reports state. A device that was never written
reports no channels rather than fabricated zeros.

## Container and quadlet

```bash
podman build --build-arg VERSION=0.1.0 -t scietex-dac-service .
podman run --rm --device /dev/spidev0.0 -v ./config:/config scietex-dac-service
```

The container only dials Valkey, so it publishes no port and needs no
capabilities. `/dev/spidevX.Y` must be passed with `--device`; without it the
worker still starts and reports the device unavailable.

For a persistent deployment, install `deploy/dac-service.container` as
`~/.config/containers/systemd/dac-service.container` and the SPI drop-in as
`~/.config/containers/systemd/dac-service.container.d/50-spi-devices.conf`,
then `systemctl --user daemon-reload && systemctl --user start dac-service`.
The Quadlet bind-mounts `~/.config/scietex`, so `dac/dac.yml` stays
hand-editable on the host.

## Development

Dependencies are managed with **uv**; checks and tests run through **tox**:

```bash
uv sync --all-extras
uv run tox                    # format, lint, type, py314
uv run tox -e py314           # tests only
```
