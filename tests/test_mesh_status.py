"""Mesh (d7-47) STATUS decode + live-status poll gating tests.

Covers the seq 0x02 active/idle payload decode in
BHyveBleDeviceBase._observe_plaintext (countdown at the 64 Hz hypothesis,
duration cross-check, only-move-earlier off-timer guard) and the opt-in
active idle poll in BHyveHT25Device.refresh_state. No hardware or Home
Assistant required.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from orbit_bhyve.devices.ht25 import BHyveHT25Device

MESH = bytes([0xD7, 0x47])
TYPE_STATUS_REPLY = 0x42  # 0x02 with the 0x40 reply bit
ROUTING = 0x40


def _dev() -> BHyveHT25Device:
    # Key-less record -> no BLE connection, no hass needed.
    record = {
        "cloud_id": "abc", "name": "Deck", "mac": "AA:BB:CC:DD:EE:FF",
        "hardware": "HT25-0000", "firmware": "0085", "stations": 1,
        "network_key": "",
    }
    return BHyveHT25Device(None, record)


def _status_frame(payload: bytes) -> bytes:
    """[mesh:2][type:1][seq:1][routing:1][payload:N] with the reply bit set."""
    return MESH + bytes([TYPE_STATUS_REPLY, 0x02, ROUTING]) + payload


def _active_payload(remaining_sec: int, duration_sec: int) -> bytes:
    """Active STATUS payload per the captured layout:
    [04][countdown u16 LE @64 Hz][0x40][duration u16 LE][00]."""
    counts = remaining_sec * 64
    return (
        bytes([0x04])
        + (counts & 0xFFFF).to_bytes(2, "little")
        + bytes([0x40 | ((counts >> 16) & 0x3F)])
        + duration_sec.to_bytes(2, "little")
        + b"\x00"
    )


IDLE_PAYLOAD = bytes.fromhex("01ffffffff0000")  # captured idle shape


def test_active_status_decodes_countdown():
    dev = _dev()
    before = datetime.now(timezone.utc)
    # The doc's captured frame: 1 s into a 600 s run -> 599 s remaining.
    dev._observe_plaintext(_status_frame(_active_payload(599, 600)))
    assert dev.state.is_watering is True
    assert dev.state.seconds_remaining == 599
    assert dev.state.active_zone == 1
    assert dev.state.extra["mesh_status_raw"] == _active_payload(599, 600).hex()
    # started_at synthesized from the duration echo (~1 s before now).
    assert dev.state.started_at is not None
    assert abs((before - dev.state.started_at).total_seconds() - 1) < 2
    # Off-timer armed ~599 s out.
    off_in = (dev.state.expected_off_at - before).total_seconds()
    assert 597 < off_in < 601


def test_captured_bytes_decode_to_599():
    # Byte-for-byte captured active frame: `04 c0 95 40 58 02 00`.
    dev = _dev()
    dev._observe_plaintext(_status_frame(bytes.fromhex("04c09540580200")))
    assert dev.state.seconds_remaining == 599
    # And the adjacent capture (`80 95`) is exactly one second later.
    dev2 = _dev()
    dev2._observe_plaintext(_status_frame(bytes.fromhex("04809540580200")))
    assert dev2.state.seconds_remaining == 598


def test_idle_status_clears_run_state():
    dev = _dev()
    dev.state.is_watering = True
    dev.state.active_zone = 1
    dev.state.seconds_remaining = 300
    dev.state.expected_off_at = datetime.now(timezone.utc) + timedelta(seconds=300)
    dev._observe_plaintext(_status_frame(IDLE_PAYLOAD))
    assert dev.state.is_watering is False
    assert dev.state.active_zone is None
    assert dev.state.seconds_remaining is None
    assert dev.state.expected_off_at is None


def test_status_transition_notifies_coordinator():
    dev = _dev()
    pokes: list[int] = []
    dev.set_state_changed_callback(lambda: pokes.append(1))
    dev._observe_plaintext(_status_frame(_active_payload(599, 600)))
    assert len(pokes) == 1          # idle -> watering
    dev._observe_plaintext(_status_frame(_active_payload(598, 600)))
    assert len(pokes) == 1          # still watering: no re-poke
    dev._observe_plaintext(_status_frame(IDLE_PAYLOAD))
    assert len(pokes) == 2          # watering -> idle


def test_countdown_failing_duration_crosscheck_not_applied():
    # A countdown decoding LONGER than the requested run means the tick-rate
    # hypothesis doesn't fit this frame: keep is_watering, drop the countdown.
    dev = _dev()
    payload = (
        bytes([0x04]) + b"\xff\xff" + bytes([0x40]) + (600).to_bytes(2, "little") + b"\x00"
    )  # 65535 counts -> 1023 s "remaining" of a 600 s run
    dev._observe_plaintext(_status_frame(payload))
    assert dev.state.is_watering is True
    assert dev.state.seconds_remaining is None
    assert dev.state.extra["mesh_status_raw"] == payload.hex()  # raw still logged


def test_off_timer_only_moves_earlier():
    dev = _dev()
    near = datetime.now(timezone.utc) + timedelta(seconds=300)
    dev.state.is_watering = True
    dev.state.expected_off_at = near
    # Device claims 599 s remaining -> later than the armed off-timer: ignored.
    dev._observe_plaintext(_status_frame(_active_payload(599, 600)))
    assert dev.state.expected_off_at == near
    # Device claims 100 s remaining -> earlier: re-anchored.
    dev._observe_plaintext(_status_frame(_active_payload(100, 600)))
    assert dev.state.expected_off_at < near


def test_short_status_frame_sets_mode_only():
    # Legacy/short reply (mode byte only) must keep working: no countdown data.
    dev = _dev()
    dev._observe_plaintext(_status_frame(bytes([0x04])))
    assert dev.state.is_watering is True
    assert dev.state.seconds_remaining is None


def test_refresh_state_active_poll_gating():
    dev = _dev()
    calls: list[int] = []

    async def fake_connect_refresh():
        calls.append(1)

    dev._connect_refresh = fake_connect_refresh  # type: ignore[method-assign]

    # Option off (default): passive poll, no connect.
    asyncio.run(dev.refresh_state())
    assert calls == []

    # Option on + idle: connects.
    dev.active_status_poll = True
    asyncio.run(dev.refresh_state())
    assert calls == [1]

    # Option on + watering: never connects mid-run.
    dev.state.is_watering = True
    asyncio.run(dev.refresh_state())
    assert calls == [1]

    # Option on + idle but reached moments ago (Sync/actuation): skipped.
    dev.state.is_watering = False
    dev.state.last_successful_poll = datetime.now(timezone.utc)
    asyncio.run(dev.refresh_state())
    assert calls == [1]

    # Stale last reach -> connects again.
    dev.state.last_successful_poll = datetime.now(timezone.utc) - timedelta(seconds=60)
    asyncio.run(dev.refresh_state())
    assert calls == [1, 1]
