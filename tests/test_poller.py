"""Tests for the per-device DAC poll loop, readings encoding, and backoff."""

import asyncio
import logging

import pytest
from conftest import FakeDAC, FakeDacChannel, RecordingStreamClient

from scietex.dac_service.config import DacChannelSettings, DacDeviceSettings, DacServiceSettings
from scietex.dac_service.devices import DacDeviceRegistry, poll_plan
from scietex.dac_service.poller import (
    MAX_BACKOFF_MS,
    DacPoller,
    backoff_ms,
    encode_code,
    encode_value,
    readings_key,
)


class FailingChannel:
    """Readable channel double whose register read always raises."""

    readable = True

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def readback(self) -> int:
        raise self.exc


class TrapReadbackChannel(FakeDacChannel):
    """Readable channel double that records any register read."""

    def __init__(self, index: int, value: int = 0, *, reference: float | None = 5.0) -> None:
        super().__init__(index, value, reference=reference)
        self.readback_calls = 0

    def readback(self) -> int:
        self.readback_calls += 1
        return super().readback()


class FakeClock:
    """Monotonic clock double advanced by the driven ``_sleep``."""

    def __init__(self) -> None:
        self.ms = 0

    def __call__(self) -> float:
        return self.ms / 1000.0

    def advance_ms(self, delay_s: float) -> None:
        self.ms += round(delay_s * 1000)


def _settings(*channels: DacChannelSettings, **service) -> DacServiceSettings:
    return DacServiceSettings(
        devices={
            "dac0": DacDeviceSettings(
                type="FAKE",
                channels=list(channels),
            )
        },
        **service,
    )


def _poller(settings: DacServiceSettings, client: RecordingStreamClient) -> tuple[DacPoller, DacDeviceRegistry]:
    registry = DacDeviceRegistry(logging.getLogger("test.dac_service.poller"))
    registry.rebuild(settings)
    poller = DacPoller(
        registry,
        logging.getLogger("test.dac_service.poller.loop"),
        service_name="DacService",
        instance_id="inst-1",
        client_provider=lambda: client,
        clock=FakeClock(),
    )
    poller.configure(settings)
    return poller, registry


def test_readings_key_is_the_frozen_contract() -> None:
    """The API builds this key from the same template; pin it literally."""
    key = readings_key("DacService", "inst-1", "dac0", 0)
    assert key == "scietex:DacService:inst-1:dac:dac0:0:readings"


def test_encode_value_keeps_none_distinguishable_from_zero() -> None:
    """``None`` is the empty field; a real zero round-trips through repr."""
    assert encode_value(None) == b""
    assert encode_value(0.0) == b"0.0"
    assert float(encode_value(1.65)) == 1.65


def test_encode_code_is_the_decimal_digits() -> None:
    """A raw code is published as digits; ``None`` stays an empty field."""
    assert encode_code(None) == b""
    assert encode_code(0) == b"0"
    assert encode_code(4095) == b"4095"


@pytest.mark.parametrize(
    ("interval_ms", "failures", "expected"),
    [
        (1000, 0, 1000),
        (1000, 1, 2000),
        (1000, 2, 4000),
        (1000, 6, MAX_BACKOFF_MS),
        (1000, 20, MAX_BACKOFF_MS),
    ],
)
def test_backoff_is_exponential_and_capped(interval_ms: int, failures: int, expected: int) -> None:
    assert backoff_ms(interval_ms, failures) == expected


@pytest.mark.asyncio
async def test_one_task_per_device_not_per_channel(fake_parts) -> None:
    """SPI is not concurrency-safe: a device is sampled by exactly one task."""
    settings = _settings(DacChannelSettings(channel=0), DacChannelSettings(channel=1))
    poller, _ = _poller(settings, RecordingStreamClient())

    await poller.start()

    assert list(poller.tasks) == ["dac0"]
    assert poller.tasks["dac0"].get_name() == "dac-poll-dac0"
    await poller.stop()
    assert poller.tasks == {}


@pytest.mark.asyncio
async def test_per_channel_cadence_publishes_to_each_stream(fake_parts) -> None:
    """A 100 ms and a 300 ms channel publish at their own cadence."""
    settings = _settings(
        DacChannelSettings(channel=0, poll_interval_ms=100),
        DacChannelSettings(channel=1, poll_interval_ms=300),
    )
    client = RecordingStreamClient()
    poller, registry = _poller(settings, client)
    registry.get("dac0").channels[0].value = 11
    registry.get("dac0").channels[1].value = 22
    clock = poller._clock
    sleeps: list[float] = []

    async def driven_sleep(delay: float) -> None:
        sleeps.append(delay)
        clock.advance_ms(delay)
        if clock.ms > 900:
            raise asyncio.CancelledError

    poller._sleep = driven_sleep

    await poller.start()
    await asyncio.gather(*poller.tasks.values(), return_exceptions=True)
    await poller.stop()

    assert client.count("scietex:DacService:inst-1:dac:dac0:0:readings") == 10
    assert client.count("scietex:DacService:inst-1:dac:dac0:1:readings") == 4
    assert sleeps == pytest.approx([0.1] * 10)
    key, fields, options = client.entries[0]
    assert key == "scietex:DacService:inst-1:dac:dac0:0:readings"
    assert fields[0][0] == b"ts"
    assert fields[1] == (b"value", b"11.0")
    assert fields[2] == (b"code", b"11")
    assert fields[3][0] == b"output"
    assert options is not None


@pytest.mark.asyncio
async def test_failed_read_backs_off_and_publishes_nothing(fake_parts) -> None:
    """A failing channel is retried exponentially and never published."""
    settings = _settings(
        DacChannelSettings(channel=0, poll_interval_ms=1000),
        failure_threshold=3,
    )
    client = RecordingStreamClient()
    poller, registry = _poller(settings, client)
    registry.get("dac0").channels = (FailingChannel(OSError("bus error")),)
    clock = poller._clock
    sleeps: list[float] = []

    async def driven_sleep(delay: float) -> None:
        sleeps.append(delay)
        clock.advance_ms(delay)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    poller._sleep = driven_sleep

    await poller.start()
    await asyncio.gather(*poller.tasks.values(), return_exceptions=True)
    await poller.stop()

    assert client.entries == []
    assert sleeps == [2.0, 4.0, 8.0]
    state = poller.states["dac0"]
    assert state.state == "error"
    assert state.consecutive_failures == 3
    assert state.last_error == "bus error"
    assert state.channels[0].value is None


@pytest.mark.asyncio
async def test_success_after_failure_publishes_and_resets(fake_parts) -> None:
    """A recovered bus read resets the counter and resumes the stream."""
    settings = _settings(DacChannelSettings(channel=0), failure_threshold=2)
    client = RecordingStreamClient()
    poller, registry = _poller(settings, client)
    registry.get("dac0").channels = (FakeDacChannel(0, value=5, reference=3.3),)
    await poller.start()
    try:
        registry.get("dac0").channels = (FailingChannel(OSError("gone")),)
        await poller._read_and_publish("dac0", 0)
        await poller._read_and_publish("dac0", 0)
        assert poller.states["dac0"].state == "error"

        registry.get("dac0").channels = (FakeDacChannel(0, value=512, reference=3.3),)
        assert await poller._read_and_publish("dac0", 0) is True
    finally:
        await poller.stop()

    assert poller.states["dac0"].state == "ok"
    assert poller.states["dac0"].consecutive_failures == 0
    assert poller.states["dac0"].channels[0].value == 512.0
    assert client.count("scietex:DacService:inst-1:dac:dac0:0:readings") == 1


@pytest.mark.asyncio
async def test_no_task_for_poll_false_device(fake_parts) -> None:
    """A device excluded from the plan keeps a stopped telemetry entry."""
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE", poll=False)})
    poller, _ = _poller(settings, RecordingStreamClient())

    await poller.start()

    assert poller.tasks == {}
    assert poller.states["dac0"].state == "stopped"
    await poller.stop()


@pytest.mark.asyncio
async def test_write_only_part_publishes_the_cached_code_with_empty_output(fake_parts) -> None:
    """A write-only part reports its cached write; no reference means no output."""
    settings = _settings(DacChannelSettings(channel=0))
    client = RecordingStreamClient()
    poller, registry = _poller(settings, client)
    registry.get("dac0").channels = (FakeDacChannel(0, reference=None, readable=False),)
    registry.record_write("dac0", 0, 321, None)

    assert await poller._read_and_publish("dac0", 0) is True

    key, fields, _ = client.entries[0]
    assert key == "scietex:DacService:inst-1:dac:dac0:0:readings"
    assert fields[1] == (b"value", b"321.0")
    assert fields[2] == (b"code", b"321")
    assert fields[3] == (b"output", b"")
    assert poller.states["dac0"].channels[0].output is None


@pytest.mark.asyncio
async def test_readback_false_never_touches_the_bus(fake_parts) -> None:
    """A ``readback=False`` channel answers from the cache and never reads the register."""
    settings = _settings(DacChannelSettings(channel=0, readback=False))
    client = RecordingStreamClient()
    poller, registry = _poller(settings, client)
    channel = TrapReadbackChannel(0, reference=5.0)
    registry.get("dac0").channels = (channel,)
    registry.record_write("dac0", 0, 100, None)

    assert await poller._read_and_publish("dac0", 0, False) is True

    assert channel.readback_calls == 0
    assert client.entries[0][1][2] == (b"code", b"100")


def test_plan_is_empty_without_channel_rows(fake_parts) -> None:
    """The loop consumes channel rows; an unconfigured device yields no plan."""
    settings = _settings()
    registry = DacDeviceRegistry(logging.getLogger("test.dac_service.poller.plan"))
    registry.rebuild(settings)
    plan = poll_plan(registry)

    assert plan == {}


def test_registry_generates_fake_geometry() -> None:
    """The fixture device has the surface the plan expands over."""
    assert len(FakeDAC().channels) == 2
