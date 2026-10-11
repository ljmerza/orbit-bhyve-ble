"""HT25 bind/rebind carry the real clock.

bind (0x81) and rebind (0x86) are the app's utc-time write (mesh message 5):
{utc_secs u32 LE, local_offset_mins i16 LE}. They used to send a random 2-byte
"sid" + f6 69 10 ff, which set the timer clock ~5 months back with a fixed
EDT offset (HW-verified 2026-10-11). No hardware or Home Assistant required.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from orbit_bhyve.devices import ht25
from orbit_bhyve.devices.ht25 import BHyveHT25Device, clock_payload
from orbit_bhyve.devices.ht25_fw0085 import BHyveHT25Fw0085Device

MESH = (0x47D7).to_bytes(2, "little")  # d7 47
HUB = (0xEB42).to_bytes(2, "little")
EDT = timezone(timedelta(minutes=-240))


def test_clock_payload_matches_hw_capture():
    # Deck 2026-10-11: bind wrote dad1f669 10ff and the timer's device-health
    # clock read back 0x69f6d1da - 240 min (+1 s).
    now = datetime.fromtimestamp(0x69F6D1DA, tz=EDT)
    assert clock_payload(now) == bytes.fromhex("dad1f66910ff")


def test_clock_payload_positive_and_half_hour_offset():
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime.fromtimestamp(1791749315, tz=ist)
    assert clock_payload(now) == (1791749315).to_bytes(4, "little") + (330).to_bytes(2, "little", signed=True)


def test_clock_payload_utc_offset_zero():
    now = datetime.fromtimestamp(1791749315, tz=timezone.utc)
    assert clock_payload(now)[4:] == b"\x00\x00"


def _dev(cls=BHyveHT25Device):
    record = {
        "cloud_id": "abc", "name": "Deck", "mac": "AA:BB:CC:DD:EE:FF",
        "hardware": "HT25-0000", "firmware": "0085", "stations": 1,
        "network_key": "", "mesh_device_id": 0x47D7,
        "hub_mesh_device_id": 0xEB42,
    }
    return cls(None, record)


class _Conn:
    def __init__(self):
        self.writes: list[bytes] = []

    async def _write_locked(self, pt: bytes) -> None:
        self.writes.append(pt)


@pytest.mark.parametrize("cls", [BHyveHT25Device, BHyveHT25Fw0085Device])
def test_init_bind_and_rebind_stamp_the_time_each_is_sent(monkeypatch, cls):
    t0 = 1791749315
    calls = iter([t0, t0 + 2])
    monkeypatch.setattr(ht25, "ha_local_now", lambda: datetime.fromtimestamp(next(calls), tz=EDT))

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(ht25.asyncio, "sleep", _no_sleep)

    conn = _Conn()
    asyncio.run(_dev(cls)._post_handshake(conn))

    clock = lambda t: t.to_bytes(4, "little") + (-240).to_bytes(2, "little", signed=True)
    assert conn.writes == [
        MESH + bytes([0x81, 0x05, 0x40]) + clock(t0),
        MESH + bytes([0x02, 0x02, 0x40, 0x00]),
        MESH + bytes([0x03, 0x03, 0x40]) + b"\x00" * 7,
        MESH + bytes([0x04, 0x01, 0x40]) + b"\x00" * 3,
        MESH + bytes([0x85, 0x00, 0x40, 0x01]) + MESH + b"\x00" * 4,
        MESH + bytes([0x85, 0x00, 0x40, 0x00]) + HUB + b"\x00" * 4,
        MESH + bytes([0x85, 0x09, 0x40, 0x00]),
        MESH + bytes([0x86, 0x05, 0x40]) + clock(t0 + 2),
    ]
