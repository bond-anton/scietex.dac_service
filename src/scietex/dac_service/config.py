"""Configuration models and YAML loader for the DAC service.

`DacServiceSettings` is the concrete constructor (L0) base and the validation
target of the framework's four-layer config merge:

* L0 — `DAC_SETTINGS_DEFAULTS`, the builtins of a default struct instance.
* L1 — the service-owned ``dac/dac.yml`` bootstrap patch (`read_dac_config`).
* L2 — the framework's ``config.yml`` snapshot from the last successful apply.
* L3 — the remote ``dac`` section of a desired-state envelope.

The layers are patch dicts merged field by field with the three-state rule: a
key absent from every layer inherits the value below, a key present with
``null`` clears it (so the L0 value resurfaces), and a key present with a value
sets it. The struct is kept deliberately thin: backend selection, pin routing,
and part geometry are validated by the HAL constructors in
`devices.build_dac`, so the YAML schema only constrains types and unknown
fields.

Per-channel sequences are deliberately out of scope for this file: ``reference``
is one scalar for the whole device (the HAL accepts a per-channel sequence, but
the config schema does not), and per-channel ``gain``, ``power_down``,
``channels``, and ``buffered`` are not exposed at all. Those knobs live on the
task layer, where a channel index already exists.

Device entries are keyed by a **string** device id (``devices: {dac0: {...}}``),
not an integer. An id names a device in task payloads and telemetry, and a YAML
mapping key is a string anyway, so an ``int`` key would need silent coercion on
every hop. `dict[str, DacDeviceSettings]` decodes string keys verbatim and
round-trips through `msgspec.to_builtins` unchanged.
"""

from pathlib import Path

import msgspec

#: Remote-config section name the settings are registered under.
DAC_SECTION: str = "dac"

#: Subdirectory under the shared config dir that namespaces this service's
#: files. The framework's ``config.yml`` snapshot and the service-owned
#: ``dac.yml`` both live here, so services sharing one config dir (the
#: framework resolves a single dir for all scietex services) cannot collide.
DAC_CONFIG_SUBDIR: str = "dac"

#: Filename of the service-owned L1 bootstrap patch in the config subdirectory.
DAC_CONFIG_FILE: str = "dac.yml"


class DacDeviceSettings(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One SPI DAC device, keyed by its id in `DacServiceSettings.devices`.

    ``type`` selects the HAL class through `devices.DAC_PARTS`. Every remaining
    field is optional and forwarded to that class only when set, so the backend
    keywords (``port``/``device`` SPI bus and chip-select) and the shared
    keywords (``reference``, ``label``, ``log_level``) can be mixed as the part
    and its transport require. ``None`` means "not configured": the constructor
    default applies.

    The trailing fields are part-specific: ``range``/``latch_pin`` for the
    current-output and latched parts, ``load_pin`` for the load-strobed voltage
    parts, and ``r_ab``/``grounded`` for the digital potentiometers. A part that
    does not accept one of them rejects it at build time; the schema stays
    uniform instead of growing 23 variants.

    ``strobe`` is deliberately absent: the HAL takes a driver object there,
    while a config entry can only carry YAML scalars. Parts that need a load
    line take ``load_pin`` (or ``latch_pin`` for AD420) as an integer GPIO pin
    instead.
    """

    type: str
    port: int | None = None
    device: int | None = None
    clock_pin: int | str | None = None
    mosi_pin: int | str | None = None
    miso_pin: int | str | None = None
    select_pin: int | str | None = None
    reference: float | None = None
    label: str | None = None
    log_level: str | None = None
    # part-specific parameters
    range: str | None = None
    load_pin: int | str | None = None
    latch_pin: int | str | None = None
    r_ab: float | None = None
    grounded: str | None = None


class DacServiceSettings(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Top-level DAC service settings."""

    devices: dict[str, DacDeviceSettings] = msgspec.field(default_factory=dict)


#: Concrete L0 base for the framework's layered merge. A field absent from every
#: layer, or explicitly cleared with ``null``, resolves to the value here.
DAC_SETTINGS_DEFAULTS: DacServiceSettings = DacServiceSettings()


def read_dac_config(conf_dir: Path | None, *, create_default: bool = True) -> dict[str, object]:
    """Read the L1 bootstrap patch from ``dac/dac.yml`` under the config directory.

    The service's files are namespaced in a ``dac/`` subdirectory so they do
    not collide with other services sharing the framework's single config dir.
    The file (and, when missing, its directory) is only created when
    ``create_default=True`` (the bootstrap path). A ``None`` or non-directory
    ``conf_dir``, a missing file/directory with ``create_default=False``, or an
    unparseable file each raise `RuntimeError`. An existing-but-invalid file is
    left untouched regardless of ``create_default``.

    The return value is the raw ``dac.yml`` mapping — the L1 patch the framework
    merges onto the L0 base and validates through `DacServiceSettings`. When the
    file is just created, the builtins of the default struct are returned so the
    patch is equivalent to an empty override.

    Args:
        conf_dir: Path to the configuration directory.
        create_default: Whether to create the directory and write a default
            ``dac.yml`` when missing. Default ``True``.

    Returns:
        The parsed ``dac.yml`` mapping as the L1 patch, or the builtins of the
        default `DacServiceSettings` when the file was just created.

    Raises:
        RuntimeError: If ``conf_dir`` is ``None`` or not a directory, the file
            is missing with ``create_default=False``, or the file cannot be parsed.
    """
    if not isinstance(conf_dir, Path):
        raise RuntimeError("Configuration dir was not set!")
    service_dir = conf_dir / DAC_CONFIG_SUBDIR
    if not service_dir.exists():
        if create_default:
            try:
                service_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                raise RuntimeError(f"Failed to create configuration directory {service_dir}!") from exc
        else:
            raise RuntimeError(
                f"Configuration directory {service_dir} does not exist and create_default=False (no default generated)."
            )
    elif not service_dir.is_dir():
        raise RuntimeError(f"Provided configuration directory path {service_dir} is not a directory!")
    dac_yml = service_dir.joinpath(DAC_CONFIG_FILE)
    if not dac_yml.exists():
        if create_default:
            settings = DacServiceSettings()
            with open(dac_yml, "wb") as f:
                f.write(msgspec.yaml.encode(settings))
            return msgspec.to_builtins(settings)
        raise RuntimeError(
            f"DAC configuration file {dac_yml} does not exist and create_default=False "
            "(pass create_default=True to generate defaults)."
        )
    try:
        with open(dac_yml, "rb") as f:
            return msgspec.yaml.decode(f.read(), type=dict)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to parse DAC configuration file {dac_yml}. Fix the file or remove it to regenerate defaults."
        ) from exc
