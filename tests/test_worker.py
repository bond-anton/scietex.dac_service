"""Tests for the DacWorker lifecycle, config apply hook, and handler wiring."""

import logging

import pytest

from scietex.dac_service import dac_worker as dac_worker_module
from scietex.dac_service import devices as devices_module
from scietex.dac_service.config import DAC_SECTION, DacDeviceSettings, DacServiceSettings
from scietex.dac_service.dac_worker import DacWorker
from scietex.dac_service.devices import DeviceUnavailable
from scietex.service import TaskProcessor, ValkeyWorker, ValkeyWorkerConfig


def _make_worker(tmp_path) -> DacWorker:
    """Build a worker with a real conf_dir and no remote-config handlers."""
    return DacWorker(
        ValkeyWorkerConfig(
            service_name="test",
            conf_dir=str(tmp_path),
            remote_config_enabled=False,
        )
    )


@pytest.fixture
def fake_bootstrap(monkeypatch):
    """Return a setter that replaces the L1 bootstrap provider's loader."""

    def _set(patch: dict) -> None:
        monkeypatch.setattr(dac_worker_module, "read_dac_config", lambda conf_dir, **kwargs: patch)

    return _set


@pytest.fixture
def skip_valkey(monkeypatch):
    """Replace ``ValkeyWorker.initialize`` with the L0+L1 seeding step only."""

    async def fake_initialize(self) -> bool:
        self.seed_config_bootstrap()
        return True

    monkeypatch.setattr(ValkeyWorker, "initialize", fake_initialize)


@pytest.mark.asyncio
async def test_cleanup_safe_when_never_started(tmp_path) -> None:
    """cleanup() is a no-op when the worker was never initialized."""
    worker = _make_worker(tmp_path)

    await worker.cleanup()

    assert worker.dac_settings is None
    assert worker.devices.generation == 0
    assert worker.devices.ids() == []


@pytest.mark.asyncio
async def test_initialize_fails_when_settings_unresolved(tmp_path, monkeypatch, skip_valkey, caplog) -> None:
    """A bootstrap loader failure leaves the section unresolved and fails startup."""

    def bad_loader(conf_dir, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(dac_worker_module, "read_dac_config", bad_loader)
    worker = _make_worker(tmp_path)

    with caplog.at_level(logging.ERROR):
        assert await worker.initialize() is False

    assert worker.dac_settings is None
    assert worker.devices.generation == 0
    assert any("were not resolved" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_initialize_opens_devices_from_bootstrap(tmp_path, fake_parts, fake_bootstrap, skip_valkey) -> None:
    """initialize() builds the registry from the merged bootstrap settings."""
    fake_bootstrap({"devices": {"dac0": {"type": "FAKE", "reference": 3.3}}})
    worker = _make_worker(tmp_path)

    assert await worker.initialize() is True

    settings = worker.dac_settings
    assert settings is not None
    assert settings.devices["dac0"].reference == 3.3
    # The registry is built from the same merged struct the worker stores.
    assert worker.devices.settings is settings
    assert worker.devices.ids() == ["dac0"]
    assert worker.devices.get("dac0").reference == 3.3
    await worker.cleanup()


@pytest.mark.asyncio
async def test_initialize_survives_unopenable_device(tmp_path, fake_parts, fake_bootstrap, skip_valkey) -> None:
    """A device that fails to open is recorded; startup still succeeds."""
    fake_bootstrap({"devices": {"dac0": {"type": "BROKEN"}}})
    worker = _make_worker(tmp_path)

    assert await worker.initialize() is True

    assert worker.dac_settings is not None
    assert worker.dac_settings.devices["dac0"].type == "BROKEN"
    assert worker.devices.ids() == ["dac0"]
    assert "SPI bus is gone" in worker.devices.errors["dac0"]
    await worker.cleanup()


def test_apply_hook_stores_without_rebuild_before_startup(tmp_path) -> None:
    """Before startup the apply hook stores settings without opening devices."""
    worker = _make_worker(tmp_path)
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")})

    worker._apply_settings(settings)

    assert worker.dac_settings is settings
    assert worker.devices.generation == 0
    assert worker.devices.settings is None
    assert worker.devices.ids() == []


@pytest.mark.asyncio
async def test_apply_hook_rebuilds_and_logs_after_startup(tmp_path, fake_parts, fake_bootstrap, skip_valkey, caplog) -> None:
    """A config delivery after startup rebuilds the live registry and logs failures."""
    fake_bootstrap({"devices": {"a": {"type": "FAKE"}}})
    worker = _make_worker(tmp_path)
    assert await worker.initialize() is True
    assert worker.devices.generation == 1

    applied = DacServiceSettings(
        devices={
            "a": DacDeviceSettings(type="FAKE"),
            "broken": DacDeviceSettings(type="BROKEN"),
        }
    )
    with caplog.at_level(logging.ERROR):
        worker._apply_settings(applied)

    assert worker.dac_settings is applied
    assert worker.devices.generation == 2
    assert worker.devices.ids() == ["a", "broken"]
    assert any("SPI bus is gone" in record.message for record in caplog.records)
    await worker.cleanup()


@pytest.mark.asyncio
async def test_identical_settings_skip_the_rebuild(tmp_path, fake_parts, fake_bootstrap, skip_valkey, monkeypatch) -> None:
    """Re-applying the same settings does not reopen devices."""
    opened: list[str] = []
    real_build_dac = devices_module.build_dac

    def counting_build_dac(device_id, cfg):
        opened.append(device_id)
        return real_build_dac(device_id, cfg)

    monkeypatch.setattr(devices_module, "build_dac", counting_build_dac)
    fake_bootstrap({"devices": {"dac0": {"type": "FAKE"}}})
    worker = _make_worker(tmp_path)
    assert await worker.initialize() is True
    assert opened == ["dac0"]
    assert worker.devices.generation == 1

    worker._apply_settings(worker.dac_settings)

    assert opened == ["dac0"]
    assert worker.devices.generation == 1
    await worker.cleanup()


@pytest.mark.asyncio
async def test_cleanup_closes_open_devices(tmp_path, fake_parts, fake_bootstrap, skip_valkey) -> None:
    """cleanup() closes the live devices and drops them from the registry."""
    fake_bootstrap({"devices": {"dac0": {"type": "FAKE"}}})
    worker = _make_worker(tmp_path)
    assert await worker.initialize() is True
    device = worker.devices.get("dac0")

    await worker.cleanup()

    assert device.closed is True
    with pytest.raises(DeviceUnavailable):
        worker.devices.get("dac0")


@pytest.mark.asyncio
async def test_handlers_registered_on_their_lanes(tmp_path) -> None:
    """list, configure, and status are control-lane; read and write are data-lane."""
    worker = _make_worker(tmp_path)

    assert await TaskProcessor.initialize(worker) is True

    control = {"DacListHandler", "DacConfigureHandler", "DacStatusHandler"}
    data = {"DacReadHandler", "DacWriteHandler"}
    assert len(control | data) == 5
    assert control <= set(worker.control_task_handlers)
    assert data <= set(worker.task_handlers)
    assert not control & set(worker.task_handlers)
    assert not data & set(worker.control_task_handlers)


def test_telemetry_key_is_per_instance(tmp_path) -> None:
    """The telemetry key embeds service and instance and never matches the heartbeat glob."""
    worker = _make_worker(tmp_path)

    assert worker._telemetry_key == f"scietex:{worker.service_name}:{worker.instance_id}:telemetry"
    assert not worker._telemetry_key.endswith(":status")


def test_section_name_is_registered(tmp_path) -> None:
    """The worker registers its settings under the dac section."""
    worker = _make_worker(tmp_path)
    worker.seed_config_bootstrap()

    assert worker.current_config_settings(DAC_SECTION) == DacServiceSettings()
