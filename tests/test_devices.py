"""Tests for the DAC device builder and registry."""

import inspect
from dataclasses import replace

import pytest

from scietex.dac_service import devices
from scietex.dac_service.config import DacDeviceSettings, DacServiceSettings
from scietex.dac_service.devices import (
    DAC_PARTS,
    ChannelNotFound,
    DacDeviceRegistry,
    DeviceNotFound,
    DeviceOpenError,
    DeviceUnavailable,
    InvalidParameter,
    PartSpec,
    UnknownDeviceType,
    UnknownParameter,
    build_dac,
)
from scietex.hal import dac as hal

#: The 23 part names the registry must cover.
EXPECTED_PARTS = (
    "MCP4231",
    "MCP4241",
    "MCP4251",
    "MCP4261",
    "MCP4802",
    "MCP4812",
    "MCP4822",
    "MCP4902",
    "MCP4912",
    "MCP4922",
    "DAC8531",
    "DAC8551",
    "DAC8552",
    "DAC121S101",
    "AD5302",
    "AD5312",
    "AD5322",
    "DAC8512",
    "AD7390",
    "AD7391",
    "AD5410",
    "AD5420",
    "AD420",
)


def _recording_fake(cls: type) -> type:
    """A stand-in with the part's signature, so the builder is still verified."""

    class Recording:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.init_args = args
            self.init_kwargs = kwargs

    Recording.__name__ = f"Recording{cls.__name__}"
    setattr(Recording, "__signature__", inspect.signature(cls))
    return Recording


def _minimal_params(spec: PartSpec) -> dict[str, object]:
    """The pin setting each ``required`` part needs to be constructible."""
    return {name: 5 for name in spec.required}


def test_registry_covers_every_exported_part() -> None:
    """Every expected part name resolves to its real HAL class, and only those."""
    assert len(DAC_PARTS) == 23
    assert set(DAC_PARTS) == set(EXPECTED_PARTS)
    for name, spec in DAC_PARTS.items():
        assert spec.cls is getattr(hal, name)


@pytest.mark.parametrize("part", sorted(EXPECTED_PARTS))
def test_builds_every_registered_part(part: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The builder resolves and constructs every one of the 23 parts."""
    spec = DAC_PARTS[part]
    fake = _recording_fake(spec.cls)
    monkeypatch.setitem(devices.DAC_PARTS, part, replace(spec, cls=fake))

    device = build_dac("dac0", DacDeviceSettings(type=part, **_minimal_params(spec)))

    assert isinstance(device, fake)


def test_ad420_receives_range_positionally(monkeypatch: pytest.MonkeyPatch) -> None:
    """AD420 takes ``range`` before the keyword-only block, so the builder passes it positionally."""
    spec = DAC_PARTS["AD420"]
    fake = _recording_fake(spec.cls)
    monkeypatch.setitem(devices.DAC_PARTS, "AD420", replace(spec, cls=fake))

    device = build_dac("dac0", DacDeviceSettings(type="AD420", range="0-5V", latch_pin=17))

    assert device.init_args == ("0-5V",)
    assert device.init_kwargs == {"latch_pin": 17}


@pytest.mark.parametrize("part", ["AD5410", "AD5420"])
def test_current_output_parts_reject_reference(part: str) -> None:
    """AD54x0 parts pick a range in configure(); a reference is a config error."""
    with pytest.raises(InvalidParameter) as excinfo:
        build_dac("dac0", DacDeviceSettings(type=part, reference=5.0))

    assert "reference" in str(excinfo.value)


@pytest.mark.parametrize("part", ["DAC8512", "AD7390", "AD7391"])
def test_strobed_parts_require_load_pin(part: str) -> None:
    """A load-strobed part is unusable without its load line."""
    with pytest.raises(InvalidParameter) as excinfo:
        build_dac("dac0", DacDeviceSettings(type=part))

    assert "load_pin" in str(excinfo.value)


def test_ad420_requires_latch_pin() -> None:
    """AD420 cannot latch a frame without its latch line."""
    with pytest.raises(InvalidParameter) as excinfo:
        build_dac("dac0", DacDeviceSettings(type="AD420"))

    assert "latch_pin" in str(excinfo.value)


def test_unknown_type_is_rejected_with_supported_list() -> None:
    """An unsupported part names the known ones in the error."""
    with pytest.raises(UnknownDeviceType) as excinfo:
        build_dac("dac0", DacDeviceSettings(type="MCP4725"))

    assert "MCP4822" in str(excinfo.value)
    assert "AD420" in str(excinfo.value)


def test_builder_rejects_parameter_the_part_lacks() -> None:
    """A config field the part's signature lacks is UnknownParameter, not a TypeError."""
    with pytest.raises(UnknownParameter):
        build_dac("dac0", DacDeviceSettings(type="MCP4802", latch_pin=17))


def test_builder_forwards_only_set_fields(fake_parts: None) -> None:
    """A settings field left at None is not forwarded to the constructor."""
    device = build_dac("dac0", DacDeviceSettings(type="FAKE", reference=4.096, port=0, label="setpoint"))

    assert device.init_kwargs == {"port": 0, "label": "setpoint"}
    assert device.reference == 4.096


def test_builder_wraps_oserror_as_open_error(fake_parts: None) -> None:
    """A constructor failure at the bus level becomes DeviceOpenError."""
    with pytest.raises(DeviceOpenError) as excinfo:
        build_dac("broken", DacDeviceSettings(type="BROKEN"))

    assert excinfo.value.device_id == "broken"
    assert isinstance(excinfo.value.cause, OSError)


def test_registry_opens_configured_devices(registry: DacDeviceRegistry, fake_parts: None) -> None:
    """A successful rebuild exposes sorted ids and a live object."""
    registry.rebuild(DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")}))

    assert registry.ids() == ["dac0"]
    assert registry.errors == {}
    assert registry.generation == 1
    assert registry.settings == DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")})
    assert registry.get("dac0").reference == 5.0


def test_registry_records_bad_device_and_keeps_others(registry: DacDeviceRegistry, fake_parts: None) -> None:
    """One bad entry is recorded while the rest still open."""
    registry.rebuild(
        DacServiceSettings(
            devices={
                "bad": DacDeviceSettings(type="MCP4725"),
                "good": DacDeviceSettings(type="FAKE"),
            }
        )
    )

    assert registry.ids() == ["bad", "good"]
    assert list(registry.errors) == ["bad"]
    assert registry.get("good").closed is False
    with pytest.raises(DeviceUnavailable):
        registry.get("bad")


def test_registry_records_open_failure(registry: DacDeviceRegistry, fake_parts: None) -> None:
    """A bus-level open failure is recorded, not raised."""
    registry.rebuild(DacServiceSettings(devices={"broken": DacDeviceSettings(type="BROKEN")}))

    assert registry.ids() == ["broken"]
    assert "Failed to open device" in registry.errors["broken"]
    with pytest.raises(DeviceUnavailable) as excinfo:
        registry.get("broken")
    assert "SPI bus is gone" in str(excinfo.value)
    assert excinfo.value.device_id == "broken"
    assert excinfo.value.reason == registry.errors["broken"]


def test_close_closes_all_and_is_idempotent(registry: DacDeviceRegistry, fake_parts: None) -> None:
    """close() closes every device, clears the set, and tolerates a second call."""
    registry.rebuild(DacServiceSettings(devices={"a": DacDeviceSettings(type="FAKE"), "b": DacDeviceSettings(type="FAKE")}))
    first = registry.get("a")
    second = registry.get("b")

    registry.close()

    assert first.closed is True
    assert second.closed is True
    # The ids stay configured, so a closed registry reports them unavailable
    # rather than unknown.
    with pytest.raises(DeviceUnavailable):
        registry.get("a")
    registry.close()


def test_get_unknown_id_raises_device_not_found(registry: DacDeviceRegistry) -> None:
    """An id that was never configured is a DeviceNotFound, not a raw KeyError."""
    with pytest.raises(DeviceNotFound) as excinfo:
        registry.get("nope")

    assert "nope" in str(excinfo.value)


def test_get_configured_id_that_failed_is_unavailable_not_not_found(registry: DacDeviceRegistry, fake_parts: None) -> None:
    """The taxonomy separates a wrong id from a configured device that did not open."""
    registry.rebuild(DacServiceSettings(devices={"broken": DacDeviceSettings(type="BROKEN")}))

    with pytest.raises(DeviceUnavailable) as excinfo:
        registry.get("broken")
    assert "Failed to open device 'broken'" in str(excinfo.value)

    with pytest.raises(DeviceNotFound) as notfound:
        registry.get("missing")
    assert "is not configured" in str(notfound.value)


def test_channel_out_of_range_raises_channel_not_found(registry: DacDeviceRegistry, fake_parts: None) -> None:
    """Bounds are checked against the live device before the HAL accessor."""
    registry.rebuild(DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")}))

    assert registry.channel("dac0", 0).index == 0
    with pytest.raises(ChannelNotFound):
        registry.channel("dac0", 2)
    with pytest.raises(DeviceNotFound):
        registry.channel("missing", 0)


def test_channels_returns_live_tuple(registry: DacDeviceRegistry, fake_parts: None) -> None:
    """channels() hands back the device's tuple in index order."""
    registry.rebuild(DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")}))

    assert [channel.index for channel in registry.channels("dac0")] == [0, 1]
    with pytest.raises(DeviceNotFound):
        registry.channels("missing")


def test_rebuild_closes_old_generation_and_bumps_generation(registry: DacDeviceRegistry, fake_parts: None) -> None:
    """Changed settings close the old objects and install a new generation."""
    registry.rebuild(DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")}))
    old = registry.get("dac0")

    registry.rebuild(DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE", reference=3.3)}))

    assert old.closed is True
    assert registry.generation == 2
    assert registry.get("dac0") is not old
    assert registry.get("dac0").reference == 3.3


def test_identical_settings_skip_rebuild(
    registry: DacDeviceRegistry, fake_parts: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same effective settings with a live device do not churn the hardware."""
    calls: list[str] = []
    original = devices.build_dac

    def recording_build(device_id: str, cfg: DacDeviceSettings) -> hal.DAC:
        calls.append(device_id)
        return original(device_id, cfg)

    monkeypatch.setattr(devices, "build_dac", recording_build)
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")})

    registry.rebuild(settings)
    registry.rebuild(DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")}))

    assert calls == ["dac0"]
    assert registry.generation == 1


def test_errors_cleared_on_successful_rebuild(registry: DacDeviceRegistry, fake_parts: None) -> None:
    """A rebuild that no longer contains a failing device drops its error."""
    registry.rebuild(
        DacServiceSettings(devices={"bad": DacDeviceSettings(type="MCP4725"), "good": DacDeviceSettings(type="FAKE")})
    )
    assert "bad" in registry.errors

    registry.rebuild(DacServiceSettings(devices={"good": DacDeviceSettings(type="FAKE")}))

    assert registry.errors == {}
    assert registry.ids() == ["good"]


def test_registry_logs_close_failure(registry: DacDeviceRegistry, fake_parts: None) -> None:
    """A failing close() is logged and does not prevent the rebuild."""
    registry.rebuild(DacServiceSettings(devices={"dac0": DacDeviceSettings(type="STUBBORN")}))
    failing = registry.get("dac0")

    registry.rebuild(DacServiceSettings(devices={"dac0": DacDeviceSettings(type="STUBBORN", reference=3.3)}))

    assert registry.generation == 2
    assert registry.get("dac0") is not failing
    assert registry.get("dac0").reference == 3.3


def test_empty_registry_state(registry: DacDeviceRegistry) -> None:
    """Before the first rebuild there are no ids, no errors, and no settings."""
    assert registry.ids() == []
    assert registry.errors == {}
    assert registry.generation == 0
    assert registry.settings is None
    assert registry.last_written() == {}


def test_record_write_caches_the_latest_state_in_index_order(registry: DacDeviceRegistry, fake_parts: None) -> None:
    """The cache keeps the last write per channel and orders devices and channels."""
    registry.rebuild(DacServiceSettings(devices={"b": DacDeviceSettings(type="FAKE"), "a": DacDeviceSettings(type="FAKE")}))

    registry.record_write("b", 1, 11, None)
    registry.record_write("a", 0, 5, 0.5)
    registry.record_write("b", 0, 10, None)
    registry.record_write("b", 1, 12, None)

    assert registry.last_written() == {
        "a": [(0, 5, 0.5)],
        "b": [(0, 10, None), (1, 12, None)],
    }


def test_record_write_ignores_an_unknown_device(registry: DacDeviceRegistry, fake_parts: None) -> None:
    """A write that raced a rebuild must not fail after the hardware accepted it."""
    registry.rebuild(DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")}))

    registry.record_write("ghost", 0, 7, None)

    assert registry.last_written() == {}


def test_last_written_returns_a_deep_copy(registry: DacDeviceRegistry, fake_parts: None) -> None:
    """Mutating the returned mapping must not reach the registry's cache."""
    registry.rebuild(DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")}))
    registry.record_write("dac0", 0, 10, None)

    snapshot = registry.last_written()
    snapshot["dac0"].append((1, 20, None))
    snapshot["other"] = []

    assert registry.last_written() == {"dac0": [(0, 10, None)]}


def test_rebuild_clears_the_cache_of_replaced_devices(registry: DacDeviceRegistry, fake_parts: None) -> None:
    """A cached code belongs to the old live object and must not outlive it."""
    registry.rebuild(DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")}))
    registry.record_write("dac0", 0, 10, None)

    registry.rebuild(DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE", reference=3.3)}))

    assert registry.last_written() == {}


def test_close_clears_the_cache(registry: DacDeviceRegistry, fake_parts: None) -> None:
    registry.rebuild(DacServiceSettings(devices={"dac0": DacDeviceSettings(type="FAKE")}))
    registry.record_write("dac0", 0, 10, None)

    registry.close()

    assert registry.last_written() == {}
