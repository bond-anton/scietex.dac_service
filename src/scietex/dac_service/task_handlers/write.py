"""``dac:write`` data-plane task: set one channel by code or by output value.

The request carries exactly one of ``code`` and ``output``. A code goes through
the HAL's raw-code path, which validates the range; an output goes through the
HAL's affine, clamped path, so a value slightly beyond the span writes the
span's end instead of being rejected. After a successful write the result is
also recorded in the registry's last-written cache, which ``dac:list``-style
operator views and later diagnostics can read without bus traffic.
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
from ._request import decode_request, error_result, ok_result, require_exactly_one

#: Task name for a single-channel write.
DAC_WRITE_TASK_NAME = "dac:write"


class DacWriteRequest(msgspec.Struct, forbid_unknown_fields=True):
    """Request payload for ``dac:write``.

    Args:
        device_id: Device to write.
        channel: Output channel index.
        code: Raw code to write, mutually exclusive with ``output``.
        output: Scaled output in the channel's unit, mutually exclusive with
            ``code``.
    """

    device_id: str
    channel: int = 0
    code: int | None = None
    output: float | None = None


class DacWriteResponse(msgspec.Struct, frozen=True):
    """Result payload of a ``dac:write`` task.

    Args:
        device_id: Device that was written.
        channel: Index that was written.
        code: Raw code the channel holds after the write.
        output: Output in the channel's unit, or ``None`` when it would need a
            register readback to compute.
        unit: Output unit symbol the channel drives.
        max_code: Largest raw code the channel accepts.
    """

    device_id: str
    channel: int
    code: int
    output: float | None
    unit: str
    max_code: int


class DacWriteHandler(TaskHandler):
    """Handle ``dac:write`` on the data lane."""

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
        return [DAC_WRITE_TASK_NAME]

    async def handle(self, task_data: TaskData, *, capabilities: TaskCapabilities) -> TaskResult:
        """Write one channel and report the state it ended in."""
        try:
            request = decode_request(task_data, DacWriteRequest)
            require_exactly_one(request, "code", "output")
            channel = self._devices.channel(request.device_id, request.channel)
            if request.code is not None:
                channel.value = request.code
            else:
                channel.output = request.output
            # `DACChannel.value` asks the bus only on a readable part; a
            # write-only part returns the code cached by the write above
            # (scietex/hal/dac/channel.py:125-136), so this is bus-free there.
            code = channel.value
            # `DACChannel.output` reads `value` again, which on a readable part
            # is a second bus transaction; only the cached write-only path is
            # bus-free, so only that path can report an output here.
            output = None if channel.readable else channel.output
        except Exception as exc:
            return error_result(exc)
        self._devices.record_write(request.device_id, request.channel, code, output)
        return ok_result(
            DacWriteResponse(
                device_id=request.device_id,
                channel=request.channel,
                code=code,
                output=output,
                unit=str(channel.unit),
                max_code=channel.max_code,
            )
        )
