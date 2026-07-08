"""Watering-program protocol tests for the HA integration.

Covers the program builders (`devices/protobuf.py`) against hardware-verified
byte references, the multi-frame RX reassembly (`connection.send_stream` +
`status.split_inner_messages`/`parse_sync_dump`), and the program device
methods that drive the verified 3-write enable handshake. No hardware or Home
Assistant required.
"""
from __future__ import annotations

import asyncio

import pytest

from orbit_bhyve.connection import BHyveBleConnection
from orbit_bhyve.devices import protobuf as tx
from orbit_bhyve.devices import status as rx
from orbit_bhyve.devices.base import DeviceState, ProgramSpec, ProgramSummary
from orbit_bhyve.devices.ht25g2 import BHyveHT25G2Device


# --- builder byte references (W0 hardware-verified; same as the CLI) --------

@pytest.mark.parametrize(
    "pb,expected",
    [
        (tx._build_set_timer_mode_pb(1), "720408011200"),   # autoMode
        (tx._build_set_timer_mode_pb(0), "720408001200"),   # offMode
        (tx._build_set_timer_mode_pb(2), "720408021200"),   # manualMode (== stop)
        (tx._STOP_PB, "720408021200"),
        (tx._build_set_active_programs_pb(0), "a201020800"),
        (tx._build_set_active_programs_pb(8), "a201020808"),  # slot D bit
        (tx._build_sync_request_pb(), "5200"),                # #10 empty
        (tx._build_program_delete_pb(4), "9a010408041200"),   # #19{#1=4,#2 NotSet}
    ],
)
def test_ha_program_builder_byte_refs(pb, expected):
    assert pb.hex() == expected


def test_ha_timer_mode_requires_empty_manual_params_marker():
    # a #14 that omits the #2 `12 00` marker is silently ignored by the device
    assert tx._build_set_timer_mode_pb(1).endswith(bytes.fromhex("1200"))


def test_ha_active_programs_bitmask_is_one_shifted():
    for slot, bit in [(1, 1), (2, 2), (3, 4), (4, 8), (5, 16)]:
        assert bit == 1 << (slot - 1)
        pb = tx._build_set_active_programs_pb(bit)
        body = rx.pb_parse(pb)[0][2]
        assert rx._pb_field(rx.pb_parse(body), 1) == bit


# --- signed-tz epoch time (#75), hardware-verified byte reference -----------

def _ha_spec(*a, **kw):
    return ProgramSpec(*a, **kw)


def test_set_epoch_time_signed_tz_byte_ref():
    # #75 setEpochTime { #1 epoch, #2 tzOffsetSec } — the coordinator poll's
    # elicitor. A negative tz offset must encode as a signed varint (EDT = -4h).
    assert (
        tx._build_set_epoch_time_pb(epoch_utc=1783317742, tz_offset_sec=-14400).hex()
        == "da041108ee89add20610c08fffffffffffffff01"
    )


# --- #19 build -> parse round-trips (HA decoder) ----------------------------

def _body(pb19):
    return rx.pb_parse(pb19)[0][2]


def test_ha_weekdays_multizone_multistart_round_trip():
    spec = _ha_spec(slot=4, day_mode="weekdays", weekday_mask=0x7F,
                    start_mins=(360, 1080), zones=((0, 300), (1, 420)), name="W0TestD")
    sch = rx.parse_program_body(_body(tx._build_program_pb(spec)))
    assert sch.slot == 4 and not sch.empty
    assert sch.day_mode == "weekdays" and sch.weekday_mask == 0x7F
    assert sch.start_mins == (360, 1080)
    assert sch.zones == ((0, 300), (1, 420))
    assert sch.name == "W0TestD" and sch.budget == 100


def test_ha_interval_round_trip_emits_anchor_iso():
    spec = _ha_spec(slot=2, day_mode="interval", interval_days=3,
                    interval_anchor="2026-06-28T00:00:00-04:00",
                    start_mins=(360,), zones=((0, 600),), name="Drip")
    sch = rx.parse_program_body(_body(tx._build_program_pb(spec)))
    assert sch.day_mode == "interval" and sch.interval_days == 3
    assert sch.interval_anchor.startswith("2026-06-28")
    assert sch.start_mins == (360,) and sch.zones == ((0, 600),)


@pytest.mark.parametrize("mode,field", [("odd", 5), ("even", 6)])
def test_ha_odd_even_empty_markers(mode, field):
    spec = _ha_spec(slot=1, day_mode=mode, start_mins=(360,), zones=((0, 60),), name="x")
    body = _body(tx._build_program_pb(spec))
    assert rx._pb_field(rx.pb_parse(body), field) == b""
    assert rx.parse_program_body(body).day_mode == mode


def test_ha_delete_produces_notset_and_parses_empty():
    sch = rx.parse_program_body(_body(tx._build_program_delete_pb(1)))
    assert sch.slot == 1 and sch.empty


def test_ha_parse_decodes_packed_single_start_hw_shape():
    # the exact bytes BTValve03 echoed for `--start 00:55` (start_min 55 = 0x37).
    body = bytes.fromhex("08061a02087f4201374a040800103c50648a0107434c4954455354")
    sch = rx.parse_program_body(body)
    assert sch.slot == 6 and sch.name == "CLITEST"
    assert sch.start_mins == (55,)
    assert sch.zones == ((0, 60),)
    assert sch.weekday_mask == 0x7F


# --- multi-frame reassembly (split_inner_messages / parse_sync_dump) --------

def _dump_stream(program_pbs, mask, status_pb=None) -> bytes:
    """A #10 sync-dump plaintext stream: concatenated inner messages (each
    aa775a0f-framed), exactly what connection.send_stream returns."""
    parts = [tx._build_message(p) for p in program_pbs]
    parts.append(tx._build_message(tx._build_set_active_programs_pb(mask)))
    if status_pb is not None:
        parts.append(tx._build_message(status_pb))
    return b"".join(parts)


def test_split_inner_messages_finds_contiguous_msgs():
    a = tx._build_program_pb(_ha_spec(1, "odd", start_mins=(360,), zones=((0, 60),), name="A"))
    stream = _dump_stream([a], mask=1)
    msgs = rx.split_inner_messages(stream)
    assert len(msgs) == 2  # the #19 body + the #20 bitmask
    assert any(rx._pb_field(rx.pb_parse(m), rx.RX_F_PROGRAM) is not None for m in msgs)


def test_parse_sync_dump_fills_enabled_from_bitmask():
    a = tx._build_program_pb(_ha_spec(1, "odd", start_mins=(360,), zones=((0, 60),), name="A"))
    c = tx._build_program_pb(_ha_spec(3, "even", start_mins=(420,), zones=((0, 60),), name="C"))
    stream = _dump_stream([a, c], mask=0b001)  # only A enabled (bit 0)
    programs, mask, _status = rx.parse_sync_dump(stream)
    assert mask == 0b001
    assert programs[1].enabled is True    # A (bit 0 set)
    assert programs[3].enabled is False   # C (bit 2 clear)


def test_parse_sync_dump_reads_status_next_start():
    sub = tx._pb_field_varint(rx.RX_F_STATUS_NEXTSTART_FLAGS, 0b001)
    sub += tx._pb_field_varint(rx.RX_F_STATUS_NEXTSTART, 1_700_000_000)
    status_pb = tx._pb_field_bytes(rx.RX_F_STATUS, sub)
    a = tx._build_program_pb(_ha_spec(1, "odd", start_mins=(360,), zones=((0, 60),), name="A"))
    programs, mask, status = rx.parse_sync_dump(_dump_stream([a], mask=1, status_pb=status_pb))
    assert status is not None and status.next_start_flags == 0b001
    assert status.next_start_epoch == 1_700_000_000


# --- connection.send_stream reassembles a real multi-frame CTR burst --------

def _handshaken_stream_conn() -> BHyveBleConnection:
    conn = BHyveBleConnection(None, "AA:BB:CC:DD:EE:FF", "00" * 16)
    conn._reply_header = rx.MSG_HEADER
    conn._handshaken = True
    conn._iv = b"\x01" * 12
    conn._rx_ctr = 1000
    return conn


def test_send_stream_reassembles_message_split_across_frames():
    # A long #19 program body deliberately spans multiple CTR-block frames. The
    # per-frame decrypt in _on_notify + send_stream's concat must rejoin it.
    conn = _handshaken_stream_conn()
    long_prog = tx._build_program_pb(_ha_spec(
        4, "weekdays", weekday_mask=42, start_mins=(360,), zones=((0, 300),),
        name="A long enough name to force this message across a frame boundary!"))
    stream_pt = _dump_stream([long_prog], mask=9)

    # Encrypt the whole plaintext stream as one CTR run at the conn's rx_ctr, then
    # chop the ciphertext into outer frames on 16-byte block boundaries.
    ks, _ = conn._aes_keystream(conn._rx_ctr, (len(stream_pt) + 15) // 16)
    ct = bytes(b ^ k for b, k in zip(stream_pt, ks[:len(stream_pt)]))
    frames = []
    for off, nblk in ((0, 1), (16, 3), (64, 99)):  # 1 block, 3 blocks, rest
        chunk = ct[off:off + nblk * 16]
        if chunk:
            frames.append(bytes([conn._frame_magic, len(chunk)]) + chunk + b"\x00\x00")

    for f in frames:
        conn._on_notify(None, f)
    got = b"".join(conn._notif_pt)
    assert got == stream_pt
    programs, mask, _ = rx.parse_sync_dump(got)
    assert mask == 9 and 4 in programs and programs[4].name.startswith("A long enough")


# --- program device methods (mirror the CLI handshake) ----------------------

def _make_gen2(**state_kwargs):
    dev = object.__new__(BHyveHT25G2Device)  # bypass HA-heavy __init__
    dev.mac = "AA:BB:CC:DD:EE:FF"
    dev.state = DeviceState(**state_kwargs)
    dev.flow_counts_per_gallon = 433
    return dev


def _is_status_elicitor(frame: bytes) -> bool:
    top = rx.pb_parse(rx.decode_inner(frame) or b"")
    return rx._pb_field(top, 15) is not None or rx._pb_field(top, 75) is not None


def _status_with_next_start(flags, epoch=1_700_000_000, run_state=1) -> bytes:
    sub = tx._pb_field_varint(rx.RX_F_STATUS_MODE, run_state)
    sub += tx._pb_field_varint(rx.RX_F_STATUS_NEXTSTART_FLAGS, flags)
    sub += tx._pb_field_varint(rx.RX_F_STATUS_NEXTSTART, epoch)
    return tx._build_message(tx._pb_field_bytes(rx.RX_F_STATUS, sub))


def _status_with_mode(mode) -> bytes:
    sub = tx._pb_field_varint(rx.RX_F_STATUS_MODE, 1)
    sub += tx._pb_field_bytes(rx.RX_F_STATUS_RUNECHO, tx._pb_field_varint(rx.RX_F_RUNECHO_MODE, mode))
    return tx._build_message(tx._pb_field_bytes(rx.RX_F_STATUS, sub))


class _FakeProgramConn:
    """Records send()/send_stream() frames, returns a queue of canned sync-dump
    streams for send_stream, and feeds a canned status via the observer on a
    status elicitor (#15/#75). is_connected stays True (pooled session)."""

    def __init__(self, device, *, dumps=None, status_pt=None):
        self.device = device
        self._dumps = list(dumps or [])
        self.status_pt = status_pt
        self.sent: list[bytes] = []
        self.streamed: list[bytes] = []
        self.disconnects = 0

    async def send(self, frame: bytes, drain_ms: int = 1500):
        self.sent.append(frame)
        if _is_status_elicitor(frame) and self.status_pt is not None:
            self.device._observe_plaintext(self.status_pt)
        return [b"\x01"]

    async def send_stream(self, frame: bytes, drain_ms: int = 4000):
        self.streamed.append(frame)
        if len(self._dumps) > 1:
            return self._dumps.pop(0)
        return self._dumps[0] if self._dumps else b""

    async def disconnect(self):
        self.disconnects += 1

    @property
    def is_connected(self):
        return True


def _program_ops(sent: list[bytes]) -> list[int]:
    """Top-level field number of each non-status, non-flow sent frame — the
    program/mode op sequence (19=store, 20=enable, 14=mode)."""
    ops = []
    for f in sent:
        top = rx.pb_parse(rx.decode_inner(f) or b"")
        num = top[0][0] if top else None
        if num in (14, 19, 20):
            ops.append(num)
    return ops


def test_get_programs_reads_and_stores():
    a = tx._build_program_pb(_ha_spec(1, "odd", start_mins=(360,), zones=((0, 60),), name="A"))
    dev = _make_gen2()
    dev.connection = _FakeProgramConn(dev, dumps=[_dump_stream([a], mask=1)])
    programs = asyncio.run(dev.get_programs())
    assert 1 in programs and programs[1].name == "A" and programs[1].enabled is True
    assert dev.state.programs[1].enabled is True
    assert dev.connection.disconnects == 1  # ephemeral teardown


def test_set_program_enabled_drives_three_write_handshake_in_order():
    spec = _ha_spec(4, "weekdays", weekday_mask=0x7F, start_mins=(360,),
                    zones=((0, 180),), name="D", enabled=True)
    stored = tx._build_program_pb(spec)
    dev = _make_gen2()
    dev.connection = _FakeProgramConn(
        dev,
        dumps=[_dump_stream([], mask=0), _dump_stream([stored], mask=0)],
        status_pt=_status_with_next_start(0b1000),  # slot D next-start reported
    )
    ok = asyncio.run(dev.set_program(spec))
    assert ok is True
    # store -> enable -> autoMode -> re-send store -> re-send enable
    assert _program_ops(dev.connection.sent) == [19, 20, 14, 19, 20]
    assert dev.state.next_start_flags == 0b1000


def test_set_program_store_only_keeps_automode_no_run_dance():
    spec = _ha_spec(4, "weekdays", weekday_mask=0x7F, start_mins=(360,),
                    zones=((0, 180),), name="D", enabled=False)
    stored = tx._build_program_pb(spec)
    dev = _make_gen2()
    dev.connection = _FakeProgramConn(
        dev, dumps=[_dump_stream([], mask=0), _dump_stream([stored], mask=0)]
    )
    ok = asyncio.run(dev.set_program(spec))
    assert ok is True
    # store -> clear-bit enable -> autoMode; NO re-send store/enable run dance
    assert _program_ops(dev.connection.sent) == [19, 20, 14]
    # the controller is returned to autoMode(1), never dropped to offMode(0)
    modes = [rx._pb_field(rx.pb_parse(rx._pb_field(rx.pb_parse(rx.decode_inner(f)), 14)), 1)
             for f in dev.connection.sent
             if rx._pb_field(rx.pb_parse(rx.decode_inner(f) or b""), 14) is not None]
    assert modes == [1]


def test_set_program_enabled_toggle_flips_bitmask():
    a = tx._build_program_pb(_ha_spec(1, "odd", start_mins=(360,), zones=((0, 60),), name="A"))
    dev = _make_gen2()
    dev.connection = _FakeProgramConn(
        dev,
        dumps=[_dump_stream([a], mask=0), _dump_stream([a], mask=1)],  # off -> on
        status_pt=_status_with_next_start(0b001),
    )
    ok = asyncio.run(dev.set_program_enabled(1, True))
    assert ok is True
    ops = _program_ops(dev.connection.sent)
    assert ops[0] == 20 and 14 in ops           # enable then autoMode
    # the enable bitmask carries bit0 (slot A)
    first = rx.pb_parse(rx._pb_field(rx.pb_parse(rx.decode_inner(dev.connection.sent[0])), 20))
    assert rx._pb_field(first, 1) == 1


def test_delete_program_clears_slot():
    a = tx._build_program_pb(_ha_spec(1, "odd", start_mins=(360,), zones=((0, 60),), name="A"))
    dev = _make_gen2()
    dev.connection = _FakeProgramConn(
        dev, dumps=[_dump_stream([a], mask=1), _dump_stream([tx._build_program_delete_pb(1)], mask=0)]
    )
    ok = asyncio.run(dev.delete_program(1))
    assert ok is True
    ops = _program_ops(dev.connection.sent)
    assert ops == [20, 19, 14]  # clear-bit, delete body, keep autoMode


def test_set_controller_mode_confirms_via_status():
    dev = _make_gen2()
    dev.connection = _FakeProgramConn(dev, status_pt=_status_with_mode(1))
    ok = asyncio.run(dev.set_controller_mode(True))
    assert ok is True
    assert dev.state.controller_mode == 1
    # a #14 timerMode was sent with mode=1
    mode14 = [f for f in dev.connection.sent
              if rx._pb_field(rx.pb_parse(rx.decode_inner(f) or b""), 14) is not None]
    assert mode14 and rx._pb_field(rx.pb_parse(rx._pb_field(rx.pb_parse(rx.decode_inner(mode14[0])), 14)), 1) == 1


# --- identify (#47 LED locate) ---------------------------------------------

def test_identify_builder_byte_refs():
    assert tx._build_identify_pb(5).hex() == "fa02020805"   # start (5s)
    assert tx._build_identify_pb(0).hex() == "fa02020800"   # stop


def test_identify_flashes_then_stops(monkeypatch):
    import orbit_bhyve.devices.protobuf as P

    async def _no_sleep(*_a):
        return None
    monkeypatch.setattr(P.asyncio, "sleep", _no_sleep)

    dev = _make_gen2()
    dev.connection = _FakeProgramConn(dev)
    ok = asyncio.run(dev.identify(seconds=6))
    assert ok is True
    id_frames = [f for f in dev.connection.sent
                 if rx._pb_field(rx.pb_parse(rx.decode_inner(f) or b""), 47) is not None]
    assert len(id_frames) == 2   # start then explicit stop (the flash latches)
    vals = [rx._pb_field(rx.pb_parse(rx._pb_field(rx.pb_parse(rx.decode_inner(f)), 47)), 1)
            for f in id_frames]
    assert vals == [6, 0]
    assert dev.connection.disconnects == 1


# --- idle coordinator poll refreshes the A–D schedules ----------------------

def _status_idle() -> bytes:
    return tx._build_message(
        tx._pb_field_bytes(rx.RX_F_STATUS, tx._pb_field_varint(rx.RX_F_STATUS_MODE, 1))
    )


class _ScriptedStreamConn:
    """Returns a scripted list of sync-dump streams, one per send_stream call. Feeds
    a canned status on a #15/#75 elicitor. Records send_stream calls in `streamed`."""

    def __init__(self, device, streams, status_pt=None):
        self.device = device
        self._streams = list(streams)
        self.status_pt = status_pt
        self.sent: list[bytes] = []
        self.streamed: list[bytes] = []
        self.disconnects = 0

    async def send(self, frame: bytes, drain_ms: int = 1500):
        self.sent.append(frame)
        if _is_status_elicitor(frame) and self.status_pt is not None:
            self.device._observe_plaintext(self.status_pt)
        return [b"\x01"]

    async def send_stream(self, frame: bytes, drain_ms: int = 4000):
        self.streamed.append(frame)
        return self._streams.pop(0) if self._streams else b""

    async def disconnect(self):
        self.disconnects += 1

    @property
    def is_connected(self):
        return True


def test_read_programs_keeps_last_known_on_empty_read_no_retry():
    # A desynced/truncated read yields an empty stream. It must NOT blank the known
    # schedules, and must NOT retry with an immediate reconnect (that races the
    # device's single-BLE-session release and wedges the link) — get_programs
    # returns the last-known state.programs, one send_stream only.
    dev = _make_gen2()
    dev.state.programs = {1: ProgramSummary(slot=1, empty=False, enabled=True, name="Keep")}
    dev.connection = _ScriptedStreamConn(dev, streams=[b""])
    programs = asyncio.run(dev.get_programs())
    assert 1 in programs and programs[1].name == "Keep"
    assert len(dev.connection.streamed) == 1   # no amplifying retry


def test_read_programs_populates_bodies_and_carries_enabled_without_mask():
    # A burst that decodes the #19 bodies but drops the small #20 mask (mask=None)
    # must still populate the schedule, and carry each slot's KNOWN enabled state
    # forward rather than reporting it as off/unknown.
    dev = _make_gen2()
    dev.state.programs = {1: ProgramSummary(slot=1, empty=False, enabled=True, name="A")}
    body_only = tx._build_message(
        tx._build_program_pb(_ha_spec(1, "odd", start_mins=(360,), zones=((0, 60),), name="A"))
    )  # a #19 message with NO trailing #20 mask
    dev.connection = _ScriptedStreamConn(dev, streams=[body_only])
    programs = asyncio.run(dev.get_programs())
    assert 1 in programs and not programs[1].empty
    assert programs[1].enabled is True         # carried forward, not blanked to None
    assert len(dev.connection.streamed) == 1


def test_read_programs_empty_device_completes_without_retry():
    # A genuinely empty device returns the #20 mask (0) with no #19 bodies — that is
    # a COMPLETE read, not a partial one, so it must not retry.
    dev = _make_gen2()
    dev.connection = _ScriptedStreamConn(dev, streams=[_dump_stream([], mask=0)])
    programs = asyncio.run(dev.get_programs())
    assert programs == {}
    assert len(dev.connection.streamed) == 1   # no retry


def test_refresh_state_reads_programs_on_idle_poll():
    a = tx._build_program_pb(_ha_spec(1, "odd", start_mins=(360,), zones=((0, 60),), name="A"))
    dev = _make_gen2(is_watering=False)
    dev.connection = _FakeProgramConn(dev, dumps=[_dump_stream([a], mask=1)], status_pt=_status_idle())
    state = asyncio.run(dev.refresh_state())
    assert state.is_watering is False
    assert 1 in state.programs and state.programs[1].enabled is True
    # the #10 syncRequest went out on the idle poll (via send_stream)
    assert any(rx._pb_field(rx.pb_parse(rx.decode_inner(f) or b""), 10) is not None
               for f in dev.connection.streamed)
