"""Gen1 (HT25 fw0085) flow-sample decode + accumulation tests.

Covers BHyveHT25Device.apply_gen1_flow_frame — the RX side of the undocumented
0x89 flow subscription — plus reset_gen1_flow and the subscribe-frame builder.
Frame guards, the device-side elapsed clock, u16 counter wrap and the
counts/litre calibration are all exercised with synthetic frames. No hardware
or Home Assistant required.

The decode takes dt from the device's own elapsed field (pt[7:9]) rather than a
host clock, so nothing here patches time.monotonic — there is no host clock in
the path to patch.
"""
from __future__ import annotations

import pytest

from orbit_bhyve.devices.ht25 import BHyveHT25Device

MESH_ID = 0x47D7
MESH = MESH_ID.to_bytes(2, "little")  # d7 47
SEQ_FLOW = 0x0B
ROUTING = 0x40
CAL = 112  # counts/litre used throughout, set explicitly on the fixture


def _dev(counts_per_litre: int = CAL) -> BHyveHT25Device:
    # Key-less record -> no BLE connection, no hass needed (same trick as
    # test_mesh_status.py::_dev).
    record = {
        "cloud_id": "abc", "name": "Deck", "mac": "AA:BB:CC:DD:EE:FF",
        "hardware": "HT25-0000", "firmware": "0085", "stations": 1,
        "network_key": "", "mesh_device_id": MESH_ID,
    }
    dev = BHyveHT25Device(None, record)
    dev.flow_counts_per_litre = counts_per_litre
    dev.reset_gen1_flow()
    return dev


def _f(
    elapsed: int,
    cum: int,
    *,
    rate: int = 0,
    mesh: bytes = MESH,
    seq: int = SEQ_FLOW,
    routing: int = ROUTING,
    ctr: int = 0x8B,
) -> bytes:
    """[mesh:2][push_ctr:1][seq:1][routing:1][rate u16][elapsed u16][cum u16].

    `ctr` is a push message counter cycling 0x80-0xBF, shared across flow,
    status and battery pushes. It is NOT a type byte and the decode must not
    inspect it — the 0x40 reply bit is clear across that whole range, so a
    TX-echo guard copied from _observe_plaintext would reject every flow frame.
    Default 0x8B is simply one observed value.
    """
    return (
        mesh
        + bytes([ctr, seq, routing])
        + rate.to_bytes(2, "little")
        + elapsed.to_bytes(2, "little")
        + (cum & 0xFFFF).to_bytes(2, "little")
    )


# --- frame guards ----------------------------------------------------------

def test_short_frame_is_rejected():
    dev = _dev()
    assert dev.apply_gen1_flow_frame(_f(9, 100)[:10]) is False
    assert dev.state.water_used_gen1_l is None


def test_foreign_mesh_address_is_rejected():
    # A neighbour's timer on the same channel must not move our counters.
    dev = _dev()
    other = (0x1234).to_bytes(2, "little")
    assert dev.apply_gen1_flow_frame(_f(9, 100, mesh=other)) is False
    assert dev.apply_gen1_flow_frame(_f(12, 212, mesh=other)) is False
    assert dev.state.water_used_gen1_l is None


def test_non_flow_seq_byte_is_rejected():
    dev = _dev()
    assert dev.apply_gen1_flow_frame(_f(9, 100, seq=0x02)) is False
    assert dev.state.water_used_gen1_l is None


def test_wrong_routing_byte_is_rejected():
    dev = _dev()
    assert dev.apply_gen1_flow_frame(_f(9, 100, routing=0x00)) is False
    assert dev.state.water_used_gen1_l is None


def test_push_counter_byte_is_not_inspected():
    # Regression guard for a real near-miss: 0x8B was mistaken for a type byte
    # and a proposed pt[2] check would have matched 1 frame in 256.
    dev = _dev()
    for ctr in (0x80, 0x8B, 0xA5, 0xBF):
        dev.reset_gen1_flow()
        assert dev.apply_gen1_flow_frame(_f(9, CAL, ctr=ctr)) is True
        assert dev.state.water_used_gen1_l == pytest.approx(1.0)


def test_frame_ignored_when_mesh_id_unknown():
    # A cloud record without a mesh_device_id must make flow frames a no-op,
    # not raise out of the mesh_address property — _observe_plaintext calls
    # this on every inbound frame, so raising here breaks all status decode.
    dev = _dev()
    dev.mesh_device_id = None
    assert dev.apply_gen1_flow_frame(_f(9, 100)) is False


# --- volume ---------------------------------------------------------------

def test_first_in_run_sample_books_its_cumulative():
    # The counter resets per run, so the first sample's ticks are real water.
    # The previous delta-accumulating decode discarded them as a baseline,
    # undercounting every run by whatever flowed before the first sample.
    dev = _dev()
    assert dev.apply_gen1_flow_frame(_f(9, CAL)) is True
    assert dev.state.water_used_gen1_l == pytest.approx(1.0)
    # No previous sample to difference against, so no rate yet.
    assert dev.state.flow_lpm_gen1 is None


def test_volume_tracks_the_cumulative_field():
    dev = _dev()
    for elapsed, cum in [(9, CAL), (12, 2 * CAL), (15, 3 * CAL)]:
        dev.apply_gen1_flow_frame(_f(elapsed, cum))
    assert dev.state.water_used_gen1_l == pytest.approx(3.0)


def test_calibration_value_scales_the_conversion():
    dev = _dev(counts_per_litre=224)
    dev.apply_gen1_flow_frame(_f(9, 224))
    assert dev.state.water_used_gen1_l == pytest.approx(1.0)


# --- rate -----------------------------------------------------------------

def test_rate_from_cumulative_delta_over_elapsed_delta():
    dev = _dev()
    dev.apply_gen1_flow_frame(_f(9, 0))
    dev.apply_gen1_flow_frame(_f(12, CAL))
    # One litre in the 3 s the device itself reported = 20 L/min.
    assert dev.state.flow_lpm_gen1 == pytest.approx(20.0)
    assert dev.state.water_used_gen1_l == pytest.approx(1.0)


def test_static_meter_mid_run_reports_zero_rate():
    # Valve open, counter not moving. The rate must fall to zero rather than
    # holding its last non-zero value.
    dev = _dev()
    dev.apply_gen1_flow_frame(_f(9, 0))
    dev.apply_gen1_flow_frame(_f(12, CAL))
    assert dev.state.flow_lpm_gen1 == pytest.approx(20.0)
    dev.apply_gen1_flow_frame(_f(15, CAL))
    assert dev.state.flow_lpm_gen1 == pytest.approx(0.0)
    assert dev.state.water_used_gen1_l == pytest.approx(1.0)


# --- run end --------------------------------------------------------------

def test_run_end_zeroes_rate_and_preserves_total():
    # After the run the device keeps sampling with every field zeroed, for as
    # long as the subscription lives. Reading those as a volume would wipe the
    # run total the moment the valve shut.
    dev = _dev()
    dev.apply_gen1_flow_frame(_f(9, 0))
    dev.apply_gen1_flow_frame(_f(12, CAL))
    for _ in range(3):
        assert dev.apply_gen1_flow_frame(_f(0, 0)) is True
    assert dev.state.flow_lpm_gen1 == pytest.approx(0.0)
    assert dev.state.water_used_gen1_l == pytest.approx(1.0)


# --- counter wrap ---------------------------------------------------------

def test_counter_wrap_is_carried():
    # u16 wraps at 65536 ticks — about 27 min on a 40 tick/s zone, well inside
    # the supported program length.
    dev = _dev()
    dev.apply_gen1_flow_frame(_f(100, 65500))
    dev.apply_gen1_flow_frame(_f(103, 40))
    assert dev.state.water_used_gen1_l == pytest.approx((65536 + 40) / CAL, abs=0.01)


def test_implausible_counter_jump_reanchors():
    # A drop too large to be a real wrap at any reachable flow rate is a
    # corrupt/desynced frame. Re-anchor rather than booking 65536 phantom
    # ticks (~585 L at this calibration) permanently.
    dev = _dev()
    dev.apply_gen1_flow_frame(_f(100, 60000))
    dev.apply_gen1_flow_frame(_f(103, 10))
    assert dev.state.water_used_gen1_l == pytest.approx(10 / CAL, abs=0.01)


def test_new_run_reanchors_instead_of_accumulating():
    # elapsed only ever decreases across a run boundary: it counts from START
    # and is unchanged by a mid-run re-subscribe.
    dev = _dev()
    dev.apply_gen1_flow_frame(_f(356, 11847))
    dev.apply_gen1_flow_frame(_f(8, 90))
    assert dev.state.water_used_gen1_l == pytest.approx(90 / CAL, abs=0.01)


# --- lifecycle ------------------------------------------------------------

def test_accumulation_notifies_coordinator():
    dev = _dev()
    pokes: list[int] = []
    dev.set_state_changed_callback(lambda: pokes.append(1))
    dev.apply_gen1_flow_frame(_f(9, CAL))
    assert pokes == [1]
    dev.apply_gen1_flow_frame(_f(12, 2 * CAL))
    assert pokes == [1, 1]


def test_reset_clears_totals_and_anchors():
    dev = _dev()
    dev.apply_gen1_flow_frame(_f(9, CAL))
    assert dev.state.water_used_gen1_l == pytest.approx(1.0)
    dev.reset_gen1_flow()
    assert dev.state.water_used_gen1_l is None
    assert dev.state.flow_lpm_gen1 is None
    # Anchors dropped too: the next run starts from its own counter rather than
    # carrying the previous run's wrap count.
    dev.apply_gen1_flow_frame(_f(9, CAL))
    assert dev.state.water_used_gen1_l == pytest.approx(1.0)


# --- subscribe frame -------------------------------------------------------

def test_subscribe_frame_carries_interval_and_count():
    dev = _dev()
    frame = dev._flow_subscribe_frame()
    assert frame[0:2] == MESH
    assert frame[2] == 0x89   # TYPE_FLOW_SUBSCRIBE
    assert frame[3] == 0x0E   # SEQ_FLOW_SUBSCRIBE
    interval = int.from_bytes(frame[5:7], "little")
    samples = int.from_bytes(frame[7:9], "little")
    assert (interval, samples) == (3000, 700)
    # The device honours the interval exactly but ignores the count, stopping
    # ~300 s after the subscribe regardless — so this asserts the wire values,
    # not a coverage budget. Coverage past 300 s comes from re-subscribing.
    assert interval == 3000
