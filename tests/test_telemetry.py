"""Tests for the DAC telemetry payload builder and the Telemetry manager loop."""

import logging
from unittest.mock import Mock

import msgspec
import pytest
from fake_worker import FailingClient, FakeDacWorker, RecordingClient
from glide import ExpiryType, RequestError

from scietex.dac_service.config import DacDeviceSettings, DacServiceSettings
from scietex.dac_service.dac_worker import DacWorker
from scietex.dac_service.devices import DacDeviceRegistry
from scietex.dac_service.telemetry import (
    TELEMETRY_INTERVAL,
    TELEMETRY_TTL,
    DacChannelTelemetry,
    DacServiceTelemetry,
    build_telemetry,
)
from scietex.service import ValkeyWorkerConfig


def _registry(settings: DacServiceSettings) -> DacDeviceRegistry:
    registry = DacDeviceRegistry(logging.getLogger("test.dac_service.telemetry"))
    registry.rebuild(settings)
    return registry


def _telemetry(worker: FakeDacWorker) -> DacServiceTelemetry:
    """Build a snapshot from the fake's four attributes."""
    return build_telemetry(
        worker.dac_settings,
        worker.devices,
        service=worker.service_name,
        instance_id=worker.instance_id,
    )


def _make_worker(tmp_path) -> DacWorker:
    """Build a real worker with a conf dir and no remote-config handlers."""
    return DacWorker(
        ValkeyWorkerConfig(
            service_name="test",
            conf_dir=str(tmp_path),
            remote_config_enabled=False,
        )
    )


def test_telemetry_interval_and_ttl() -> None:
    """The push cadence and TTL are the frozen contract."""
    assert TELEMETRY_INTERVAL == 2.0
    assert TELEMETRY_TTL == 6


def test_unconfigured_snapshot_is_empty_and_encodes() -> None:
    """A pre-startup snapshot reports configured=False and survives msgpack."""
    telemetry = _telemetry(FakeDacWorker())

    assert telemetry.service == "dac-test"
    assert telemetry.instance_id == "instance-1"
    assert telemetry.configured is False
    assert telemetry.devices == []
    assert telemetry.schema_version == 1
    assert msgspec.msgpack.decode(msgspec.msgpack.encode(telemetry), type=DacServiceTelemetry) == telemetry


def test_snapshot_reports_the_last_written_channel(fake_parts) -> None:
    """An open device reports the channel state its last write left behind."""
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE", reference=3.3)})
    registry = _registry(settings)
    registry.record_write("dac0", 1, 1234, 1.5)

    telemetry = _telemetry(FakeDacWorker(settings=settings, registry=registry))

    assert telemetry.configured is True
    [device] = telemetry.devices
    assert device.device_id == "dac0"
    assert device.type == "FAKE"
    assert device.available is True
    assert device.error is None
    assert device.channels == [DacChannelTelemetry(channel=1, code=1234, output=1.5, unit="V")]


def test_device_without_writes_reports_no_channels(fake_parts) -> None:
    """A device that was never written has an empty channel list, not zeros."""
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")})

    telemetry = _telemetry(FakeDacWorker(settings=settings, registry=_registry(settings)))

    [device] = telemetry.devices
    assert device.available is True
    assert device.channels == []


def test_snapshot_never_touches_the_bus(fake_parts, monkeypatch) -> None:
    """Building a snapshot calls no read API on the device or its channels."""
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")})
    registry = _registry(settings)
    registry.record_write("dac0", 0, 100, None)
    device = registry.get("dac0")
    channel = device.channels[0]
    trap = Mock(side_effect=AssertionError("telemetry touched the bus"))
    monkeypatch.setattr(type(device), "read_all", trap)
    monkeypatch.setattr(type(channel), "readback", trap)
    monkeypatch.setattr(type(channel), "value", trap)

    telemetry = _telemetry(FakeDacWorker(settings=settings, registry=registry))

    assert trap.call_count == 0
    assert telemetry.devices[0].channels[0].code == 100


def test_snapshot_reports_open_failure(fake_parts) -> None:
    """A configured device that failed to open is still listed with its reason."""
    settings = DacServiceSettings(devices={"broken": DacDeviceSettings(type="BROKEN")})

    telemetry = _telemetry(FakeDacWorker(settings=settings, registry=_registry(settings)))

    assert telemetry.configured is True
    [device] = telemetry.devices
    assert device.device_id == "broken"
    assert device.type == "BROKEN"
    assert device.available is False
    assert device.error is not None
    assert "SPI bus is gone" in device.error
    assert device.channels == []


def test_snapshot_round_trips_through_msgpack(fake_parts) -> None:
    """The wire form decodes back to an equal snapshot."""
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE", reference=3.3)})
    registry = _registry(settings)
    registry.record_write("dac0", 0, 4095, 3.3)
    telemetry = _telemetry(FakeDacWorker(settings=settings, registry=registry))

    decoded = msgspec.msgpack.decode(msgspec.msgpack.encode(telemetry), type=DacServiceTelemetry)

    assert decoded == telemetry
    assert decoded.devices[0].channels[0].unit == "V"


@pytest.mark.asyncio
async def test_telemetry_loop_writes_key_with_ttl(tmp_path, monkeypatch) -> None:
    """The manager writes scietex:{service}:{instance}:telemetry with TTL 6."""
    monkeypatch.setattr("scietex.dac_service.dac_worker.TELEMETRY_INTERVAL", 0)
    worker = _make_worker(tmp_path)
    client = RecordingClient()
    monkeypatch.setattr(worker, "_client", client)

    await worker.telemetry_loop()

    assert len(client.calls) == 1
    key, value, expiry = client.calls[0]
    assert key == f"scietex:{worker.service_name}:{worker.instance_id}:telemetry"
    assert expiry.expiry_type == ExpiryType.SEC
    assert int(expiry.value) == 6

    decoded = msgspec.msgpack.decode(value, type=DacServiceTelemetry)
    assert decoded.configured is False
    assert decoded.devices == []
    assert decoded.schema_version == 1


@pytest.mark.asyncio
async def test_telemetry_loop_reports_write_failure(tmp_path, monkeypatch) -> None:
    """A Glide failure is logged and reported to the transport health."""
    monkeypatch.setattr("scietex.dac_service.dac_worker.TELEMETRY_INTERVAL", 0)
    worker = _make_worker(tmp_path)
    client = FailingClient(RequestError("valkey is down"))
    monkeypatch.setattr(worker, "_client", client)

    failures: list[Exception] = []
    monkeypatch.setattr(worker._health, "report_failure", failures.append)

    await worker.telemetry_loop()

    assert client.calls == 1
    assert len(failures) == 1
    assert str(failures[0]) == "valkey is down"
