"""Device registry: validated settings to live HAL objects.

`build_dac` maps one `DacDeviceSettings` entry onto a concrete
`scietex.hal.dac` class, rejecting anything the class would not accept instead
of letting a `TypeError` surface from deep inside the constructor.
`DacDeviceRegistry` owns the live objects, rebuilds them when the effective
settings change, and records a per-device failure in `errors` rather than
raising, so one unplugged converter never keeps the rest of the service from
starting.

`poll_plan` turns the settings plus the live geometry into the per-device,
per-channel sampling plan the poll loop consumes. `read_channel` reads a register
over the bus and `read_cached_channel` answers from the write cache; both share
`ChannelReading`, so a polled, an on-demand, and a just-written value are scaled
the same way. Bus reads take the per-device lock so the poll loop and an
on-demand task never interleave two SPI transactions on one chip select. The
registry itself stays synchronous and not thread-safe; the HAL reads are
blocking bus transactions by design.
"""

import asyncio
import inspect
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from scietex.hal import dac as hal

from .config import DacDeviceSettings, DacServiceSettings

#: Hard bounds on a sampling period. Below 100 ms one converter's blocking
#: transaction would starve the event loop; above an hour the cadence is
#: indistinguishable from polling being off.
MIN_INTERVAL_MS: int = 100
MAX_INTERVAL_MS: int = 3_600_000

_LOG = logging.getLogger(__name__)


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
        # One bus lock per device id, shared by the poll loop and the task
        # handlers. An SPI transaction is not re-entrant on a chip select, so
        # two readers of the same converter must queue, not interleave.
        self._locks: dict[str, asyncio.Lock] = {}

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
        # A cached code describes the hardware, not the live object, so it
        # outlives a rebuild of the same part. A removed device or a changed
        # ``type`` invalidated the code: a different part does not hold it.
        previous = self._settings.devices if self._settings is not None else {}
        self._last_written = {
            device_id: channels
            for device_id, channels in self._last_written.items()
            if device_id in settings.devices
            and device_id in previous
            and previous[device_id].type == settings.devices[device_id].type
        }
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

    def poll_targets(self) -> list[tuple[str, hal.DAC]]:
        """Open devices whose settings entry has ``poll=True``, in settings order."""
        if self._settings is None:
            return []
        return [
            (device_id, self._devices[device_id])
            for device_id, cfg in self._settings.devices.items()
            if cfg.poll and device_id in self._devices
        ]

    def read_lock(self, device_id: str) -> asyncio.Lock:
        """Return the bus lock serializing reads of ``device_id``.

        The lock is created on first use and kept across rebuilds: a rebuild
        runs only after the poll tasks are stopped, so no reader is holding it
        when the device object is replaced.
        """
        lock = self._locks.get(device_id)
        if lock is None:
            lock = self._locks[device_id] = asyncio.Lock()
        return lock

    def last_written_channel(self, device_id: str, channel: int) -> tuple[int, float | None] | None:
        """Return ``(code, output)`` cached by the last successful write, if any."""
        cached = self._last_written.get(device_id)
        return None if cached is None else cached.get(channel)

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


@dataclass(frozen=True)
class ChannelReading:
    """One channel's state: the raw code and the output it maps to."""

    device_id: str
    channel: int
    code: int
    output: float | None


def _output_for_code(channel: hal.DACChannel, code: int) -> float | None:
    """Scale ``code`` onto the channel's span without touching the bus.

    ``DACChannel.output`` would ask a readable part for its register again, so
    the span is applied here from the same public bounds it uses. Every part in
    `DAC_PARTS` maps code linearly onto ``lower_bound..reference``; a divider
    with an inverted curve is not among them.
    """
    reference = channel.reference
    if reference is None:
        return None
    lower = channel.lower_bound
    level = lower + code / channel.max_code * (reference - lower)
    return min(max(level, lower), reference)


def _reading(device_id: str, channel: int, code: int, hal_channel: hal.DACChannel) -> ChannelReading:
    """Pair a code with the output the channel's current span gives it."""
    return ChannelReading(
        device_id=device_id,
        channel=channel,
        code=code,
        output=_output_for_code(hal_channel, code),
    )


async def read_channel(
    registry: DacDeviceRegistry,
    device_id: str,
    index: int,
) -> ChannelReading:
    """Read one register over the bus under the device's lock.

    This is the single bus read path shared by the poll loop and ``dac:read``,
    so a polled and an on-demand value come from the same accessor. The device
    is resolved before the lock is taken, so an unknown or unavailable id never
    waits behind a slow bus transaction.

    Raises:
        DeviceNotFound: No device is configured under ``device_id``.
        DeviceUnavailable: The device is configured but not open.
        ChannelNotFound: The device has no output at ``index``.
        DACReadbackError: The part cannot report its registers.
    """
    registry.get(device_id)
    async with registry.read_lock(device_id):
        hal_channel = registry.channel(device_id, index)
        code = hal_channel.readback()
    return _reading(device_id, index, code, hal_channel)


async def read_cached_channel(
    registry: DacDeviceRegistry,
    device_id: str,
    index: int,
) -> ChannelReading | None:
    """Answer from the write cache without touching the bus.

    A write-only part that was never written falls back to its HAL shadow code:
    the parts in scope power up at zero scale, so that is the state the hardware
    holds rather than a guess. A readable part with ``readback=False`` and no
    cached write has no bus-free answer, so the result is ``None`` and the poll
    loop publishes nothing until the next write.

    Raises:
        DeviceNotFound: No device is configured under ``device_id``.
        DeviceUnavailable: The device is configured but not open.
        ChannelNotFound: The device has no output at ``index``.
    """
    hal_channel = registry.channel(device_id, index)
    cached = registry.last_written_channel(device_id, index)
    if cached is not None:
        return _reading(device_id, index, cached[0], hal_channel)
    if not hal_channel.readable:
        return _reading(device_id, index, hal_channel.value, hal_channel)
    return None


@dataclass(frozen=True)
class DacChannelPlan:
    """One channel the poll loop samples: index, period, label, and source."""

    channel: int
    interval_ms: int
    label: str | None = None
    readback: bool = True


def poll_plan(
    registry: DacDeviceRegistry,
    logger: logging.Logger | None = None,
) -> dict[str, list[DacChannelPlan]]:
    """Expand the settings and the live geometry into the sampling plan.

    The device subset comes from `DacDeviceRegistry.poll_targets`, so a device
    with ``poll=False``, an unopened device, and (service-wide)
    ``poll_enabled=False`` never reach the plan. Per device:

    * an empty ``channels`` list samples every output the HAL part reports, at
      the device ``poll_interval_ms``;
    * an explicit channel row overrides that period and an ``enabled=False``
      row is dropped;
    * a row naming an output the part does not have is dropped with a warning,
      because it could only fail on every tick;
    * every period is clamped to ``[MIN_INTERVAL_MS, MAX_INTERVAL_MS]`` with a
      warning, so a misconfigured value degrades instead of starving the loop.

    Returns:
        Device id -> channels to sample. A device with no sampleable channel is
        absent, and so is every device when polling is disabled.
    """
    settings = registry.settings
    if settings is None or not settings.poll_enabled:
        return {}
    log = logger if logger is not None else _LOG
    plan: dict[str, list[DacChannelPlan]] = {}
    for device_id, device in registry.poll_targets():
        cfg = settings.devices[device_id]
        channels = _device_plan(device_id, device, cfg, log)
        if channels:
            plan[device_id] = channels
    return plan


def _device_plan(
    device_id: str,
    device: hal.DAC,
    cfg: DacDeviceSettings,
    logger: logging.Logger,
) -> list[DacChannelPlan]:
    """Per-channel plan for one open device, with clamping and drops applied."""
    limit = len(device.channels)
    if not cfg.channels:
        interval = _clamp_interval(device_id, cfg.poll_interval_ms, logger)
        return [DacChannelPlan(channel, interval) for channel in range(limit)]

    plans: list[DacChannelPlan] = []
    seen: set[int] = set()
    for entry in cfg.channels:
        if not entry.enabled:
            continue
        if entry.channel in seen:
            logger.warning("Device %s: duplicate channel %d row ignored", device_id, entry.channel)
            continue
        if not 0 <= entry.channel < limit:
            logger.warning(
                "Device %s: channel %d is outside the part's %d outputs",
                device_id,
                entry.channel,
                limit,
            )
            continue
        seen.add(entry.channel)
        plans.append(
            DacChannelPlan(
                channel=entry.channel,
                interval_ms=_clamp_interval(device_id, entry.poll_interval_ms, logger),
                label=entry.label,
                readback=entry.readback,
            )
        )
    return plans


def _clamp_interval(device_id: str, interval_ms: int, logger: logging.Logger) -> int:
    """Clamp one sampling period into the supported range, warning on a change."""
    clamped = min(max(interval_ms, MIN_INTERVAL_MS), MAX_INTERVAL_MS)
    if clamped != interval_ms:
        logger.warning(
            "Device %s: poll_interval_ms=%d is outside [%d, %d]; using %d",
            device_id,
            interval_ms,
            MIN_INTERVAL_MS,
            MAX_INTERVAL_MS,
            clamped,
        )
    return clamped
