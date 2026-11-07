"""``dac:list`` control-plane task: report the devices this worker can drive.

The API cannot know which converters are configured or actually open on the
worker host, so the operator's device picker asks the worker. The answer
includes devices that failed to open with the recorded reason: a missing
converter is a state the UI must show, not a reason to fail the whole listing.

This is a control-plane task because the answer describes the worker's own
hardware, not an output write. It never touches the bus: every field is read
from the live objects and the registry's recorded state.
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

from ..devices import DacDeviceRegistry, DeviceNotFound, DeviceUnavailable
from ._request import decode_request, error_result, ok_result

#: Task name for the device/channel enumeration control task.
DAC_LIST_TASK_NAME = "dac:list"


class DacListRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Request payload for ``dac:list``.

    Args:
        device_id: Id to describe. ``None`` or an empty payload lists every
            configured device.
    """

    device_id: str | None = None


class DacDeviceInfo(msgspec.Struct, frozen=True):
    """One configured device and its open state.

    Args:
        device_id: Id from the ``devices`` mapping in ``dac.yml``.
        type: HAL part name the entry was built from.
        available: Whether the device is open.
        channels: Number of output channels; ``0`` when unavailable.
        resolution: Raw-code width in bits, or ``None`` when unavailable.
        max_code: Largest raw code, or ``None`` when unavailable.
        unit: Output unit symbol the channels drive, or ``None``.
        reference: Upper bound the outputs map to, or ``None`` when the
            scaling is unknown.
        readable: Whether the part can report its registers back.
        error: Why the device is unavailable; ``None`` when it is available.
    """

    device_id: str
    type: str
    available: bool
    channels: int
    resolution: int | None
    max_code: int | None
    unit: str | None
    reference: float | None
    readable: bool
    error: str | None = None


class DacListResponse(msgspec.Struct, frozen=True):
    """Result payload of a ``dac:list`` task.

    Args:
        devices: Requested devices, sorted by id; empty is a normal result.
    """

    devices: list[DacDeviceInfo]


class DacListHandler(TaskHandler):
    """Handle ``dac:list`` on the control lane."""

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
        return [DAC_LIST_TASK_NAME]

    async def handle(self, task_data: TaskData, *, capabilities: TaskCapabilities) -> TaskResult:
        """Describe one configured device, or every device when none is named."""
        try:
            request = decode_request(task_data, DacListRequest)
            if request.device_id is None:
                infos = [self._describe(device_id) for device_id in self._devices.ids()]
            else:
                infos = [self._describe(request.device_id)]
        except Exception as exc:
            return error_result(exc)
        return ok_result(DacListResponse(devices=infos))

    def _describe(self, device_id: str) -> DacDeviceInfo:
        """Build one entry, reporting an open failure instead of raising it.

        An id that is not configured at all is still a terminal request error;
        an id that is configured but failed to open is a successful listing
        entry with ``available=False``.
        """
        known = self._devices.ids()
        if device_id not in known:
            raise DeviceNotFound(f"Device {device_id!r} is not configured; known ids: {known}")
        settings = self._devices.settings
        cfg = settings.devices.get(device_id) if settings is not None else None
        part_type = cfg.type if cfg is not None else ""
        try:
            device = self._devices.get(device_id)
        except DeviceUnavailable:
            return DacDeviceInfo(
                device_id=device_id,
                type=part_type,
                available=False,
                channels=0,
                resolution=None,
                max_code=None,
                unit=None,
                reference=None,
                readable=False,
                error=self._devices.errors.get(device_id),
            )
        first = device.channels[0] if device.channels else None
        return DacDeviceInfo(
            device_id=device_id,
            type=part_type,
            available=True,
            channels=len(device.channels),
            resolution=device.resolution,
            max_code=device.max_code,
            unit=str(first.unit) if first is not None else None,
            reference=device.reference,
            readable=device.readable,
        )
