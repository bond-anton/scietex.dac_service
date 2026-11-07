"""``dac:configure`` control-plane task: select a part's range and control bits.

The request carries only the fields the caller wants to change; a field left
``None`` is not forwarded, so the part keeps its previous value. The response is
read back from the control register when the part exposes one, which keeps it
truthful about the fields the request omitted: a configuration call that never
names ``output`` still reports the real OUTEN bit.

A part with no ``configure()`` is rejected with `ConfigureUnsupported` instead
of silently accepting a no-op request. The forwarded keywords are bound against
the live signature first, so a HAL signature change surfaces as
``INVALID_REQUEST`` rather than a `TypeError` from deep inside the call.
"""

import inspect
from collections.abc import Callable
from typing import ClassVar

import msgspec

from scietex.hal.dac import DACError
from scietex.service.task_handler import (
    TaskCapabilities,
    TaskData,
    TaskHandler,
    TaskHandlerContext,
    TaskResult,
)

from ..devices import DacDeviceRegistry
from ._request import ConfigureUnsupported, decode_request, error_result, ok_result

#: Task name for the configuration control task.
DAC_CONFIGURE_TASK_NAME = "dac:configure"

#: Request fields forwarded to ``configure()``. ``output`` is not one of them:
#: it maps onto the separate enable/disable calls.
_FORWARDED_FIELDS = ("range", "slew", "sr_clock", "sr_step", "rext", "dcen")

#: Range code in the control register -> range name. The HAL keeps this table
#: private and exposes only the code through `read_control()`.
_RANGE_NAMES: dict[int, str] = {0b101: "4-20mA", 0b110: "0-20mA", 0b111: "0-24mA"}

#: Range reported when the control register holds a code the HAL does not use.
_RANGE_UNKNOWN = "unknown"

#: Range the HAL selects when `configure()` is called without one.
_DEFAULT_RANGE = "4-20mA"


class DacConfigureRequest(msgspec.Struct, forbid_unknown_fields=True):
    """Request payload for ``dac:configure``.

    Args:
        device_id: Device to configure.
        range: Output range name, or ``None`` to keep the current one.
        slew: ``True`` to enable the slew-rate controller, ``False`` to disable
            it, ``None`` to keep it as it is.
        sr_clock: Slew clock divider in ``0..15``, or ``None`` to keep it.
        sr_step: Slew step size in ``0..7``, or ``None`` to keep it.
        rext: External current-setting resistor selection, or ``None`` to keep
            it.
        dcen: Daisy-chain output enable, or ``None`` to keep it.
        output: ``True`` to enable the current output, ``False`` to disable it,
            ``None`` to leave OUTEN untouched.
    """

    device_id: str
    range: str | None = None
    slew: bool | None = None
    sr_clock: int | None = None
    sr_step: int | None = None
    rext: bool | None = None
    dcen: bool | None = None
    output: bool | None = None


class DacConfigureResponse(msgspec.Struct, frozen=True):
    """Result payload of a ``dac:configure`` task.

    Args:
        device_id: Device that was configured.
        range: Effective output range name, ``"unknown"`` when the control
            register holds a range code the HAL does not use.
        slew: Whether the slew-rate controller is enabled.
        sr_clock: Slew clock divider in ``0..15``.
        sr_step: Slew step size in ``0..7``.
        rext: Whether an external current-setting resistor is selected.
        dcen: Whether the daisy-chain output is enabled.
        output_enabled: Whether OUTEN is set, read from the part rather than
            taken from the request.
    """

    device_id: str
    range: str
    slew: bool
    sr_clock: int
    sr_step: int
    rext: bool
    dcen: bool
    output_enabled: bool


class DacConfigureHandler(TaskHandler):
    """Handle ``dac:configure`` on the control lane."""

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
        return [DAC_CONFIGURE_TASK_NAME]

    async def handle(self, task_data: TaskData, *, capabilities: TaskCapabilities) -> TaskResult:
        """Apply the requested control-word fields and report the result."""
        try:
            request = decode_request(task_data, DacConfigureRequest)
            device = self._devices.get(request.device_id)
            configure = self._require_method(device, "configure", request.device_id)
            # Resolve the output method before configuring anything: a part
            # that cannot switch its output must be rejected before it is
            # partially reconfigured.
            output_method = self._output_method(device, request)
            applied = {name: getattr(request, name) for name in _FORWARDED_FIELDS if getattr(request, name) is not None}
            self._bind(configure, applied, request.device_id)
            try:
                configure(**applied)
                if output_method is not None:
                    output_method()
            except DACError:
                # A DACError is a hardware state, not a malformed request; it
                # has its own row in the error table.
                raise
            except (TypeError, ValueError) as exc:
                raise msgspec.ValidationError(f"Device {request.device_id!r} rejected the configuration: {exc}") from exc
            response = self._describe(device, request)
        except Exception as exc:
            return error_result(exc)
        return ok_result(response)

    @staticmethod
    def _require_method(device: object, name: str, device_id: str) -> Callable[..., None]:
        """Return the device method ``name``, or reject the part.

        Raises:
            ConfigureUnsupported: The part does not expose ``name``.
        """
        method = getattr(device, name, None)
        if not callable(method):
            raise ConfigureUnsupported(f"Device {device_id!r} of type {type(device).__name__} has no {name}()")
        return method

    @classmethod
    def _output_method(cls, device: object, request: DacConfigureRequest) -> Callable[..., None] | None:
        """Return the enable/disable call the request asks for, if any."""
        if request.output is None:
            return None
        name = "enable_output" if request.output else "disable_output"
        return cls._require_method(device, name, request.device_id)

    @staticmethod
    def _bind(configure: Callable[..., None], applied: dict[str, object], device_id: str) -> None:
        """Bind the forwarded keywords against the live signature.

        This is the same discipline the registry applies to constructors: a
        keyword the part no longer accepts fails here as a terminal request
        error instead of surfacing as a `TypeError` from the call.

        Raises:
            msgspec.ValidationError: The part does not accept these keywords.
        """
        try:
            inspect.signature(configure).bind(**applied)
        except TypeError as exc:
            raise msgspec.ValidationError(
                f"configure() of device {device_id!r} does not accept {sorted(applied)}: {exc}"
            ) from exc

    @classmethod
    def _describe(cls, device: object, request: DacConfigureRequest) -> DacConfigureResponse:
        """Describe the state the part holds after the configuration."""
        read_control = getattr(device, "read_control", None)
        control = read_control() if callable(read_control) else None
        if control is not None:
            return DacConfigureResponse(
                device_id=request.device_id,
                range=_RANGE_NAMES.get(control.range_code, _RANGE_UNKNOWN),
                slew=control.sr_enabled,
                sr_clock=control.sr_clock,
                sr_step=control.sr_step,
                rext=control.rext,
                dcen=control.dcen,
                output_enabled=control.outen,
            )
        return cls._echoed_state(request)

    @staticmethod
    def _echoed_state(request: DacConfigureRequest) -> DacConfigureResponse:
        """Echo the applied values for a part that cannot report its registers.

        Fields the request omitted keep the HAL defaults, because without a
        readable control register there is no way to learn the part's prior
        state. No in-scope part takes this branch today: every configurable
        HAL family exposes ``read_control()``.
        """
        return DacConfigureResponse(
            device_id=request.device_id,
            range=request.range or _DEFAULT_RANGE,
            slew=bool(request.slew),
            sr_clock=request.sr_clock or 0,
            sr_step=request.sr_step or 0,
            rext=bool(request.rext),
            dcen=bool(request.dcen),
            output_enabled=bool(request.output),
        )
