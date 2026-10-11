"""Protobuf-family additions from the app-schema gap report (2026-10-11).

Pins the exact bytes of each new TX frame and the RX decode of each new field,
plus the device-method behaviour around them. Frames are built from the app's
schema and serializers (static analysis); none of the new writes is
hardware-verified, so these tests pin our encoding, not device behaviour.
No hardware or Home Assistant required.
"""
from __future__ import annotations

import asyncio
import struct

import pytest

from orbit_bhyve.devices import BHyveHT25ADevice, BHyveHT25G2Device, BHyveHT34ADevice
from orbit_bhyve.devices import protobuf as pb
from orbit_bhyve.devices import status as rx
from orbit_bhyve.devices.base import DeviceState, ProgramSummary


def _f32(field: int, value: float) -> bytes:
    """A float32 field (wire 5), which the varint/bytes builders can't emit."""
    return bytes([(field << 3) | 5]) + struct.pack("<f", value)


def _flow_msg(*, hz=None, ticks=None, gpm=None, gal=None) -> bytes:
    """A #59 FlowSensorData message with only the given fields present."""
    inner = b""
    if hz is not None:
        inner += pb._pb_field_varint(rx.RX_F_WATERING_ACTIVE, hz)
    if ticks is not None:
        inner += pb._pb_field_varint(rx.RX_F_FLOW_TOTAL, ticks)
    if gpm is not None:
        inner += _f32(rx.RX_F_FLOW_RATE_GPM, gpm)
    if gal is not None:
        inner += _f32(rx.RX_F_FLOW_VOLUME_GAL, gal)
    return pb._pb_field_bytes(rx.RX_F_WATERING, inner)


def _status_msg(run_state: int) -> bytes:
    return pb._pb_field_bytes(rx.RX_F_STATUS, pb._pb_field_varint(rx.RX_F_STATUS_MODE, run_state))


def _make_gen2(**state_kwargs):
    dev = object.__new__(BHyveHT25G2Device)  # bypass HA-heavy __init__
    dev.mac = "AA:BB:CC:DD:EE:FF"
    dev.state = DeviceState(**state_kwargs)
    dev.flow_counts_per_gallon = 433
    return dev


class _FakeConn:
    """Records sent frames; feeds `reply` (a full inner message) through the
    device observer on every send, as _on_notify would."""

    def __init__(self, device, reply: bytes | None = None):
        self.device = device
        self.reply = reply
        self.sent: list[bytes] = []
        self.disconnects = 0

    async def send(self, frame: bytes, drain_ms: int = 1500):
        self.sent.append(frame)
        if self.reply is not None:
            self.device._observe_plaintext(self.reply)
        return [b"\x01"]

    async def send_stream(self, frame: bytes, drain_ms: int = 4000):
        self.sent.append(frame)
        return b""

    async def disconnect(self):
        self.disconnects += 1

    @property
    def is_connected(self):
        return True


# --- flow math like the app (#59) ------------------------------------------

def test_app_flow_gpm_prefers_device_gpm_then_hz_line():
    assert rx.app_flow_gpm(4.49, 50) == 4.49                     # #59.#4 wins
    assert rx.app_flow_gpm(None, 32) == round(0.1310135205953254 * 32 + 0.3840268315690179, 2)
    assert rx.app_flow_gpm(None, 32) == 4.58
    assert rx.app_flow_gpm(None, 0) == 0.0                       # 0 Hz -> 0 gpm, not the offset
    assert rx.app_flow_gpm(None, None) is None                   # no #59 seen


def test_flow_gallons_prefers_device_volume_then_calibrated_ticks():
    assert rx.flow_gallons(3.5, 9999, 433) == 3.5                # #59.#5 wins
    assert rx.flow_gallons(None, 866, 433) == 2.0                # ticks / Flow calibration
    assert rx.flow_gallons(None, 1000, None) == round(1000 * 0.00228498, 3)  # app constant
    assert rx.flow_gallons(None, None, 433) is None


def test_extract_status_decodes_flow_hz_and_volume():
    st = rx.extract_status(_flow_msg(hz=32, ticks=489, gal=1.25))
    assert st.flow_hz == 32
    assert st.flow_total == 489
    assert st.flow_volume_gal == 1.25
    # Protobuf omits a zero #1: a #59 without it is 0 Hz, not "unknown".
    assert rx.extract_status(_flow_msg(ticks=10)).flow_hz == 0
    # No #59 at all -> None.
    assert rx.extract_status(_status_msg(1)).flow_hz is None


def test_apply_stores_and_clears_flow_hz_and_volume():
    dev = _make_gen2(is_watering=True, active_zone=1)
    rx.apply_status_plaintext(dev, pb._build_message(_flow_msg(hz=30, ticks=100, gal=0.5)))
    assert dev.state.flow_hz == 30
    assert dev.state.flow_volume_gal == 0.5
    rx.apply_status_plaintext(dev, pb._build_message(_status_msg(1)))  # idle
    assert dev.state.flow_hz is None
    assert dev.state.flow_volume_gal is None


def test_pick_flow_gpm_precedence():
    slope = [(0.0, 0), (4.45, 144)]
    # 1. a populated #59.#4 wins over the slope and the Hz line
    assert pb._pick_flow_gpm(slope, 433, 3.0, 40) == 3.0
    # 2. the calibrated slope wins over the Hz line when it has two samples
    assert pb._pick_flow_gpm(slope, 433, None, 40) == pb._gpm_from_samples(slope, 433)
    # ...including a flat counter (0 gpm) — the slope is authoritative there
    assert pb._pick_flow_gpm([(0.0, 5), (4.0, 5)], 433, None, 40) == 0.0
    # 3. the Hz line only when there is no slope to take
    assert pb._pick_flow_gpm([(0.0, 5)], 433, None, 32) == 4.58
    assert pb._pick_flow_gpm([], 433, None, 0) == 0.0
    assert pb._pick_flow_gpm([], 433, None, None) == 0.0


def test_read_flow_uses_device_gpm_when_populated():
    dev = _make_gen2(is_watering=True)
    dev.connection = _FakeConn(dev, pb._build_message(_flow_msg(hz=30, ticks=500, gpm=2.5)))
    asyncio.run(dev.read_flow())
    assert dev.state.flow_gpm == 2.5


def test_read_flow_falls_back_to_slope_without_device_gpm():
    # The fake answers every send with the same counter, so the slope is flat:
    # 0 gpm, even though #59.#1 says 30 Hz — the slope wins with >= 2 samples.
    dev = _make_gen2(is_watering=True)
    dev.connection = _FakeConn(dev, pb._build_message(_flow_msg(hz=30, ticks=500)))
    asyncio.run(dev.read_flow())
    assert dev.state.flow_gpm == 0.0
    assert dev.state.flow_hz == 30


# --- run program now (#14 manualMode + activeProgramFlags) ------------------

@pytest.mark.parametrize(
    "slot,expected",
    [
        (1, "7206080212021001"),  # A = bit0
        (2, "7206080212021002"),  # B = bit1
        (3, "7206080212021004"),
        (4, "7206080212021008"),  # D = bit3
    ],
)
def test_run_program_builder_byte_refs(slot, expected):
    assert pb._build_run_program_pb(slot).hex() == expected
    # Shape: #14 { #1 manualMode=2, #2 manualModeParams { #2 activeProgramFlags } }
    tm = rx.pb_parse(rx._pb_field(rx.pb_parse(pb._build_run_program_pb(slot)), 14))
    assert rx._pb_field(tm, 1) == 2
    assert rx._pb_field(rx.pb_parse(rx._pb_field(tm, 2)), 2) == 1 << (slot - 1)


def _stored(slot, zones, budget=100):
    return ProgramSummary(slot=slot, empty=False, enabled=True, zones=zones, budget=budget)


def test_run_program_confirms_via_status():
    dev = _make_gen2(is_watering=False)
    dev.connection = _FakeConn(dev, pb._build_message(_status_msg(4)))
    assert asyncio.run(dev.run_program(2)) is True
    assert dev.connection.sent[0] == pb._build_message(pb._build_run_program_pb(2))
    assert dev.state.is_watering is True
    assert dev.state.duration_enforced_by_host is False  # Gen2 times programs itself
    assert dev.connection.disconnects == 1


def test_run_program_not_confirmed_returns_false():
    dev = _make_gen2(is_watering=False)
    dev.connection = _FakeConn(dev, pb._build_message(_status_msg(1)))
    assert asyncio.run(dev.run_program(1)) is False


def _make_ht25a(**state_kwargs):
    dev = object.__new__(BHyveHT25ADevice)
    dev.mac = "AA:BB:CC:DD:EE:01"
    dev.hardware, dev.firmware = "HT25A-0001", "0098"
    dev.state = DeviceState(**state_kwargs)
    dev.flow_counts_per_gallon = 433
    return dev


def test_run_program_on_ht25a_times_the_run_from_the_host():
    # HT25A ignores manual run times, so the program's own length is enforced
    # from the host, exactly like a manual run on that class.
    dev = _make_ht25a(is_watering=False, programs={1: _stored(1, ((0, 600),))})
    dev.connection = _FakeConn(dev, pb._build_message(_status_msg(4)))

    async def run():
        ok = await dev.run_program(1)
        armed = dev._host_stop_handle is not None
        dev._cancel_host_stop()
        return ok, armed

    ok, armed = asyncio.run(run())
    assert ok is True and armed is True
    assert dev.state.duration_enforced_by_host is True
    left = (dev.state.expected_off_at - dev.state.started_at).total_seconds()
    assert left == 600


def test_run_program_on_ht25a_refuses_without_known_length():
    dev = _make_ht25a(is_watering=False)  # no program read yet
    dev.connection = _FakeConn(dev, pb._build_message(_status_msg(4)))
    assert asyncio.run(dev.run_program(1)) is False
    assert dev.connection.sent == []  # nothing went out


def test_program_run_sec_scales_up_by_budget_never_down():
    dev = _make_gen2(programs={
        1: _stored(1, ((0, 300), (1, 300)), budget=150),
        2: _stored(2, ((0, 300),), budget=50),
        3: ProgramSummary(slot=3, empty=True),
    })
    assert dev._program_run_sec(1) == 900
    assert dev._program_run_sec(2) == 300
    assert dev._program_run_sec(3) == 0
    assert dev._program_run_sec(4) == 0


def test_run_program_returns_false_on_ble_error():
    from bleak.exc import BleakError

    class _Broken(_FakeConn):
        async def send(self, frame, drain_ms=1500):
            raise BleakError("Not connected")

    dev = object.__new__(BHyveHT34ADevice)
    dev.mac = "AA:BB:CC:DD:EE:02"
    dev.state = DeviceState()
    dev.connection = _Broken(dev)
    assert asyncio.run(dev.run_program(1)) is False
