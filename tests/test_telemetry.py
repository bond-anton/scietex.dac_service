"""Tests for the DAC telemetry payload builder and the Telemetry manager loop."""

import logging

import msgspec
import pytest
from conftest import FakeDacChannel
from fake_worker import FailingClient, FakeDacWorker, RecordingClient
from glide import ExpiryType, RequestError

from scietex.dac_service import telemetry as telemetry_module
from scietex.dac_service.config import DacDeviceSettings, DacServiceSettings
from scietex.dac_service.dac_worker import DacWorker
from scietex.dac_service.devices import DacDeviceRegistry
from scietex.dac_service.poller import DacPoller
from scietex.dac_service.telemetry import (
    SCHEMA_VERSION,
    TELEMETRY_INTERVAL,
    TELEMETRY_TTL,
    build_telemetry,
)
from scietex.service import ValkeyWorkerConfig


class ToggleChannel(FakeDacChannel):
    """Channel double whose register read fails while ``exc`` is set."""

    def __init__(self, code: int = 0) -> None:
        super().__init__(0, value=code)
        self.exc: Exception | None = None

    def readback(self) -> int:
        if self.exc is not None:
            raise self.exc
        return super().readback()


def _registry(settings: DacServiceSettings) -> DacDeviceRegistry:
    registry = DacDeviceRegistry(logging.getLogger("test.dac_service.telemetry"))
    registry.rebuild(settings)
    return registry


def _poller(registry: DacDeviceRegistry, settings: DacServiceSettings, **kwargs) -> DacPoller:
    poller = DacPoller(
        registry,
        logging.getLogger("test.dac_service.telemetry.poller"),
        service_name="dac-test",
        instance_id="instance-1",
        **kwargs,
    )
    poller.configure(settings)
    return poller


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


def test_build_telemetry_before_initialize_is_unconfigured() -> None:
    """A pre-startup worker reports configured=False with no devices."""
    payload = build_telemetry(FakeDacWorker())

    assert payload["service"] == "dac-test"
    assert payload["instance_id"] == "instance-1"
    assert payload["configured"] is False
    assert payload["poll_enabled"] is True
    assert payload["state"] == "stopped"
    assert payload["devices"] == {}
    assert payload["schema_version"] == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_build_telemetry_reports_channels_and_states(fake_parts) -> None:
    """A polled device reports its state, type, and per-channel snapshots."""
    settings = DacServiceSettings(
        devices={
            "dac1": DacDeviceSettings(type="FAKE", poll=False),
            "dac0": DacDeviceSettings(type="FAKE", reference=10.24),
        }
    )
    registry = _registry(settings)
    poller = _poller(registry, settings)
    registry.get("dac0").channels[1].value = 512
    await poller.start()
    try:
        assert await poller._read_and_publish("dac0", 1) is True
    finally:
        await poller.stop()

    payload = build_telemetry(FakeDacWorker(settings=settings, registry=registry, poller=poller))

    assert payload["configured"] is True
    assert payload["state"] == "ok"
    assert list(payload["devices"]) == ["dac0", "dac1"]
    polled = payload["devices"]["dac0"]
    assert polled["state"] == "ok"
    assert polled["type"] == "FAKE"
    assert polled["channel_count"] == 2
    assert polled["last_error"] is None
    assert polled["last_ok"] is not None
    channel = polled["channels"]["1"]
    assert channel["value"] == 512.0
    assert channel["code"] == 512
    assert channel["output"] == pytest.approx(512 * 10.24 / 4095)
    assert channel["ts"] is not None
    assert channel["error"] is None
    stopped = payload["devices"]["dac1"]
    assert stopped["state"] == "stopped"
    assert stopped["channel_count"] == 2
    assert stopped["channels"] == {}


@pytest.mark.asyncio
async def test_build_telemetry_reports_open_failure(fake_parts) -> None:
    """A configured device that failed to open is reported as an error."""
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="BROKEN")})
    registry = _registry(settings)
    poller = _poller(registry, settings)
    await poller.start()
    await poller.stop()

    payload = build_telemetry(FakeDacWorker(settings=settings, registry=registry, poller=poller))

    assert payload["state"] == "error"
    device = payload["devices"]["dac0"]
    assert device["state"] == "error"
    assert device["type"] == "BROKEN"
    assert "SPI bus is gone" in device["last_error"]
    assert device["last_ok"] is None
    assert device["channels"] == {}
    assert "channel_count" not in device


def test_channel_count_follows_the_hal_geometry(fake_parts) -> None:
    """The count is the part's output count, present even before any channel row."""
    settings = DacServiceSettings(devices={"loop": DacDeviceSettings(type="AD54X0")})
    registry = _registry(settings)

    payload = build_telemetry(FakeDacWorker(settings=settings, registry=registry, poller=_poller(registry, settings)))

    assert payload["devices"]["loop"]["channel_count"] == 1


@pytest.mark.asyncio
async def test_output_stays_none_without_reference(fake_parts) -> None:
    """An unscaled channel reports a null output, never a zero."""
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")})
    registry = _registry(settings)
    poller = _poller(registry, settings)
    registry.get("dac0").channels = (FakeDacChannel(0, value=100, reference=None),)
    await poller.start()
    try:
        await poller._read_and_publish("dac0", 0)
    finally:
        await poller.stop()

    payload = build_telemetry(FakeDacWorker(settings=settings, registry=registry, poller=poller))

    channel = payload["devices"]["dac0"]["channels"]["0"]
    assert channel["value"] == 100.0
    assert channel["code"] == 100
    assert channel["output"] is None


@pytest.mark.asyncio
async def test_error_threshold_flips_state_and_recovery_clears_it(fake_parts) -> None:
    """Consecutive failures reach the threshold; one success restores the device."""
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")}, failure_threshold=2)
    registry = _registry(settings)
    poller = _poller(registry, settings)
    channel = ToggleChannel()
    registry.get("dac0").channels = (channel,)
    await poller.start()
    try:
        channel.exc = OSError("bus error")
        await poller._read_and_publish("dac0", 0)
        await poller._read_and_publish("dac0", 0)
        failed = build_telemetry(FakeDacWorker(settings=settings, registry=registry, poller=poller))
        assert failed["state"] == "error"
        assert failed["devices"]["dac0"]["state"] == "error"
        assert failed["devices"]["dac0"]["last_error"] == "bus error"
        assert failed["devices"]["dac0"]["channels"]["0"]["value"] is None
        assert failed["devices"]["dac0"]["channels"]["0"]["error"] == "bus error"

        channel.exc = None
        channel.value = 7
        await poller._read_and_publish("dac0", 0)
        recovered = build_telemetry(FakeDacWorker(settings=settings, registry=registry, poller=poller))
        assert recovered["state"] == "ok"
        assert recovered["devices"]["dac0"]["state"] == "ok"
        assert recovered["devices"]["dac0"]["last_error"] is None
        assert recovered["devices"]["dac0"]["channels"]["0"]["value"] == 7.0
        assert recovered["devices"]["dac0"]["channels"]["0"]["error"] is None
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_poll_disabled_reports_stopped(fake_parts) -> None:
    """A configured device is still listed when service-wide polling is off."""
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")}, poll_enabled=False)
    registry = _registry(settings)
    poller = _poller(registry, settings)
    await poller.start()
    await poller.stop()

    payload = build_telemetry(FakeDacWorker(settings=settings, registry=registry, poller=poller))

    assert payload["poll_enabled"] is False
    assert payload["devices"]["dac0"]["state"] == "stopped"


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

    decoded = msgspec.msgpack.decode(value, type=dict)
    assert decoded["configured"] is False
    assert decoded["devices"] == {}
    assert decoded["state"] == "stopped"
    assert decoded["schema_version"] == SCHEMA_VERSION


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


def test_state_constants_are_the_contract() -> None:
    """The three states the API and UI switch on are spelled exactly."""
    assert telemetry_module.STATE_OK == "ok"
    assert telemetry_module.STATE_ERROR == "error"
    assert telemetry_module.STATE_STOPPED == "stopped"
