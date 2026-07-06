"""Pooled BLE connection layer shared by all device classes.

Owns: AES handshake, keystream cipher, frame I/O, write lock, idle disconnect.
Per-device-class knobs: frame_magic, trailer_const, post_handshake_hook.

Cipher (verified against 257 captured frames + actuated commands):
  AES-128-ECB used as a CTR-style keystream — block = IV(12B) || ctr_LE(4B),
  encrypted, XORed with plaintext.
  IV = init_rx[0:4] || init_tx[4:12].
  TX counter = uint32_LE(init_tx[12:16]); RX counter = init_tx[16:20].
  Frame = [magic][len][ciphertext (len bytes)][trailer u16_LE].
  Trailer = sum(plaintext) + trailer_const + len.
Commands use WRITE_REQ (response=True) for its ATT delivery ack; char 6c72 also
advertises write-without-response, and both modes have been observed to actuate
the HT34A (fw0107), so WRITE_REQ is a safe default across device classes. The
device's higher-level ack is a NOTIFICATION, not an ATT Write Response — the
ESPHome BLE proxy never relays the (absent) write response, so writes wait only
a capped WRITE_ACK_TIMEOUT_SEC and the notification drain is the real ack.
"""
from __future__ import annotations

import asyncio
import logging
import os
import struct
from collections.abc import Awaitable, Callable

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import AES_CHAR, READ_CHAR, WRITE_CHAR

_LOGGER = logging.getLogger(__name__)

# A connect can succeed on a weak/proxy link while the post-connect handshake
# (subscribe + AES write/read) stalls with no natural timeout — wedging the
# pooled connection for ~30s and leaking it. Bound the handshake and retry the
# whole open, disconnecting between tries; healthy links pass on attempt 1.
HANDSHAKE_TIMEOUT_SEC = 10.0
OPEN_MAX_ATTEMPTS = 3

# The device acks a command via NOTIFICATION, not an ATT Write Response. Over a
# direct link the (unused) write response still arrives in <200ms, but over an
# ESPHome BLE proxy it is never relayed — so a response=True write would block
# ~30s. Cap the wait; the notification drain in send() is the real ack.
WRITE_ACK_TIMEOUT_SEC = 0.8

# Event-driven drain: the reply usually lands in 50-150ms and a status burst
# arrives as a few frames, but an ack-then-status reply can gap ~150ms between
# the small ack and the richer #16 status (observed 147ms in a live capture).
# So after the first frame, keep the drain window open only until no new frame
# has arrived for this long — returning ~4x faster than sleeping the full
# drain_ms, without truncating a multi-frame reply. drain_ms remains the hard cap.
NOTIF_QUIET_SEC = 0.35

PostHandshakeHook = Callable[["BHyveBleConnection"], Awaitable[None]]
PlaintextObserver = Callable[[bytes], None]


class BHyveBleConnection:
    """One per physical device. Reused across commands within an idle window."""

    def __init__(
        self,
        hass,
        mac: str,
        network_key: str,
        *,
        frame_magic: int = 0x10,
        trailer_const: int = 0x10,
        reply_header: bytes | None = None,
        idle_disconnect_sec: int = 60,
        gatt_settle_ms: int = 300,
    ):
        self.hass = hass
        self.mac = mac
        self._key = bytes.fromhex(network_key)
        self._frame_magic = frame_magic & 0xFF
        self._trailer_const = trailer_const & 0xFF
        # First-frame plaintext header used to detect a CTR desync. Protobuf
        # classes reply with the inner-message header b"\xaa\x77\x5a\x0f"; the
        # d7-47 mesh classes use a different frame shape ([mesh:2][type][seq]
        # [routing]...) that legitimately never starts with it, so they pass
        # None here to skip the header-based desync self-heal entirely.
        self._reply_header = reply_header
        self._idle_sec = idle_disconnect_sec
        self._gatt_settle_ms = gatt_settle_ms

        self._client: BleakClient | None = None
        self._iv: bytes | None = None
        self._tx_ctr: int = 0
        self._rx_ctr: int = 0
        self._lock = asyncio.Lock()
        self._notif_buf: list[bytes] = []
        self._notif_event = asyncio.Event()  # set on every notification; drives _drain
        self._last_rx_frame: bytes | None = None  # last raw RX frame, for de-dup
        self._handshaken = False
        self._post_handshake_hook: PostHandshakeHook | None = None
        self._plaintext_observer: PlaintextObserver | None = None
        self._idle_timer: asyncio.TimerHandle | None = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    def set_post_handshake_hook(self, hook: PostHandshakeHook | None) -> None:
        """Run a per-device-class init sequence right after the AES handshake.
        Used by HT25 for its 8-step bind-status-info dance."""
        self._post_handshake_hook = hook

    def set_plaintext_observer(self, observer: PlaintextObserver | None) -> None:
        """Receive every successfully decrypted notification plaintext.
        Lets a device class parse responses (battery, status, etc.) without
        re-decrypting — re-decrypt would advance the rx counter and desync
        subsequent frames."""
        self._plaintext_observer = observer

    async def ensure_connected(self) -> None:
        """Connect + handshake if not already pooled. _open() retries the whole
        connect+handshake OPEN_MAX_ATTEMPTS times internally: on a marginal proxy
        link the connect succeeds but the handshake GATT exchange can stall, and
        a clean retry usually gets through while the bounded handshake means we
        fail cleanly rather than wedging. Call inside a lock if you need
        exclusive access; idempotent otherwise."""
        if self.is_connected and self._handshaken:
            return
        # _open() already retries OPEN_MAX_ATTEMPTS times internally; don't wrap
        # it in a second retry loop or the attempts multiply (3x3=9 handshakes,
        # ~90-150s on a stalling device). One call = one bounded retry budget.
        try:
            await self._open()
        except (BleHandshakeError, asyncio.TimeoutError) as err:
            raise BleHandshakeError(f"{self.mac}: handshake failed: {err}") from err

    async def _open(self) -> None:
        from homeassistant.components.bluetooth import async_ble_device_from_address

        ble_device = async_ble_device_from_address(self.hass, self.mac, connectable=True)
        if ble_device is None:
            raise BleNotConnectable(f"{self.mac}: not in range of any connectable BLE adapter")

        last_err: Exception | None = None
        for attempt in range(1, OPEN_MAX_ATTEMPTS + 1):
            try:
                _LOGGER.debug("%s: connecting (attempt %d/%d)", self.mac, attempt, OPEN_MAX_ATTEMPTS)
                self._client = await establish_connection(
                    BleakClient, ble_device, self.mac, max_attempts=3
                )
                _LOGGER.debug("%s: connected", self.mac)
                if self._gatt_settle_ms > 0:
                    await asyncio.sleep(self._gatt_settle_ms / 1000.0)
                # Bound the handshake: on a marginal link the connect succeeds
                # but the GATT exchange below can hang indefinitely.
                await asyncio.wait_for(self._handshake(), timeout=HANDSHAKE_TIMEOUT_SEC)
            except (asyncio.TimeoutError, BleakError, OSError, BleHandshakeError) as err:
                last_err = err
                _LOGGER.debug(
                    "%s: open attempt %d/%d failed: %s", self.mac, attempt, OPEN_MAX_ATTEMPTS, err
                )
                await self.disconnect()  # clean slate so the retry gets a fresh GATT window
                if attempt < OPEN_MAX_ATTEMPTS:
                    await asyncio.sleep(0.5)  # space out retries on a marginal link
                continue
            # Handshake succeeded — run the per-device-class init, then we're open.
            if self._post_handshake_hook is not None:
                await self._post_handshake_hook(self)
            return

        raise BleHandshakeError(
            f"{self.mac}: open failed after {OPEN_MAX_ATTEMPTS} attempts: {last_err}"
        )

    async def _handshake(self) -> None:
        """Subscribe + AES handshake. Bounded by a timeout in _open() because
        these GATT reads/writes are what stall on a weak link. The
        post-handshake hook and open-retry are driven by _open()."""
        # Subscribe BEFORE writing — device may stay silent otherwise.
        self._notif_buf.clear()
        self._last_rx_frame = None  # fresh CTR stream — don't dedup across sessions
        await self._client.start_notify(READ_CHAR, self._on_notify)

        # AES handshake. Phone forces init_tx[11]=0x00.
        init_tx = bytearray(os.urandom(20))
        init_tx[11] = 0x00
        init_tx = bytes(init_tx)
        await self._client.write_gatt_char(AES_CHAR, init_tx)
        rx = bytes(await self._client.read_gatt_char(AES_CHAR))
        if rx[:4] == b"\x00\x00\x00\x00" or any(rx[4:]):
            raise BleHandshakeError(f"{self.mac}: invalid handshake rx={rx.hex()}")

        buf = rx[:4] + init_tx[4:]
        self._iv = buf[:12]
        self._tx_ctr = struct.unpack("<I", buf[12:16])[0]
        self._rx_ctr = struct.unpack("<I", buf[16:20])[0]
        self._handshaken = True
        _LOGGER.debug("%s: handshake ok, iv=%s tx_ctr=0x%08x", self.mac, self._iv.hex(), self._tx_ctr)

    def _on_notify(self, _sender, data) -> None:
        """Bleak notification callback. Buffers the raw frame for the
        command drain, then best-effort decrypts + logs the plaintext so
        we can reverse-engineer the status response (for battery, etc.).
        Decryption advances the rx counter — necessary for the next
        notification's plaintext to be correct."""
        frame = bytes(data)
        # Drop an exact re-delivery of the previous frame. A proxy/link can
        # re-emit the same notification (observed: dozens of identical frames in
        # a burst while the vendor app held the device's single BLE session). We
        # decrypt every delivery, so a re-delivery would advance the RX counter
        # and desync the CTR stream — poisoning every subsequent frame until the
        # next handshake. Same plaintext at a new counter yields different
        # ciphertext, so byte-identical consecutive frames are always dupes.
        if frame == self._last_rx_frame:
            _LOGGER.debug("%s: dropped duplicate rx frame %s", self.mac, frame.hex())
            return
        self._last_rx_frame = frame
        self._notif_buf.append(frame)
        self._notif_event.set()  # wake any in-flight drain (see _drain)
        if not self._handshaken or self._iv is None:
            return
        try:
            pt = self.decrypt(frame)
            _LOGGER.info(
                "%s: notif pt=%s (ct=%s)",
                self.mac, pt.hex(), frame.hex(),
            )
            if (
                self._reply_header is not None
                and len(pt) >= 4
                and pt[:4] != self._reply_header
            ):
                # Only the FIRST frame of a reply must start with the inner-message
                # header. A long reply that CTR-streams as consecutive 16-byte blocks
                # (each its own outer frame) has continuation frames that legitimately
                # do NOT — none of the capabilities wired today produce those, but
                # Program reads (#19, Phase 4) will. _notif_buf is cleared before each
                # write, so len<=1 means this is the reply's first frame: a bad header
                # there is a real CTR desync → self-heal. A bad header on a later frame
                # is a continuation (or a mid-stream desync we recover on the next
                # reply), so don't tear the session down under it. (Full multi-frame
                # reassembly in the observer is deferred to the Programs phase.)
                if len(self._notif_buf) <= 1:
                    _LOGGER.warning(
                        "%s: CTR desync detected (bad header %s) — scheduling disconnect to self-heal",
                        self.mac, pt[:4].hex(),
                    )
                    self.hass.add_job(self.disconnect)
                return
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "%s: CTR desync detected (decrypt failed: %s) — scheduling disconnect to self-heal",
                self.mac, err,
            )
            self.hass.add_job(self.disconnect)
            return
        if self._plaintext_observer is not None:
            try:
                self._plaintext_observer(pt)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("%s: plaintext observer raised: %s", self.mac, err)

    async def disconnect(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None
        if self._client is not None:
            try:
                await self._client.stop_notify(READ_CHAR)
            except Exception:
                pass
            try:
                await self._client.disconnect()
            except Exception:
                pass
        self._client = None
        self._handshaken = False
        self._iv = None
        self._last_rx_frame = None

    def _arm_idle_timer(self) -> None:
        if self._idle_sec <= 0:
            return
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        loop = asyncio.get_running_loop()
        self._idle_timer = loop.call_later(
            self._idle_sec, lambda: asyncio.create_task(self._idle_close()),
        )

    async def _idle_close(self) -> None:
        async with self._lock:
            if not self.is_connected:
                return
            _LOGGER.debug("%s: idle disconnect", self.mac)
            await self.disconnect()

    def _aes_keystream(self, counter: int, n_blocks: int) -> tuple[bytes, int]:
        out = bytearray()
        c = counter & 0xFFFFFFFF
        encryptor = Cipher(algorithms.AES(self._key), modes.ECB()).encryptor()
        for _ in range(n_blocks):
            out.extend(encryptor.update(self._iv + struct.pack("<I", c)))
            c = (c + 1) & 0xFFFFFFFF
        return bytes(out), c

    def _aes_xor(self, counter: int, plaintext: bytes) -> tuple[bytes, int]:
        n_blocks = (len(plaintext) + 15) // 16
        keystream, next_ctr = self._aes_keystream(counter, n_blocks)
        return bytes(b ^ k for b, k in zip(plaintext, keystream[:len(plaintext)])), next_ctr

    def encrypt(self, plaintext: bytes) -> bytes:
        if self._iv is None:
            # A disconnect scheduled via hass.add_job (e.g. a desync self-heal)
            # can interleave with an in-flight handshake/init sequence across its
            # awaits and null the session mid-write. Fail cleanly with a typed
            # error the open-retry can catch, not a TypeError from _aes_keystream.
            raise BleHandshakeError(
                f"{self.mac}: cannot encrypt — session cleared (disconnected mid-handshake)"
            )
        ct, self._tx_ctr = self._aes_xor(self._tx_ctr, plaintext)
        trailer = (sum(plaintext) + self._trailer_const + len(plaintext)) & 0xFFFF
        return bytes([self._frame_magic, len(ct)]) + ct + struct.pack("<H", trailer)

    def decrypt(self, frame: bytes) -> bytes:
        """Decrypt a notification frame (advances RX counter). For status decoding."""
        if len(frame) < 4 or frame[0] != self._frame_magic:
            raise ValueError(f"{self.mac}: bad frame magic in {frame.hex()}")
        ct_len = frame[1]
        ct = frame[2:2 + ct_len]
        pt, self._rx_ctr = self._aes_xor(self._rx_ctr, ct)
        return pt

    async def _write_locked(self, plaintext: bytes) -> None:
        """Encrypt + write. Caller MUST already hold self._lock and have
        an established connection. Used by send()/send_raw() and by the
        post-handshake hook (which runs inside _open() inside the lock)."""
        frame = self.encrypt(plaintext)
        assert self._client is not None
        # WRITE_CHAR (6c72) uses a Write Request (response=True). Over some
        # transports (notably the ESPHome Bluetooth proxy), the ATT Write
        # Response is never relayed back, so a plain response=True write blocks
        # ~30s. Issue the Write Request but cap the wait; the notification drain
        # in send() is the real ack. Over a direct link the response arrives in
        # <200ms so this returns immediately.
        try:
            await asyncio.wait_for(
                self._client.write_gatt_char(WRITE_CHAR, frame, response=True),
                timeout=WRITE_ACK_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            pass

    async def _drain(self, drain_ms: int) -> None:
        """Collect the device's reply, returning as soon as it goes quiet
        instead of always sleeping the full drain window. Wake on the first
        notification, then hold the window open only while frames keep arriving
        (NOTIF_QUIET_SEC between them), hard-capped at drain_ms so a silent
        device still returns on time. Call with _notif_buf freshly cleared."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + drain_ms / 1000.0
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            # Clear before snapshotting the buffer: a frame arriving from here
            # on re-sets the event and wakes us; a frame already buffered means
            # we're settling and should wait only a short quiet window for more.
            self._notif_event.clear()
            timeout = min(NOTIF_QUIET_SEC, remaining) if self._notif_buf else remaining
            try:
                await asyncio.wait_for(self._notif_event.wait(), timeout)
            except asyncio.TimeoutError:
                return  # no new frame within the window -> reply complete (or silent)

    async def send(self, plaintext: bytes, *, drain_ms: int = 1500) -> list[bytes]:
        """Encrypt + WRITE_REQ + drain notifications until the reply goes quiet
        (bounded by `drain_ms`). Returns the raw notification frames received."""
        async with self._lock:
            await self.ensure_connected()
            self._notif_buf.clear()
            await self._write_locked(plaintext)
            await self._drain(drain_ms)
            received = list(self._notif_buf)
            self._notif_buf.clear()
            self._arm_idle_timer()
            return received

    async def send_raw(self, plaintext: bytes) -> None:
        """Encrypt + WRITE_REQ with no notification drain. For init-step
        sequences where the caller batches drain after the last step."""
        async with self._lock:
            await self.ensure_connected()
            await self._write_locked(plaintext)

    async def send_actuation(self, plaintext: bytes, *, drain_ms: int = 1500) -> list[bytes]:
        """Re-run the per-device init sequence, then send a command — atomically.

        Some devices only honour a watering command in a freshly-initialised
        session: a pooled connection's per-class bind/init goes stale, so the
        command is ack'd but SILENTLY IGNORED (no actuation). Re-run the init
        hook before the command rather than trusting the pooled session. For a
        device class with no post-handshake hook this is just ensure_connected
        + send."""
        async with self._lock:
            if self.is_connected and self._handshaken:
                # Pooled connection — refresh the (possibly stale) bind in place.
                # But over an ESPHome proxy the pooled link can already be dead at
                # the GATT layer while is_connected still reads True, so the refresh
                # writes fail with BleakError ("Not connected"). Don't let that
                # surface as a failed actuation: drop the stale session and reopen
                # a fresh one (ensure_connected re-runs the hook). HW-observed on a
                # mesh HT25 STOP over a proxy — the very next fresh connect landed.
                try:
                    if self._post_handshake_hook is not None:
                        await self._post_handshake_hook(self)
                except (BleakError, OSError, asyncio.TimeoutError) as err:
                    _LOGGER.debug(
                        "%s: pooled bind refresh failed (%s) — reopening a fresh session",
                        self.mac, err,
                    )
                    await self.disconnect()
                    await self.ensure_connected()
            else:
                # Cold — ensure_connected() retries the open and runs the hook.
                await self.ensure_connected()
            self._notif_buf.clear()
            await self._write_locked(plaintext)
            await self._drain(drain_ms)
            received = list(self._notif_buf)
            self._notif_buf.clear()
            self._arm_idle_timer()
            return received


class BleNotConnectable(Exception):
    """Device not in range of any connectable BLE adapter."""


class BleHandshakeError(Exception):
    """AES handshake failed (bad key, bad device, or device in weird state)."""
