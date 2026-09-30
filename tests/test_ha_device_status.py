"""Status sensor maps the raw #16.#1 deviceStatus enum (see sensor.DEVICE_STATUS_STATES).
Same HA-guard and stand-in coordinator style as test_ha_rain_delay.py."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from orbit_bhyve.devices.base import DeviceState  # noqa: E402
from orbit_bhyve.sensor import BHyveDeviceStatusSensor  # noqa: E402


def _sensor(device_status):
    device = SimpleNamespace(
        name="Corner Fence",
        unique_id="fence",
        cloud_id="cloud-fence",
        hardware="HT25A-0001",
        firmware="0098",
        mac="44:67:55:1D:67:DB",
        state=DeviceState(device_status=device_status),
    )
    return BHyveDeviceStatusSensor(SimpleNamespace(device=device, data=None))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, "idle"),  # deviceOff — healthy units idle here (controller out of autoMode)
        (1, "idle"),
        (2, "low_battery"),
        (3, "rain_delay"),
        (4, "watering"),
        (5, "mesh_offline"),
        (9, None),  # unmapped -> unknown
        (None, None),  # no status read yet
    ],
)
def test_status_sensor_maps_device_status(raw, expected):
    sensor = _sensor(raw)
    assert sensor.native_value == expected
    assert sensor.extra_state_attributes == {"raw_device_status": raw}
    if expected is not None:
        assert expected in sensor.options
