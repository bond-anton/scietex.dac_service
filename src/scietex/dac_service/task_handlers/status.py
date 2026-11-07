"""``dac:status`` control-plane task: report one configured device's state.

The handler works for every part the registry knows. Geometry, scaling, and the
last-written cache are always reported; the decoded ``status`` and ``control``
registers appear only for parts that expose ``read_status()``/``read_control()``
(the AD54x0 family). Nothing else touches the bus, so a write-only part costs no
transactions at all and a readable part costs only the two register reads the
caller asked for by name.

A configured device that failed to open is a retryable ``DEVICE_UNAVAILABLE``,
matching the other handlers: the hardware may come back without the id or the
request changing.
"""

from typing import ClassVar

import msgspec

from scietex.service.task_handler import (
    TaskCapabilities,
    TaskData,
    TaskHandler,
    TaskHandlerContext,
    TaskResult,
)

from ..devices import DacDeviceRegistry
from ._request import decode_request, error_result, ok_result

#: Task name for the device-state control task.
DAC_STATUS_TASK_NAME = "dac:status"


def _register_fields(register: object) -> dict[str, object]:
    """Flatten one decoded register into JSON-encodable named fields.

    ``msgspec.to_builtins`` converts the values but renders a named tuple as a
    plain tuple, which loses the field names a caller needs; a `Struct` or
    dataclass already converts to a dict. Both HAL registers are named tuples,
    so their names come from ``_fields`` and are paired with the values here.
    """
    values = msgspec.to_builtins(register)
    if isinstance(values, dict):
        return {str(name): value for name, value in values.items()}
    names = getattr(register, "_fields", ())
    return {name: value for name, value in zip(names, values, strict=True)}


class DacStatusRequest(msgspec.Struct, forbid_unknown_fields=True):
    """Request payload for ``dac:status``.

    Args:
        device_id: Device to describe.
    """

    device_id: str


class DacChannelState(msgspec.Struct, frozen=True):
    """One channel's last recorded write.

    Args:
        channel: Output channel index.
        code: Raw code the channel holds, or ``None`` when never written.
        output: Output in the channel's unit, or ``None`` when unknown.
    """

    channel: int
    code: int | None
    output: float | None


class DacStatusResponse(msgspec.Struct, frozen=True, kw_only=True):
    """Result payload of a ``dac:status`` task.

    Args:
        device_id: Device that was described.
        type: HAL part name the entry was built from.
        available: Whether the device is open; always true here, because an
            unavailable device answers with an error instead.
        error: Why the device is unavailable; always ``None`` for this result.
        readable: Whether the part can report its registers back.
        channels: Number of output channels.
        resolution: Raw-code width in bits.
        max_code: Largest raw code the part accepts.
        unit: Output unit symbol the channels drive.
        reference: Upper bound the outputs map to, or ``None`` when unknown.
        last_written: Cached writes for this device, ordered by channel.
        status: Decoded status register, or ``None`` when the part has none.
        control: Decoded control register, or ``None`` when the part has none.
    """

    device_id: str
    type: str
    available: bool
    error: str | None = None
    readable: bool
    channels: int
    resolution: int | None
    max_code: int | None
    unit: str | None
    reference: float | None
    last_written: list[DacChannelState]
    status: dict[str, object] | None = None
    control: dict[str, object] | None = None


class DacStatusHandler(TaskHandler):
    """Handle ``dac:status`` on the control lane."""

    control: ClassVar[bool] = True

    def __init__(self, name: str, context: TaskHandlerContext, *, devices: DacDeviceRegistry) -> None:
        """Initialize the handler.

        Args:
            name: Human-readable name for this handler instance.
            context: Narrow context provided by the owning processor.
            devices: Shared registry injected by the worker, so the handler
                sees live rebuilds instead of a startup snapshot.
        """
        super().__init__(name, context)
        self._devices = devices

    @property
    def supported_tasks(self) -> list[str]:
        """Task types this handler serves."""
        return [DAC_STATUS_TASK_NAME]

    async def handle(self, task_data: TaskData, *, capabilities: TaskCapabilities) -> TaskResult:
        """Describe one configured device, reading its registers if it has them."""
        try:
            request = decode_request(task_data, DacStatusRequest)
            device = self._devices.get(request.device_id)
            response = self._describe(device, request.device_id)
        except Exception as exc:
            return error_result(exc)
        return ok_result(response)

    def _describe(self, device: object, device_id: str) -> DacStatusResponse:
        """Build the response from the live object and the registry cache."""
        channels = getattr(device, "channels")
        first = channels[0] if channels else None
        read_status = getattr(device, "read_status", None)
        read_control = getattr(device, "read_control", None)
        return DacStatusResponse(
            device_id=device_id,
            type=self._part_type(device, device_id),
            available=True,
            error=None,
            readable=bool(getattr(device, "readable")),
            channels=len(channels),
            resolution=getattr(device, "resolution", None),
            max_code=getattr(device, "max_code", None),
            unit=str(first.unit) if first is not None else None,
            reference=getattr(device, "reference", None),
            last_written=self._last_written(device_id),
            status=_register_fields(read_status()) if callable(read_status) else None,
            control=_register_fields(read_control()) if callable(read_control) else None,
        )

    def _part_type(self, device: object, device_id: str) -> str:
        """Return the configured part name, falling back to the class name."""
        settings = self._devices.settings
        cfg = settings.devices.get(device_id) if settings is not None else None
        return cfg.type if cfg is not None else type(device).__name__

    def _last_written(self, device_id: str) -> list[DacChannelState]:
        """Return this device's cached writes, filtered out of the registry."""
        entries = self._devices.last_written().get(device_id, [])
        return [DacChannelState(channel=channel, code=code, output=output) for channel, code, output in entries]
