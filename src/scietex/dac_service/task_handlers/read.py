"""``dac:read`` data-plane task: read one channel's raw code back.

The handler reads the live device at request time; nothing is cached, so the
result is the bus state at the moment the task ran. A part that cannot report
its registers raises `DACReadbackError` from `DACChannel.readback`, which the
shared error table maps to a non-retryable ``DAC_READBACK_ERROR``; the handler
does not pre-check `readable` and synthesize the code itself.
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

#: Task name for a single-channel read.
DAC_READ_TASK_NAME = "dac:read"


class DacReadRequest(msgspec.Struct, forbid_unknown_fields=True):
    """Request payload for ``dac:read``.

    Args:
        device_id: Device to read.
        channel: Output channel index.
    """

    device_id: str
    channel: int = 0


class DacReadResponse(msgspec.Struct, frozen=True):
    """Result payload of a ``dac:read`` task.

    Args:
        device_id: Device the reading came from.
        channel: Index that was read.
        code: Raw code the device reported.
        unit: Output unit symbol the channel drives.
        reference: Upper bound the output maps to, or ``None`` when unknown.
        max_code: Largest raw code the channel accepts.
    """

    device_id: str
    channel: int
    code: int
    unit: str
    reference: float | None
    max_code: int


class DacReadHandler(TaskHandler):
    """Handle ``dac:read`` on the data lane."""

    control: ClassVar[bool] = False

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
        return [DAC_READ_TASK_NAME]

    async def handle(self, task_data: TaskData, *, capabilities: TaskCapabilities) -> TaskResult:
        """Read one channel's raw code back over the bus."""
        try:
            request = decode_request(task_data, DacReadRequest)
            channel = self._devices.channel(request.device_id, request.channel)
            code = channel.readback()
        except Exception as exc:
            return error_result(exc)
        return ok_result(
            DacReadResponse(
                device_id=request.device_id,
                channel=request.channel,
                code=code,
                unit=str(channel.unit),
                reference=channel.reference,
                max_code=channel.max_code,
            )
        )
