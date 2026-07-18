"""Abstract base for all per-model device classes."""
from __future__ import annotations

import abc
import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..connection import BHyveBleConnection
from ..const import DEFAULT_FLOW_COUNTS_PER_GALLON

_LOGGER = logging.getLogger(__name__)


# Per-chemistry linear fuel-gauge endpoints: (mV at 0%, mV at 100%). AA cells
# in a 2x series (3 V nominal). See the "Battery Chemistry Options" plan for the
# electrochemical rationale; the conservative Ni-MH 0% floor (2350 mV, well above
# true-empty) exists to warn before solenoid-latch failure under water pressure.
# Regulated 1.5 V Li-Ion outputs a constant ~3000 mV until it dies, so no linear
# gauge can track it — it shares the alkaline endpoints and is documented as
# unmeasurable by percent.
BATTERY_CHEMISTRIES: dict[str, tuple[int, int]] = {
    "alkaline": (2400, 3000),
    "nimh": (2350, 2750),
    "lithium_primary": (2600, 3400),
    "lithium_regulated": (2400, 3000),
}
DEFAULT_BATTERY_CHEMISTRY = "alkaline"


def _mv_to_pct(mv: int, chemistry: str = DEFAULT_BATTERY_CHEMISTRY) -> int:
    """Linear voltage->percent fuel gauge for the selected battery chemistry.
    Endpoints live in BATTERY_CHEMISTRIES.

    Provenance / accuracy caveat: none of these curves are calibrated against a
    known state of charge (that needs a controlled discharge). The alkaline
    endpoints (2400/3000) are Orbit's stock assumption; they reproduce Orbit's
    OWN displayed gauge — HW cross-checked 2026-07-17 on our XD (BT4ValveXD01):
    2828 mV -> 71%, matching the 71% the B-Hyve app showed for the same device.
    (The inherited upstream/wxfield anchor points — Hill 33%/2602 mV,
    Corner 34%/2606 mV, Deck 65%/2771 mV — were CLOUD-reported percentages on
    devices that are not ours, so they are a weaker cross-reference to the same
    alkaline gauge, not measurements.) The Ni-MH / lithium endpoints are
    engineering estimates from the discharge-profile analysis (see the
    Battery-Chemistry-Options plan), with a deliberately conservative Ni-MH 0%
    floor for solenoid-latch safety; there is no cheap ground truth for them
    (Orbit's gauge assumes alkaline and mis-reports them too)."""
    lo, hi = BATTERY_CHEMISTRIES.get(chemistry, BATTERY_CHEMISTRIES[DEFAULT_BATTERY_CHEMISTRY])
    pct = round((mv - lo) * 100 / (hi - lo))
    return max(0, min(100, pct))


# Watering-program slots (A=1 .. F=6). The device carries six schedule slots;
# the app exposes A-D. A=bit0 in the #20 activeProgramFlags enable bitmask.
PROGRAM_SLOTS = {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6}
SLOT_LETTERS = {v: k for k, v in PROGRAM_SLOTS.items()}
# The slots surfaced as HA entities (the app-visible A-D), one per program switch
# and summary sensor.
UI_SLOTS = ("A", "B", "C", "D")


def parse_start_minutes(tok: object) -> int:
    """Parse one program start-time token to minutes-from-midnight (0..1439).

    Guards two YAML/service footguns instead of silently wrapping with `% 1440`:
    an *unquoted* HH:MM inside a YAML list is parsed as a sexagesimal INT
    (`18:00` -> `1080`, later coerced to the string ``"1080"``), and an
    ``HH:MM:SS`` string (what HA time selectors emit) is not something ``int()``
    accepts. Require an explicit ``HH:MM[:SS]`` string and reject out-of-range /
    non-time tokens — the old ``(int(h)*60+int(m)) % 1440`` turned ``18:00`` into
    midnight. Raises ``ValueError`` on bad input (the service layer maps that to a
    ``ServiceValidationError``); kept HA-free so it is unit-testable standalone."""
    s = str(tok).strip()
    parts = s.split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        raise ValueError(
            f"start_times entries must be HH:MM (or HH:MM:SS); got {tok!r}. "
            "Quote bare times in YAML so 18:00 isn't read as the integer 1080."
        )
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"start_times entry out of range: {tok!r}")
    return hour * 60 + minute


def accumulate_gallons(
    total: float, prev_gpm: float | None, dt_sec: float, cap_sec: float
) -> float:
    """One left-rectangle step of the "Water used" integral: book `prev_gpm` —
    the rate that was live at the START of the interval — over `dt_sec`,
    bounded by `cap_sec` so a restart/outage gap can't book a huge block of
    phantom gallons. The caller gates on "previous update was watering"; this
    handles only the math. Kept HA-free so it is unit-testable standalone
    (same rationale as parse_start_minutes)."""
    if not prev_gpm or prev_gpm <= 0 or dt_sec <= 0:
        return total
    return total + prev_gpm * (min(dt_sec, cap_sec) / 60.0)


@dataclass
class ProgramSpec:
    """A watering program to WRITE (#19 setProgramSchedule).

    Zones are 0-indexed station ids on the wire (the CLI/service layer maps its
    1-indexed zones down). Exactly one day-mode is expressed by which of the
    weekday/interval/odd/even/once fields is populated (per `day_mode`)."""
    slot: int                            # 1=A .. 6=F
    day_mode: str                        # "weekdays" | "interval" | "odd" | "even" | "once"
    weekday_mask: int | None = None      # weekdays: bit0=Sun .. bit6=Sat
    interval_days: int | None = None     # interval: N
    interval_anchor: str | None = None   # interval: ISO-8601 anchor
    start_mins: tuple[int, ...] = ()     # minutes-from-midnight (device-local)
    zones: tuple[tuple[int, int], ...] = ()   # (station_id_0idx, run_sec)
    name: str = ""
    budget: int = 100
    enabled: bool = False                # drive the enable handshake after storing


@dataclass
class ProgramSummary:
    """A #19 program body decoded from a device read (connect burst / sync dump)."""
    slot: int
    empty: bool = True                   # #2 programTypeNotSet present -> empty slot
    enabled: bool | None = None          # from the #20 bitmask, not the #19 body
    day_mode: str | None = None
    weekday_mask: int | None = None
    interval_days: int | None = None
    interval_anchor: str | None = None
    start_mins: tuple[int, ...] = ()
    zones: tuple[tuple[int, int], ...] = ()   # (station_id_0idx, run_sec)
    name: str | None = None
    budget: int | None = None


@dataclass
class DeviceState:
    is_watering: bool = False
    active_zone: int | None = None
    seconds_remaining: int | None = None
    flow_total: int | None = None  # #59.#3 raw cumulative counter (transient; feeds flow_gpm)
    flow_gpm: float | None = None  # instantaneous flow rate from read_flow's slope (Gen2)
    flow_gpm_device: float | None = None  # #59.#4 device-reported gpm float (unconfirmed on HW)
    started_at: datetime | None = None
    expected_off_at: datetime | None = None
    last_command_at: datetime | None = None
    last_command_label: str | None = None
    is_connected: bool = False
    notifications_last_cmd: int = 0
    device_clock: int | None = None  # #7 device clock, Unix epoch seconds
    rain_delay_minutes: int | None = None
    rain_delay_ends: datetime | None = None
    last_successful_poll: datetime | None = None
    consecutive_timeouts: int = 0
    # Controller / program state (protobuf family).
    controller_mode: int | None = None   # #16.#2.#1 timerMode.mode: 0=off, 1=auto, 2=manual
    next_start_flags: int | None = None  # #16.#9 nextStartProgramFlags (slot bitmask, A=bit0)
    next_start_at: datetime | None = None  # #16.#10 nextStartTimeSecEpochUTC as an aware datetime
    programs: dict[int, ProgramSummary] = field(default_factory=dict)  # slot(1-6) -> summary
    extra: dict[str, Any] = field(default_factory=dict)


class BHyveBleDeviceBase(abc.ABC):
    """One per physical device. Owns the BLE connection."""

    # Per-class overrides — defaults are HT25's values.
    frame_magic: int = 0x10
    trailer_const: int = 0x10
    # First-frame reply header for the connection's CTR-desync self-heal.
    # None (the mesh default) disables the header-based check: d7-47 mesh replies
    # use a [mesh:2][type][seq][routing] shape, not the protobuf inner-message
    # header, so the check would misfire on every mesh reply. The protobuf class
    # sets this to b"\xaa\x77\x5a\x0f".
    reply_header: bytes | None = None
    GATT_SETTLE_MS: int = 300
    # Whether the model exposes an inline flow sensor (#57/#59). Gen2 (HT25G2)
    # only per app captures; the XD has no flow screen. Verify on hardware
    # before trusting (the `flow` CLI probes both) — see docs/ble_protocol.md.
    has_flow: bool = False

    def __init__(
        self,
        hass,
        record: dict[str, Any],
        *,
        idle_disconnect_sec: int = 60,
        flow_counts_per_gallon: int = DEFAULT_FLOW_COUNTS_PER_GALLON,
    ):
        self.hass = hass
        # Counts→gallons scale for the flow sensor (Gen2). Configurable per
        # install via the options flow; read_flow divides the counter slope by it.
        self.flow_counts_per_gallon = flow_counts_per_gallon
        self.cloud_id: str = record["cloud_id"]
        self.name: str = record["name"]
        self.mac: str = record["mac"]
        self.hardware: str = record["hardware"]
        self.firmware: str = record["firmware"]
        self.stations: int = record["stations"]
        self.mesh_id: str | None = record.get("mesh_id")
        self.mesh_device_id: int | None = record.get("mesh_device_id")
        self.bridge_device_id: str | None = record.get("bridge_device_id")
        self.hub_mesh_device_id: int | None = record.get("hub_mesh_device_id")
        # Battery is read LIVE over BLE (#16.#14.#3 / mesh info-ack) and is the
        # only source we trust. Deliberately NOT seeded from the cloud snapshot in
        # `record`: the cloud reports on a chemistry-aware discharge curve that
        # disagrees with our linear _mv_to_pct (esp. for NiMH), so seeding it
        # painted a wrong value — inconsistent with the voltage sensor — into the
        # battery sensor's long-term statistics at every startup, until the first
        # poll replaced it. Start unknown; apply_status_plaintext fills battery_mv
        # in from the device on the first successful poll.
        self.battery_mv: int | None = None
        # User-selected AA cell chemistry driving the voltage->percent gauge
        # (battery_pct). Restored/updated by the Battery-chemistry select entity;
        # defaults to alkaline (Orbit's stock assumption).
        self.battery_chemistry: str = DEFAULT_BATTERY_CHEMISTRY
        self.network_key: str = record["network_key"]
        self.state = DeviceState()
        # Optional callback a coordinator registers so an out-of-band state
        # change (e.g. a BLE notification ack) can refresh entities now instead
        # of waiting for the next poll. See _notify_state_changed.
        self._state_changed_cb: Callable[[], None] | None = None

        if self.network_key and self.mac:
            self.connection: BHyveBleConnection | None = BHyveBleConnection(
                hass,
                self.mac,
                self.network_key,
                frame_magic=self.frame_magic,
                trailer_const=self.trailer_const,
                reply_header=self.reply_header,
                idle_disconnect_sec=idle_disconnect_sec,
                gatt_settle_ms=self.GATT_SETTLE_MS,
            )
            self.connection.set_post_handshake_hook(self._post_handshake)
            self.connection.set_plaintext_observer(self._observe_plaintext)
        else:
            # Hubs and key-less records (skip BLE entirely).
            self.connection = None

    @property
    def fw_int(self) -> int:
        try:
            return int(self.firmware)
        except (TypeError, ValueError):
            return 0

    @property
    def battery_pct(self) -> int | None:
        """Voltage-derived battery percent for the selected chemistry. Computed
        (not stored) so changing the chemistry select recalculates it immediately,
        without waiting for the next BLE poll to re-stamp it."""
        if self.battery_mv is None:
            return None
        return _mv_to_pct(self.battery_mv, self.battery_chemistry)

    @property
    def unique_id(self) -> str:
        return f"orbit_bhyve_{self.mac.replace(':', '').lower()}"

    @property
    def _api_lock(self) -> asyncio.Lock:
        if not hasattr(self, "_api_lock_var"):
            self._api_lock_var = asyncio.Lock()
        return self._api_lock_var

    async def async_setup(self) -> None:
        """Hook for device classes that want pre-warming. Default: no-op."""

    async def async_manual_sync(self) -> None:
        """Extra work for an explicit Sync-button press, run before the
        coordinator refresh. Default no-op: classes whose refresh_state already
        connects (protobuf's #15 read) need nothing more. The mesh classes,
        whose refresh_state is passive (no BLE), override this to force a
        connect so the button actually pulls live state off the device."""

    async def async_unload(self) -> None:
        if self.connection is not None:
            await self.connection.disconnect()

    def set_state_changed_callback(self, cb: Callable[[], None] | None) -> None:
        """Register a callback fired when device state changes out of band
        (outside the coordinator poll), e.g. from a notification ack."""
        self._state_changed_cb = cb

    def _notify_state_changed(self) -> None:
        if self._state_changed_cb is not None:
            self._state_changed_cb()

    def _mark_reached(self) -> None:
        """Record a successful device reach for the connectivity diagnostics
        (Connected / Last successful poll / Consecutive timeouts). Connectivity
        is EVENT-DRIVEN under the ephemeral connect-on-demand model: the live
        socket is torn down between operations, so "reachable" means the last
        reach succeeded — not that a socket is open right now."""
        self.state.is_connected = True
        self.state.last_successful_poll = datetime.now(timezone.utc)
        self.state.consecutive_timeouts = 0

    def _mark_unreachable(self) -> None:
        """Record a failed device reach (couldn't connect / talk to the device)."""
        self.state.is_connected = False
        self.state.consecutive_timeouts += 1

    async def _post_handshake(self, conn: BHyveBleConnection) -> None:
        """Override to send per-class init frames after the AES handshake."""

    def _observe_plaintext(self, pt: bytes) -> None:
        """Parse d7-47 mesh status replies: battery (seq 0x03) and watering
        state (seq 0x02).

        Frame layout: [mesh:2][type:1][seq:1][routing:1][payload:N]. Replies
        set the 0x40 reply bit in the type byte and carry routing=0x40 (TX
        echoes with the bit clear are skipped). Verified against fw0085 (Deck)
        and fw0041 (Hill, Corner) cross-checked with cloud snapshots."""
        if len(pt) < 6 or pt[4] != 0x40 or not (pt[2] & 0x40):
            return
        seq = pt[3]
        if seq == 0x03 and len(pt) >= 12:
            # Info-ack: payload bytes 4-5 (pt[9:11]) are battery mV, LE.
            mv = int.from_bytes(pt[9:11], "little")
            if 1500 <= mv <= 4000:  # out-of-band => malformed; don't poison state
                self.battery_mv = mv  # battery_pct is a chemistry-aware property
        elif seq == 0x02 and len(pt) >= 6:
            # Status reply/push: payload[0] (pt[5]) is the watering mode —
            # 0x04 = watering, 0x01 = idle. Authoritative device state, used to
            # confirm an actuation actually took.
            mode = pt[5]
            if mode == 0x04:
                self.state.is_watering = True
            elif mode == 0x01:
                self.state.is_watering = False

    @abc.abstractmethod
    async def start_watering(self, station: int, duration_sec: int) -> bool:
        ...

    @abc.abstractmethod
    async def stop_watering(self, station: int | None = None) -> bool:
        ...

    async def refresh_state(self) -> DeviceState:
        """Default (mesh): passive — return the last-known state without opening
        BLE. Connectivity is event-driven (stamped on each real device reach via
        _mark_reached / _mark_unreachable), so a passive poll must NOT overwrite
        state.is_connected with the transient live socket: under the ephemeral
        model that socket is torn down between operations and would read False
        even right after a successful reach, pinning the Connected sensor off.
        Subclasses that connect every poll (protobuf's #15 read) override this
        and stamp connectivity themselves."""
        return self.state

    @property
    def rssi(self) -> int | None:
        """Latest RSSI from the bluetooth manager's most recent advertisement.
        Works even while disconnected, and unlike bleak's BLEDevice.rssi (now
        deprecated and always None) it actually returns a value."""
        from homeassistant.components.bluetooth import async_last_service_info

        if not self.mac:
            return None
        info = async_last_service_info(self.hass, self.mac, connectable=True)
        return info.rssi if info is not None else None

    def _stamp_command(self, label: str, n_notifs: int) -> None:
        self.state.last_command_at = datetime.now(timezone.utc)
        self.state.last_command_label = label
        self.state.notifications_last_cmd = n_notifs


class UnsupportedModel(Exception):
    def __init__(self, hardware: str, firmware: str):
        super().__init__(f"no device class for hardware={hardware!r} firmware={firmware!r}")
        self.hardware = hardware
        self.firmware = firmware
