"""Test doubles for the SPI DAC HAL plus shared registry fixtures."""

import logging

import pytest

from scietex.dac_service import devices
from scietex.dac_service.devices import DacDeviceRegistry, PartSpec
from scietex.hal.dac import AD54x0Control, AD54x0Status, DACCodeError, DACReadbackError

#: Distinguishes "not forwarded by the builder" from an explicit ``None`` in the
#: fake constructors, so a test can assert the builder forwards only set fields.
_UNSET = object()


class FakeDacChannel:
    """Stand-in for `hal.DACChannel` returning a fixed code."""

    def __init__(
        self,
        index: int,
        value: int = 0,
        *,
        unit: str = "V",
        max_code: int = 4095,
        reference: float | None = 5.0,
        grounded: str = "B",
        readable: bool = True,
    ) -> None:
        self.index = index
        self._value = value
        self._unit = unit
        self._max_code = max_code
        self._reference = reference
        self.grounded = grounded
        self.readable = readable
        #: Output floor code 0 maps to. Every fake part is a plain voltage or
        #: current output, so the span starts at zero.
        self.lower_bound = 0.0
        #: When set, the next code write raises it, standing in for a channel
        #: whose output is disabled or whose bus rejects the frame.
        self.write_error: Exception | None = None

    @property
    def value(self) -> int:
        return self._value

    @value.setter
    def value(self, code: int) -> None:
        if self.write_error is not None:
            raise self.write_error
        if not 0 <= code <= self._max_code:
            raise DACCodeError(f"Expected a code within 0..{self._max_code}, got {code}")
        self._value = code

    @property
    def unit(self) -> str:
        return self._unit

    @property
    def reference(self) -> float | None:
        return self._reference

    @property
    def max_code(self) -> int:
        return self._max_code

    @property
    def voltage(self) -> float | None:
        if self._reference is None:
            return None
        return self._value * self._reference / self._max_code

    @property
    def current(self) -> float | None:
        if self._reference is None:
            return None
        return self._value * self._reference / self._max_code

    def readback(self) -> int:
        if not self.readable:
            raise DACReadbackError(f"Channel {self.index} belongs to a write-only device")
        return self._value

    @property
    def output(self) -> float | None:
        return self.voltage if self._unit == "V" else self.current

    @output.setter
    def output(self, output: float | None) -> None:
        if output is None or self._reference is None:
            return
        self.value = int(round(output / self._reference * self._max_code))


class FakeDAC:
    """Stand-in for `hal.DAC` with the same geometry and accessor surface."""

    def __init__(
        self,
        channels: int = 2,
        readable: bool = True,
        reference: float | None = 5.0,
        unit: str = "V",
        *,
        port: object = _UNSET,
        device: object = _UNSET,
        clock_pin: object = _UNSET,
        mosi_pin: object = _UNSET,
        miso_pin: object = _UNSET,
        select_pin: object = _UNSET,
        label: object = _UNSET,
        log_level: object = _UNSET,
        r_ab: object = _UNSET,
        grounded: object = _UNSET,
        range: object = _UNSET,
        strobe: object = _UNSET,
        load_pin: object = _UNSET,
        latch_pin: object = _UNSET,
    ) -> None:
        if reference is not None and reference <= 0:
            raise ValueError(f"Expected a positive reference, got {reference!r}")
        self.reference = reference
        self.readable = readable
        self.unit = unit
        self.resolution = 12
        self.max_code = 4095
        self.channels = self._make_channels(channels, unit, reference, readable)
        self.transport = None
        self.closed = False
        self.init_kwargs = {
            name: value
            for name, value in {
                "port": port,
                "device": device,
                "clock_pin": clock_pin,
                "mosi_pin": mosi_pin,
                "miso_pin": miso_pin,
                "select_pin": select_pin,
                "label": label,
                "log_level": log_level,
                "r_ab": r_ab,
                "grounded": grounded,
                "range": range,
                "strobe": strobe,
                "load_pin": load_pin,
                "latch_pin": latch_pin,
            }.items()
            if value is not _UNSET
        }

    def _make_channels(self, count: int, unit: str, reference: float | None, readable: bool) -> tuple[FakeDacChannel, ...]:
        return tuple(FakeDacChannel(index, unit=unit, reference=reference, readable=readable) for index in range(count))

    def read_all(self) -> tuple[int, ...]:
        return tuple(channel.value for channel in self.channels)

    def write_all(self, values: list[int]) -> tuple[int, ...]:
        if len(values) != len(self.channels):
            raise ValueError(f"Expected {len(self.channels)} codes, got {len(values)}")
        for channel, code in zip(self.channels, values, strict=True):
            channel.value = code
        return tuple(values)

    def channel(self, index: int) -> FakeDacChannel:
        if not 0 <= index < len(self.channels):
            raise IndexError(f"Channel {index} does not exist")
        return self.channels[index]

    def close(self) -> None:
        self.closed = True


class BrokenDAC(FakeDAC):
    """Fake device whose constructor fails at the bus level."""

    def __init__(self, **kwargs: object) -> None:
        raise OSError("SPI bus is gone")


class NoPinsDAC(FakeDAC):
    """Fake device that accepts only the shared keyword arguments."""

    def __init__(self, *, reference: object = _UNSET, label: object = _UNSET) -> None:
        kwargs = {}
        if reference is not _UNSET:
            kwargs["reference"] = reference
        if label is not _UNSET:
            kwargs["label"] = label
        super().__init__(**kwargs)


class WriteOnlyDAC(FakeDAC):
    """Fake converter that cannot report its registers back."""

    def __init__(self, *, reference: object = _UNSET, label: object = _UNSET) -> None:
        kwargs = {}
        if reference is not _UNSET:
            kwargs["reference"] = reference
        if label is not _UNSET:
            kwargs["label"] = label
        super().__init__(readable=False, **kwargs)


#: Range name -> control-register code and upper full scale, mirroring the
#: AD54x0 HAL table so the double answers `read_control()` like the part.
_AD54X0_RANGES: dict[str, tuple[int, float]] = {
    "4-20mA": (0b101, 0.020),
    "0-20mA": (0b110, 0.020),
    "0-24mA": (0b111, 0.024),
}

#: Defaults `configure()` applies to fields the request omits, as the HAL does.
_AD54X0_CONFIGURE_DEFAULTS: dict[str, object] = {
    "range": "4-20mA",
    "slew": False,
    "sr_clock": 0,
    "sr_step": 0,
    "rext": False,
    "dcen": False,
}


class FakeAD54x0(FakeDAC):
    """Stand-in for the AD54x0 family: configurable and register-readable.

    Every control-word field is a plain attribute, so a test can pre-set the
    state the part powers up in. ``configure`` records exactly the keywords it
    received in ``configure_calls``, then applies the HAL defaults to the rest
    as the real `configure()` does; ``events`` keeps the call order across
    configure/enable/disable.
    """

    def __init__(self) -> None:
        super().__init__(channels=1, reference=None, unit="A")
        self.range = "4-20mA"
        self.slew = False
        self.sr_clock = 0
        self.sr_step = 0
        self.rext = False
        self.dcen = False
        self.outen = False
        self.status_calls = 0
        self.control_calls = 0
        self.configure_calls: list[dict[str, object]] = []
        self.events: list[str] = []

    def configure(
        self,
        *,
        range: object = _UNSET,
        slew: object = _UNSET,
        sr_clock: object = _UNSET,
        sr_step: object = _UNSET,
        rext: object = _UNSET,
        dcen: object = _UNSET,
        force: object = _UNSET,
    ) -> None:
        self.events.append("configure")
        applied = {
            name: value
            for name, value in {
                "range": range,
                "slew": slew,
                "sr_clock": sr_clock,
                "sr_step": sr_step,
                "rext": rext,
                "dcen": dcen,
            }.items()
            if value is not _UNSET
        }
        self.configure_calls.append(applied)
        for name, default in _AD54X0_CONFIGURE_DEFAULTS.items():
            setattr(self, name, applied.get(name, default))
        self.reference = _AD54X0_RANGES[self.range][1]

    def enable_output(self, *, force: bool = False) -> None:
        self.events.append("enable_output")
        self.outen = True

    def disable_output(self, *, force: bool = False) -> None:
        self.events.append("disable_output")
        self.outen = False

    def read_status(self) -> AD54x0Status:
        self.status_calls += 1
        return AD54x0Status(iout_fault=False, slew_active=False, overtemp=False)

    def read_control(self) -> AD54x0Control:
        self.control_calls += 1
        range_code = _AD54X0_RANGES.get(self.range, (0b000, 0.0))[0]
        return AD54x0Control(
            range_code=range_code,
            rext=self.rext,
            outen=self.outen,
            sr_clock=self.sr_clock,
            sr_step=self.sr_step,
            sr_enabled=self.slew,
            dcen=self.dcen,
            raw=0,
        )


class StubbornCloseDAC(FakeDAC):
    """Fake device whose close() fails, exercised by the rebuild path."""

    def close(self) -> None:
        raise OSError("already gone")


class RecordingStreamClient:
    """Valkey client double that records every ``xadd``."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, list[tuple[bytes, bytes]], object]] = []

    async def xadd(self, key, fields, options=None):
        self.entries.append((key, fields, options))

    def keys(self) -> list[str]:
        return [key for key, _, _ in self.entries]

    def count(self, key: str) -> int:
        return sum(1 for entry_key in self.keys() if entry_key == key)


@pytest.fixture
def fake_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the part table with the fake classes (restored by monkeypatch)."""
    monkeypatch.setattr(
        devices,
        "DAC_PARTS",
        {
            "FAKE": PartSpec(FakeDAC),
            "BROKEN": PartSpec(BrokenDAC),
            "NOPINS": PartSpec(NoPinsDAC),
            "STUBBORN": PartSpec(StubbornCloseDAC),
            "WRITEONLY": PartSpec(WriteOnlyDAC),
            "AD54X0": PartSpec(FakeAD54x0),
        },
    )


@pytest.fixture
def registry() -> DacDeviceRegistry:
    return DacDeviceRegistry(logging.getLogger("test.dac_service.devices"))
