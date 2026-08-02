"""Gen1 (HT25 fw0085) flow session lifecycle: the connection hold and the six
release paths.

The hold exists because BHyveBleConnection._arm_idle_timer only ever fires on a
write, while a flow subscription writes once and then only receives — so at the
default 60 s idle the link closes mid-stream and a 10-minute program records
roughly the first minute of its water. Measured on hardware: 20 samples at
idle_disconnect 60 against 93-99 with it raised.

Releasing matters just as much. A run that ends on the device's own timer never
reaches valve.async_close_valve, and hass.async_create_task is not tied to the
config entry — an orphaned renewal task was observed surviving an integration
reload and reconnecting every 240 s against a device the integration no longer
owned, until a full HA restart.

Async work runs via asyncio.run() inside sync tests rather than
@pytest.mark.asyncio: pytest-asyncio is not in tests/requirements-test.txt, so a
marker-based test would break under a clean install.
"""
from __future__ import annotations

import asyncio

import pytest

from orbit_bhyve.connection import BHyveBleConnection
from orbit_bhyve.devices.ht25 import (
    D747_ROUTING,
    GEN1_HOLD_GRACE_SEC,
    SEQ_FLOW_END,
    BHyveHT25Device,
)

MESH_ID = 0x47D7
MESH = MESH_ID.to_bytes(2, "little")
KEY = "00" * 16  # never used: nothing here opens a BLE session


def _conn(idle_sec: int = 60) -> BHyveBleConnection:
    return BHyveBleConnection(
        None, "AA:BB:CC:DD:EE:FF", KEY, idle_disconnect_sec=idle_sec
    )


class _FakeConn:
    """Records hold/release/disconnect instead of touching BLE."""

    def __init__(self) -> None:
        self.held = False
        self.holds: list[float] = []
        self.releases = 0
        self.disconnects = 0

    @property
    def is_held(self) -> bool:
        return self.held

    def hold_open(self, max_sec: float) -> None:
        self.held = True
        self.holds.append(max_sec)

    def release(self) -> None:
        self.held = False
        self.releases += 1

    async def disconnect(self) -> None:
        self.disconnects += 1


def _dev(*, enabled: bool = True) -> BHyveHT25Device:
    dev = BHyveHT25Device(None, {
        "cloud_id": "abc", "name": "Deck", "mac": "AA:BB:CC:DD:EE:FF",
        "hardware": "HT25-0000", "firmware": "0085", "stations": 1,
        "network_key": "", "mesh_device_id": MESH_ID,
    })
    dev.connection = _FakeConn()
    dev.flow_gen1_enabled = enabled
    dev.reset_gen1_flow()
    return dev


async def _live_task() -> asyncio.Task:
    """A task that is definitely running, so cancellation is observable."""
    started = asyncio.Event()

    async def _forever() -> None:
        started.set()
        await asyncio.sleep(3600)

    task = asyncio.get_running_loop().create_task(_forever())
    await started.wait()
    return task


async def _settle() -> None:
    """Yield long enough for a cancellation to propagate.

    Deliberately does NOT await the task. A task that was not cancelled — the
    exact condition these assertions exist to catch — is sitting in a one-hour
    sleep, so awaiting it would hang the suite instead of failing it. asyncio
    .wait_for is no good either: it cancels its target on timeout, which would
    make task.cancelled() true and the assertion pass for the wrong reason.
    asyncio.run() cleans up any still-pending task on the way out.
    """
    await asyncio.sleep(0.05)


# --- the connection hold ---------------------------------------------------

def test_hold_open_disarms_the_idle_timer_and_blocks_re_arming():
    async def scenario():
        conn = _conn()
        conn._arm_idle_timer()
        assert conn._idle_timer is not None
        conn.hold_open(60)
        assert conn.is_held is True
        assert conn._idle_timer is None
        # A later write must not resurrect it — this is the actual defect: the
        # subscribe itself arms the timer that then closes the link.
        conn._arm_idle_timer()
        assert conn._idle_timer is None
    asyncio.run(scenario())


def test_release_re_arms_rather_than_disconnecting():
    async def scenario():
        conn = _conn()
        conn.hold_open(60)
        conn.release()
        assert conn.is_held is False
        # Release hands the link back to the idle machinery; it must not close
        # the session itself, keeping one place in the code that tears down.
        assert conn._idle_timer is not None
    asyncio.run(scenario())


def test_release_is_idempotent_and_safe_without_a_hold():
    async def scenario():
        conn = _conn()
        conn.release()               # no hold at all
        conn.hold_open(60)
        conn.release()
        conn.release()               # six paths release this; doubles happen
        assert conn.is_held is False
    asyncio.run(scenario())


def test_hold_open_refreshes_rather_than_nesting():
    async def scenario():
        conn = _conn()
        conn.hold_open(60)
        first = conn._hold_expiry
        conn.hold_open(60)
        assert conn._hold_expiry is not first
        assert first.cancelled()
        # One release still clears it — the hold is idempotent, not counted.
        conn.release()
        assert conn.is_held is False
    asyncio.run(scenario())


def test_hold_expires_on_its_own_backstop():
    async def scenario():
        conn = _conn()
        conn.hold_open(0.05)
        assert conn.is_held is True
        await asyncio.sleep(0.15)
        # A missed release path must cost one run, not the batteries.
        assert conn.is_held is False
        assert conn._idle_timer is not None
    asyncio.run(scenario())


def test_disconnect_clears_a_hold():
    async def scenario():
        conn = _conn()
        conn.hold_open(600)
        await conn.disconnect()
        # A hold refers to a live session. Left set, it would suppress the idle
        # timer of the *next* connection indefinitely.
        assert conn.is_held is False
        assert conn._hold_expiry is None
    asyncio.run(scenario())


def test_release_with_idle_disconnect_disabled_arms_nothing():
    """idle_disconnect <= 0 means "never auto-close".

    Releasing hands the link back to the idle machinery, which for this
    configuration means no timer at all — release must not invent one.

    This deliberately does NOT exercise the _held guard in _arm_idle_timer: the
    _idle_sec <= 0 check short-circuits first whatever the order, so the two
    guards are order-independent. The hold guard is covered by
    test_hold_open_disarms_the_idle_timer_and_blocks_re_arming, which is the
    mutation detector for it.
    """
    async def scenario():
        conn = _conn(idle_sec=0)
        conn.hold_open(60)
        assert conn.is_held is True
        conn.release()
        assert conn.is_held is False
        assert conn._idle_timer is None
    asyncio.run(scenario())


# --- device release paths --------------------------------------------------

def test_end_gen1_flow_cancels_the_task_and_releases():
    async def scenario():
        dev = _dev()
        task = await _live_task()
        dev._flow_sub_task = task
        dev.connection.hold_open(600)
        dev._end_gen1_flow()
        await _settle()
        assert task.cancelled()
        assert dev._flow_sub_task is None
        assert dev.connection.is_held is False
    asyncio.run(scenario())


def test_end_gen1_flow_is_idempotent():
    async def scenario():
        dev = _dev()
        task = await _live_task()
        dev._flow_sub_task = task
        dev.connection.hold_open(600)
        dev._end_gen1_flow()
        dev._end_gen1_flow()
        dev._end_gen1_flow()
        await _settle()
        # A run can end via the 0x0C frame, the wall-clock auto-close and a
        # STOP at effectively the same moment.
        assert dev.connection.releases >= 1
    asyncio.run(scenario())


def test_on_watering_finished_releases():
    # The coordinator's wall-clock auto-close calls this. A run that ends on the
    # device's own timer never reaches valve.async_close_valve, so this is the
    # common path, not a fallback.
    async def scenario():
        dev = _dev()
        task = await _live_task()
        dev._flow_sub_task = task
        dev.connection.hold_open(600)
        dev.on_watering_finished()
        await _settle()
        assert task.cancelled()
        assert dev.connection.is_held is False
    asyncio.run(scenario())


def test_async_unload_cancels_the_renewal_task():
    """Regression: the orphaned-task failure, observed on hardware.

    hass.async_create_task is not tied to the config entry, so before the
    async_unload override a renewal task survived an integration reload and kept
    reconnecting, re-running the 8-step init and re-subscribing every 240 s
    against a device the integration no longer owned. Only a full HA restart
    stopped it.
    """
    async def scenario():
        dev = _dev()
        task = await _live_task()
        dev._flow_sub_task = task
        dev.connection.hold_open(600)
        await dev.async_unload()
        await _settle()
        assert task.cancelled(), "renewal task survived config entry unload"
        assert dev._flow_sub_task is None
        assert dev.connection.is_held is False
        assert dev.connection.disconnects == 1
    asyncio.run(scenario())


def test_run_end_frame_releases_the_session():
    async def scenario():
        dev = _dev()
        task = await _live_task()
        dev._flow_sub_task = task
        dev.connection.hold_open(600)
        # seq 0x0C run-end frame; run duration 180 s at pt[6:8] LE.
        frame = (MESH + bytes([0x90, SEQ_FLOW_END, D747_ROUTING, 0x00])
                 + (180).to_bytes(2, "little") + b"\x00\x00\x00\x00")
        dev._observe_plaintext(frame)
        await _settle()
        assert task.cancelled()
        assert dev.connection.is_held is False
    asyncio.run(scenario())


def test_run_end_frame_from_another_device_is_ignored():
    async def scenario():
        dev = _dev()
        dev.connection.hold_open(600)
        other = (0x1234).to_bytes(2, "little")
        frame = (other + bytes([0x90, SEQ_FLOW_END, D747_ROUTING, 0x00])
                 + (180).to_bytes(2, "little") + b"\x00\x00\x00\x00")
        dev._observe_plaintext(frame)
        assert dev.connection.is_held is True
    asyncio.run(scenario())


def test_switch_off_mid_run_stops_measuring():
    # Turning the opt-in off withdraws consent for the session cost, so it has
    # to take effect now rather than at the end of the program. switch.py calls
    # on_watering_finished() for exactly this.
    async def scenario():
        dev = _dev()
        task = await _live_task()
        dev._flow_sub_task = task
        dev.connection.hold_open(600)
        dev.flow_gen1_enabled = False
        dev.on_watering_finished()
        await _settle()
        assert task.cancelled()
        assert dev.connection.is_held is False
    asyncio.run(scenario())


def test_hold_backstop_is_scoped_to_the_run_duration():
    async def scenario():
        dev = _dev()
        dev.connection.hold_open(600 + GEN1_HOLD_GRACE_SEC)
        assert dev.connection.holds == [610]
    asyncio.run(scenario())
