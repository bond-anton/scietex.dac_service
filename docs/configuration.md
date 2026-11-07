# Configuration

The service reads its bootstrap settings from `dac/dac.yml`, a YAML file in a
`dac/` subdirectory of the config directory. The file is the **L1 bootstrap
patch** of the framework's four-layer merge: a field-level map merged onto the
constructor defaults, then overlaid by the framework's `dac/config.yml` snapshot
and the remote `dac` section. The merged result is validated through
`DacServiceSettings`, a frozen struct with `forbid_unknown_fields=True`, so a
typo in a field name is a hard error, not a silently ignored key.

## Config directory resolution

The config directory is resolved once, at worker construction, by the framework's
`prepare_conf_dir`. The first **existing** directory wins:

1. The `--conf-dir` CLI argument
2. `SCIETEX_CONFIG_DIR`
3. `$XDG_CONFIG_HOME/scietex`
4. `~/.config/scietex`
5. `/etc/scietex`
6. `/usr/local/etc/scietex`
7. `./config` (current working directory)
8. `~/.config/scietex` — **created** if none of the above exist

Only step 8 creates a directory; steps 1–7 require the directory to already
exist. A set-but-nonexistent `SCIETEX_CONFIG_DIR` is silently skipped, so in a
container always create the directory before the worker starts.

## File layout

The service namespaces its files under a `dac/` subdirectory so that multiple
`scietex.*` services sharing one config directory cannot collide:

```
<conf_dir>/
└── dac/
    ├── dac.yml        # service-owned bootstrap (this page)
    └── config.yml     # framework snapshot: core: + services.dac (see remote-config.md)
```

`DAC_CONFIG_SUBDIR` in `config.py` is the single source of truth for the
subdirectory name; `DAC_CONFIG_FILE` names the bootstrap file. The service sets
`config_file="dac/config.yml"`, so the framework's snapshot lands at
`<conf_dir>/dac/config.yml` and holds `core:` plus `services.dac`. The framework
writes exactly this one snapshot per service; there is no separate shared root
`config.yml`.

`dac.yml` is the **L1 bootstrap patch**: a field-level map that holds only the
`dac` section. It can never carry a `core:` key — the merged section is converted
through `DacServiceSettings`, whose `forbid_unknown_fields=True` rejects that as
an unknown field. The framework never writes `dac.yml`.

## Settings precedence

Effective settings resolve through four layers, later layers winning:

```
constructor default (L0)  <  dac.yml (L1)  <  config.yml (L2)  <  remote dac section (L3)
```

Each layer is a **field-level patch**, not a whole-object replacement. A key has
three states:

- **absent** — inherit the value from the layer below;
- **`null`** — clear the field, falling back to the L0 constructor default;
- **a value** — set the field.

The L0 base is `DAC_SETTINGS_DEFAULTS` (a concrete `DacServiceSettings`
instance). `config.yml` is the namespaced snapshot at
`<conf_dir>/dac/config.yml`; a successful remote apply auto-writes it, so it
mirrors the last-applied remote config and is re-applied ahead of the remote read
on the next start. See [Remote configuration](remote-config.md).

A `devices` entry is an exception to field-level merge: an entry is replaced
**wholesale**, never deep-merged with the entry of the same id below it. A device
present only in a lower layer is kept; a device present in the winning layer
takes that layer's entry as-is.

## `dac.yml` top-level fields

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `devices` | mapping | `{}` | Devices keyed by **string** device id |

There is no service-wide polling switch: DAC telemetry reads an in-memory cache
and never touches the bus. An empty mapping is a valid configuration — the
worker starts with no devices and reports an empty `dac:list`.

Device keys are strings (`dac0`, `pressure-loop`), not integers; the key becomes
the device's `device_id` in task payloads and telemetry, so the key and the id
can never disagree.

## Device fields

Every entry under `devices` is a `DacDeviceSettings`:

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `type` | string | — (required) | Part name from the supported table |
| `port` | int or null | `null` | SPI bus number for the spidev backend |
| `device` | int or null | `null` | Chip-select for the spidev backend |
| `clock_pin` | int or string or null | `null` | SPI clock pin (gpiozero backend) |
| `mosi_pin` | int or string or null | `null` | SPI MOSI pin (gpiozero backend) |
| `miso_pin` | int or string or null | `null` | SPI MISO pin (gpiozero backend) |
| `select_pin` | int or string or null | `null` | Chip-select pin (gpiozero backend) |
| `reference` | float or null | `null` | Full-scale reference used to scale codes into output units |
| `label` | string or null | `null` | Human-readable label forwarded to the HAL |
| `log_level` | string or null | `null` | Per-device log level forwarded to the HAL |
| `range` | string or null | `null` | Output range name (AD420) |
| `load_pin` | int or string or null | `null` | Load-strobe GPIO (DAC8512, AD7390, AD7391) |
| `latch_pin` | int or string or null | `null` | Latch GPIO (AD420) |
| `r_ab` | float or null | `null` | End-to-end resistance in ohms (digital potentiometers) |
| `grounded` | string or null | `null` | Grounded terminal `A` or `B` (digital potentiometers) |

Fields left unset (`null` or absent) are not forwarded to the HAL constructor, so
the class default applies; this keeps "not configured" distinct from "explicitly
disabled".

### Choosing a transport

There are exactly two forms, and they are mutually exclusive:

- **spidev** — set `port` and `device`, both **integers**. `port` is the SPI bus
  number and `device` is the chip-select: `port: 0, device: 0` maps to
  `/dev/spidev0.0`. `/dev/spidevX.Y` is a real path only in a container
  `--device` flag; it is never written in `dac.yml`.
- **gpiozero** — set **any** of `clock_pin`, `mosi_pin`, `miso_pin`,
  `select_pin`. Any one of these keys selects the bit-banged backend instead of
  spidev.

Mixing the two — for example `port` together with `select_pin` — is rejected by
the HAL constructor; the service forwards the configured keywords unchanged and
records the failure as described in
[Validation versus degradation](#validation-versus-degradation).

## Supported parts

`DAC_PARTS` in `devices.py` lists 23 part names. Channels, resolution, and
readback capability are read from the constructed device, so the table below is
descriptive; the constructor signature is the authority on fields.

| `type` | Channels | Resolution | Readable | Required parameter |
| --- | --- | --- | --- | --- |
| `MCP4231` | 2 | 7-bit | yes | — |
| `MCP4241` | 2 | 7-bit | yes | — |
| `MCP4251` | 2 | 8-bit | yes | — |
| `MCP4261` | 2 | 8-bit | yes | — |
| `MCP4802` | 2 | 8-bit | no | — |
| `MCP4812` | 2 | 10-bit | no | — |
| `MCP4822` | 2 | 12-bit | no | — |
| `MCP4902` | 2 | 8-bit | no | — |
| `MCP4912` | 2 | 10-bit | no | — |
| `MCP4922` | 2 | 12-bit | no | — |
| `DAC8531` | 1 | 16-bit | no | — |
| `DAC8551` | 1 | 16-bit | no | — |
| `DAC8552` | 2 | 16-bit | no | — |
| `DAC121S101` | 1 | 12-bit | no | — |
| `AD5302` | 2 | 8-bit | no | — |
| `AD5312` | 2 | 10-bit | no | — |
| `AD5322` | 2 | 12-bit | no | — |
| `DAC8512` | 1 | 12-bit | no | `load_pin` |
| `AD7390` | 1 | 12-bit | no | `load_pin` |
| `AD7391` | 1 | 10-bit | no | `load_pin` |
| `AD5410` | 1 | 12-bit | yes | — (no `reference`) |
| `AD5420` | 1 | 16-bit | yes | — (no `reference`) |
| `AD420` | 1 | 16-bit | no | `latch_pin` |

Notes:

- The MCP4xxx parts are digital potentiometers. Construction reads both wipers
  over the bus, so a rebuild produces SPI traffic even without a task.
- `AD5410` and `AD5420` reject `reference` outright; the current output is
  specified in milliamps, not scaled from a voltage reference. They need
  `dac:configure` with `output: true` before a write, otherwise `dac:write`
  returns `DAC_OUTPUT_DISABLED`.
- `DAC8512`, `AD7390`, and `AD7391` require `load_pin`; without a load line the
  part has no way to latch a written frame. `AD420` requires `latch_pin`.
- A part-specific field it does not accept is rejected at apply time as an
  unknown parameter, not ignored.

## `range` values for AD420

`AD420` takes `range` as its first constructor argument and defaults to
`4-20mA` when the field is unset.

| `range` | Meaning |
| --- | --- |
| `0-5V` | Voltage output, 0–5 V |
| `4-20mA` | Current loop, 4–20 mA (default) |
| `0-20mA` | Current loop, 0–20 mA |
| `0-24mA` | Current loop, 0–24 mA |

`dac:configure` applies to `AD5410` and `AD5420` only; both are current-output
parts, and their control register encodes `4-20mA`, `0-20mA` and `0-24mA`. The
`0-5V` option belongs to `AD420`, which has no control register, so it never
appears in a `dac:configure` readback.

## Validation versus degradation

Failures split into two classes with different outcomes.

**Decode time — the configuration is rejected.** The merged section is converted
through `DacServiceSettings` / `DacDeviceSettings`, both
`forbid_unknown_fields=True`:

- An unknown key at any level is a hard error (a typo is never ignored).
- A missing `type` on a device entry is a validation error.
- A value of the wrong type (for example `port: "zero"`, `reference: [1.0, 2.0]`)
  is a hard error.

**Apply time — the device is degraded, the worker keeps running.** These are
caught per device by `DacDeviceRegistry.rebuild` and recorded instead of raised:

- an unknown `type` name (not in `DAC_PARTS`);
- a parameter the part does not accept, or a combination it rejects (including
  mixing the two transport forms and omitting a required `load_pin`/`latch_pin`);
- a bus that will not open (missing `/dev/spidevX.Y`, pin conflict, permission
  error).

For each degraded case the device appears in `dac:list` and in telemetry with
`available: false` and the recorded `error` string; the other devices stay
usable, and the worker keeps running. A rebuild retries every device that failed,
so a converter that appears later is picked up on the next apply.

### Unsupported configuration

Per-channel sequences are deliberately not exposed by the config schema in this
release. `channels`, `gain`, `power_down`, `buffered`, and a per-channel list for
`reference` are rejected at decode time as unknown fields or a type mismatch;
`reference` is one scalar for the whole device. Use the task layer — a task
already addresses one channel by index — for per-channel control.

`strobe` is not a config field either. The HAL takes a driver object there,
while a config entry can only carry YAML scalars. Parts that need a strobe line
take `load_pin` (`latch_pin` for AD420) as a GPIO pin number; a `strobe:` key is
rejected as an unknown field.

### Task error codes

When a task reaches a device that is not usable, the worker maps the exception to
a task error (first match wins):

| Exception | Code | Retryable |
| --- | --- | --- |
| `DeviceNotFound` | `DEVICE_NOT_FOUND` | no — id is not configured |
| `ChannelNotFound` | `CHANNEL_NOT_FOUND` | no — channel index out of range |
| `DeviceUnavailable` | `DEVICE_UNAVAILABLE` | yes — configured but not open |
| `DeviceOpenError` | `DEVICE_UNAVAILABLE` | yes — open failed |
| `DACReadbackError` | `DAC_READBACK_ERROR` | no — part is not readable |
| `DACOutputDisabledError` | `DAC_OUTPUT_DISABLED` | no — OUTEN is clear |
| `DACCodeError` | `DAC_CODE_OUT_OF_RANGE` | no — code outside the part's range |
| `DACReferenceError` | `DAC_REFERENCE_ERROR` | no — reference is invalid |
| `DACStrobeError` | `DAC_STROBE_ERROR` | yes |
| `TransportError` / `TransportClosed` | `TRANSPORT_ERROR` | yes |
| decode error / `DeviceConfigError` | `INVALID_REQUEST` | no |
| `ConfigureUnsupported` | `DAC_CONFIGURE_UNSUPPORTED` | no — part has no `configure()` |
| `DACError` | `DAC_ERROR` | yes |
| anything else | `DEVICE_IO_ERROR` | yes |

Retryable codes mean the hardware or configuration may recover without the
request changing; non-retryable codes mean the request itself is wrong.

## Worked examples

Two voltage-output converters sharing one SPI bus:

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

`dac0` maps to `/dev/spidev0.0` and `dac1` to `/dev/spidev0.1`. Both are
write-only parts: `dac:write` works, `dac:read` returns `DAC_READBACK_ERROR`.

A current-loop pair with the part-specific pins:

```yaml
devices:
  loop0:
    type: AD420
    port: 0
    device: 0
    latch_pin: 22
    range: 4-20mA
  loop1:
    type: AD5410
    port: 0
    device: 1
```

`loop0` needs `latch_pin` to latch each frame and defaults to `4-20mA`.
`loop1` has no `reference` field and its output stays disabled until
`dac:configure` is called with `{"device_id": "loop1", "output": true}`.

## Regenerating defaults

If `dac/dac.yml` is missing, the service writes a default file on first run
(`read_dac_config(..., create_default=True)`, the bootstrap path). If the file
exists but cannot be parsed, it is **left untouched** and the loader raises
`RuntimeError`; fix the file or remove it to regenerate defaults. Passing
`create_default=False` makes a missing file an error instead of writing one.
