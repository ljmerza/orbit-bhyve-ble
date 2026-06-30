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
WRITE_REQ (response=True) is required — WRITE_CMD is silently dropped. The
device acks via NOTIFICATION, not an ATT Write Response; the ESPHome BLE proxy
doesn't relay the (absent) write response, so writes are sent with a capped
wait (WRITE_ACK_TIMEOUT_SEC) and the notification drain is the real ack.
"""
from __future__ import annotations

import asyncio
import logging
import os
import struct
from collections.abc import Awaitable, Callable

from bleak import BleakClient
from bleak_retry_connector import establish_connection
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import AES_CHAR, READ_CHAR, WRITE_CHAR

_LOGGER = logging.getLogger(__name__)

# Cap on waiting for an ATT Write Response that the device never sends (it acks
# via notification instead). Over a direct link the response arrives in <200ms;
# over the ESPHome proxy it never comes, so we proceed after this. The device's
# notification ack arrives in ~50-150ms, so this is a generous floor that still
# keeps cold-start init (8 writes) under ~7s.
WRITE_ACK_TIMEOUT_SEC = 0.8

# The connect succeeds even on a weak proxy link, but the post-connect handshake
# (subscribe + AES read/write) can stall on a marginal signal. Bound it so it
# fails cleanly instead of wedging the connection, and retry the whole open a
# few times — a clean retry often catches a good GATT window.
HANDSHAKE_TIMEOUT_SEC = 10.0
OPEN_MAX_ATTEMPTS = 3

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
        idle_disconnect_sec: int = 60,
    ):
        self.hass = hass
        self.mac = mac
        self._key = bytes.fromhex(network_key)
        self._frame_magic = frame_magic & 0xFF
        self._trailer_const = trailer_const & 0xFF
        self._idle_sec = idle_disconnect_sec

        self._client: BleakClient | None = None
        self._iv: bytes | None = None
        self._tx_ctr: int = 0
        self._rx_ctr: int = 0
        self._lock = asyncio.Lock()
        self._notif_buf: list[bytes] = []
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
        """Connect + handshake if not already pooled, retrying the whole open a
        few times. On a marginal proxy link the connect succeeds but the
        handshake GATT exchange can stall; a clean retry usually gets through,
        and the bounded handshake means we fail cleanly rather than wedging.
        Call inside a lock if you need exclusive access; idempotent otherwise."""
        if self.is_connected and self._handshaken:
            return
        last_err: Exception | None = None
        for attempt in range(1, OPEN_MAX_ATTEMPTS + 1):
            try:
                await self._open()
                return
            except (BleHandshakeError, asyncio.TimeoutError) as err:
                last_err = err
                _LOGGER.debug(
                    "%s: open attempt %d/%d failed: %s",
                    self.mac, attempt, OPEN_MAX_ATTEMPTS, err,
                )
                await self.disconnect()
                if attempt < OPEN_MAX_ATTEMPTS:
                    await asyncio.sleep(0.5)
        raise BleHandshakeError(
            f"{self.mac}: handshake failed after {OPEN_MAX_ATTEMPTS} attempts: {last_err}"
        )

    async def _open(self) -> None:
        from homeassistant.components.bluetooth import async_ble_device_from_address

        ble_device = async_ble_device_from_address(self.hass, self.mac, connectable=True)
        if ble_device is None:
            raise BleNotConnectable(f"{self.mac}: not in range of any connectable BLE adapter")

        _LOGGER.debug("%s: connecting", self.mac)
        self._client = await establish_connection(BleakClient, ble_device, self.mac, max_attempts=3)
        _LOGGER.debug("%s: connected", self.mac)

        # Note: tried writing the provisioning frame [0x01 0x00 || key] to
        # NETWORK_CHAR (0x6c76) here — char is firmware-locked on every
        # device tested ("Write not permitted"). Confirmed not the actual
        # mechanism for fw0041 the v1 commit fad91eae assumed.

        # The post-connect handshake (subscribe + AES read/write) is the part
        # that stalls on a marginal link, so bound it — ensure_connected()
        # retries the whole open on timeout.
        try:
            await asyncio.wait_for(self._handshake(), timeout=HANDSHAKE_TIMEOUT_SEC)
        except asyncio.TimeoutError as err:
            raise BleHandshakeError(
                f"{self.mac}: handshake timed out after {HANDSHAKE_TIMEOUT_SEC}s"
            ) from err

        # One-shot GATT enumeration — looking for standard Battery Service
        # (0x180F / 0x2A19) or anything else the device exposes that we
        # haven't been using. Logged at INFO for reverse-engineering work.
        try:
            for service in self._client.services:
                _LOGGER.info("%s: gatt svc %s", self.mac, service.uuid)
                for char in service.characteristics:
                    _LOGGER.info(
                        "%s:   char %s props=%s",
                        self.mac, char.uuid, list(char.properties),
                    )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("%s: gatt enum failed: %s", self.mac, err)

        if self._post_handshake_hook is not None:
            await self._post_handshake_hook(self)

    async def _handshake(self) -> None:
        """Subscribe + AES handshake. Bounded by a timeout in _open() because
        these GATT reads/writes are what stall on a weak link."""
        # Subscribe BEFORE writing — device may stay silent otherwise.
        self._notif_buf.clear()
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
        self._notif_buf.append(frame)
        if not self._handshaken or self._iv is None:
            return
        try:
            pt = self.decrypt(frame)
            _LOGGER.info(
                "%s: notif pt=%s (ct=%s)",
                self.mac, pt.hex(), frame.hex(),
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("%s: notif decrypt failed: %s raw=%s", self.mac, err, frame.hex())
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
        """Encrypt + WRITE_REQ. Caller MUST already hold self._lock and have
        an established connection. Used by send()/send_raw() and by the
        post-handshake hook (which runs inside _open() inside the lock)."""
        frame = self.encrypt(plaintext)
        assert self._client is not None
        # WRITE_CHAR (6c72) requires a Write Request (response=True) — the device
        # silently drops Write Commands. But it answers via NOTIFICATION and, on
        # some transports (notably the ESPHome Bluetooth proxy), the ATT Write
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

    async def send(self, plaintext: bytes, *, drain_ms: int = 1500) -> list[bytes]:
        """Encrypt + WRITE_REQ + drain notifications for `drain_ms`. Returns
        the list of raw notification frames received."""
        async with self._lock:
            await self.ensure_connected()
            self._notif_buf.clear()
            await self._write_locked(plaintext)
            await asyncio.sleep(drain_ms / 1000.0)
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

        HT25 only honours a watering command in a freshly-initialised session:
        a pooled connection's bind goes stale, so the command is ack'd but
        SILENTLY IGNORED (no actuation). Re-run the full init before every
        actuation rather than trusting the pooled session."""
        async with self._lock:
            if self.is_connected and self._handshaken:
                # Pooled connection — refresh the (possibly stale) bind in place.
                if self._post_handshake_hook is not None:
                    await self._post_handshake_hook(self)
            else:
                # Cold — ensure_connected() retries the open and runs the hook.
                await self.ensure_connected()
            self._notif_buf.clear()
            await self._write_locked(plaintext)
            await asyncio.sleep(drain_ms / 1000.0)
            received = list(self._notif_buf)
            self._notif_buf.clear()
            self._arm_idle_timer()
            return received


class BleNotConnectable(Exception):
    """Device not in range of any connectable BLE adapter."""


class BleHandshakeError(Exception):
    """AES handshake failed (bad key, bad device, or device in weird state)."""
