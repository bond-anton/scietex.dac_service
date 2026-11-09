"""Tests for the ``dac:list``, ``dac:read``, ``dac:write``, ``dac:configure``, and ``dac:status`` handlers."""

import logging
from uuid import uuid4

import msgspec
import pytest
from conftest import RecordingStreamClient

from scietex.dac_service.config import DacDeviceSettings, DacServiceSettings
from scietex.dac_service.devices import (
    ChannelNotFound,
    DacDeviceRegistry,
    DeviceConfigError,
    DeviceNotFound,
    DeviceOpenError,
    DeviceUnavailable,
)
from scietex.dac_service.poller import DacPoller
from scietex.dac_service.task_handlers import (
    DAC_CONFIGURE_TASK_NAME,
    DAC_LIST_TASK_NAME,
    DAC_READ_TASK_NAME,
    DAC_STATUS_TASK_NAME,
    DAC_WRITE_TASK_NAME,
    DacChannelState,
    DacConfigureHandler,
    DacConfigureResponse,
    DacListHandler,
    DacListResponse,
    DacReadHandler,
    DacReadResponse,
    DacStatusHandler,
    DacStatusResponse,
    DacWriteHandler,
    DacWriteResponse,
)
from scietex.dac_service.task_handlers._request import ConfigureUnsupported, error_result
from scietex.hal.dac import (
    DACCodeError,
    DACError,
    DACOutputDisabledError,
    DACReadbackError,
    DACReferenceError,
    DACStrobeError,
)
from scietex.hal.spi import TransportClosed, TransportError
from scietex.service.task_handler import TaskCapabilities, TaskData, TaskHandlerContext


def _context() -> TaskHandlerContext:
    return TaskHandlerContext(service_name="test", instance_id="inst", logger=logging.getLogger("test"))


def _capabilities() -> TaskCapabilities:
    async def _noop(_task_id, _value) -> None:
        return None

    return TaskCapabilities(task_id=uuid4(), _write_progress=_noop)


def _handler(handler_class, registry: DacDeviceRegistry, **kwargs):
    return handler_class("test", _context(), devices=registry, **kwargs)


def _task(task: str, payload: bytes | str | dict | None = None) -> TaskData:
    """Build task data; a mapping is JSON-encoded, a str/bytes is passed through."""
    if isinstance(payload, dict):
        raw: bytes | str = msgspec.json.encode(payload)
    else:
        raw = payload if payload is not None else b""
    return TaskData(task_id=str(uuid4()), task=task, payload=raw)


def _json(payload: dict) -> bytes:
    return msgspec.json.encode(payload)


def _registry(settings: DacServiceSettings) -> DacDeviceRegistry:
    registry = DacDeviceRegistry(logging.getLogger("test.dac_service.handlers"))
    registry.rebuild(settings)
    return registry


def _decode(result, response_type):
    assert result.status == "success"
    return msgspec.json.decode(result.payload, type=response_type)


def _device(registry: DacDeviceRegistry, device_id: str = "dac0"):
    """Return the open fake device so a test can script its channels."""
    return registry.get(device_id)


def test_error_result_maps_the_dac_taxonomy() -> None:
    """Every exception family maps to its wire code and retry policy."""
    cases = [
        (DeviceNotFound("missing"), "DEVICE_NOT_FOUND", False),
        (ChannelNotFound("dac0[9]"), "CHANNEL_NOT_FOUND", False),
        (DeviceUnavailable("dac0", "SPI bus is gone"), "DEVICE_UNAVAILABLE", True),
        (DeviceOpenError("dac0", OSError("gone")), "DEVICE_UNAVAILABLE", True),
        (DACReadbackError("write-only"), "DAC_READBACK_ERROR", False),
        (DACOutputDisabledError("disabled"), "DAC_OUTPUT_DISABLED", False),
        (DACCodeError("out of range"), "DAC_CODE_OUT_OF_RANGE", False),
        (DACReferenceError("no reference"), "DAC_REFERENCE_ERROR", False),
        (DACStrobeError("no strobe"), "DAC_STROBE_ERROR", True),
        (TransportClosed("closed"), "TRANSPORT_ERROR", True),
        (TransportError("bus error"), "TRANSPORT_ERROR", True),
        (DeviceConfigError("bad settings"), "INVALID_REQUEST", False),
        (ConfigureUnsupported("no configure()"), "DAC_CONFIGURE_UNSUPPORTED", False),
        (msgspec.ValidationError("bad type"), "INVALID_REQUEST", False),
        (DACError("generic DAC failure"), "DAC_ERROR", True),
        (OSError("bus error"), "DEVICE_IO_ERROR", True),
    ]

    for exc, error_code, retryable in cases:
        result = error_result(exc)
        assert result.status == "error"
        assert result.error_code == error_code
        assert result.retryable is retryable


def test_list_declares_control_lane_and_task_name() -> None:
    handler = _handler(DacListHandler, _registry(DacServiceSettings()))
    assert handler.supported_tasks == [DAC_LIST_TASK_NAME]
    assert DacListHandler.control is True


@pytest.mark.asyncio
async def test_list_empty_registry_is_a_normal_result() -> None:
    result = await _handler(DacListHandler, _registry(DacServiceSettings())).handle(
        _task(DAC_LIST_TASK_NAME), capabilities=_capabilities()
    )

    assert _decode(result, DacListResponse).devices == []


@pytest.mark.asyncio
async def test_list_reports_available_device_with_geometry(fake_parts) -> None:
    """A live device reports part type, geometry, unit, and scaling."""
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE", reference=3.3)})
    result = await _handler(DacListHandler, _registry(settings)).handle(
        _task(DAC_LIST_TASK_NAME), capabilities=_capabilities()
    )

    [device] = _decode(result, DacListResponse).devices
    assert device.device_id == "dac0"
    assert device.type == "FAKE"
    assert device.available is True
    assert device.error is None
    assert device.channels == 2
    assert device.resolution == 12
    assert device.max_code == 4095
    assert device.unit == "V"
    assert device.reference == 3.3
    assert device.readable is True


@pytest.mark.asyncio
async def test_list_reports_open_failure(fake_parts) -> None:
    """A configured device that failed to open is listed, not raised."""
    settings = DacServiceSettings(devices={"broken": DacDeviceSettings(type="BROKEN")})
    result = await _handler(DacListHandler, _registry(settings)).handle(
        _task(DAC_LIST_TASK_NAME), capabilities=_capabilities()
    )

    [device] = _decode(result, DacListResponse).devices
    assert device.available is False
    assert device.type == "BROKEN"
    assert device.error is not None
    assert "SPI bus is gone" in device.error
    assert device.channels == 0
    assert device.resolution is None
    assert device.max_code is None
    assert device.unit is None
    assert device.reference is None
    assert device.readable is False


@pytest.mark.asyncio
async def test_list_filters_by_device_id(fake_parts) -> None:
    settings = DacServiceSettings(devices={"a": DacDeviceSettings(type="FAKE"), "b": DacDeviceSettings(type="FAKE")})
    result = await _handler(DacListHandler, _registry(settings)).handle(
        _task(DAC_LIST_TASK_NAME, _json({"device_id": "b"})), capabilities=_capabilities()
    )

    assert [device.device_id for device in _decode(result, DacListResponse).devices] == ["b"]


@pytest.mark.asyncio
async def test_list_unfiltered_reports_every_device_sorted(fake_parts) -> None:
    settings = DacServiceSettings(
        devices={
            "b": DacDeviceSettings(type="FAKE"),
            "a": DacDeviceSettings(type="FAKE"),
            "broken": DacDeviceSettings(type="BROKEN"),
        }
    )
    result = await _handler(DacListHandler, _registry(settings)).handle(
        _task(DAC_LIST_TASK_NAME), capabilities=_capabilities()
    )

    infos = _decode(result, DacListResponse).devices
    assert [device.device_id for device in infos] == ["a", "b", "broken"]
    assert [device.available for device in infos] == [True, True, False]


def test_read_declares_data_lane_and_task_name() -> None:
    handler = _handler(DacReadHandler, _registry(DacServiceSettings()))
    assert handler.supported_tasks == [DAC_READ_TASK_NAME]
    assert DacReadHandler.control is False


@pytest.mark.asyncio
async def test_read_returns_code_and_channel_metadata(fake_parts) -> None:
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE", reference=3.3)})
    registry = _registry(settings)
    _device(registry).channels[1].value = 2048

    result = await _handler(DacReadHandler, registry).handle(
        _task(DAC_READ_TASK_NAME, _json({"device_id": "dac0", "channel": 1})), capabilities=_capabilities()
    )

    response = _decode(result, DacReadResponse)
    assert response.device_id == "dac0"
    assert response.channel == 1
    assert response.code == 2048
    assert response.unit == "V"
    assert response.reference == 3.3
    assert response.max_code == 4095


@pytest.mark.asyncio
async def test_read_unknown_device_is_terminal() -> None:
    result = await _handler(DacReadHandler, _registry(DacServiceSettings())).handle(
        _task(DAC_READ_TASK_NAME, _json({"device_id": "missing"})), capabilities=_capabilities()
    )

    assert (result.status, result.error_code, result.retryable) == ("error", "DEVICE_NOT_FOUND", False)


@pytest.mark.asyncio
async def test_read_out_of_range_channel_is_terminal(fake_parts) -> None:
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")})
    result = await _handler(DacReadHandler, _registry(settings)).handle(
        _task(DAC_READ_TASK_NAME, _json({"device_id": "dac0", "channel": 99})), capabilities=_capabilities()
    )

    assert (result.status, result.error_code, result.retryable) == ("error", "CHANNEL_NOT_FOUND", False)


@pytest.mark.asyncio
async def test_read_write_only_part_surfaces_readback_error(fake_parts) -> None:
    """The HAL's DACReadbackError maps to a non-retryable code, not to I/O."""
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="WRITEONLY")})
    result = await _handler(DacReadHandler, _registry(settings)).handle(
        _task(DAC_READ_TASK_NAME, _json({"device_id": "dac0"})), capabilities=_capabilities()
    )

    assert (result.status, result.error_code, result.retryable) == ("error", "DAC_READBACK_ERROR", False)


@pytest.mark.asyncio
async def test_read_configured_failed_device_is_retryable(fake_parts) -> None:
    settings = DacServiceSettings(devices={"broken": DacDeviceSettings(type="BROKEN")})
    result = await _handler(DacReadHandler, _registry(settings)).handle(
        _task(DAC_READ_TASK_NAME, _json({"device_id": "broken"})), capabilities=_capabilities()
    )

    assert (result.status, result.error_code, result.retryable) == ("error", "DEVICE_UNAVAILABLE", True)
    assert "SPI bus is gone" in str(result.error)


def test_write_declares_data_lane_and_task_name() -> None:
    handler = _handler(DacWriteHandler, _registry(DacServiceSettings()))
    assert handler.supported_tasks == [DAC_WRITE_TASK_NAME]
    assert DacWriteHandler.control is False


@pytest.mark.asyncio
async def test_write_by_code(fake_parts) -> None:
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE", reference=3.3)})
    registry = _registry(settings)
    result = await _handler(DacWriteHandler, registry).handle(
        _task(DAC_WRITE_TASK_NAME, _json({"device_id": "dac0", "channel": 1, "code": 1234})),
        capabilities=_capabilities(),
    )

    response = _decode(result, DacWriteResponse)
    assert response.device_id == "dac0"
    assert response.channel == 1
    assert response.code == 1234
    assert response.unit == "V"
    assert response.max_code == 4095
    # A readable part would need a second bus transaction for the output.
    assert response.output is None
    assert _device(registry).channels[1].value == 1234


@pytest.mark.asyncio
async def test_write_by_output(fake_parts) -> None:
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE", reference=3.3)})
    registry = _registry(settings)
    result = await _handler(DacWriteHandler, registry).handle(
        _task(DAC_WRITE_TASK_NAME, _json({"device_id": "dac0", "output": 1.65})),
        capabilities=_capabilities(),
    )

    response = _decode(result, DacWriteResponse)
    assert response.code == round(1.65 / 3.3 * 4095)
    assert response.output is None
    assert _device(registry).channels[0].value == response.code


@pytest.mark.asyncio
async def test_write_reports_output_for_a_write_only_part(fake_parts) -> None:
    """The cached code makes the output computable with no extra bus traffic."""
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="WRITEONLY", reference=3.3)})
    result = await _handler(DacWriteHandler, _registry(settings)).handle(
        _task(DAC_WRITE_TASK_NAME, _json({"device_id": "dac0", "output": 1.65})),
        capabilities=_capabilities(),
    )

    response = _decode(result, DacWriteResponse)
    assert response.code == round(1.65 / 3.3 * 4095)
    assert response.output == pytest.approx(response.code * 3.3 / 4095)


@pytest.mark.asyncio
async def test_write_zero_output_is_a_value_not_an_absent_field(fake_parts) -> None:
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")})
    registry = _registry(settings)
    result = await _handler(DacWriteHandler, registry).handle(
        _task(DAC_WRITE_TASK_NAME, _json({"device_id": "dac0", "output": 0.0})),
        capabilities=_capabilities(),
    )

    response = _decode(result, DacWriteResponse)
    assert response.code == 0
    assert _device(registry).channels[0].value == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"code": 10, "output": 1.0}])
async def test_write_requires_exactly_one_target(fake_parts, payload) -> None:
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")})
    result = await _handler(DacWriteHandler, _registry(settings)).handle(
        _task(DAC_WRITE_TASK_NAME, _json({"device_id": "dac0", **payload})), capabilities=_capabilities()
    )

    assert (result.status, result.error_code, result.retryable) == ("error", "INVALID_REQUEST", False)


@pytest.mark.asyncio
async def test_write_out_of_range_code_is_terminal(fake_parts) -> None:
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")})
    result = await _handler(DacWriteHandler, _registry(settings)).handle(
        _task(DAC_WRITE_TASK_NAME, _json({"device_id": "dac0", "code": 4096})), capabilities=_capabilities()
    )

    assert (result.status, result.error_code, result.retryable) == ("error", "DAC_CODE_OUT_OF_RANGE", False)


@pytest.mark.asyncio
async def test_write_output_disabled_is_non_retryable(fake_parts) -> None:
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")})
    registry = _registry(settings)
    _device(registry).channels[0].write_error = DACOutputDisabledError("output disabled")

    result = await _handler(DacWriteHandler, registry).handle(
        _task(DAC_WRITE_TASK_NAME, _json({"device_id": "dac0", "code": 10})), capabilities=_capabilities()
    )

    assert (result.status, result.error_code, result.retryable) == ("error", "DAC_OUTPUT_DISABLED", False)
    assert registry.last_written() == {}


@pytest.mark.asyncio
async def test_write_records_the_last_written_state(fake_parts) -> None:
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="WRITEONLY", reference=3.3)})
    registry = _registry(settings)

    await _handler(DacWriteHandler, registry).handle(
        _task(DAC_WRITE_TASK_NAME, _json({"device_id": "dac0", "channel": 1, "code": 321})),
        capabilities=_capabilities(),
    )

    written = registry.last_written()
    assert list(written) == ["dac0"]
    [(channel, code, output)] = written["dac0"]
    assert (channel, code) == (1, 321)
    assert output == pytest.approx(321 * 3.3 / 4095)


def _recording_poller(registry: DacDeviceRegistry, client: RecordingStreamClient) -> DacPoller:
    return DacPoller(
        registry,
        logging.getLogger("test.dac_service.handlers.poller"),
        service_name="DacService",
        instance_id="inst",
        client_provider=lambda: client,
    )


@pytest.mark.asyncio
async def test_write_publishes_an_immediate_reading(fake_parts) -> None:
    """A write reports its setpoint to the stream without waiting for a tick."""
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE", reference=3.3)})
    registry = _registry(settings)
    client = RecordingStreamClient()
    poller = _recording_poller(registry, client)

    result = await _handler(DacWriteHandler, registry, poller=poller).handle(
        _task(DAC_WRITE_TASK_NAME, _json({"device_id": "dac0", "channel": 1, "code": 1234})),
        capabilities=_capabilities(),
    )

    assert _decode(result, DacWriteResponse).output is None
    [entry] = client.entries
    key, fields, _ = entry
    assert key == "scietex:DacService:inst:dac:dac0:1:readings"
    assert fields[2] == (b"code", b"1234")
    assert float(fields[3][1]) == pytest.approx(1234 * 3.3 / 4095)
    assert poller.states["dac0"].channels[1].code == 1234


@pytest.mark.asyncio
async def test_write_publishes_the_cached_output_of_a_write_only_part(fake_parts) -> None:
    """A write-only part's immediate reading carries its scaled output."""
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="WRITEONLY", reference=3.3)})
    registry = _registry(settings)
    client = RecordingStreamClient()
    poller = _recording_poller(registry, client)

    await _handler(DacWriteHandler, registry, poller=poller).handle(
        _task(DAC_WRITE_TASK_NAME, _json({"device_id": "dac0", "output": 1.65})),
        capabilities=_capabilities(),
    )

    [entry] = client.entries
    fields = entry[1]
    code = int(fields[2][1])
    assert float(fields[3][1]) == pytest.approx(code * 3.3 / 4095)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"device_id": "dac0", "code": 17},
        '{"device_id": "dac0", "code": 17}',
        b'{"device_id": "dac0", "code": 17}',
    ],
    ids=["mapping", "str", "bytes"],
)
async def test_write_decodes_every_payload_form(fake_parts, payload) -> None:
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")})
    result = await _handler(DacWriteHandler, _registry(settings)).handle(
        _task(DAC_WRITE_TASK_NAME, payload), capabilities=_capabilities()
    )

    assert _decode(result, DacWriteResponse).code == 17


def _ad54x0_registry() -> DacDeviceRegistry:
    """Registry with one configurable, register-readable AD54x0 double."""
    return _registry(DacServiceSettings(devices={"dac0": DacDeviceSettings(type="AD54X0")}))


def test_configure_declares_control_lane_and_task_name() -> None:
    handler = _handler(DacConfigureHandler, _registry(DacServiceSettings()))
    assert handler.supported_tasks == [DAC_CONFIGURE_TASK_NAME]
    assert DacConfigureHandler.control is True


@pytest.mark.asyncio
async def test_configure_forwards_only_the_fields_it_was_given(fake_parts) -> None:
    """A ``False`` or ``0`` is forwarded; an omitted field is not."""
    registry = _ad54x0_registry()
    result = await _handler(DacConfigureHandler, registry).handle(
        _task(
            DAC_CONFIGURE_TASK_NAME,
            _json({"device_id": "dac0", "range": "0-24mA", "slew": False, "sr_clock": 0, "rext": True}),
        ),
        capabilities=_capabilities(),
    )

    response = _decode(result, DacConfigureResponse)
    assert _device(registry).configure_calls == [{"range": "0-24mA", "slew": False, "sr_clock": 0, "rext": True}]
    assert (response.range, response.slew, response.sr_clock, response.rext) == ("0-24mA", False, 0, True)


@pytest.mark.asyncio
async def test_configure_enables_the_output_after_configure(fake_parts) -> None:
    registry = _ad54x0_registry()
    result = await _handler(DacConfigureHandler, registry).handle(
        _task(DAC_CONFIGURE_TASK_NAME, _json({"device_id": "dac0", "range": "0-20mA", "output": True})),
        capabilities=_capabilities(),
    )

    response = _decode(result, DacConfigureResponse)
    assert response.output_enabled is True
    # The control word is written before the output bit is touched.
    assert _device(registry).events == ["configure", "enable_output"]


@pytest.mark.asyncio
async def test_configure_disables_the_output(fake_parts) -> None:
    registry = _ad54x0_registry()
    _device(registry).outen = True

    result = await _handler(DacConfigureHandler, registry).handle(
        _task(DAC_CONFIGURE_TASK_NAME, _json({"device_id": "dac0", "output": False})),
        capabilities=_capabilities(),
    )

    response = _decode(result, DacConfigureResponse)
    assert response.output_enabled is False
    assert _device(registry).events == ["configure", "disable_output"]


@pytest.mark.asyncio
async def test_configure_leaves_the_output_bit_alone_when_not_asked(fake_parts) -> None:
    registry = _ad54x0_registry()
    _device(registry).outen = True

    result = await _handler(DacConfigureHandler, registry).handle(
        _task(DAC_CONFIGURE_TASK_NAME, _json({"device_id": "dac0", "range": "4-20mA"})),
        capabilities=_capabilities(),
    )

    response = _decode(result, DacConfigureResponse)
    # OUTEN is read from the control register, not inferred from the omitted
    # request field.
    assert response.output_enabled is True
    assert _device(registry).events == ["configure"]


@pytest.mark.asyncio
async def test_configure_reports_the_state_read_back_from_the_control_register(fake_parts) -> None:
    registry = _ad54x0_registry()
    result = await _handler(DacConfigureHandler, registry).handle(
        _task(
            DAC_CONFIGURE_TASK_NAME,
            _json(
                {
                    "device_id": "dac0",
                    "range": "0-24mA",
                    "slew": True,
                    "sr_clock": 5,
                    "sr_step": 3,
                    "rext": True,
                    "dcen": True,
                }
            ),
        ),
        capabilities=_capabilities(),
    )

    response = _decode(result, DacConfigureResponse)
    assert (
        response.range,
        response.slew,
        response.sr_clock,
        response.sr_step,
        response.rext,
        response.dcen,
    ) == ("0-24mA", True, 5, 3, True, True)
    assert _device(registry).control_calls == 1


@pytest.mark.asyncio
async def test_configure_rejects_an_unknown_field(fake_parts) -> None:
    registry = _ad54x0_registry()
    result = await _handler(DacConfigureHandler, registry).handle(
        _task(DAC_CONFIGURE_TASK_NAME, _json({"device_id": "dac0", "current": 0.01})),
        capabilities=_capabilities(),
    )

    assert (result.status, result.error_code, result.retryable) == ("error", "INVALID_REQUEST", False)
    assert _device(registry).configure_calls == []


@pytest.mark.asyncio
async def test_configure_unknown_device_is_terminal() -> None:
    result = await _handler(DacConfigureHandler, _registry(DacServiceSettings())).handle(
        _task(DAC_CONFIGURE_TASK_NAME, _json({"device_id": "missing"})), capabilities=_capabilities()
    )

    assert (result.status, result.error_code, result.retryable) == ("error", "DEVICE_NOT_FOUND", False)


@pytest.mark.asyncio
async def test_configure_configured_failed_device_is_retryable(fake_parts) -> None:
    settings = DacServiceSettings(devices={"broken": DacDeviceSettings(type="BROKEN")})
    result = await _handler(DacConfigureHandler, _registry(settings)).handle(
        _task(DAC_CONFIGURE_TASK_NAME, _json({"device_id": "broken", "range": "4-20mA"})),
        capabilities=_capabilities(),
    )

    assert (result.status, result.error_code, result.retryable) == ("error", "DEVICE_UNAVAILABLE", True)
    assert "SPI bus is gone" in str(result.error)


@pytest.mark.asyncio
async def test_configure_part_without_configure_is_rejected(fake_parts) -> None:
    """A part with no ``configure()`` fails instead of silently doing nothing."""
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")})
    registry = _registry(settings)
    _device(registry).channels[0].value = 42

    result = await _handler(DacConfigureHandler, registry).handle(
        _task(DAC_CONFIGURE_TASK_NAME, _json({"device_id": "dac0", "range": "0-20mA"})),
        capabilities=_capabilities(),
    )

    assert (result.status, result.error_code, result.retryable) == ("error", "DAC_CONFIGURE_UNSUPPORTED", False)
    assert _device(registry).channels[0].value == 42


def test_status_declares_control_lane_and_task_name() -> None:
    handler = _handler(DacStatusHandler, _registry(DacServiceSettings()))
    assert handler.supported_tasks == [DAC_STATUS_TASK_NAME]
    assert DacStatusHandler.control is True


@pytest.mark.asyncio
async def test_status_of_a_plain_part_reports_geometry_without_registers(fake_parts) -> None:
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE", reference=3.3)})
    registry = _registry(settings)
    registry.record_write("dac0", 1, 2048, 1.65)

    result = await _handler(DacStatusHandler, registry).handle(
        _task(DAC_STATUS_TASK_NAME, _json({"device_id": "dac0"})), capabilities=_capabilities()
    )

    response = _decode(result, DacStatusResponse)
    assert response.device_id == "dac0"
    assert response.type == "FAKE"
    assert response.available is True
    assert response.error is None
    assert response.readable is True
    assert response.channels == 2
    assert response.resolution == 12
    assert response.max_code == 4095
    assert response.unit == "V"
    assert response.reference == 3.3
    assert response.last_written == [DacChannelState(channel=1, code=2048, output=1.65)]
    assert response.status is None
    assert response.control is None


@pytest.mark.asyncio
async def test_status_filters_last_written_to_the_requested_device(fake_parts) -> None:
    settings = DacServiceSettings(devices={"a": DacDeviceSettings(type="FAKE"), "b": DacDeviceSettings(type="FAKE")})
    registry = _registry(settings)
    registry.record_write("a", 0, 1, 0.1)
    registry.record_write("b", 1, 2, 0.2)

    result = await _handler(DacStatusHandler, registry).handle(
        _task(DAC_STATUS_TASK_NAME, _json({"device_id": "b"})), capabilities=_capabilities()
    )

    assert _decode(result, DacStatusResponse).last_written == [DacChannelState(channel=1, code=2, output=0.2)]


@pytest.mark.asyncio
async def test_status_of_an_ad54x0_decodes_both_registers(fake_parts) -> None:
    registry = _ad54x0_registry()
    device = _device(registry)
    device.range = "0-20mA"
    device.rext = True

    result = await _handler(DacStatusHandler, registry).handle(
        _task(DAC_STATUS_TASK_NAME, _json({"device_id": "dac0"})), capabilities=_capabilities()
    )

    response = _decode(result, DacStatusResponse)
    assert (response.unit, response.channels) == ("A", 1)
    assert response.status == {"iout_fault": False, "slew_active": False, "overtemp": False}
    assert response.control == {
        "range_code": 0b110,
        "rext": True,
        "outen": False,
        "sr_clock": 0,
        "sr_step": 0,
        "sr_enabled": False,
        "dcen": False,
        "raw": 0,
    }
    assert (device.status_calls, device.control_calls) == (1, 1)


@pytest.mark.asyncio
async def test_status_configured_failed_device_is_retryable(fake_parts) -> None:
    settings = DacServiceSettings(devices={"broken": DacDeviceSettings(type="BROKEN")})
    result = await _handler(DacStatusHandler, _registry(settings)).handle(
        _task(DAC_STATUS_TASK_NAME, _json({"device_id": "broken"})), capabilities=_capabilities()
    )

    assert (result.status, result.error_code, result.retryable) == ("error", "DEVICE_UNAVAILABLE", True)
    assert "SPI bus is gone" in str(result.error)


@pytest.mark.asyncio
async def test_status_unknown_device_is_terminal() -> None:
    result = await _handler(DacStatusHandler, _registry(DacServiceSettings())).handle(
        _task(DAC_STATUS_TASK_NAME, _json({"device_id": "missing"})), capabilities=_capabilities()
    )

    assert (result.status, result.error_code, result.retryable) == ("error", "DEVICE_NOT_FOUND", False)
