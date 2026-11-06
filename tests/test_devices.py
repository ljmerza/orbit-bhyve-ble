"""Device-class dispatch + structure tests.

Verifies resolve_device_class() routes each hardware/firmware/type to the right
class — in particular that Gen2 HT25G2 valves (which share the "HT25" prefix
with the older mesh hose timers) land on the protobuf class, not the mesh one —
and that the consolidated protobuf family keeps its expected shape. No hardware
or Home Assistant required.
"""
from __future__ import annotations

import asyncio
import sys
import types
import struct
from datetime import datetime, timedelta, timezone

import pytest

from orbit_bhyve.devices import (
    BHyveHT25ADevice,
    BHyveHT25Device,
    BHyveHT25Fw0085Device,
    BHyveHT25G2Device,
    BHyveHT34ADevice,
    BHyveWT25G2Device,
    BHyveHubDevice,
    UnsupportedModel,
    resolve_device_class,
)
from orbit_bhyve.connection import (
    BHyveBleConnection,
    BleNotConnectable,
    advertised_name_for,
    match_advertised,
)
from orbit_bhyve.devices import protobuf as pb
from orbit_bhyve.devices import status as rx
from orbit_bhyve.devices.base import DeviceState, _mv_to_pct
from orbit_bhyve.devices.protobuf import BHyveProtobufDevice


@pytest.mark.parametrize(
    "hardware,firmware,type_,expected",
    [
        ("", "", "bridge", BHyveHubDevice),               # hub wins on type
        ("HT34A-0001", "0107", "", BHyveHT34ADevice),     # XD 4-port
        ("HT31-0001", "0058", "", BHyveHT34ADevice),      # 1-port Smart Hose Tap (XD protobuf)
        ("HT25G2-0001", "0111", "", BHyveHT25G2Device),   # Gen2 by suffix
        ("HT25-0001", "0111", "", BHyveHT25G2Device),       # Gen2, bare hardware string
        ("HT25-0000", "0085", "", BHyveHT25Fw0085Device),   # mesh fw0085 (upstream subclass)
        ("HT25-0000", "0041", "", BHyveHT25Device),         # mesh base (fw0041)
        ("HT25G2-0001", "0098", "", BHyveHT25G2Device),     # Gen2, unseen firmware (issue #47)
        ("HT25-0001", "0098", "", BHyveHT25G2Device),       # Gen2, bare hardware + unseen firmware
        ("HT25A-0001", "0098", "", BHyveHT25ADevice),       # issue #47; own class (offMode stop, host-timed runs)
        ("HT25A-0001", "0111", "", BHyveHT25ADevice),       # HT25A prefix wins regardless of firmware
        ("HT25G2-0000", "0111", "", BHyveHT25G2Device),     # explicit G2 prefix wins over -0000 suffix
        # WT = sprinkler controllers, a separate line from the HT hose timers.
        ("WT25G2-0001", "0096", "", BHyveWT25G2Device),     # 12-station, tested
        ("WT25-0001", "0092", "", BHyveWT25G2Device),       # Gen1 sibling, untested
    ],
)
def test_resolve_routes(hardware, firmware, type_, expected):
    assert resolve_device_class(hardware=hardware, firmware=firmware, type_=type_) is expected


def test_resolve_unknown_raises():
    with pytest.raises(UnsupportedModel):
        resolve_device_class(hardware="ZZ99", firmware="0001", type_="")


def test_wt24_is_not_guessed():
    """WT24 is an older generation with an unknown protocol, so it stays
    unsupported rather than routed to the protobuf class by prefix."""
    with pytest.raises(UnsupportedModel):
        resolve_device_class(hardware="WT24-0001", firmware="0090", type_="")


def test_protobuf_family_subclassing():
    assert issubclass(BHyveHT34ADevice, BHyveProtobufDevice)
    assert issubclass(BHyveHT25G2Device, BHyveProtobufDevice)
    assert issubclass(BHyveWT25G2Device, BHyveProtobufDevice)
    # Flow is Gen2-hose-valve-only; untested on a controller.
    assert BHyveWT25G2Device.has_flow is False


@pytest.mark.parametrize(
    "cls,label",
    [(BHyveHT34ADevice, "HT34A"), (BHyveHT25G2Device, "HT25G2")],
)
def test_protobuf_family_attrs(cls, label):
    assert cls.log_label == label
    assert cls.frame_magic == 0x11
    assert cls.trailer_const == 0x11


@pytest.mark.parametrize(
    "mv,pct",
    [
        (2400, 0),     # curve floor
        (3000, 100),   # curve ceiling
        (2700, 50),    # midpoint
        (2000, 0),     # below floor clamps
        (3500, 100),   # above ceiling clamps
    ],
)
def test_mv_to_pct(mv, pct):
    assert _mv_to_pct(mv) == pct


@pytest.mark.parametrize(
    "chemistry,mv,pct",
    [
        # NiMH plateau (2450 mV) reads a healthy 25% on its curve vs a false ~8% on
        # alkaline; endpoints 2350..2750.
        ("nimh", 2450, 25),
        ("nimh", 2350, 0),
        ("nimh", 2750, 100),
        # Primary lithium: 2600..3400; its 3000 mV plateau reads 50%.
        ("lithium_primary", 3000, 50),
        ("lithium_primary", 2600, 0),
        # Regulated li-ion shares alkaline endpoints (unmeasurable-by-percent, so
        # it just tracks the alkaline curve).
        ("lithium_regulated", 2700, 50),
        # Unknown chemistry falls back to the alkaline curve rather than dividing by
        # a bogus span.
        ("bogus", 2700, 50),
    ],
)
def test_mv_to_pct_chemistry(chemistry, mv, pct):
    assert _mv_to_pct(mv, chemistry) == pct


def test_battery_pct_property_follows_chemistry():
    # battery_pct is derived live from battery_mv + battery_chemistry, so flipping
    # the chemistry re-gauges the same voltage with no new BLE read.
    record = {
        "cloud_id": "abc", "name": "T", "mac": "AA:BB:CC:DD:EE:FF",
        "hardware": "HT25G2-0001", "firmware": "0111", "stations": 1,
        "network_key": "",  # no key -> no BLE connection (no hass needed)
    }
    dev = BHyveHT25G2Device(None, record)
    assert dev.battery_pct is None            # no voltage yet
    dev.battery_mv = 2450
    assert dev.battery_chemistry == "alkaline"  # default
    assert dev.battery_pct == _mv_to_pct(2450, "alkaline")
    dev.battery_chemistry = "nimh"
    assert dev.battery_pct == _mv_to_pct(2450, "nimh")
    assert dev.battery_pct == 25


def test_cloud_battery_snapshot_is_not_seeded():
    # The cloud battery snapshot must NEVER seed the sensor: it's on a different
    # discharge curve than _mv_to_pct, so it painted a wrong (voltage-inconsistent)
    # value into long-term stats at every startup. Battery starts unknown and is
    # only ever set by a live BLE decode.
    record = {
        "cloud_id": "abc", "name": "T", "mac": "AA:BB:CC:DD:EE:FF",
        "hardware": "HT25G2-0001", "firmware": "0111", "stations": 1,
        "network_key": "",  # no key -> no BLE connection created (no hass needed)
        "battery_pct": 88, "battery_mv": 2999,  # cloud snapshot — must be ignored
    }
    dev = BHyveHT25G2Device(None, record)
    assert dev.battery_pct is None
    assert dev.battery_mv is None


# --- actuation confirm-via-#15 (regression for stuck-open valve) ----------

def _status_frame(run_state: int) -> bytes:
    """A CRC-valid #16 status notification carrying only the run-state."""
    inner = pb._pb_field_bytes(rx.RX_F_STATUS, pb._pb_field_varint(rx.RX_F_STATUS_MODE, run_state))
    return pb._build_message(inner)


def _flow_frame(active: int, total: int) -> bytes:
    """A CRC-valid #59 watering/flow notification { #1=flow-active, #3=cumulative }."""
    inner = pb._pb_field_bytes(
        rx.RX_F_WATERING,
        pb._pb_field_varint(rx.RX_F_WATERING_ACTIVE, active)
        + pb._pb_field_varint(rx.RX_F_FLOW_TOTAL, total),
    )
    return pb._build_message(inner)


_STATUS_REQ_FRAME = None  # set lazily to the #15{} frame the device emits


def _is_status_elicitor(frame: bytes) -> bool:
    """True if `frame` is a status-eliciting request: either #15{} getDeviceStatus
    (post-command confirm reads) or #75 setEpochTime (the coordinator poll, which
    sets the clock AND returns the same #16 status). Both should yield on_status."""
    top = rx.pb_parse(rx.decode_inner(frame) or b"")
    return rx._pb_field(top, 15) is not None or rx._pb_field(top, 75) is not None


def _sent_status_request(conn) -> bool:
    return any(_is_status_elicitor(f) for f in conn.sent)


class _FakeConn:
    """Stand-in for BHyveBleConnection: records sent frames and feeds a canned
    plaintext back through the device's own observer, exactly as _on_notify
    would after decrypting an RX notification."""

    def __init__(self, device, *, on_status=None, on_command=None):
        self.device = device
        self.on_status = on_status      # fed on a status elicitor (#15{} or #75)
        self.on_command = on_command    # fed on any other frame (start/stop/rain)
        self.sent: list[bytes] = []
        self.disconnects = 0

    async def send(self, frame: bytes, drain_ms: int = 1500):
        self.sent.append(frame)
        pt = self.on_status if _is_status_elicitor(frame) else self.on_command
        if pt is not None:
            self.device._observe_plaintext(pt)
        return [b"\x01"]  # one notification (the bare #30 ack for a stop)

    async def send_stream(self, frame: bytes, drain_ms: int = 4000):
        # The idle coordinator poll reads programs via #10 syncRequest; these
        # fakes carry no program dump, so return an empty stream (no programs).
        self.sent.append(frame)
        return b""

    async def disconnect(self):
        self.disconnects += 1

    @property
    def is_connected(self):
        return True


def _make_device(**state_kwargs):
    dev = object.__new__(BHyveHT25G2Device)  # bypass HA-heavy __init__
    dev.mac = "AA:BB:CC:DD:EE:FF"
    dev.state = DeviceState(**state_kwargs)
    dev.flow_counts_per_gallon = 433  # set by base __init__ from options normally
    return dev


def test_stop_confirms_via_status_poll_when_reply_lacks_status():
    # The device answers a stop with a bare #30 ack (no #16), so the stop send
    # alone can't confirm; the #15{} poll returns idle and closes the valve.
    dev = _make_device(is_watering=True, active_zone=1, seconds_remaining=600)
    dev.state.expected_off_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    dev.connection = _FakeConn(dev, on_status=_status_frame(1), on_command=None)

    ok = asyncio.run(dev.stop_watering())

    assert ok is True
    assert dev.state.is_watering is False
    assert dev.state.expected_off_at is None       # wall-clock auto-close disarmed
    assert pb._build_message(pb._REQUEST_STATUS_PB) in dev.connection.sent


def test_stop_confirms_immediately_via_stop_ack():
    # If the stop command response contains the #30 stop command acknowledgment,
    # it must confirm the stop immediately and bypass/skip needing status poll confirmations.
    dev = _make_device(is_watering=True, active_zone=1, seconds_remaining=600)
    dev.state.expected_off_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    
    stop_ack = pb._pb_field_bytes(30, b"\x08\x01")
    stop_ack_frame = pb._build_message(stop_ack)
    
    dev.connection = _FakeConn(dev, on_status=None, on_command=stop_ack_frame)
    
    ok = asyncio.run(dev.stop_watering())
    
    assert ok is True
    assert dev.state.is_watering is False
    assert dev.state.expected_off_at is None


def test_stop_not_confirmed_when_device_still_watering():
    # If the #15{} poll still shows run-state 4, the stop must NOT falsely
    # confirm — it retries with a fresh session and reports failure.
    dev = _make_device(is_watering=True, active_zone=1, seconds_remaining=600)
    dev.connection = _FakeConn(dev, on_status=_status_frame(4), on_command=None)

    ok = asyncio.run(dev.stop_watering())

    assert ok is False
    assert dev.state.is_watering is True
    assert dev.connection.disconnects == 2         # both attempts retried


def test_refresh_state_polls_status_and_sees_out_of_band_run():
    # The coordinator poll must actually read the device (#15{}) so a run HA
    # didn't start (a scheduled program) becomes visible.
    dev = _make_device(is_watering=False)
    dev.connection = _FakeConn(dev, on_status=_status_frame(4), on_command=_status_frame(4))

    state = asyncio.run(dev.refresh_state())

    assert state.is_watering is True
    # The poll elicitor is #75 setEpochTime (reads status AND syncs the clock).
    assert _sent_status_request(dev.connection)


def test_refresh_state_poll_uses_epoch_time_elicitor():
    # The coordinator poll must use #75 setEpochTime (not the bare #15{}) so it
    # keeps the device clock honest while reading status — HW-validated content-
    # identical reply (session 2026-07-06).
    dev = _make_device(is_watering=False)
    dev.connection = _FakeConn(dev, on_status=_status_frame(1), on_command=None)
    asyncio.run(dev.refresh_state())
    sent_fields = [rx._pb_field(rx.pb_parse(rx.decode_inner(f) or b""), 75) for f in dev.connection.sent]
    assert any(v is not None for v in sent_fields)  # a #75 frame was sent
    # and NOT the bare #15{} on the poll path
    assert pb._build_message(pb._REQUEST_STATUS_PB) not in dev.connection.sent


def test_start_arms_wall_clock_autoclose():
    # A confirmed start must arm expected_off_at so the coordinator can close
    # the valve on the wall clock even if a later BLE read/stop fails.
    dev = _make_device(is_watering=False)
    dev.connection = _FakeConn(dev, on_status=_status_frame(4), on_command=_status_frame(4))

    ok = asyncio.run(dev.start_watering(1, 600))

    assert ok is True
    assert dev.state.is_watering is True
    assert dev.state.active_zone == 1
    assert dev.state.expected_off_at is not None


def test_stop_confirms_when_rain_delay_active():
    # After a stop with a rain delay active the device reports run-state 3
    # (rain-delay), which is "not watering" — the stop should confirm.
    dev = _make_device(is_watering=True, active_zone=1)
    dev.connection = _FakeConn(dev, on_status=_status_frame(3), on_command=None)

    ok = asyncio.run(dev.stop_watering())

    assert ok is True
    assert dev.state.is_watering is False


def _status_clock_and_rain_delay(clock: int, minutes: int) -> bytes:
    """A #16 status carrying #7 device clock (top-level) + an active #16.#13
    rain-delay block, as the device would answer a #15{} poll."""
    rd = pb._pb_field_varint(rx.RX_F_RD_MINUTES, minutes)
    rd += pb._pb_field_varint(rx.RX_F_RD_ENABLED, 1)
    sub = pb._pb_field_varint(rx.RX_F_STATUS_MODE, 3)
    sub += pb._pb_field_bytes(rx.RX_F_STATUS_RAINDELAY, rd)
    top = pb._pb_field_varint(rx.RX_F_CLOCK, clock)
    top += pb._pb_field_bytes(rx.RX_F_STATUS, sub)
    return pb._build_message(top)


def test_set_rain_delay_anchors_expiry_to_device_clock():
    # The device honors the absolute #3 expiry literally, so set_rain_delay must
    # read the device clock (#7) via a #15{} poll first and compute
    # #3 = deviceClock + minutes*60 — NOT host time. A host/device skew would
    # otherwise end the delay early/late.
    clock = 1_700_000_000
    minutes = 360
    dev = _make_device(is_watering=False)
    dev.connection = _FakeConn(
        dev, on_status=_status_clock_and_rain_delay(clock, minutes), on_command=None
    )

    ok = asyncio.run(dev.set_rain_delay(minutes))

    assert ok is True
    status_req = pb._build_message(pb._REQUEST_STATUS_PB)
    unsub_req = pb._build_message(pb._FLOW_UNSUBSCRIBE_PB)
    rd_frames = [f for f in dev.connection.sent if f != status_req and f != unsub_req]
    assert len(rd_frames) == 1
    rd_pb = rx.pb_parse(rx._pb_field(rx.pb_parse(rx.decode_inner(rd_frames[0])), 17))
    assert rx._pb_field(rd_pb, rx.RX_F_RD_MINUTES) == minutes
    assert rx._pb_field(rd_pb, rx.RX_F_RD_EXPIRY) == clock + minutes * 60


def _written_rain_delay_minutes(frame: bytes) -> int | None:
    """If `frame` is a setRainDelay (#17) write, return its #1 minutes; else None."""
    inner = rx.decode_inner(frame)
    if inner is None:
        return None
    body = rx._pb_field(rx.pb_parse(inner), 17)
    if body is None:
        return None
    return rx._pb_field(rx.pb_parse(body), rx.RX_F_RD_MINUTES) or 0


class _IgnoresStatusEchoesRainDelayConn(_FakeConn):
    """A unit (like BTValve01 while active) that ignores the passive #15 query and
    does NOT echo the set write's own reply ("suppressed while active"), but DOES
    answer the status-recovery no-op — a #17 write issued right after a failed #15
    — with the resulting #16 status. Tells the two #17 writes apart by whether a
    #15 immediately preceded them: only the recovery no-op does."""

    def __init__(self, device, *, clock: int):
        super().__init__(device, on_status=None, on_command=None)
        self._clock = clock

    async def send(self, frame: bytes, drain_ms: int = 1500):
        prev = self.sent[-1] if self.sent else None
        self.sent.append(frame)
        minutes = _written_rain_delay_minutes(frame)
        if minutes is not None and prev == pb._build_message(pb._REQUEST_STATUS_PB):
            self.device._observe_plaintext(_status_clock_and_rain_delay(self._clock, minutes))
        return [b"\x01"]


def test_set_rain_delay_survives_recovery_on_unit_that_ignores_15():
    # Regression: on a unit that ignores #15 and whose set reply is suppressed, the
    # confirm read-back falls into _recover_status, whose #16-eliciting no-op re-
    # asserts self.state. set_rain_delay must SEED the intended minutes BEFORE that
    # read, so the no-op re-sends the delay just set — not a stale bare #17{#1=0}
    # clear that silently cancels it (the "rain-delay set … unconfirmed" bug, where
    # the entity snapped back to 0). has_flow=False isolates the rain-delay recovery
    # path from the orthogonal flow (#57) dance.
    clock = 1_700_000_000
    dev = _make_device(is_watering=False)
    dev.has_flow = False
    dev.connection = _IgnoresStatusEchoesRainDelayConn(dev, clock=clock)

    ok = asyncio.run(dev.set_rain_delay(120))

    # Pre-fix: the recovery no-op read stale state (0) and sent #17{#1=0}, so the
    # device echoed 0 -> ok False, minutes 0. Post-fix: the seed makes the no-op
    # re-assert 120, the device echoes 120 back -> confirmed.
    assert ok is True
    assert dev.state.rain_delay_minutes == 120


# --- flow (#57/#59, Gen2-only) --------------------------------------------

def test_has_flow_only_on_gen2():
    # Gen2 (HT25G2) has an inline flow sensor; the XD (HT34A) does not.
    assert BHyveHT25G2Device.has_flow is True
    assert BHyveHT34ADevice.has_flow is False


def test_flow_subscribe_pb_round_trips_and_matches_capture():
    frame = pb._build_message(pb._FLOW_SUBSCRIBE_PB)
    assert rx.decode_inner(frame) == pb._FLOW_SUBSCRIBE_PB
    sub = rx.pb_parse(rx._pb_field(rx.pb_parse(pb._FLOW_SUBSCRIBE_PB), 57))
    assert rx._pb_field(sub, 1) == 1000   # sendDataIntervalMilliSec (app capture)
    assert rx._pb_field(sub, 2) == 10     # sendDataDurationSec: covers read_flow's ~4.8 s window


def test_flow_subscribe_and_unsubscribe_byte_refs():
    # #57 #2 is sendDataDurationSec (app schema). Subscribe streams for 10 s;
    # the unsubscribe keeps interval 0 (the HW-verified cancel) unchanged.
    assert pb._FLOW_SUBSCRIBE_PB == bytes.fromhex("ca030508e807100a")
    assert pb._FLOW_UNSUBSCRIBE_PB == bytes.fromhex("ca030408001002")
    unsub = rx.pb_parse(rx._pb_field(rx.pb_parse(pb._FLOW_UNSUBSCRIBE_PB), 57))
    assert rx._pb_field(unsub, 1) == 0


def test_read_flow_noop_on_flowless_device():
    # The XD must never be poked with #57 — it has no flow path.
    xd = object.__new__(BHyveHT34ADevice)
    xd.mac = "AA:BB:CC:DD:EE:FF"
    xd.state = DeviceState()
    xd.connection = _FakeConn(xd)
    asyncio.run(xd.read_flow())
    assert xd.connection.sent == []


def test_gpm_from_samples_computes_rate():
    # 144 counts over 4.45 s = 32.4 counts/s → ~4.49 gpm at 433 counts/gal
    # (matches the measured BTValve01 calibration run).
    gpm = pb._gpm_from_samples([(0.0, 0), (4.45, 144)], 433)
    assert 4.4 <= gpm <= 4.6


def test_gpm_from_samples_scales_with_calibration():
    # Halving counts-per-gallon doubles the reported gpm (the option is honoured).
    base = pb._gpm_from_samples([(0.0, 0), (4.0, 200)], 400)
    half = pb._gpm_from_samples([(0.0, 0), (4.0, 200)], 200)
    assert round(half, 2) == round(base * 2, 2)


def test_gpm_from_samples_zero_when_no_advance():
    assert pb._gpm_from_samples([(0.0, 100), (4.0, 100)], 433) == 0.0  # counter flat
    assert pb._gpm_from_samples([(0.0, 100)], 433) == 0.0              # one sample
    assert pb._gpm_from_samples([], 433) == 0.0                         # no samples


class _BleErrorConn(_FakeConn):
    """A link that drops mid-write (proxy teardown after a CTR self-heal)."""

    async def send(self, frame: bytes, drain_ms: int = 1500):
        from bleak.exc import BleakError
        raise BleakError("Not connected")


@pytest.mark.parametrize("method, args", [("set_rain_delay", (60,)), ("clear_rain_delay", ())])
def test_rain_delay_writes_return_false_on_ble_error(method, args):
    # set/clear_rain_delay must carry _ble_write_guard like every other protobuf
    # write: a transient BleakError returns False (the entity raises a clean
    # HomeAssistantError from that) instead of surfacing a raw exception.
    dev = _make_device(is_watering=False)
    dev.connection = _BleErrorConn(dev)
    assert asyncio.run(getattr(dev, method)(*args)) is False
    assert dev.connection.disconnects >= 1


class _DesyncThenFreshConn:
    """First flow subscribe decodes to nothing (simulates a desynced RX counter);
    after a disconnect (fresh handshake) the #59 decodes cleanly."""

    def __init__(self, device):
        self.device = device
        self.sent: list[bytes] = []
        self.disconnects = 0
        self._fresh = False

    async def send(self, frame: bytes, drain_ms: int = 1500):
        self.sent.append(frame)
        if self._fresh:
            self.device._observe_plaintext(_flow_frame(active=1, total=500))
        return [b"\x01"]

    async def disconnect(self):
        self.disconnects += 1
        self._fresh = True

    @property
    def is_connected(self):
        return True


def test_read_flow_resyncs_after_desynced_session():
    # No decodable #59 on the pooled session → read_flow drops the connection and
    # retries once on a fresh handshake, which decodes (regression for "Check flow
    # only updated at the next poll").
    dev = _make_device(is_watering=True)
    dev.connection = _DesyncThenFreshConn(dev)
    asyncio.run(dev.read_flow())
    assert dev.connection.disconnects == 1
    assert dev.state.flow_gpm == 0.0  # a decodable #59 (flow 0 here) was committed
    n_sub = dev.connection.sent.count(pb._build_message(pb._FLOW_SUBSCRIBE_PB))
    assert n_sub == 2  # both attempts subscribed exactly once


def test_read_flow_no_reconnect_when_healthy():
    # A healthy session decodes #59 on the first pass → no wasteful reconnect.
    dev = _make_device(is_watering=True)
    dev.connection = _FakeConn(dev, on_status=None, on_command=_flow_frame(active=1, total=500))
    asyncio.run(dev.read_flow())
    assert dev.connection.disconnects == 0
    assert dev.state.flow_gpm == 0.0


def test_read_flow_always_unsubscribes():
    # read_flow must cancel the stream (#57{#1=0}) as its last act, so it never
    # leaves a persistent #59 subscription that starves the next poll's #16 read.
    dev = _make_device(is_watering=True)
    dev.connection = _FakeConn(dev, on_status=None, on_command=_flow_frame(active=1, total=500))
    asyncio.run(dev.read_flow())
    assert dev.connection.sent[-1] == pb._build_message(pb._FLOW_UNSUBSCRIBE_PB)


class _FlowCancelConn:
    """A flow connection that never feeds samples, so read_flow parks in its
    sample-sleep loop and can be cancelled there — exposing what the finally does
    under an HA-restart-style cancellation. Tracks reconnects and sends."""

    def __init__(self):
        self.sent: list[bytes] = []
        self.ensure_connected_calls = 0
        self.disconnects = 0
        self._connected = True

    async def send(self, frame: bytes, drain_ms: int = 1500):
        self.sent.append(frame)
        return [b"\x01"]

    async def ensure_connected(self):
        self.ensure_connected_calls += 1
        self._connected = True

    async def disconnect(self):
        self.disconnects += 1
        self._connected = False

    @property
    def is_connected(self):
        return self._connected


def test_read_flow_cancelled_does_not_reconnect():
    # An HA restart/unload can cancel read_flow mid-sample. Its finally must NOT
    # reconnect (that races the teardown and fights the device's single-session
    # limit) — it only unsubscribes if the session is still up, and the shielded
    # write still lands so no #59 stream is left behind.
    dev = _make_device(is_watering=True)
    conn = _FlowCancelConn()
    dev.connection = conn

    async def run():
        task = asyncio.create_task(dev.read_flow())
        await asyncio.sleep(0.05)  # let it reach the sample sleep
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert conn.ensure_connected_calls == 0  # never reconnected from the finally
    assert conn.sent[-1] == pb._build_message(pb._FLOW_UNSUBSCRIBE_PB)  # still unsubscribed


def test_refresh_state_subscribes_flow_while_watering():
    # A Gen2 poll that finds a run in progress should also spot-check flow so the
    # gauge tracks live: the #57 subscribe is issued and flow_gpm is computed.
    dev = _make_device(is_watering=False)
    dev.connection = _FakeConn(
        dev, on_status=_status_frame(4), on_command=_flow_frame(active=1, total=489)
    )
    state = asyncio.run(dev.refresh_state())
    assert state.is_watering is True
    assert pb._build_message(pb._FLOW_SUBSCRIBE_PB) in dev.connection.sent
    assert state.flow_gpm is not None  # a numeric gauge value was set


def test_refresh_state_skips_flow_when_idle():
    # No run -> no flow subscribe (don't wake the radio for nothing).
    dev = _make_device(is_watering=False)
    dev.connection = _FakeConn(dev, on_status=_status_frame(1), on_command=None)
    asyncio.run(dev.refresh_state())
    assert pb._build_message(pb._FLOW_SUBSCRIBE_PB) not in dev.connection.sent


# --- event-driven drain (connection.py) -----------------------------------

def _make_conn() -> BHyveBleConnection:
    return BHyveBleConnection(None, "AA:BB:CC:DD:EE:FF", "00" * 16)


def test_on_notify_drops_duplicate_redelivery():
    # A re-delivered (byte-identical) frame must not be decrypted again — doing
    # so would advance the RX counter and desync the CTR stream. It's buffered
    # once; the duplicate is dropped.
    conn = _make_conn()
    frame = b"\x11\x02\x00\x00"
    conn._on_notify(None, frame)
    conn._on_notify(None, frame)
    assert conn._notif_buf == [frame]


def test_drain_returns_at_cap_when_silent():
    # No reply -> _drain waits out the (short) drain_ms cap and returns.
    conn = _make_conn()

    async def run():
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await conn._drain(120)  # 120ms hard cap, no frames delivered
        return loop.time() - t0

    elapsed = asyncio.run(run())
    assert 0.10 <= elapsed < 0.30


def test_drain_returns_early_after_reply_goes_quiet():
    # A frame at 20ms should let _drain return ~one quiet window later, far
    # short of the 2s cap — this is the latency win over a fixed sleep.
    conn = _make_conn()

    async def run():
        loop = asyncio.get_running_loop()

        async def feed():
            await asyncio.sleep(0.02)
            conn._notif_buf.append(b"\x11\x02\x00\x00")
            conn._notif_event.set()

        t0 = loop.time()
        task = asyncio.create_task(feed())
        await conn._drain(2000)
        elapsed = loop.time() - t0
        await task
        return elapsed

    elapsed = asyncio.run(run())
    assert elapsed < 1.0            # returned well before the 2s cap
    assert conn._notif_buf         # the frame is retained for the caller


def test_device_state_health_metrics():
    state = DeviceState()
    assert state.last_successful_poll is None
    assert state.consecutive_timeouts == 0
    now = datetime.now(timezone.utc)
    state.last_successful_poll = now
    state.consecutive_timeouts += 1
    assert state.last_successful_poll == now
    assert state.consecutive_timeouts == 1


def test_configurable_gatt_settle_ms():
    assert BHyveHT25G2Device.GATT_SETTLE_MS == 300
    assert BHyveHT34ADevice.GATT_SETTLE_MS == 300


# --- Connected = poll reachability (A1) ------------------------------------

class _RaisingConn:
    """A connection whose status send always fails, to exercise the poll-failure
    path in refresh_state."""

    def __init__(self):
        self.disconnects = 0

    async def send(self, frame: bytes, drain_ms: int = 1500):
        raise RuntimeError("boom")

    async def disconnect(self):
        self.disconnects += 1

    @property
    def is_connected(self):
        return False


def test_connected_true_on_successful_poll():
    # A poll that reads a clean #16 marks the device reachable and resets timeouts.
    dev = _make_device(is_watering=False, consecutive_timeouts=3)
    dev.connection = _FakeConn(dev, on_status=_status_frame(1), on_command=None)
    state = asyncio.run(dev.refresh_state())
    assert state.is_connected is True
    assert state.consecutive_timeouts == 0
    assert dev.connection.disconnects >= 1  # ephemeral teardown


def test_connected_false_on_failed_poll():
    # A poll whose status read raises marks the device unreachable (not a stale
    # "connected"), increments the timeout counter, and still disconnects cleanly.
    dev = _make_device(is_watering=False, is_connected=True)
    dev.connection = _RaisingConn()
    state = asyncio.run(dev.refresh_state())
    assert state.is_connected is False
    assert state.consecutive_timeouts == 1
    assert dev.connection.disconnects == 1


# --- no-status polls aren't phantom successes; over-the-air wedge recovery ----

def test_poll_without_status_is_not_a_success():
    # A poll that CONNECTS but decodes no #16 (a wedge recovery couldn't clear
    # this cycle) must NOT stamp a phantom "successful poll" — that false success
    # is exactly what masked frozen battery on the stuck valves. It counts as a
    # timeout so the diagnostic sensors surface it.
    dev = _make_device(is_watering=False, consecutive_timeouts=2)
    dev.connection = _FakeConn(dev, on_status=None, on_command=None)  # device silent
    state = asyncio.run(dev.refresh_state())
    assert state.last_successful_poll is None
    assert state.consecutive_timeouts == 3
    assert state.is_connected is True  # reached the device, just got no data


class _IgnoresStatusQueryConn(_FakeConn):
    """Models a unit (BTValve04) that ignores the passive #15 getDeviceStatus
    query outright but DOES echo a full #16 in reply to a setRainDelay (#17)
    write — the on_command frame is fed only for the rain-delay no-op."""

    async def send(self, frame: bytes, drain_ms: int = 1500):
        self.sent.append(frame)
        rd_noop = pb._build_message(pb._build_rain_delay_pb(0, None))
        if frame == rd_noop and self.on_command is not None:
            self.device._observe_plaintext(self.on_command)
        return [b"\x01"]


def test_recovers_status_via_rain_delay_noop_when_15_ignored():
    # When #15 is ignored, recovery falls back to a benign #17 no-op that echoes
    # the #16 status — turning a phantom-failed poll into a real success.
    dev = _make_device(is_watering=False)
    dev.connection = _IgnoresStatusQueryConn(dev, on_status=None, on_command=_status_frame(1))
    state = asyncio.run(dev.refresh_state())
    assert getattr(dev, "_status_parsed", False) is True
    assert state.last_successful_poll is not None            # now a genuine success
    assert pb._build_message(pb._build_rain_delay_pb(0, None)) in dev.connection.sent


def test_status_recovery_noop_never_wipes_active_rain_delay():
    # The #16-eliciting write must RE-ASSERT an active rain delay (a no-op), never
    # send a bare clear that would silently cancel it.
    ends = datetime.now(timezone.utc) + timedelta(hours=1)
    dev = _make_device(is_watering=False, rain_delay_minutes=60, rain_delay_ends=ends)
    frame = pb._build_message(dev._noop_rain_delay_pb())
    rd = rx.pb_parse(rx._pb_field(rx.pb_parse(rx.decode_inner(frame)), 17))
    assert rx._pb_field(rd, rx.RX_F_RD_MINUTES) == 60
    assert rx._pb_field(rd, rx.RX_F_RD_EXPIRY) == int(ends.timestamp())


# --- multi-frame-safe CTR self-heal (A2) -----------------------------------

class _FakeHass:
    def __init__(self):
        self.jobs: list = []

    def add_job(self, target, *args):
        self.jobs.append(target)


def _handshaken_conn(reply_header: bytes | None = rx.MSG_HEADER) -> tuple[BHyveBleConnection, _FakeHass]:
    # Default to a protobuf connection (header-based self-heal enabled); pass
    # reply_header=None for the mesh path, which has no header check.
    conn = _make_conn()
    conn._reply_header = reply_header
    conn._handshaken = True
    conn._iv = b"\x00" * 12
    conn._rx_ctr = 0
    hass = _FakeHass()
    conn.hass = hass
    return conn, hass


def _frame_decrypting_to(conn: BHyveBleConnection, pt: bytes, ctr: int) -> bytes:
    """Build an RX frame that conn.decrypt() (at rx_ctr==ctr) recovers to `pt`."""
    ks, _ = conn._aes_keystream(ctr, (len(pt) + 15) // 16)
    ct = bytes(b ^ k for b, k in zip(pt, ks[: len(pt)]))
    return bytes([conn._frame_magic, len(ct)]) + ct + b"\x00\x00"


def test_ctr_selfheal_on_bad_first_frame():
    # A garbage first frame (bad inner header) is a real CTR desync → self-heal.
    conn, hass = _handshaken_conn()
    frame = _frame_decrypting_to(conn, b"\xd0\x18\x29\x07deadbeef", 0)
    conn._on_notify(None, frame)
    assert len(hass.jobs) == 1  # disconnect scheduled


def test_ctr_selfheal_ignores_bad_continuation_frame():
    # A valid header frame first, then a non-header CONTINUATION frame (as a long
    # CTR-streamed reply would produce). The continuation must NOT trip the
    # self-heal — only the reply's first frame is required to carry the header.
    conn, hass = _handshaken_conn()
    good = _frame_decrypting_to(conn, rx.MSG_HEADER + b"\x00" * 8, 0)
    conn._on_notify(None, good)             # first frame: header ok, buffered
    cont = _frame_decrypting_to(conn, b"\x11\x22\x33\x44moredata", conn._rx_ctr)
    conn._on_notify(None, cont)             # continuation: bad header, not first
    assert hass.jobs == []                  # no disconnect scheduled


def test_selfheal_disconnect_never_orphans_a_link_opened_while_it_waits():
    # The self-heal disconnect runs unlocked. If an open lands while it is waiting
    # on the old link, the new client must survive it - nulling it without closing
    # it leaves the device's one BLE connection held by a link nothing references.
    conn = _make_conn()

    class _Client:
        def __init__(self):
            self.closed = False
            self.gate = asyncio.Event()

        async def stop_notify(self, *_a):
            pass

        async def disconnect(self):
            await self.gate.wait()
            self.closed = True

    old, new = _Client(), _Client()

    async def go():
        conn._client, conn._handshaken, conn._iv = old, True, b"\x00" * 12
        heal = asyncio.create_task(conn.disconnect())
        await asyncio.sleep(0)               # heal is now waiting on the old link
        assert conn._client is None and not conn._handshaken
        conn._client, conn._handshaken, conn._iv = new, True, b"\x01" * 12   # a reopen
        old.gate.set()
        await heal
    asyncio.run(go())
    assert old.closed, "the old link was never closed"
    assert conn._client is new and conn._handshaken and conn._iv == b"\x01" * 12, \
        "the disconnect nulled a session opened after it started"


def test_ctr_selfheal_skipped_for_mesh_replies():
    # Regression (ljmerza PR #24 feedback): mesh d7-47 replies use a
    # [mesh:2][type][seq][routing] shape, NOT the protobuf inner-message header,
    # so a mesh connection (reply_header=None) must NOT misclassify its first
    # bind-step reply as a CTR desync and schedule a disconnect mid-handshake.
    conn, hass = _handshaken_conn(reply_header=None)
    mesh_reply = _frame_decrypting_to(conn, bytes.fromhex("d747c10540") + b"\x00" * 6, 0)
    conn._on_notify(None, mesh_reply)
    assert hass.jobs == []                  # no disconnect scheduled


def test_encrypt_raises_cleanly_when_session_cleared():
    # A disconnect scheduled mid-handshake nulls _iv; encrypt() must raise a
    # typed BleHandshakeError (open-retry catches it), not a TypeError from the
    # keystream (the crash ljmerza hit on the mesh path).
    from orbit_bhyve.connection import BleHandshakeError

    conn = _make_conn()
    conn._iv = None
    with pytest.raises(BleHandshakeError):
        conn.encrypt(b"\x00\x01\x02\x03")


# --- connect/handshake retry is single-level (connection.py) ---------------

def test_ensure_connected_does_not_multiply_retries(monkeypatch):
    # Regression guard: ensure_connected must NOT wrap _open in a second retry
    # loop. _open already retries OPEN_MAX_ATTEMPTS times; a nested loop made it
    # 3x3=9 connect+handshake attempts (~90-150s on a stalling device). A fully
    # failing open must attempt connect exactly OPEN_MAX_ATTEMPTS times, not N^2.
    import sys
    import types as _types

    from orbit_bhyve import connection as conn_mod
    from orbit_bhyve.connection import BleHandshakeError, OPEN_MAX_ATTEMPTS

    # Stub the function-local HA bluetooth import so _open resolves a device.
    for name in ("homeassistant", "homeassistant.components",
                 "homeassistant.components.bluetooth"):
        monkeypatch.setitem(sys.modules, name, _types.ModuleType(name))
    monkeypatch.setattr(
        sys.modules["homeassistant.components.bluetooth"],
        "async_ble_device_from_address",
        lambda *a, **k: object(),  # any non-None "in range" device
        raising=False,
    )

    calls = {"connect": 0}

    class _FakeClient:
        is_connected = True

        async def disconnect(self):
            pass

        async def stop_notify(self, *_a):
            pass

    async def _fake_establish(*_a, **_k):
        calls["connect"] += 1
        return _FakeClient()

    monkeypatch.setattr(conn_mod, "establish_connection", _fake_establish)
    # No real gatt-settle / inter-attempt backoff sleeps — keep the test instant.
    async def _no_sleep(*_a, **_k):
        pass
    monkeypatch.setattr(conn_mod.asyncio, "sleep", _no_sleep)

    conn = _make_conn()

    async def _always_fail_handshake():
        raise BleHandshakeError("forced handshake failure")
    monkeypatch.setattr(conn, "_handshake", _always_fail_handshake)

    with pytest.raises(BleHandshakeError):
        asyncio.run(conn.ensure_connected())

    assert calls["connect"] == OPEN_MAX_ATTEMPTS  # 3, never 9


def test_a_missing_characteristic_clears_the_gatt_cache_before_the_retry(monkeypatch):
    # HA reuses a device's cached GATT table on every connect. If that table lacks
    # our characteristic, only clearing it lets the next attempt re-discover; any
    # other failure leaves the cache alone.
    import sys
    import types as _types

    from bleak.exc import BleakCharacteristicNotFoundError

    from orbit_bhyve import connection as conn_mod

    for name in ("homeassistant", "homeassistant.components",
                 "homeassistant.components.bluetooth"):
        monkeypatch.setitem(sys.modules, name, _types.ModuleType(name))
    monkeypatch.setattr(sys.modules["homeassistant.components.bluetooth"],
                        "async_ble_device_from_address", lambda *a, **k: object(),
                        raising=False)

    cleared = []

    class _Client:
        is_connected = True

        async def clear_cache(self):
            cleared.append(self)
            return True

        async def disconnect(self):
            pass

        async def stop_notify(self, *_a):
            pass

    async def _establish(*_a, **_k):
        return _Client()

    async def _no_sleep(*_a, **_k):
        pass

    monkeypatch.setattr(conn_mod, "establish_connection", _establish)
    monkeypatch.setattr(conn_mod.asyncio, "sleep", _no_sleep)

    conn = _make_conn()
    tries = {"n": 0}

    async def _handshake():
        tries["n"] += 1
        if tries["n"] == 1:
            raise BleakCharacteristicNotFoundError("00006c73-fe32-4f58-8b78-98e42b2c047f")
        if tries["n"] == 2:
            raise conn_mod.BleHandshakeError("a plain handshake failure")
        conn._handshaken = True
    monkeypatch.setattr(conn, "_handshake", _handshake)

    asyncio.run(conn.ensure_connected())
    assert tries["n"] == 3
    assert len(cleared) == 1, "only the missing-characteristic failure clears the cache"


# --- stale pooled session self-heals on actuation (connection.py) ----------

def test_send_actuation_reopens_when_pooled_bind_refresh_fails():
    # Regression (ljmerza PR #24 hardware feedback): over an ESPHome proxy a
    # pooled session can be dead at the GATT layer while is_connected still reads
    # True. send_actuation's in-place bind refresh then fails with BleakError
    # ("Not connected") — which must NOT propagate to the caller (it surfaced as
    # a failed close_valve service call in HA). Instead send_actuation drops the
    # stale session and reopens a fresh one, then completes the command write.
    from bleak.exc import BleakError

    conn = _make_conn()
    conn._handshaken = True

    class _StaleClient:
        is_connected = True  # is_connected property → True → pooled-refresh branch

        async def disconnect(self):
            pass

    conn._client = _StaleClient()

    calls = {"hook": 0, "disconnect": 0, "ensure_connected": 0, "writes": []}

    async def _hook(_c):
        calls["hook"] += 1
        if calls["hook"] == 1:
            # Stale pooled link: the first bind-refresh write fails at the proxy.
            raise BleakError("Bluetooth GATT Error ... Not connected")

    async def _disconnect():
        calls["disconnect"] += 1

    async def _ensure_connected():
        calls["ensure_connected"] += 1

    async def _write_locked(pt):
        calls["writes"].append(pt)

    async def _drain(_ms):
        pass

    conn.set_post_handshake_hook(_hook)
    conn.disconnect = _disconnect
    conn.ensure_connected = _ensure_connected
    conn._write_locked = _write_locked
    conn._drain = _drain
    conn._arm_idle_timer = lambda: None

    # Must not raise, even though the pooled bind refresh failed.
    result = asyncio.run(conn.send_actuation(b"\xaa\xbb", drain_ms=1))

    assert calls["disconnect"] == 1          # stale session dropped
    assert calls["ensure_connected"] == 1    # fresh session reopened
    assert calls["writes"] == [b"\xaa\xbb"]  # command still written after recovery
    assert result == []


# --- Sync button forces a connect on mesh (regression) ---------------------

class _SyncConn:
    """Minimal connection that counts connects/disconnects for the Sync test."""

    def __init__(self):
        self.ensure_connected_calls = 0
        self.disconnects = 0
        self._connected = False
        self.sent: list[bytes] = []

    async def ensure_connected(self):
        self.ensure_connected_calls += 1
        self._connected = True

    async def send(self, plaintext, *, drain_ms=1500):
        self.sent.append(plaintext)
        return []

    async def disconnect(self):
        self.disconnects += 1
        self._connected = False

    @property
    def is_connected(self):
        return self._connected


def test_sync_button_forces_connect_on_mesh():
    # Regression (ljmerza PR #24 feedback): #24 dropped the forced connect from
    # the Sync button and routed it through refresh_state, which on mesh is
    # passive (no BLE) — so the button became a no-op there (never refreshed
    # battery/state). async_manual_sync must force a fresh connect (the 7-step
    # init that refreshes battery/state via _observe_plaintext) and tear it down.
    dev = object.__new__(BHyveHT25Device)  # bypass HA-heavy __init__
    dev.mac = "44:67:55:52:94:A1"
    dev.mesh_device_id = 0x8A5B
    dev.state = DeviceState()
    conn = _SyncConn()
    dev.connection = conn
    asyncio.run(dev.async_manual_sync())
    assert conn.ensure_connected_calls == 1   # forced a fresh connect
    assert conn.disconnects >= 1              # torn down afterwards (ephemeral)
    # ...and reads the last run (msg 12) and the button run time (msg 7).
    assert [f.hex() for f in conn.sent] == ["5b8a1c0c4000", "5b8a17074000"]


def test_manual_sync_is_noop_for_protobuf():
    # Protobuf refresh_state connects on its own (#15 read), so the base hook
    # must NOT add a second connect on the Sync button.
    dev = _make_device(is_watering=False)
    conn = _SyncConn()
    dev.connection = conn
    asyncio.run(dev.async_manual_sync())
    assert conn.ensure_connected_calls == 0


# --- event-driven mesh connectivity (Connected sensor) ---------------------

def test_manual_sync_marks_connected_on_success():
    # A Sync that reaches the device must flip the Connected diagnostic on and
    # stamp last_successful_poll / clear consecutive_timeouts. Event-driven,
    # because the ephemeral disconnect closes the socket before the coordinator
    # refresh runs — reading the live socket then would read False.
    dev = object.__new__(BHyveHT25Device)
    dev.mac = "44:67:55:52:94:A1"
    dev.mesh_device_id = 0x8A5B
    dev.state = DeviceState(consecutive_timeouts=3)
    dev.connection = _SyncConn()
    asyncio.run(dev.async_manual_sync())
    assert dev.state.is_connected is True
    assert dev.state.consecutive_timeouts == 0
    assert dev.state.last_successful_poll is not None


class _FailSyncConn:
    """A connection whose connect always fails (device unreachable)."""

    async def disconnect(self):
        pass

    async def ensure_connected(self):
        from bleak.exc import BleakError
        raise BleakError("Not connected")

    @property
    def is_connected(self):
        return False


def test_manual_sync_marks_unreachable_on_failure():
    # A Sync that can't reach the device flips Connected off and counts a timeout
    # (and must not raise — the button/service call stays healthy).
    dev = object.__new__(BHyveHT25Device)
    dev.mac = "44:67:55:52:94:A1"
    dev.state = DeviceState(is_connected=True, consecutive_timeouts=0)
    dev.connection = _FailSyncConn()
    asyncio.run(dev.async_manual_sync())
    assert dev.state.is_connected is False
    assert dev.state.consecutive_timeouts == 1


def test_mesh_passive_poll_does_not_clobber_connected():
    # The passive mesh poll must NOT overwrite event-driven connectivity with the
    # torn-down live socket (which reads False under the ephemeral model) — doing
    # so pinned the Connected sensor off right after a successful reach.
    dev = object.__new__(BHyveHT25Device)
    dev.mac = "44:67:55:52:94:A1"
    dev.state = DeviceState(is_connected=True)

    class _DownConn:
        @property
        def is_connected(self):
            return False   # socket torn down between operations

    dev.connection = _DownConn()
    asyncio.run(dev.refresh_state())
    assert dev.state.is_connected is True   # not clobbered


# --- HT25A-0001 fw0098: offMode stop, restart detection, host-timed runs ------
# Hardware facts (2026-09-14, see docs/ble-reliability-and-behavior.md "HT25A-0001
# fw0098 quirks"): the shared STOP frame (manualMode{}) (re)starts a 1800 s run
# on this unit, offMode stops it, and the requested runTimeSec is ignored.

from orbit_bhyve.devices.protobuf import _build_set_timer_mode_pb, _build_start_pb

_OFF_PB = _build_set_timer_mode_pb(0)
_AUTO_PB = _build_set_timer_mode_pb(1)


def _status_full(run_state: int, remaining: int | None = None, mode: int | None = None) -> bytes:
    """A #16 status with run-state, optional #6.#5 remaining and #2.#1 mode."""
    sub = pb._pb_field_varint(rx.RX_F_STATUS_MODE, run_state)
    if remaining is not None:
        sub += pb._pb_field_bytes(
            rx.RX_F_STATUS_PROGRESS, pb._pb_field_varint(rx.RX_F_PROGRESS_REMAINING, remaining)
        )
    if mode is not None:
        sub += pb._pb_field_bytes(rx.RX_F_STATUS_RUNECHO, pb._pb_field_varint(rx.RX_F_RUNECHO_MODE, mode))
    return pb._build_message(pb._pb_field_bytes(rx.RX_F_STATUS, sub))


def _inner(frame: bytes) -> bytes:
    return rx.decode_inner(frame) or b""


class _ScriptConn(_FakeConn):
    """_FakeConn whose status reply depends on which command frames were sent so
    far: `status_for(sent_inner_pbs)` returns the #16 plaintext to feed on the
    next status elicitor. Command frames get no reply (a bare ack)."""

    def __init__(self, device, status_for):
        super().__init__(device)
        self.status_for = status_for

    async def send(self, frame: bytes, drain_ms: int = 1500):
        self.sent.append(frame)
        if _is_status_elicitor(frame):
            pt = self.status_for(self.commands())
            if pt is not None:
                self.device._observe_plaintext(pt)
        return [b"\x01"]

    def commands(self) -> list[bytes]:
        # Only #14 timerMode frames (start / stop / offMode / autoMode); the
        # #57 flow-unsubscribe that refresh_status sends on has_flow devices
        # and the status elicitors are not actuation commands.
        return [_inner(f) for f in self.sent if _inner(f)[:1] == b"\x72"]


def _make_ht25a(**state_kwargs):
    dev = object.__new__(BHyveHT25ADevice)
    dev.mac = "AA:BB:CC:DD:EE:FF"
    dev.hardware = "HT25A-0001"
    dev.firmware = "0098"
    dev.state = DeviceState(**state_kwargs)
    dev.flow_counts_per_gallon = 433
    return dev


def test_ht25a_class_flags():
    assert issubclass(BHyveHT25ADevice, BHyveHT25G2Device)
    assert BHyveHT25ADevice.log_label == "HT25A"
    assert BHyveHT25ADevice.duration_honored is False
    dev = _make_ht25a()
    assert dev._stop_frames() == [_OFF_PB, _build_start_pb(0, 0)]
    assert pb._STOP_PB not in dev._stop_frames()


def test_ht25a_stop_sends_offmode_then_restores_automode():
    # Device was in autoMode (#16.#2.#1 = 1) before the stop: offMode stops the
    # run, then autoMode is re-sent so stored programs stay armed.
    dev = _make_ht25a(is_watering=True, active_zone=1, seconds_remaining=470, controller_mode=1)
    dev.connection = _ScriptConn(dev, lambda cmds: _status_full(0) if _OFF_PB in cmds else _status_full(4, 470))

    ok = asyncio.run(dev.stop_watering())

    assert ok is True
    assert dev.state.is_watering is False
    assert dev.state.duration_enforced_by_host is False
    cmds = dev.connection.commands()
    assert cmds == [_OFF_PB, _AUTO_PB]
    assert pb._STOP_PB not in cmds


def test_ht25a_stop_skips_automode_restore_when_not_auto():
    dev = _make_ht25a(is_watering=True, active_zone=1, seconds_remaining=470, controller_mode=2)
    dev.connection = _ScriptConn(dev, lambda cmds: _status_full(0) if _OFF_PB in cmds else _status_full(4, 470))

    assert asyncio.run(dev.stop_watering()) is True
    assert dev.connection.commands() == [_OFF_PB]


def test_ht25a_stop_falls_back_to_zero_run_once():
    # offMode not confirmed -> the zero-second run is tried once; still watering
    # -> False. No frame is ever sent twice.
    dev = _make_ht25a(is_watering=True, active_zone=1, seconds_remaining=470)
    dev.connection = _ScriptConn(dev, lambda cmds: _status_full(4, 469))

    ok = asyncio.run(dev.stop_watering())

    assert ok is False
    assert dev.connection.commands() == [_OFF_PB, _build_start_pb(0, 0)]
    assert dev.last_stop_error == "device still watering after stop"


def test_stop_restart_detected_escalates_to_offmode_and_never_resends():
    # Gen2 class on a unit that takes manualMode{} as a start: remaining jumps
    # 470 -> 1800 after the STOP. The STOP frame must not be sent again;
    # offMode is tried once and confirms idle.
    dev = _make_device(is_watering=True, active_zone=1, seconds_remaining=470)
    dev.hardware, dev.firmware = "HT25A-0001", "0098"

    def status_for(cmds):
        if _OFF_PB in cmds:
            return _status_full(0)
        if pb._STOP_PB in cmds:
            return _status_full(4, 1800)
        return _status_full(4, 470)

    dev.connection = _ScriptConn(dev, status_for)
    ok = asyncio.run(dev.stop_watering())

    assert ok is True
    cmds = dev.connection.commands()
    assert cmds.count(pb._STOP_PB) == 1
    assert cmds.count(_OFF_PB) == 1
    assert dev.state.is_watering is False


def test_stop_restart_still_watering_after_offmode_reports_error():
    dev = _make_device(is_watering=True, active_zone=1, seconds_remaining=470)
    dev.connection = _ScriptConn(dev, lambda cmds: _status_full(4, 1800) if cmds else _status_full(4, 470))

    ok = asyncio.run(dev.stop_watering())

    assert ok is False
    assert dev.last_stop_error == "STOP restarted the run"
    cmds = dev.connection.commands()
    assert cmds == [pb._STOP_PB, _OFF_PB]          # each frame exactly once
    assert dev.connection.disconnects == 2           # fresh session + finally


def test_stop_unconfirmed_never_resends_same_frame():
    # Still watering but counting down (no restart): escalate to offMode once
    # rather than repeating the STOP frame.
    dev = _make_device(is_watering=True, active_zone=1, seconds_remaining=470)
    remaining = iter([469, 468, 467])
    dev.connection = _ScriptConn(dev, lambda cmds: _status_full(4, next(remaining)))

    ok = asyncio.run(dev.stop_watering())

    assert ok is False
    assert dev.connection.commands() == [pb._STOP_PB, _OFF_PB]
    assert dev.last_stop_error == "device still watering after stop"


def test_restart_detection_with_unknown_previous_remaining():
    dev = _make_device(is_watering=True, seconds_remaining=None)
    dev.state.seconds_remaining = 1800
    assert dev._stop_restarted_run(None) is True
    dev.state.seconds_remaining = 400
    assert dev._stop_restarted_run(None) is False
    assert dev._stop_restarted_run(470) is False
    dev.state.seconds_remaining = 471
    assert dev._stop_restarted_run(470) is True
    dev.state.is_watering = False
    assert dev._stop_restarted_run(470) is False


def test_fw0111_stop_path_unchanged_when_confirmed():
    # A Gen2 fw0111 unit confirms idle after the shared STOP: no escalation,
    # no autoMode traffic.
    dev = _make_device(is_watering=True, active_zone=1, seconds_remaining=600, controller_mode=1)
    dev.connection = _ScriptConn(dev, lambda cmds: _status_full(1) if pb._STOP_PB in cmds else _status_full(4, 600))

    assert asyncio.run(dev.stop_watering()) is True
    assert dev.connection.commands() == [pb._STOP_PB]


# --- host-side duration enforcement -------------------------------------------

def _run_in_loop(coro):
    return asyncio.run(coro)


def _ht25a_running_conn(dev):
    """Device reports a 1800 s run after any START; idle after offMode."""
    def status_for(cmds):
        if cmds and cmds[-1] == _OFF_PB:
            return _status_full(0)
        if any(c[:2] == b"\x72\x0a" for c in cmds):   # a manual-run frame was sent
            return _status_full(4, 1796)
        return _status_full(0)
    return _ScriptConn(dev, status_for)


def test_host_timer_sends_stop_at_requested_duration(monkeypatch):
    monkeypatch.setattr(BHyveHT25ADevice, "_host_stop_delay", lambda self, d: 0.05)
    dev = _make_ht25a()
    dev.connection = _ht25a_running_conn(dev)

    async def scenario():
        ok = await dev.start_watering(1, 120)
        assert ok is True
        assert dev.state.duration_enforced_by_host is True
        assert dev._host_stop_handle is not None
        assert _OFF_PB not in dev.connection.commands()
        await asyncio.sleep(0.2)          # timer fires, stop task starts
        await dev._host_stop_task         # includes the 1 s solenoid settle
        return dev.connection.commands()

    cmds = _run_in_loop(scenario())
    assert cmds.count(_OFF_PB) == 1
    assert dev.state.is_watering is False
    assert dev.state.duration_enforced_by_host is False


def test_host_timer_not_armed_when_device_honours_duration():
    dev = _make_device()
    dev.connection = _ScriptConn(dev, lambda cmds: _status_full(4, 600) if cmds else _status_full(1))

    async def scenario():
        assert await dev.start_watering(1, 600) is True
        assert dev._host_stop_handle is None
        assert dev.state.duration_enforced_by_host is False

    _run_in_loop(scenario())


def test_host_timer_armed_on_any_class_when_remaining_mismatches(monkeypatch):
    # Even the generic Gen2 class times the run when the device reports a
    # remaining time > 60 s away from the request.
    monkeypatch.setattr(BHyveHT25G2Device, "_host_stop_delay", lambda self, d: 0.05)
    dev = _make_device()
    dev.connection = _ScriptConn(dev, lambda cmds: _status_full(4, 1800) if cmds else _status_full(1))

    async def scenario():
        assert await dev.start_watering(1, 120) is True
        assert dev.state.duration_enforced_by_host is True
        assert dev._host_stop_handle is not None
        dev._cancel_host_stop()

    _run_in_loop(scenario())


def test_host_timer_cancelled_by_explicit_stop(monkeypatch):
    monkeypatch.setattr(BHyveHT25ADevice, "_host_stop_delay", lambda self, d: 0.05)
    dev = _make_ht25a()
    dev.connection = _ht25a_running_conn(dev)

    async def scenario():
        await dev.start_watering(1, 120)
        assert await dev.stop_watering() is True
        assert dev._host_stop_handle is None
        await asyncio.sleep(0.3)
        return dev.connection.commands()

    cmds = _run_in_loop(scenario())
    assert cmds.count(_OFF_PB) == 1   # only the explicit stop, the timer never fired


def test_host_timer_rearmed_by_new_start(monkeypatch):
    monkeypatch.setattr(BHyveHT25ADevice, "_host_stop_delay", lambda self, d: 0.05)
    dev = _make_ht25a()
    dev.connection = _ht25a_running_conn(dev)

    async def scenario():
        await dev.start_watering(1, 120)
        first = dev._host_stop_handle
        await dev.start_watering(1, 120)
        assert dev._host_stop_handle is not first
        assert first.cancelled()
        await asyncio.sleep(0.2)
        await dev._host_stop_task
        return dev.connection.commands()

    cmds = _run_in_loop(scenario())
    assert cmds.count(_OFF_PB) == 1   # one timer fired for the second run only


def test_host_timer_cancelled_by_unload(monkeypatch):
    monkeypatch.setattr(BHyveHT25ADevice, "_host_stop_delay", lambda self, d: 0.05)
    dev = _make_ht25a()
    dev.connection = _ht25a_running_conn(dev)

    async def scenario():
        await dev.start_watering(1, 120)
        await dev.async_unload()
        assert dev._host_stop_handle is None
        await asyncio.sleep(0.3)
        return dev.connection.commands()

    cmds = _run_in_loop(scenario())
    assert _OFF_PB not in cmds


# --- valve entity surfaces start/stop failures ---------------------------------

def test_valve_close_and_open_raise_on_failure():
    # The valve entity needs Home Assistant; the bare `Tests` CI job doesn't install it.
    pytest.importorskip("homeassistant")
    from types import SimpleNamespace
    from homeassistant.exceptions import HomeAssistantError
    from orbit_bhyve.valve import BHyveZoneValve

    calls = {"refresh": 0}

    async def refresh():
        calls["refresh"] += 1

    class _Dev:
        name, mac, log_label = "Garden", "AA:BB:CC:DD:EE:FF", "HT25A"
        last_stop_error = "STOP restarted the run"
        stations = 1

        async def start_watering(self, station, duration):
            return False

        async def stop_watering(self, station=None):
            return False

    valve = object.__new__(BHyveZoneValve)
    valve.coordinator = SimpleNamespace(device=_Dev(), async_request_refresh=refresh, preferred_duration_sec=None)
    valve._station = 1
    valve._default_duration = 600

    with pytest.raises(HomeAssistantError, match="STOP restarted the run"):
        asyncio.run(valve.async_close_valve())
    with pytest.raises(HomeAssistantError, match="START not confirmed"):
        asyncio.run(valve.async_open_valve(duration=120))
    assert calls["refresh"] == 0

# --- Wi-Fi-capable models advertise on a different radio than the cloud reports ------

class _Info:
    def __init__(self, name, address):
        self.name = name
        self.address = address


def test_advertised_name_is_built_from_the_cloud_mac():
    # As advertised by a WT25G2 sprinkler controller.
    assert advertised_name_for("44:67:55:0A:1B:2C") == "bhyve_0A1B2C"
    assert advertised_name_for("4467550a1b2c") == "bhyve_0A1B2C"


def test_match_advertised_finds_the_ble_radio():
    """The cloud reports the WI-FI mac for a mains controller; the BLE radio has its
    own address and only the advertised name links them."""
    infos = [
        _Info("SomeoneElse", "AA:BB:CC:DD:EE:FF"),
        _Info("bhyve_0A1B2C", "44:67:55:1A:1B:2C"),   # differs in the 4th octet
    ]
    assert match_advertised("44:67:55:0A:1B:2C", infos) == "44:67:55:1A:1B:2C"


def test_match_advertised_is_case_insensitive():
    assert match_advertised("44:67:55:0A:1B:2C",
                            [_Info("BHYVE_0a1b2c", "44:67:55:1A:1B:2C")]) \
        == "44:67:55:1A:1B:2C"


def test_match_advertised_returns_none_rather_than_guessing():
    """No address offset is inferred: an unmatched name returns None."""
    infos = [_Info("bhyve_AAAAAA", "11:22:33:44:55:66"), _Info(None, "77:88:99:AA:BB:CC")]
    assert match_advertised("44:67:55:0A:1B:2C", infos) is None
    assert match_advertised("44:67:55:0A:1B:2C", []) is None


# --- MTU: the device rejects ATT long writes -----------------------------------------

class _FakeClient:
    def __init__(self, mtu):
        self.mtu_size = mtu
        self.written = []

    async def write_gatt_char(self, _uuid, data, response=True):
        self.written.append(bytes(data))


def _conn(mtu=23, split_writes=True):
    c = BHyveBleConnection(None, "44:67:55:0A:1B:2C", "00" * 16,
                           frame_magic=0x11, trailer_const=0x11,
                           split_writes=split_writes)
    c._client = _FakeClient(mtu)
    c._iv = b"\x00" * 12
    return c


def test_max_write_len_follows_the_negotiated_mtu():
    assert _conn(23).max_write_len == 20        # the WT25G2 negotiates MTU 23
    assert _conn(517).max_write_len == 514
    c = _conn(23)
    c._client = None                            # unknown link -> conservative
    assert c.max_write_len == 20


async def _write(c, pt):
    await c._write_locked(pt)


def test_a_long_message_is_split_across_frames():
    """A message bigger than one ATT write goes out as several frames, the same way
    the device sends long replies (a 28-byte #75 setEpochTime goes as 20B + 16B)."""
    c = _conn(23)                                   # 20-byte writes, 16-byte budget
    asyncio.run(_write(c, b"x" * 28))
    frames = c._client.written
    assert len(frames) == 2, "28 bytes must not go out as one oversize write"
    assert [len(f) for f in frames] == [20, 16]
    assert all(len(f) <= c.max_write_len for f in frames)
    # The CTR stream advances one block per frame, exactly as the RX side decrypts.
    assert c._tx_ctr == 2


def test_a_short_message_is_still_one_frame():
    c = _conn(23)
    asyncio.run(_write(c, b"x" * 10))
    assert len(c._client.written) == 1
    assert len(c._client.written[0]) == 14


def test_a_roomy_link_does_not_chunk():
    """Devices that negotiate a large MTU keep the single write; chunking follows
    the negotiated limit."""
    c = _conn(517)
    asyncio.run(_write(c, b"x" * 28))
    assert len(c._client.written) == 1


def test_a_link_without_split_writes_keeps_the_single_long_write():
    """Only the WT line splits. An HT link sends a 40-byte program write as ONE
    long write, as upstream does and has verified on HT hardware."""
    c = _conn(23, split_writes=False)
    asyncio.run(_write(c, b"x" * 40))
    assert len(c._client.written) == 1
    assert c._tx_ctr == 3


def test_only_the_wt_class_splits_writes():
    assert BHyveWT25G2Device.SPLIT_WRITES is True
    for cls in (BHyveHT25Device, BHyveHT25G2Device, BHyveHT25ADevice,
                BHyveHT25Fw0085Device, BHyveHT34ADevice):
        assert cls.SPLIT_WRITES is False, cls.__name__


def test_split_budget_leaves_room_for_the_frame_overhead():
    c = _conn(23)
    assert [len(p) for p in c._split(b"y" * 40)] == [16, 16, 8]
    assert c._split(b"") == [b""]


def test_a_partial_reply_does_not_survive_the_session():
    """A reply half-collected when the link drops must not be extended by the next
    session's frames: a header-less first frame after a reconnect is a CTR desync and
    has to reach the self-heal, not be appended to whatever was left behind."""
    c = BHyveBleConnection(None, "44:67:55:0A:1B:2C", "00" * 16,
                           frame_magic=0x11, trailer_const=0x11,
                           reply_header=b"\xaa\x77\x5a\x0f")
    c._iv = b"\x00" * 12
    c._tx_ctr = c._rx_ctr = 0
    c._handshaken = True
    c._on_notify(None, bytearray(c.encrypt(_WT_BURST[0])))
    assert len(c._assembly) == 16, "the first frame should be held for its continuations"
    asyncio.run(c.disconnect())
    assert c._assembly == bytearray(), "disconnect must drop the partial reply"


def test_a_truncated_reply_does_not_outlive_its_exchange():
    """An exchange whose reply is cut short must not leave a fragment that absorbs
    the next desync: three of a six-frame burst arrive during the drain, then a
    bad-header frame arrives after it and must reach the self-heal.
    """
    c = _conn(23)
    c._reply_header = _WT_BURST[0][:4]
    c._handshaken = True
    scheduled = []
    c.hass = types.SimpleNamespace(add_job=lambda *a: scheduled.append(a))

    def deliver(plaintext):
        # Encrypt on the RX counter, which is what _on_notify decrypts with.
        ct, _ = c._aes_xor(c._rx_ctr, plaintext)
        trailer = (sum(plaintext) + c._trailer_const + len(plaintext)) & 0xFFFF
        c._on_notify(None, bytearray(bytes([c._frame_magic, len(ct)]) + ct
                                     + struct.pack("<H", trailer)))

    async def exchange():
        async def feed_three():
            await asyncio.sleep(0)
            for f in _WT_BURST[:3]:
                deliver(f)
        async with c._lock:
            task = asyncio.ensure_future(feed_three())
            await c._write_and_drain(b"x" * 10, 30)
            await task

    asyncio.run(exchange())
    assert c._assembly == bytearray(), \
        f"a truncated reply outlived its exchange ({len(c._assembly)}B left)"
    deliver(b"\xde\xad\xbe\xef" + b"\x00" * 12)
    assert scheduled, "the stray frame was absorbed instead of tripping the self-heal"


def test_a_failed_split_write_closes_the_session():
    """A split write that raises partway leaves the TX counter ahead of the device,
    so the session is disconnected (not just marked stale: the device accepts one
    BLE connection). A failed single-frame write leaves the session open."""
    class _FailOnPiece:
        def __init__(self, n):
            self.mtu_size, self.n, self.fail_on, self.written = 23, 0, n, []
            self.disconnected = False

        async def write_gatt_char(self, *a, **k):
            self.n += 1
            if self.n == self.fail_on:
                raise RuntimeError("Not connected")
            self.written.append(bytes(a[1]))

        async def stop_notify(self, *a, **k):
            pass

        async def disconnect(self):
            self.disconnected = True

    c = _conn(23)
    client = _FailOnPiece(2)
    c._client, c._handshaken = client, True

    async def go():
        async with c._lock:
            await c._write_locked(b"z" * 40)          # 40B at MTU 23 -> 3 pieces
    with pytest.raises(RuntimeError):
        asyncio.run(go())
    assert client.disconnected, "the abandoned link was never closed"
    assert c._client is None and not c._handshaken, "the session was left usable"

    c2 = _conn(23)
    single = _FailOnPiece(1)
    c2._client, c2._handshaken = single, True

    async def go2():
        async with c2._lock:
            await c2._write_locked(b"z" * 10)         # one piece
    with pytest.raises(RuntimeError):
        asyncio.run(go2())
    assert c2._handshaken, "a single-frame failure must leave the session open"


def test_split_pieces_are_aes_block_aligned():
    """encrypt/decrypt advance the CTR counter by whole blocks per frame, so pieces
    are block-aligned. At MTU 128 a 126-byte #19 program write splits 112+14."""
    pieces = _conn(128)._split(b"y" * 126)
    assert [len(p) for p in pieces] == [112, 14]
    for mtu in (23, 69, 100, 128, 247, 517):
        c = _conn(mtu)
        sizes = [len(p) for p in c._split(b"y" * 600)]
        assert all(s % 16 == 0 for s in sizes[:-1]), f"MTU {mtu}: {sizes} not aligned"
        assert all(s + 4 <= c.max_write_len for s in sizes), f"MTU {mtu}: a piece cannot be framed"
        assert all(s <= 255 for s in sizes), f"MTU {mtu}: a piece exceeds the length field"


def test_a_frame_too_short_to_hold_a_header_is_not_a_desync():
    """A plaintext shorter than a header (the small ack ahead of #16) goes to the
    observer without a desync check; a short continuation (the burst's 3-byte last
    frame) is appended to the reply in flight."""
    seen, scheduled = [], []
    c = _conn(23)
    c._reply_header = _WT_BURST[0][:4]
    c._handshaken = True
    c.hass = types.SimpleNamespace(add_job=lambda *a: scheduled.append(a))
    c.set_plaintext_observer(seen.append)

    def deliver(plaintext):
        ct, _ = c._aes_xor(c._rx_ctr, plaintext)
        trailer = (sum(plaintext) + c._trailer_const + len(plaintext)) & 0xFFFF
        c._on_notify(None, bytearray(bytes([c._frame_magic, len(ct)]) + ct
                                     + struct.pack("<H", trailer)))

    deliver(b"\x01\x02")
    assert not scheduled, "a 2-byte frame has no header to be wrong about"
    assert seen == [b"\x01\x02"], "a short frame must reach the observer"

    for f in _WT_BURST:                      # the last frame is 3 bytes
        deliver(f)
    assert any(len(m) == 83 for m in seen), "a short continuation must still be appended"


class _FakeBluetooth(types.ModuleType):
    """Stand in for homeassistant.components.bluetooth.

    connection.py imports it inside _open, so a module in sys.modules is enough to
    drive the whole open path — the same trick conftest uses for the package itself.
    """

    def __init__(self, infos, by_address):
        super().__init__("homeassistant.components.bluetooth")
        self.infos, self.by_address, self.looked_up = infos, by_address, []

    def async_discovered_service_info(self, hass, connectable=True):
        return self.infos

    def async_ble_device_from_address(self, hass, address, connectable=True):
        self.looked_up.append(address)
        return self.by_address.get(address)


def _install_bluetooth(infos, by_address):
    ha = types.ModuleType("homeassistant"); ha.__path__ = []
    comp = types.ModuleType("homeassistant.components"); comp.__path__ = []
    bt = _FakeBluetooth(infos, by_address)
    sys.modules.update({"homeassistant": ha, "homeassistant.components": comp,
                        "homeassistant.components.bluetooth": bt})
    return bt


class _Advert:
    def __init__(self, name, address):
        self.name, self.address = name, address


def test_open_falls_back_to_the_advertised_name_and_caches_it(monkeypatch):
    """A WT controller's cloud record carries its Wi-Fi MAC, so the direct lookup
    finds nothing; the BLE address is found by advertised name and cached for later
    opens.
    """
    import orbit_bhyve.connection as mod
    device = object()
    bt = _install_bluetooth([_Advert("bhyve_0A1B2C", "44:67:55:1A:1B:2C")],
                            {"44:67:55:1A:1B:2C": device})
    reached = {}

    async def _stop_at_connect(cls, ble_device, name, max_attempts=3):
        reached["device"] = ble_device
        raise RuntimeError("far enough")

    monkeypatch.setattr(mod, "establish_connection", _stop_at_connect)
    monkeypatch.setattr(mod, "OPEN_MAX_ATTEMPTS", 1)
    c = _conn(23)
    c._client = None

    with pytest.raises(Exception):
        asyncio.run(c._open())
    assert reached.get("device") is device, "the fallback never reached the connect call"
    assert bt.looked_up[0] == "44:67:55:0A:1B:2C", "the cloud MAC must be tried first"
    assert c._ble_address == "44:67:55:1A:1B:2C", "the resolved address was not cached"

    bt.looked_up.clear()
    with pytest.raises(Exception):
        asyncio.run(c._open())
    assert bt.looked_up[0] == "44:67:55:1A:1B:2C", "a later open must use the cache"


def test_open_says_what_it_looked_for_when_nothing_advertises():
    """The error names the advertised name it looked for."""
    _install_bluetooth([_Advert("bhyve_AAAAAA", "11:22:33:44:55:66")], {})
    c = BHyveBleConnection(None, "44:67:55:0A:1B:2C", "00" * 16,
                           frame_magic=0x11, trailer_const=0x11)
    with pytest.raises(BleNotConnectable) as err:
        asyncio.run(c._open())
    assert "bhyve_0A1B2C" in str(err.value)


def test_the_handshake_does_not_depend_on_the_network_key():
    """The handshake derives the IV and both counters from the nonce exchange alone,
    so it succeeds against any B-Hyve. A wrong device is caught later by the desync
    self-heal, which also drops the resolved address."""
    import orbit_bhyve.connection as mod

    class _Nonce:
        async def start_notify(self, *a, **k): pass
        async def write_gatt_char(self, *a, **k): pass
        async def read_gatt_char(self, *a, **k):
            return bytearray(bytes([0x11, 0x22, 0x33, 0x44]) + b"\x00" * 16)

    saved, mod.os = mod.os, types.SimpleNamespace(urandom=lambda n: b"\x5a" * n)
    try:
        states = []
        for key in ("00" * 16, "ff" * 16):
            c = BHyveBleConnection(None, "44:67:55:0A:1B:2C", key,
                                   frame_magic=0x11, trailer_const=0x11)
            c._client = _Nonce()
            asyncio.run(c._handshake())
            states.append((c._handshaken, c._iv, c._tx_ctr, c._rx_ctr))
    finally:
        mod.os = saved
    assert states[0][0] and states[1][0], "the handshake should succeed with either key"
    assert states[0][1:] == states[1][1:], "the handshake must not depend on the key"


def test_a_desync_drops_a_resolved_address():
    """A desync is also what a wrong device looks like, so the self-heal drops the
    resolved address."""
    scheduled = []
    c = _conn(23)
    c._reply_header = b"\xaa\x77\x5a\x0f"
    c._handshaken = True
    c._ble_address = "44:67:55:1A:1B:2C"
    c.hass = types.SimpleNamespace(add_job=lambda *a: scheduled.append(a))
    ct, _ = c._aes_xor(c._rx_ctr, b"\xde\xad\xbe\xef" + b"\x00" * 12)
    pt = b"\xde\xad\xbe\xef" + b"\x00" * 12
    trailer = (sum(pt) + c._trailer_const + len(pt)) & 0xFFFF
    c._on_notify(None, bytearray(bytes([c._frame_magic, len(ct)]) + ct
                                 + struct.pack("<H", trailer)))
    assert scheduled, "the desync self-heal did not fire"
    assert c._ble_address is None, "a resolved address survived the self-heal"


def test_send_stream_and_send_actuation_really_reset_the_rx_state():
    """The fake client records what _assembly held at the moment the frame went out:
    a write must not carry a previous exchange's fragment into its reply. (The paths
    also reset after the drain, so checking state afterwards would not catch a
    missing pre-write reset.)
    """
    class _Recorder(_FakeClient):
        def __init__(self, conn, mtu=23):
            super().__init__(mtu)
            self.conn, self.assembly_at_write, self.is_connected = conn, None, True

        async def write_gatt_char(self, _uuid, data, response=True):
            self.assembly_at_write = bytes(self.conn._assembly)
            await super().write_gatt_char(_uuid, data, response)

    for name, call in (("send_stream", lambda c: c.send_stream(b"x" * 10, drain_ms=1)),
                       ("send_actuation", lambda c: c.send_actuation(b"x" * 10, drain_ms=1))):
        c = _conn(23)
        c._client = _Recorder(c)
        c._handshaken = True
        c._iv = b"\x00" * 12
        c._assembly = bytearray(b"left over from the last exchange")

        async def go():
            return await call(c)

        asyncio.run(go())
        assert c._client.assembly_at_write == b"", \
            f"{name} wrote while a previous exchange's fragment was still buffered"
        assert c._assembly == bytearray(), f"{name} left a fragment behind"


def test_reset_rx_moves_all_three_buffers_together():
    """The raw frames, their plaintexts and the partial inner message are one unit:
    reset two of the three and a stale fragment survives to be spliced onto a later
    reply, or to absorb the bad-header frame that should trip the CTR self-heal."""
    c = _conn(23)
    c._notif_buf.append(b"raw")
    c._notif_pt.append(b"plain")
    c._assembly = bytearray(b"partial")
    c._reset_rx()
    assert list(c._notif_buf) == [] and c._notif_pt == [] and c._assembly == bytearray()


def test_every_write_path_resets_the_rx_state():
    """Every write path on the connection resets the RX state first.

    Paths are discovered, not listed, so a new one is covered. `devices/ht25.py`'s
    post-handshake hook writes through `_write_locked` without a reset; that is safe
    only while its class has no `reply_header`, which the second half asserts."""
    import inspect
    paths = []
    for name, fn in vars(BHyveBleConnection).items():
        if not inspect.iscoroutinefunction(fn) or name == "_write_locked":
            continue
        body = inspect.getsource(fn)
        if "_write_locked(" in body:
            paths.append((name, body))
    assert {n for n, _ in paths} >= {"_write_and_drain", "send_stream", "send_actuation",
                                     "send_raw"}, f"discovery missed a known path: {paths}"
    for name, body in paths:
        assert "_reset_rx()" in body.split("_write_locked(")[0], \
            f"{name} writes without resetting the RX state first"

    # Writers outside the connection class are exempt only while their classes have
    # no effective (possibly inherited) reply_header.
    import importlib
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "orbit_bhyve"
    checked = 0
    for mod_path in sorted(root.rglob("*.py")):
        if mod_path.name == "connection.py" or "_write_locked(" not in mod_path.read_text():
            continue
        rel = mod_path.relative_to(root).with_suffix("")
        module = importlib.import_module("orbit_bhyve." + ".".join(rel.parts))
        for obj in vars(module).values():
            if not (inspect.isclass(obj) and obj.__module__ == module.__name__):
                continue
            checked += 1
            assert getattr(obj, "reply_header", None) is None, (
                f"{obj.__name__} writes through _write_locked directly AND has an "
                f"effective reply_header — it can accumulate a partial reply, so it "
                f"must reset the RX state like the connection's own paths")
    assert checked, "no out-of-class writer found — the exemption check is not running"


# --- multi-frame status reassembly (a WT25G2 #16 burst) -----------------------------

# A #16 status reply from a 12-station WT25G2, with the device address and clock
# replaced by placeholders (CRC re-stamped): 83 bytes as six CTR blocks of <=16
# bytes. Only the first carries the aa775a0f inner header.
_WT_BURST = [
    bytes.fromhex("aa775a0f4d000a064467550a1b2c1219"),
    bytes.fromhex("323032362d30312d30325430333a3034"),
    bytes.fromhex("3a30352b30303a303082012508011202"),
    bytes.fromhex("0801221d0a19323032362d30312d3032"),
    bytes.fromhex("5430343a30303a30302b30303a303010"),
    bytes.fromhex("0120ff"),
]


def test_a_single_frame_of_a_long_reply_does_not_decode():
    """decode_inner needs the whole message; the first frame alone returns None."""
    assert rx.decode_inner(_WT_BURST[0]) is None
    assert rx.decode_inner(b"".join(_WT_BURST)) is not None


def test_observer_gets_the_reassembled_message_not_the_fragments():
    """Through the notify path, the observer sees one complete message, not six
    fragments."""
    seen = []
    c = BHyveBleConnection(None, "44:67:55:0A:1B:2C", "00" * 16,
                           frame_magic=0x11, trailer_const=0x11,
                           reply_header=b"\xaa\x77\x5a\x0f")
    c._iv = b"\x00" * 12
    c._tx_ctr = c._rx_ctr = 0
    c._handshaken = True
    c.set_plaintext_observer(seen.append)
    for chunk in _WT_BURST:
        c._on_notify(None, bytearray(c.encrypt(chunk)))
    assert len(seen) == 1, f"expected one reassembled message, got {len(seen)}"
    assert len(seen[0]) == 83
    assert rx.decode_inner(seen[0]) is not None


def test_a_short_reply_still_completes_on_its_first_frame():
    """Devices whose status fits one frame must be unaffected by reassembly."""
    seen = []
    c = BHyveBleConnection(None, "44:67:55:0A:1B:2C", "00" * 16,
                           frame_magic=0x11, trailer_const=0x11,
                           reply_header=b"\xaa\x77\x5a\x0f")
    c._iv = b"\x00" * 12
    c._tx_ctr = c._rx_ctr = 0
    c._handshaken = True
    c.set_plaintext_observer(seen.append)
    short = _status_frame(1)
    c._on_notify(None, bytearray(c.encrypt(short)))
    assert len(seen) == 1
    assert seen[0] == short[:short[4] + 6]


def test_reassembled_status_actually_parses():
    """The reassembled burst parses into device state."""
    whole = b"".join(_WT_BURST)
    st = rx.extract_status(rx.decode_inner(whole))
    assert st.is_watering is False
    dev = _make_device(is_watering=True)          # opposite of the expected result
    dev._status_parsed = False                    # set by refresh_status in real use
    rx.apply_status_plaintext(dev, whole)
    assert dev._status_parsed is True
    assert dev.state.is_watering is False
