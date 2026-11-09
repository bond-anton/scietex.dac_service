"""Minimal worker and Valkey-client doubles for telemetry tests.

`build_telemetry` takes the settings, the registry, and the poll snapshots
separately, so a real `DacWorker` is unnecessary for payload-shape tests. The
client doubles record or fail the single ``set`` call the Telemetry manager
makes.
"""

import logging

from scietex.dac_service.devices import DacDeviceRegistry
from scietex.dac_service.poller import DacPoller


class FakeDacWorker:
    """Stand-in exposing exactly the identity and state a snapshot is built from."""

    def __init__(
        self,
        *,
        settings=None,
        registry: DacDeviceRegistry | None = None,
        poller: DacPoller | None = None,
        service_name: str = "dac-test",
        instance_id: str = "instance-1",
    ) -> None:
        self.service_name = service_name
        self.instance_id = instance_id
        self.dac_settings = settings
        self.devices = (
            registry if registry is not None else DacDeviceRegistry(logging.getLogger("test.dac_service.fake_worker"))
        )
        self.poller = (
            poller
            if poller is not None
            else DacPoller(
                self.devices,
                logging.getLogger("test.dac_service.fake_worker.poller"),
                service_name=service_name,
                instance_id=instance_id,
            )
        )


class RecordingClient:
    """Valkey client double that records each ``set`` call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, object]] = []

    async def set(self, key, value=None, expiry=None):
        self.calls.append((key, value, expiry))


class FailingClient:
    """Valkey client double whose every write raises a configured error."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    async def set(self, key, value=None, expiry=None):
        self.calls += 1
        raise self.exc
