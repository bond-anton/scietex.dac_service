# Deployment

How to run `scietex.dac_service` as a container (Podman or Docker) on a host
with SPI DAC hardware, and how to give the worker access to the SPI device
nodes. The service is a plain Python console script that talks to Valkey; see
[Configuration](configuration.md) for the `dac/dac.yml` format and
[Remote configuration](remote-config.md) for runtime updates.

## Entry point

The package installs one console script:

```bash
start-dac-service
```

| Flag | Environment fallback | Default | Meaning |
| --- | --- | --- | --- |
| `--conf-dir` | `SCIETEX_CONFIG_DIR` | auto-resolved | Configuration directory containing `dac/dac.yml` |
| `--service-name` | `SCIETEX_SERVICE_NAME` | `DacService` | Service name reported to the framework |
| `--logging-level` | `SCIETEX_LOGGING_LEVEL` | `INFO` | Logging level |

Precedence is **CLI flag > environment variable > built-in default**.
`--conf-dir` defaults to `None` so the framework's `prepare_conf_dir` resolves
the directory itself — that resolver already honors `SCIETEX_CONFIG_DIR`.

The worker runs in the foreground until the framework tears it down. `podman
stop` / `systemctl --user stop` send `SIGTERM`, which the framework handles
without extra wiring.

## Container image

| Item | Value |
| --- | --- |
| Image | `registry.buro-nts.ru/scietex-dac-service` |
| Entrypoint | `start-dac-service` |
| Config dir | `/config` (`SCIETEX_CONFIG_DIR`) |
| Config volume | `VOLUME ["/config"]` |
| User | `appuser` (non-root) |

The image installs the released wheel
`scietex.dac_service[$EXTRAS]==$VERSION` from PyPI, so **the matching version
must already be published on PyPI** or the build fails at the `pip install`
step. The image and the PyPI artifact for a version are the same code.

`EXTRAS` selects the DAC backend:

| Extra | Installs | For |
| --- | --- | --- |
| `spi` (default) | `scietex.hal.dac[spi]` | `/dev/spidev*` SPI buses |
| `gpiozero` | `scietex.hal.dac[gpiozero]` | plain GPIO pins via `gpiochip` |

## Build

```bash
./build_image.sh
```

```bash
./build_image.sh --latest
```

The script reads the version from `src/scietex/dac_service/version.py`, builds a
multi-arch manifest for `linux/amd64,linux/arm64`, installs the matching PyPI
wheel, and pushes to `registry.buro-nts.ru/scietex-dac-service`. `--latest`
pushes the additional `latest` tag.

To build for the GPIO backend:

```bash
./build_image.sh --extras gpiozero
```

`--extras` accepts any extra name from `pyproject.toml`. Builds are multi-arch
and pushed directly to the registry; there is no local-only mode.

## Run

```bash
podman run --rm \
  --device /dev/spidev0.0 \
  -v ~/.config/scietex:/config:z \
  registry.buro-nts.ru/scietex-dac-service:latest
```

| Option | Purpose |
| --- | --- |
| `--device /dev/spidev0.0` | Passes the SPI node into the container |
| `-v ~/.config/scietex:/config:z` | Host-owned config tree, SELinux-relabelled as shared (`:z`) |

Without `--device`, the worker still starts: the device node is absent, so the
registry records every configured device as unavailable and `dac:list` reports
`available: false` for each. Nothing else fails.

## Quadlet (systemd user unit)

Install `deploy/dac-service.container` as
`~/.config/containers/systemd/scietex-dac-service.container`:

```ini
[Unit]
Description=Scietex DAC service (SPI)
After=network-online.target
Wants=network-online.target

[Container]
Image=registry.buro-nts.ru/scietex-dac-service:latest
ContainerName=scietex-dac-service
AutoUpdate=registry
Environment=SCIETEX_SERVICE_NAME=DacService
Environment=SCIETEX_LOGGING_LEVEL=INFO
EnvironmentFile=-%h/.config/scietex/dac.env
# Bind mount rather than a named volume so dac/dac.yml stays hand-editable on
# the host at ~/.config/scietex/dac/dac.yml.
Volume=%h/.config/scietex:/config:z
AddDevice=/dev/spidev0.0
# The service only dials Valkey; it never listens. No PublishPort, no
# NET_BIND_SERVICE.
NoNewPrivileges=true
ReadOnly=true
Tmpfs=/tmp:rw,size=16m

[Service]
Restart=on-failure
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=default.target
```

Then reload systemd and start it:

```bash
systemctl --user daemon-reload
systemctl --user start scietex-dac-service
```

The `Volume=` line bind-mounts the host config tree instead of using a named
volume, so `~/.config/scietex/dac/dac.yml` is editable on the host and survives
container replacement. `:z` applies the shared SELinux relabel
(`container_file_t`, no private MCS category), which is what a tree mounted by
more than one container needs. `ReadOnly=true` makes the container root
filesystem read-only; `Tmpfs=/tmp` supplies the only writable scratch space.

The service never listens on a socket — it only dials Valkey — so the unit has
no `PublishPort`, the image has no `EXPOSE`, and no `NET_BIND_SERVICE` capability
is needed.

## SPI device drop-in

Install `deploy/dac-service.container.d/50-spi-devices.conf` as
`~/.config/containers/systemd/scietex-dac-service.container.d/50-spi-devices.conf`:

```ini
[Container]
# Additional SPI buses and, for the gpiozero backend, the GPIO controller.
AddDevice=/dev/spidev0.1
AddDevice=/dev/spidev1.0
AddDevice=/dev/gpiochip0
# Map the container uid to the invoking user so host-owned device nodes are
# writable inside the container.
UserNS=keep-id
# Pass the host supplementary groups that own the device node through to the
# container, without the image knowing their GIDs.
GroupAdd=keep-groups
# Device nodes cannot be relabelled for the container; disable per-container
# SELinux labelling for this unit.
SecurityLabelDisable=true
# DeviceAllow= is a [Service] systemd key, not a Quadlet [Container] key. It
# belongs in [Service] only if this unit is additionally hardened with
# DevicePolicy=closed; the default policy already permits the AddDevice= nodes.
```

The four settings exist because rootless Podman does not otherwise reach the
device nodes:

| Setting | Rationale |
| --- | --- |
| `AddDevice=` | Makes the listed nodes present inside the container; extend the list for every bus or `gpiochip` used |
| `UserNS=keep-id` | Maps the container uid to the invoking host user, so a host-owned device node is openable |
| `GroupAdd=keep-groups` | Passes the host supplementary groups that own the node through, without baking their GIDs into the image |
| `SecurityLabelDisable=true` | Device nodes cannot be relabelled per container, so per-container SELinux labelling is turned off for this unit |

`DeviceAllow=` is a `[Service]` systemd key, not a `[Container]` Quadlet key. It
is only relevant when the unit additionally sets `DevicePolicy=closed`; with the
default policy the `AddDevice=` nodes are already permitted.

## Diagnostics

| Symptom | Cause |
| --- | --- |
| No `/dev/spidev*` node inside the container | `--device` (or Quadlet `AddDevice=`) missing |
| `PermissionError` opening the node | uid/group mapping — the drop-in (`UserNS=keep-id`, `GroupAdd=keep-groups`) is not installed |
| `No such device` | Wrong bus or chip-select index in the device's `port` / `device` field |
| `DAC_READBACK_ERROR` from `dac:read` | The part is write-only; read the last written value from `dac:status` or telemetry instead |
| `DAC_OUTPUT_DISABLED` from `dac:write` | An AD54x0 part whose OUTEN bit is clear; run `dac:configure` with `output: true` first |
| `DAC_CONFIGURE_UNSUPPORTED` | `dac:configure` on a part with no `configure()` method |
| `DEVICE_NOT_FOUND` from any task | Device id absent from `dac/dac.yml` |
| `DEVICE_UNAVAILABLE` from any task | Device configured but the bus would not open |

## Multiple devices on one bus

One `dac.yml`, one worker. Each SPI device is a separate entry with its own
`port` (bus) and `device` (chip select), so several converters can share a bus:

```yaml
devices:
  dac0:
    type: MCP4822
    port: 0
    device: 0
    reference: 4.096
  dac1:
    type: DAC8552
    port: 0
    device: 1
    reference: 5.0
```

`port` is the bus number and `device` is the chip select, so `dac0` resolves to
`/dev/spidev0.0` and `dac1` to `/dev/spidev0.1`. Both values are integers, not
paths. Each distinct node must also be passed through with `--device` or
`AddDevice=`. See [Configuration](configuration.md) for the full field list.

## Health and shutdown

The worker exposes no HTTP server and no health endpoint. Liveness is the
telemetry key:

| Property | Value |
| --- | --- |
| Key | `scietex:DacService:<instance_id>:telemetry` |
| Encoding | msgpack |
| Interval | 2.0 s |
| TTL | 6 s |

A missing or expired key means the worker is gone; a key with a device reporting
`available: false` means only that device is gone. A container healthcheck should
probe the key through `valkey-cli`, not an HTTP port.

Shutdown is framework-driven: `SIGTERM` stops the managers, closes every open
device, and releases the Valkey connection. The Quadlet sets
`TimeoutStopSec=30`, which is the budget the framework has to finish.

## See also

- [Configuration](configuration.md) — `dac/dac.yml` and the settings layers
- [Remote configuration](remote-config.md) — live applies over Valkey
- [Index](index.md)
- [README](../README.md)
