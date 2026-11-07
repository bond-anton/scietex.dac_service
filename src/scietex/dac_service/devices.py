"""Device registry: validated settings to live HAL objects.

`build_dac` maps one `DacDeviceSettings` entry onto a concrete
`scietex.hal.dac` class, rejecting anything the class would not accept instead
of letting a `TypeError` surface from deep inside the constructor.
`DacDeviceRegistry` owns the live objects, rebuilds them when the effective
settings change, and records a per-device failure in `errors` rather than
raising, so one unplugged converter never keeps the rest of the service from
starting.

The registry is synchronous and not thread-safe: the worker drives it from one
task, and the HAL bus transactions are blocking by design.
"""

import inspect
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from scietex.hal import dac as hal

from .config import DacDeviceSettings, DacServiceSettings


class DeviceConfigError(ValueError):
    """A device settings entry cannot be turned into a HAL object."""


class UnknownDeviceType(DeviceConfigError):
    """The configured ``type`` is not a supported part."""


class UnknownParameter(DeviceConfigError):
    """A configured field is not a constructor parameter of the device class."""


class InvalidParameter(DeviceConfigError):
    """A parameter or parameter combination the device class rejects."""


class DeviceNotFound(KeyError):
    """No device is configured under the requested id."""


class ChannelNotFound(KeyError):
    """The device has no output channel at that index."""


class DeviceUnavailable(RuntimeError):
    """The id is configured but its device is not open.

    Retryable: the hardware or its configuration may recover without the id
    changing, unlike `DeviceNotFound`, which is a wrong request.

    ``reason`` keeps the recorded open error so a caller can log it without
    parsing the message.
    """

    def __init__(self, device_id: str, reason: str) -> None:
        super().__init__(f"Device {device_id!r} is unavailable: {reason}")
        self.device_id = device_id
        self.reason = reason


class DeviceOpenError(RuntimeError):
    """The device class was resolvable but opening the hardware failed.

    ``cause`` keeps the original `DACError`/`OSError` so a caller can log or
    inspect the backend failure without parsing the message.
    """

    def __init__(self, device_id: str, cause: BaseException) -> None:
        super().__init__(f"Failed to open device {device_id!r}: {cause}")
        self.device_id = device_id
        self.cause = cause


@dataclass(frozen=True)
class PartSpec:
    """Constructor metadata for one supported DAC part.

    ``leading`` names parameters that the class takes positionally before the
    rest; ``forbidden`` names parameters it rejects outright; ``required`` names
    parameters that must all be supplied. A required load line is not merely a
    convenience: without it the part has no way to latch a written frame.
    """

    cls: type
    leading: tuple[str, ...] = ()
    forbidden: frozenset[str] = frozenset()
    required: tuple[str, ...] = ()


#: Supported parts by the ``type`` string used in ``dac.yml``.
DAC_PARTS: dict[str, PartSpec] = {
    "MCP4231": PartSpec(hal.MCP4231),
    "MCP4241": PartSpec(hal.MCP4241),
    "MCP4251": PartSpec(hal.MCP4251),
    "MCP4261": PartSpec(hal.MCP4261),
    "MCP4802": PartSpec(hal.MCP4802),
    "MCP4812": PartSpec(hal.MCP4812),
    "MCP4822": PartSpec(hal.MCP4822),
    "MCP4902": PartSpec(hal.MCP4902),
    "MCP4912": PartSpec(hal.MCP4912),
    "MCP4922": PartSpec(hal.MCP4922),
    "DAC8531": PartSpec(hal.DAC8531),
    "DAC8551": PartSpec(hal.DAC8551),
    "DAC8552": PartSpec(hal.DAC8552),
    "DAC121S101": PartSpec(hal.DAC121S101),
    "AD5302": PartSpec(hal.AD5302),
    "AD5312": PartSpec(hal.AD5312),
    "AD5322": PartSpec(hal.AD5322),
    "DAC8512": PartSpec(hal.DAC8512, required=("load_pin",)),
    "AD7390": PartSpec(hal.AD7390, required=("load_pin",)),
    "AD7391": PartSpec(hal.AD7391, required=("load_pin",)),
    "AD5410": PartSpec(hal.AD5410, forbidden=frozenset({"reference"})),
    "AD5420": PartSpec(hal.AD5420, forbidden=frozenset({"reference"})),
    "AD420": PartSpec(hal.AD420, leading=("range",), required=("latch_pin",)),
}

#: Settings field -> constructor keyword. A field the settings leave ``None`` is
#: not forwarded, so the class default applies and no backend keyword is
#: mistaken for "explicitly disabled".
_TO_KWARG: dict[str, str] = {
    "port": "port",
    "device": "device",
    "clock_pin": "clock_pin",
    "mosi_pin": "mosi_pin",
    "miso_pin": "miso_pin",
    "select_pin": "select_pin",
    "reference": "reference",
    "label": "label",
    "log_level": "log_level",
    "range": "range",
    "load_pin": "load_pin",
    "latch_pin": "latch_pin",
    "r_ab": "r_ab",
    "grounded": "grounded",
}


def build_dac(device_id: str, cfg: DacDeviceSettings) -> hal.DAC:
    """Build the HAL device described by one settings entry.

    Args:
        device_id: Id from the settings mapping, used only in error messages.
        cfg: The validated per-device settings.

    Returns:
        The constructed device.

    Raises:
        UnknownDeviceType: The ``type`` is not in `DAC_PARTS`.
        UnknownParameter: A forwarded field is not a constructor parameter.
        InvalidParameter: A forwarded field, or the combination of fields, is
            rejected by the class signature or by the constructor itself.
        DeviceOpenError: The constructor failed at the bus level
            (`DACError` or `OSError`).
    """
    spec = DAC_PARTS.get(cfg.type)
    if spec is None:
        raise UnknownDeviceType(f"Unknown DAC type {cfg.type!r}; known: {sorted(DAC_PARTS)}")

    params: dict[str, object] = {
        kwarg: getattr(cfg, field_name) for field_name, kwarg in _TO_KWARG.items() if getattr(cfg, field_name) is not None
    }

    sig = inspect.signature(spec.cls)
    forbidden = sorted(set(params) & spec.forbidden)
    if forbidden:
        raise InvalidParameter(f"{cfg.type} does not accept {forbidden}")

    unknown = sorted(set(params) - set(sig.parameters))
    if unknown:
        raise UnknownParameter(f"Device {device_id!r} of type {cfg.type!r} does not accept {unknown}")

    missing = [name for name in spec.required if name not in params]
    if missing:
        # A load line is the only way such a part latches a written frame;
        # leaving it out would build a device that silently never updates.
        raise InvalidParameter(f"{cfg.type} requires {missing}")

    args = tuple(params.pop(name) for name in spec.leading if name in params)
    try:
        sig.bind(*args, **params)
    except TypeError as exc:
        raise InvalidParameter(f"Invalid parameters for device {device_id!r}: {exc}") from exc

    try:
        return spec.cls(*args, **params)
    except (hal.DACError, OSError) as exc:
        raise DeviceOpenError(device_id, exc) from exc
    except (TypeError, ValueError) as exc:
        raise InvalidParameter(f"Invalid parameters for device {device_id!r}: {exc}") from exc


# MCP4xxx construction reads every wiper over the bus, so `rebuild` must
# short-circuit on unchanged settings to avoid re-reading the bus on every
# applied snapshot.


class DacDeviceRegistry:
    """Live devices keyed by the string id from `DacServiceSettings.devices`.

    A rebuild replaces the whole set: the old objects are closed first, then
    every configured entry is opened independently. Failures are collected in
    `errors` and never raised, so the service always starts with whatever
    hardware is present.
    """

    def __init__(self, logger: logging.Logger) -> None:
        self._log = logger
        self._devices: dict[str, hal.DAC] = {}
        self._errors: dict[str, str] = {}
        self._settings: DacServiceSettings | None = None
        self._generation = 0
        self._last_written: dict[str, dict[int, tuple[int, float | None]]] = {}

    @property
    def errors(self) -> Mapping[str, str]:
        """Per-device failure messages from the last rebuild."""
        return dict(self._errors)

    @property
    def generation(self) -> int:
        """Number of rebuilds that replaced the live device set."""
        return self._generation

    @property
    def settings(self) -> DacServiceSettings | None:
        """Settings the live set was built from, or ``None`` before the first rebuild."""
        return self._settings

    def rebuild(self, settings: DacServiceSettings) -> None:
        """Close current devices, then open every configured one.

        A per-device failure is recorded in `errors` and never raised, so the
        service boots with whatever hardware is present. A registry-wide
        failure (malformed settings) propagates.

        Identical settings with at least one live device are a no-op, which
        keeps a repeated apply from churning open file descriptors. The guard
        deliberately requires a live device: a rebuild after every device
        failed retries the hardware instead of caching the failure.

        Args:
            settings: The merged settings to realize.
        """
        if self._settings == settings and self._devices:
            return
        old = self._devices
        self._devices = {}
        self._errors = {}
        for device_id, dev in old.items():
            try:
                dev.close()
            except Exception as exc:
                self._log.warning("Error closing %s: %s", device_id, exc)
            # A rebuild replaces the live object, so a code cached against the
            # old object must not outlive it.
            self._last_written.pop(device_id, None)
        for device_id, cfg in settings.devices.items():
            try:
                self._devices[device_id] = build_dac(device_id, cfg)
            except (DeviceConfigError, DeviceOpenError) as exc:
                self._errors[device_id] = str(exc)
                self._log.error("Device %s is unavailable: %s", device_id, exc)
        self._settings = settings
        self._generation += 1

    def close(self) -> None:
        """Close every open device and drop the registry state.

        Idempotent: a second call finds nothing to close. A failing ``close()``
        is logged and does not keep the remaining devices open.
        """
        for device_id, dev in self._devices.items():
            try:
                dev.close()
            except Exception as exc:
                self._log.warning("Error closing %s: %s", device_id, exc)
        self._devices = {}
        self._errors = {}
        self._last_written = {}

    def ids(self) -> list[str]:
        """Sorted ids the operator should see, including devices that failed to open."""
        configured = self._settings.devices if self._settings is not None else {}
        return sorted(set(configured) | set(self._devices) | set(self._errors))

    def get(self, device_id: str) -> hal.DAC:
        """Return the open device under ``device_id``.

        Raises:
            DeviceUnavailable: The id is configured but the device did not open.
                The message carries the recorded open error.
            DeviceNotFound: The id is not configured at all. The message lists
                the known ids.
        """
        if device_id in self._devices:
            return self._devices[device_id]
        reason = self._errors.get(device_id)
        if reason is not None:
            raise DeviceUnavailable(device_id, reason)
        configured = self._settings.devices if self._settings is not None else {}
        if device_id in configured:
            # Configured, neither open nor recorded: only a closed registry
            # reaches this. Treat it as a state of the hardware, not a bad id.
            raise DeviceUnavailable(device_id, "configured but not open; rebuild to retry")
        raise DeviceNotFound(f"Device {device_id!r} is not configured; known ids: {self.ids()}")

    def channel(self, device_id: str, index: int) -> hal.DACChannel:
        """Return output channel ``index`` of ``device_id``.

        Bounds are checked against the live device first, so an out-of-range
        index raises `ChannelNotFound` instead of the HAL's bare `IndexError`.

        Raises:
            DeviceUnavailable: The device is configured but not open.
            DeviceNotFound: No device is configured under ``device_id``.
            ChannelNotFound: The device has no channel at ``index``.
        """
        device = self.get(device_id)
        if not 0 <= index < len(device.channels):
            raise ChannelNotFound(f"{device_id}[{index}]")
        return device.channel(index)

    def channels(self, device_id: str) -> tuple[hal.DACChannel, ...]:
        """Return every output channel of ``device_id`` in index order.

        Raises:
            DeviceUnavailable: The device is configured but not open.
            DeviceNotFound: No device is configured under ``device_id``.
        """
        return self.get(device_id).channels

    def record_write(self, device_id: str, channel: int, code: int, output: float | None) -> None:
        """Cache the state a successful write left one channel in.

        A task that raced a rebuild may reference an id whose live object is
        gone; the cache is advisory, so an unknown id is ignored instead of
        failing the task after its write already succeeded.

        Args:
            device_id: Id of the device that was written.
            channel: Output channel index.
            code: Raw code the channel holds after the write.
            output: Output in the channel's unit, or ``None`` when unknown.
        """
        if device_id not in self._devices:
            return
        self._last_written.setdefault(device_id, {})[channel] = (code, output)

    def last_written(self) -> dict[str, list[tuple[int, int, float | None]]]:
        """Return the cached writes as ``id -> [(channel, code, output), ...]``.

        The result is a deep copy ordered by device id and channel index, so a
        caller can render it without seeing later registry mutations.
        """
        return {
            device_id: [(channel, code, output) for channel, (code, output) in sorted(channels.items())]
            for device_id, channels in sorted(self._last_written.items())
        }
