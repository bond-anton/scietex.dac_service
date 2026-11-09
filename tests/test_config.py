"""Tests for the DAC service configuration models and YAML loader."""

from pathlib import Path

import msgspec
import pytest

from scietex.dac_service.config import (
    DAC_CONFIG_FILE,
    DAC_CONFIG_SUBDIR,
    DAC_SETTINGS_DEFAULTS,
    DacChannelSettings,
    DacDeviceSettings,
    DacServiceSettings,
    read_dac_config,
)
from scietex.service.config_merge import resolve_section


def _config_path(conf_dir: Path) -> Path:
    """The namespaced path the loader reads and writes."""
    return conf_dir / DAC_CONFIG_SUBDIR / DAC_CONFIG_FILE


def test_settings_defaults_constant_is_l0_base() -> None:
    """The exported L0 base is a concrete default struct instance."""
    assert isinstance(DAC_SETTINGS_DEFAULTS, DacServiceSettings)
    assert DAC_SETTINGS_DEFAULTS == DacServiceSettings()
    assert DAC_SETTINGS_DEFAULTS.devices == {}


def test_read_dac_config_writes_defaults_when_missing(tmp_path: Path) -> None:
    """A missing file is created under the dac/ subdir with the default patch."""
    patch = read_dac_config(tmp_path)

    assert patch == msgspec.to_builtins(DacServiceSettings())
    path = _config_path(tmp_path)
    assert path.is_file()
    assert msgspec.yaml.decode(path.read_bytes(), type=dict) == patch


def test_read_dac_config_returns_dict_not_struct(tmp_path: Path) -> None:
    """The loader returns the L1 patch dict, never a struct."""
    patch = read_dac_config(tmp_path)

    assert type(patch) is dict
    assert not isinstance(patch, DacServiceSettings)


def test_read_dac_config_none_dir_raises() -> None:
    """A None conf_dir is rejected with RuntimeError."""
    with pytest.raises(RuntimeError):
        read_dac_config(None)


def test_read_dac_config_missing_without_create_raises(tmp_path: Path) -> None:
    """A missing file with create_default=False raises and writes nothing."""
    with pytest.raises(RuntimeError):
        read_dac_config(tmp_path, create_default=False)

    assert not _config_path(tmp_path).exists()


def test_read_dac_config_malformed_raises_and_leaves_file(tmp_path: Path) -> None:
    """An unparseable file raises RuntimeError and is left untouched."""
    path = _config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("unterminated: [flow\n")

    with pytest.raises(RuntimeError):
        read_dac_config(tmp_path)

    assert path.read_text() == "unterminated: [flow\n"


def test_read_dac_config_partial_file_returns_only_present_keys(tmp_path: Path) -> None:
    """A partial file yields a partial patch — absent keys inherit the layer below."""
    path = _config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("devices:\n  dac0:\n    type: MCP4822\n")

    assert read_dac_config(tmp_path) == {"devices": {"dac0": {"type": "MCP4822"}}}


def test_string_device_ids_survive_round_trip() -> None:
    """Device ids stay plain strings through YAML, builtins, and decode."""
    settings = DacServiceSettings(devices={"dac0": DacDeviceSettings(type="MCP4822", select_pin="CE0")})

    encoded = msgspec.yaml.encode(settings)
    decoded = msgspec.yaml.decode(encoded, type=DacServiceSettings)

    assert b"dac0:" in encoded
    assert list(msgspec.to_builtins(settings)["devices"]) == ["dac0"]
    assert list(decoded.devices) == ["dac0"]
    assert all(isinstance(key, str) for key in decoded.devices)


def test_four_layer_precedence() -> None:
    """L3 beats L2 beats L1 beats L0, and absent keys inherit the layer below."""
    l1 = {"devices": {"dac0": {"type": "MCP4822", "reference": 2.048}}}
    l2 = {"devices": {"dac0": {"reference": 4.096, "label": "setpoint"}}}
    l3 = {"devices": {"dac0": {"label": "from-remote"}}}

    settings = resolve_section(DacServiceSettings, [l3, l2, l1], defaults=DAC_SETTINGS_DEFAULTS)

    assert settings.devices["dac0"].type == "MCP4822"
    assert settings.devices["dac0"].reference == 4.096
    assert settings.devices["dac0"].label == "from-remote"
    assert settings.devices["dac0"].load_pin is None


def test_null_resets_field_to_l0() -> None:
    """A null in a higher layer clears the field so the L0 value resurfaces."""
    l1 = {"devices": {"dac0": {"type": "MCP4822", "reference": 3.3}}}
    l3 = {"devices": {"dac0": {"reference": None}}}

    settings = resolve_section(DacServiceSettings, [l3, l1], defaults=DAC_SETTINGS_DEFAULTS)

    assert settings.devices["dac0"].reference is None


def test_forbid_unknown_fields_rejects_vref() -> None:
    """The struct field is ``reference``; ``vref`` is rejected, not silently ignored."""
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({"type": "MCP4822", "vref": 3.3}, DacDeviceSettings)


def test_devices_must_be_a_mapping() -> None:
    """A non-mapping ``devices`` fails validation instead of coercing."""
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({"devices": "dac0"}, DacServiceSettings)


def test_type_is_required_per_device() -> None:
    """A device entry without ``type`` cannot be validated."""
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({"devices": {"dac0": {"port": 0}}}, DacServiceSettings)


def test_port_is_an_integer_bus_number() -> None:
    """``port`` is the SPI bus number, so a device path is rejected."""
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({"type": "MCP4822", "port": "/dev/spidev0.0"}, DacDeviceSettings)

    settings = msgspec.convert({"type": "MCP4822", "port": 0, "device": 0}, DacDeviceSettings)

    assert settings.port == 0
    assert settings.device == 0


def test_part_specific_fields_round_trip() -> None:
    """Every part-specific field survives struct construction, builtins, and YAML."""
    raw = {
        "type": "AD420",
        "range": "4-20mA",
        "load_pin": 27,
        "latch_pin": 22,
        "r_ab": 10000.0,
        "grounded": "A",
    }

    settings = msgspec.convert(raw, DacDeviceSettings)
    builtins = msgspec.to_builtins(settings)

    assert {key: builtins[key] for key in raw} == raw
    assert msgspec.yaml.decode(msgspec.yaml.encode(settings), type=DacDeviceSettings) == settings


def test_strobe_is_not_config_representable() -> None:
    """``strobe`` is a driver object, so a config entry carrying it is rejected."""
    with pytest.raises(msgspec.ValidationError):
        msgspec.yaml.decode(b"type: DAC8512\nstrobe: 17\n", type=DacDeviceSettings)


def test_reference_is_scalar_not_a_sequence() -> None:
    """Per-channel ``reference`` sequences are out of scope: only a scalar is valid."""
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({"type": "MCP4822", "reference": [2.048, 4.096]}, DacDeviceSettings)


def test_channel_row_decodes_from_a_yaml_shaped_mapping() -> None:
    """A channel row is the polling description, so all of it round-trips."""
    settings = msgspec.convert(
        {"type": "MCP4822", "channels": [{"channel": 1, "poll_interval_ms": 250, "label": "setpoint"}]},
        DacDeviceSettings,
    )

    assert settings.channels == [DacChannelSettings(channel=1, poll_interval_ms=250, label="setpoint")]


def test_channel_row_defaults_keep_a_bare_row_valid() -> None:
    """Only the index is required; the rest falls back to the polling defaults."""
    [row] = msgspec.convert({"type": "MCP4822", "channels": [{"channel": 0}]}, DacDeviceSettings).channels

    assert (row.poll_interval_ms, row.enabled, row.label, row.readback) == (2000, True, None, True)


def test_channel_row_rejects_an_unknown_field() -> None:
    """A misspelt channel knob fails validation instead of being ignored."""
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({"type": "MCP4822", "channels": [{"channel": 0, "interval": 100}]}, DacDeviceSettings)


def test_device_polling_fields_default_so_an_old_entry_still_decodes() -> None:
    """A ``dac.yml`` written before polling existed parses with the poll defaults."""
    settings = msgspec.convert({"type": "MCP4822"}, DacDeviceSettings)

    assert settings.poll is True
    assert settings.channels == []


def test_service_polling_fields_default() -> None:
    """The service-wide polling knobs have concrete defaults."""
    settings = DacServiceSettings()

    assert settings.poll_enabled is True
    assert settings.failure_threshold == 3
    assert settings.stream_maxlen == 10000
