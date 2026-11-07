"""Telemetry payloads the DAC worker publishes to Valkey.

The snapshot reports which configured converters are actually open and the
channel state left by the last successful write, so an operator sees a device
disappear or a write land without the worker going down. The key shape, push
interval, and TTL are the frozen contract: key
``scietex:{service}:{instance_id}:telemetry`` (the ``telemetry`` suffix never
matches the heartbeat discovery glob ``scietex:*:*:status``), pushed every 2 s
with a 6 s TTL.

Unlike the ADC snapshot, nothing here goes to the bus: channel state comes from
`DacDeviceRegistry.last_written`, the cache `dac:write` fills after a write, and
the unit is read from the live channel object, which is a local property. A
telemetry tick therefore cannot disturb an in-flight write, and a write-only
part (no readback) still reports state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import msgspec

from .devices import DeviceUnavailable

if TYPE_CHECKING:
    from .config import DacServiceSettings
    from .devices import DacDeviceRegistry

#: Push cadence and key TTL: 2 s interval, 6 s TTL so a single missed beat does
#: not expire the key.
TELEMETRY_INTERVAL: float = 2.0
TELEMETRY_TTL: int = 6


class DacChannelTelemetry(msgspec.Struct, frozen=True):
    """State one successful write left a channel in."""

    channel: int
    code: int | None
    output: float | None
    unit: str | None


class DacDeviceTelemetry(msgspec.Struct, frozen=True):
    """Open state and written channels of one configured device."""

    device_id: str
    type: str
    available: bool
    error: str | None = None
    channels: list[DacChannelTelemetry] = msgspec.field(default_factory=list)


class DacServiceTelemetry(msgspec.Struct, frozen=True):
    """Telemetry snapshot pushed by the DAC worker.

    ``configured`` is False only before the first settings resolution; the
    default struct has no devices but is a valid configuration.
    """

    service: str
    instance_id: str
    configured: bool
    devices: list[DacDeviceTelemetry]
    timestamp: datetime = msgspec.field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: int = 1


def build_telemetry(
    settings: DacServiceSettings | None,
    registry: DacDeviceRegistry,
    *,
    service: str,
    instance_id: str,
) -> DacServiceTelemetry:
    """Snapshot the effective settings and the live device registry.

    Pure with respect to the hardware: a device that is configured but failed to
    open is reported with ``available=False`` and the reason recorded by the
    registry, while an open device reports the channels the registry cached
    after a write. A device that was never written reports no channels at all
    rather than fabricated zeros, so "not written yet" stays distinguishable
    from "written zero". A worker before startup reports ``configured=False``
    with an empty device list.

    Args:
        settings: The effective settings, or ``None`` before startup.
        registry: The live device registry shared with the task handlers.
        service: Service name the snapshot is published under.
        instance_id: Worker instance the snapshot was taken from.

    Returns:
        The snapshot, ready for msgpack encoding.
    """
    if settings is None:
        return DacServiceTelemetry(service=service, instance_id=instance_id, configured=False, devices=[])

    errors = registry.errors
    written = registry.last_written()
    devices: list[DacDeviceTelemetry] = []
    for device_id in registry.ids():
        cfg = settings.devices.get(device_id)
        device_type = cfg.type if cfg is not None else ""
        try:
            device = registry.get(device_id)
        except DeviceUnavailable:
            devices.append(
                DacDeviceTelemetry(
                    device_id=device_id,
                    type=device_type,
                    available=False,
                    error=errors.get(device_id),
                )
            )
            continue
        # A local property, not a bus read: every channel of a part shares the
        # unit, so the first one is enough for the cached channel entries. A
        # channel-less part (a digital potentiometer) has no unit to report.
        unit = str(device.channels[0].unit) if device.channels else None
        channels = [
            DacChannelTelemetry(channel=channel, code=code, output=output, unit=unit)
            for channel, code, output in written.get(device_id, [])
        ]
        devices.append(
            DacDeviceTelemetry(
                device_id=device_id,
                type=device_type,
                available=True,
                channels=channels,
            )
        )
    return DacServiceTelemetry(
        service=service,
        instance_id=instance_id,
        configured=True,
        devices=devices,
    )
