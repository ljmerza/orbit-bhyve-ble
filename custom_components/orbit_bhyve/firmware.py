"""HT25 bootloader firmware recovery over BLE.

An HT25 whose firmware update failed blinks white every ~2s and boots into its
bootloader. It still advertises the 0xfe32 service with 6c72 (write) and 6c73
(read/notify), but the 6c71 AES characteristic is gone and nothing is
encrypted. Re-sending the official image over this channel recovers it.

Protocol (from issue #61, verified on two HT25-0000 units with fw0085):
  Frame = flag:u8 | len:u8 | payload | checksum:u16LE
  checksum = sum(flag, len, payload) & 0xffff
  flag 0x12 commands: 0 = read progress, 1 = start (version u16, size u32),
                      2 = install validated image
  flag 0x13: up to 16 raw image bytes, sent sequentially, no address prefix.
  Writes are Write Requests (response=True); a rejected start comes back as an
  ATT error, so the write response matters here.

Progress reply quirk: the size field is two LE u16 words, HIGH word first,
while the received offset is an ordinary u32LE. Decoding both with one struct
gives a nonsense size. The start command sends size as ordinary u32LE — the
device rejects a word-swapped size.

No Home Assistant imports: the CLI in scripts/ drives this module directly.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import struct
from collections.abc import Callable
from dataclasses import dataclass

from bleak import BleakClient
from bleak.exc import BleakError

from .const import AES_CHAR, READ_CHAR, SERVICE_UUID, WRITE_CHAR

_LOGGER = logging.getLogger(__name__)

FLAG_COMMAND = 0x12
FLAG_DATA = 0x13
CMD_PROGRESS = 0
CMD_START = 1
CMD_INSTALL = 2

STATUS_IDLE = 0
STATUS_RECEIVING = 1
STATUS_VALIDATION_FAILED = 2  # from the vendor app; never observed on hardware
STATUS_VALIDATION_PASSED = 3

CHUNK_SIZE = 16
PROGRESS_FRAME_LEN = 18

# The vendor app's OTA-v1 path fetches HT25-0000 images under ht25-0001.
# Only this mapping has been verified; other families are unsupported.
FIRMWARE_HARDWARE_VERSION = {"HT25-0000": "ht25-0001"}

# SHA-256 of images that have recovered real hardware. The hash identifies the
# image the cloud served; it is not proof of authenticity.
KNOWN_IMAGES: dict[int, tuple[int, str]] = {
    85: (52874, "adc4002dc1830f27aa02001c47490cf0ecd6a09ccaeee95ea1d015b7f126abb3"),
}

PROGRESS_TIMEOUT_SEC = 3.0
# Check the device's offset after the first block, then every N blocks.
PROGRESS_CHECK_EVERY = 256
# Some transports (ESPHome BLE proxies, per connection.py) never relay the ATT
# Write Response. Without it a lost block goes unnoticed until the next offset
# check, and since data frames carry no address the device has already appended
# the following blocks at the wrong place. So check far more often, and restart
# the transfer instead of resuming it. Only #61's with-response mode is verified
# on hardware.
NO_RESPONSE_CHECK_EVERY = 16
MAX_RESTARTS = 2
WRITE_RESPONSE_PROBE_SEC = 2.0
# Retries per block after an ambiguous write failure. Each retry re-reads the
# device's offset first rather than blindly resending.
MAX_WRITE_RETRIES = 3


class BootloaderError(Exception):
    """The bootloader replied with something unexpected; transfer aborted."""


class FirmwareImageError(Exception):
    """The image doesn't match what we know to be safe to send."""


@dataclass(frozen=True)
class Progress:
    command: int
    status: int
    version: int
    size: int
    offset: int


def build_frame(flag: int, payload: bytes) -> bytes:
    if len(payload) > 0xFF:
        raise ValueError("payload too long")
    body = bytes([flag, len(payload)]) + payload
    return body + struct.pack("<H", sum(body) & 0xFFFF)


def progress_command() -> bytes:
    return build_frame(FLAG_COMMAND, struct.pack("<H", CMD_PROGRESS))


def start_command(version: int, size: int) -> bytes:
    return build_frame(FLAG_COMMAND, struct.pack("<HHI", CMD_START, version, size))


def install_command() -> bytes:
    return build_frame(FLAG_COMMAND, struct.pack("<H", CMD_INSTALL))


def data_frame(chunk: bytes) -> bytes:
    if not 0 < len(chunk) <= CHUNK_SIZE:
        raise ValueError(f"chunk must be 1-{CHUNK_SIZE} bytes")
    return build_frame(FLAG_DATA, chunk)


def parse_progress(raw: bytes) -> Progress:
    raw = bytes(raw)
    if len(raw) != PROGRESS_FRAME_LEN or raw[:2] != bytes([FLAG_COMMAND, 0x0E]):
        raise BootloaderError(f"unexpected progress frame {raw.hex()}")
    if sum(raw[:-2]) & 0xFFFF != int.from_bytes(raw[-2:], "little"):
        raise BootloaderError(f"bad checksum on progress frame {raw.hex()}")
    command, status, version, size_raw, offset = struct.unpack("<HHHII", raw[2:-2])
    size = ((size_raw & 0xFFFF) << 16) | (size_raw >> 16)
    return Progress(command, status, version, size, offset)


def verify_image(image: bytes, version: int, *, allow_unknown: bool = False) -> None:
    """Refuse an image whose size/hash doesn't match a known-good one."""
    known = KNOWN_IMAGES.get(version)
    digest = hashlib.sha256(image).hexdigest()
    if known is None:
        if not allow_unknown:
            raise FirmwareImageError(
                f"fw{version:04d} (sha256 {digest}) has never been verified on "
                "hardware; refusing without allow_unknown"
            )
        return
    size, sha = known
    if len(image) != size or digest != sha:
        raise FirmwareImageError(
            f"fw{version:04d} mismatch: got {len(image)} bytes sha256 {digest}, "
            f"expected {size} bytes sha256 {sha}"
        )


def recovery_supported(hardware: str | None) -> bool:
    return hardware in FIRMWARE_HARDWARE_VERSION


def identify_image(image: bytes) -> int | None:
    """Version of the known-good image `image` is, or None if it's none of them."""
    digest = hashlib.sha256(image).hexdigest()
    for version, (size, sha) in KNOWN_IMAGES.items():
        if len(image) == size and digest == sha:
            return version
    return None


def find_download_urls(meta) -> list[str]:
    """Every http(s) string in the firmware-metadata response.

    The endpoint's schema isn't documented; #61 only says it carries metadata
    and a temporary download URL. Callers require exactly one.
    """
    if isinstance(meta, dict):
        return [u for v in meta.values() for u in find_download_urls(v)]
    if isinstance(meta, list):
        return [u for v in meta for u in find_download_urls(v)]
    if isinstance(meta, str) and meta.startswith("http"):
        return [meta]
    return []


def is_bootloader(client: BleakClient) -> bool:
    """fe32 present with 6c72/6c73 but no 6c71 AES characteristic.

    False whenever the service table can't be inspected: this runs on every
    normal connect, so it must never break the regular handshake path.
    """
    try:
        service = client.services.get_service(SERVICE_UUID)
    except (AttributeError, BleakError):
        return False
    if service is None:
        return False
    chars = {c.uuid.lower() for c in service.characteristics}
    return (
        AES_CHAR not in chars
        and WRITE_CHAR in chars
        and READ_CHAR in chars
    )


ProgressCallback = Callable[[int, int], None]


class BootloaderSession:
    """Drives one firmware transfer over an already-connected BleakClient.

    write_response=True is #61's verified mode. Pass False, or call
    detect_write_response(), for a transport that doesn't relay write responses.
    """

    def __init__(self, client: BleakClient, *, write_response: bool = True) -> None:
        self._client = client
        self._write_response = write_response
        self._latest: Progress | None = None
        self._event = asyncio.Event()

    @property
    def write_response(self) -> bool:
        return self._write_response

    async def start_notify(self) -> None:
        await self._client.start_notify(READ_CHAR, self._on_notify)

    def _on_notify(self, _sender, data: bytearray) -> None:
        try:
            self._latest = parse_progress(bytes(data))
        except BootloaderError as err:
            _LOGGER.debug("ignoring bootloader notification: %s", err)
            return
        self._event.set()

    async def _write(self, frame: bytes) -> None:
        await self._client.write_gatt_char(WRITE_CHAR, frame, response=self._write_response)

    async def detect_write_response(self) -> bool:
        """Send a harmless progress query as a Write Request and see if it acks.

        Sets the session's write mode from the answer and returns it.
        """
        try:
            await asyncio.wait_for(
                self._client.write_gatt_char(WRITE_CHAR, progress_command(), response=True),
                WRITE_RESPONSE_PROBE_SEC,
            )
            self._write_response = True
        except asyncio.TimeoutError:
            self._write_response = False
        # Let the probe's own progress notification land before the next query.
        await asyncio.sleep(0.5)
        return self._write_response

    async def read_progress(self) -> Progress:
        """Ask for progress; take the notification, else read 6c73 directly."""
        self._event.clear()
        await self._write(progress_command())
        try:
            await asyncio.wait_for(self._event.wait(), PROGRESS_TIMEOUT_SEC)
            assert self._latest is not None
            return self._latest
        except asyncio.TimeoutError:
            raw = await self._client.read_gatt_char(READ_CHAR)
            return parse_progress(bytes(raw))

    async def _start(self, version: int, size: int) -> None:
        await self._write(start_command(version, size))
        progress = await self.read_progress()
        try:
            self._expect(progress, STATUS_RECEIVING, version, size, 0)
        except BootloaderError as err:
            raise BootloaderError(
                f"device did not (re)start the transfer: {err}. Pulling the "
                "batteries resets the bootloader to idle; then retry."
            ) from err

    async def upload(
        self,
        image: bytes,
        version: int,
        on_progress: ProgressCallback | None = None,
    ) -> Progress:
        """Send `image`, resuming a matching in-progress transfer if present.

        Returns the final progress frame, which is guaranteed to show
        validation passed with the full image received. Does NOT install.
        """
        size = len(image)
        check_every = PROGRESS_CHECK_EVERY if self._write_response else NO_RESPONSE_CHECK_EVERY
        progress = await self.read_progress()
        _LOGGER.debug("initial bootloader progress: %s", progress)

        if (
            progress.status == STATUS_RECEIVING
            and progress.version == version
            and progress.size == size
            and 0 < progress.offset <= size
        ):
            offset = progress.offset
            _LOGGER.info("resuming transfer at offset %d/%d", offset, size)
        elif progress.status == STATUS_VALIDATION_PASSED and (
            progress.version == version and progress.size == progress.offset == size
        ):
            _LOGGER.info("image already received and validated")
            return progress
        else:
            await self._start(version, size)
            offset = 0

        restarts = 0
        blocks_sent = 0
        while offset < size:
            chunk = image[offset:offset + CHUNK_SIZE]
            offset = await self._send_block(chunk, offset, version, size)
            blocks_sent += 1
            if blocks_sent == 1 or blocks_sent % check_every == 0 or offset == size:
                progress = await self.read_progress()
                # After the last block the device leaves RECEIVING for a
                # validation verdict; the check after the loop judges that.
                finished = offset == size and progress.status != STATUS_RECEIVING
                if not finished and not self._offset_ok(progress, version, size, offset):
                    if self._write_response or restarts >= MAX_RESTARTS:
                        self._expect(progress, STATUS_RECEIVING, version, size, offset)
                    restarts += 1
                    _LOGGER.warning(
                        "offset drift (sent %d, device has %d); restarting transfer (%d/%d)",
                        offset, progress.offset, restarts, MAX_RESTARTS,
                    )
                    await self._start(version, size)
                    offset = blocks_sent = 0
            if on_progress:
                on_progress(offset, size)

        progress = await self.read_progress()
        if progress.status == STATUS_VALIDATION_FAILED:
            raise BootloaderError(f"device rejected the image: {progress}")
        self._expect(progress, STATUS_VALIDATION_PASSED, version, size, size)
        return progress

    async def _send_block(self, chunk: bytes, offset: int, version: int, size: int) -> int:
        """Write one block; on an ambiguous failure, trust the device's offset."""
        for attempt in range(MAX_WRITE_RETRIES + 1):
            try:
                await self._write(data_frame(chunk))
                return offset + len(chunk)
            except (BleakError, asyncio.TimeoutError) as err:
                if attempt == MAX_WRITE_RETRIES:
                    raise
                _LOGGER.warning("block write at %d failed (%s); re-reading offset", offset, err)
                progress = await self.read_progress()
                self._expect(progress, STATUS_RECEIVING, version, size, None)
                if progress.offset == offset + len(chunk):
                    return progress.offset
                if progress.offset != offset:
                    raise BootloaderError(
                        f"device offset {progress.offset} after failed write at {offset}"
                    ) from err
        raise AssertionError("unreachable")

    async def install(self) -> None:
        """Tell the bootloader to install. The device drops the link as it reboots."""
        try:
            await self._write(install_command())
        except (BleakError, asyncio.TimeoutError) as err:
            _LOGGER.debug("install write ended with %s (expected on reboot)", err)

    @staticmethod
    def _offset_ok(progress: Progress, version: int, size: int, offset: int) -> bool:
        return (
            progress.status == STATUS_RECEIVING
            and progress.version == version
            and progress.size == size
            and progress.offset == offset
        )

    @staticmethod
    def _expect(
        progress: Progress, status: int, version: int, size: int, offset: int | None
    ) -> None:
        if (
            progress.status != status
            or progress.version != version
            or progress.size != size
            or (offset is not None and progress.offset != offset)
        ):
            raise BootloaderError(
                f"expected status={status} version={version} size={size} "
                f"offset={offset}, got {progress}"
            )
