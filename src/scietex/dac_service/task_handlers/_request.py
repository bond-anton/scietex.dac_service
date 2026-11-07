"""Request decoding and result construction shared by the DAC task handlers.

The exception-to-error-code table lives here and nowhere else: the HAL, the
registry, and the framework each raise their own exception type, and the wire
taxonomy must not drift between handlers. A handler wraps its body in one
``except Exception`` and hands the exception to `error_result`.

The table is ordered: an earlier rule wins, so every `DACError` subclass is
listed before the `DACError` catch-all, and `msgspec.ValidationError` is not
listed separately because it is a `msgspec.DecodeError`.
"""

from typing import TypeVar

import msgspec

from scietex.hal.dac import (
    DACCodeError,
    DACError,
    DACOutputDisabledError,
    DACReadbackError,
    DACReferenceError,
    DACStrobeError,
)
from scietex.hal.spi import TransportError
from scietex.service.task_handler import TaskData, TaskResult

from ..devices import (
    ChannelNotFound,
    DeviceConfigError,
    DeviceNotFound,
    DeviceOpenError,
    DeviceUnavailable,
)

_RequestT = TypeVar("_RequestT", bound=msgspec.Struct)


class ConfigureUnsupported(RuntimeError):
    """The device family cannot be configured through this handler.

    Terminal: the request names a part with no ``configure()`` (or no output
    enable/disable method), and retrying cannot add the missing method.
    """


#: Exception family -> (error code, retryable). Ordered: an earlier rule wins,
#: so a subclass must be listed before its base. ``TransportClosed`` is a
#: `TransportError`, and `msgspec.ValidationError` is a `msgspec.DecodeError`,
#: so the base entries also cover those subclasses.
_ERROR_RULES: tuple[tuple[type[Exception], str, bool], ...] = (
    (DeviceNotFound, "DEVICE_NOT_FOUND", False),
    (ChannelNotFound, "CHANNEL_NOT_FOUND", False),
    (DeviceUnavailable, "DEVICE_UNAVAILABLE", True),
    (DeviceOpenError, "DEVICE_UNAVAILABLE", True),
    (DACReadbackError, "DAC_READBACK_ERROR", False),
    (DACOutputDisabledError, "DAC_OUTPUT_DISABLED", False),
    (DACCodeError, "DAC_CODE_OUT_OF_RANGE", False),
    (DACReferenceError, "DAC_REFERENCE_ERROR", False),
    (DACStrobeError, "DAC_STROBE_ERROR", True),
    (TransportError, "TRANSPORT_ERROR", True),
    (msgspec.DecodeError, "INVALID_REQUEST", False),
    (DeviceConfigError, "INVALID_REQUEST", False),
    (ConfigureUnsupported, "DAC_CONFIGURE_UNSUPPORTED", False),
    (DACError, "DAC_ERROR", True),
)


def decode_request(task_data: TaskData, request_type: type[_RequestT]) -> _RequestT:
    """Decode the JSON request payload as ``request_type``.

    An empty payload decodes as ``{}`` so a task whose request fields all have
    defaults can be submitted without a body. A malformed body or a missing
    required field raises `msgspec.DecodeError` (with `msgspec.ValidationError`
    as its subclass for type mismatches); the handler maps both to
    ``INVALID_REQUEST`` through `error_result`.

    Args:
        task_data: The task whose ``payload`` holds the JSON request.
        request_type: The `msgspec.Struct` type to validate against.

    Returns:
        The decoded request.

    Raises:
        msgspec.DecodeError: The payload is not valid JSON for ``request_type``.
    """
    return msgspec.json.decode(task_data.payload or b"{}", type=request_type)


def require_exactly_one(payload: msgspec.Struct, *names: str) -> None:
    """Require exactly one of ``names`` to be set on ``payload``.

    "Set" means not ``None``: a legitimate ``0.0`` or ``0`` still counts, which
    is why the check is not a truthiness test.

    Args:
        payload: Decoded request struct.
        names: Field names on ``payload`` that are mutually exclusive.

    Raises:
        msgspec.ValidationError: Zero or more than one of the fields is set.
    """
    present = [name for name in names if getattr(payload, name) is not None]
    if len(present) != 1:
        joined = ", ".join(names)
        raise msgspec.ValidationError(f"Exactly one of {joined} must be set, got {present or 'none'}")


def ok_result(response: msgspec.Struct) -> TaskResult:
    """Build a successful result carrying the JSON-encoded ``response``.

    Responses are JSON because the API serializes task-result bytes as base64
    and the UI decodes them without a msgpack dependency.
    """
    return TaskResult(status="success", payload=msgspec.json.encode(response))


def error_result(exc: Exception) -> TaskResult:
    """Map a handler exception onto the shared task error taxonomy.

    Device and channel ids that do not resolve are terminal: the request is
    wrong, not the hardware. A configured device that is unavailable, one that
    could not be opened, and every bus failure are retryable: the hardware may
    come back. An unexpected exception is treated as a transient I/O failure so
    a single busy-bus error does not permanently fail the task.
    """
    for exc_type, error_code, retryable in _ERROR_RULES:
        if isinstance(exc, exc_type):
            return TaskResult(
                status="error",
                error=str(exc),
                error_code=error_code,
                retryable=retryable,
            )
    return TaskResult(
        status="error",
        error=str(exc),
        error_code="DEVICE_IO_ERROR",
        retryable=True,
    )
