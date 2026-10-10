"""Bootloader firmware-recovery protocol tests (firmware.py).

Frame vectors are the representative frames from issue #61, captured on real
HT25-0000 units recovering fw0085. The session tests run against a fake
bootloader; no hardware required.
"""
from __future__ import annotations

import asyncio
import struct

import pytest
from bleak.exc import BleakError

from orbit_bhyve import firmware as fw
from orbit_bhyve.const import READ_CHAR, WRITE_CHAR

FW85_SIZE = 52874


def _hex(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", ""))


# --- framing -------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("12 0e 0000 0000 0000 00000000 00000000 2000", fw.Progress(0, 0, 0, 0, 0)),
        ("12 0e 0000 0100 5500 00008ace 00000000 ce01", fw.Progress(0, 1, 85, FW85_SIZE, 0)),
        ("12 0e 0000 0100 5500 00008ace 10000000 de01", fw.Progress(0, 1, 85, FW85_SIZE, 16)),
        ("12 0e 0000 0300 5500 00008ace 8ace0000 2803", fw.Progress(0, 3, 85, FW85_SIZE, FW85_SIZE)),
    ],
)
def test_parse_progress_captured_frames(raw, expected):
    assert fw.parse_progress(_hex(raw)) == expected


def test_parse_progress_size_word_order_differs_from_offset():
    # Same bytes `8a ce` mean 52874 as size only when word-swapped.
    p = fw.parse_progress(_hex("12 0e 0000 0300 5500 00008ace 8ace0000 2803"))
    assert p.size == p.offset == FW85_SIZE


def test_parse_progress_rejects_bad_checksum():
    with pytest.raises(fw.BootloaderError, match="checksum"):
        fw.parse_progress(_hex("12 0e 0000 0000 0000 00000000 00000000 2100"))


@pytest.mark.parametrize("raw", ["12 0e 00", "13 0e" + "00" * 16, "12 0f" + "00" * 16])
def test_parse_progress_rejects_wrong_shape(raw):
    with pytest.raises(fw.BootloaderError):
        fw.parse_progress(_hex(raw))


def test_command_frames():
    assert fw.progress_command() == _hex("12 02 0000 1400")
    assert fw.install_command() == _hex("12 02 0200 1600")
    start = fw.start_command(85, FW85_SIZE)
    # Size goes out as ordinary u32LE — NOT word-swapped like the reply.
    assert start[:-2] == _hex("12 08 0100 5500 8ace0000")
    assert start[-2:] == struct.pack("<H", sum(start[:-2]))


def test_data_frame_unpadded_final_block():
    frame = fw.data_frame(b"\x01" * 10)
    assert frame[:2] == b"\x13\x0a"
    assert len(frame) == 2 + 10 + 2


@pytest.mark.parametrize("n", [0, 17])
def test_data_frame_rejects_bad_length(n):
    with pytest.raises(ValueError):
        fw.data_frame(b"\x00" * n)


# --- image verification --------------------------------------------------

def test_verify_image_rejects_wrong_bytes_for_known_version():
    with pytest.raises(fw.FirmwareImageError, match="mismatch"):
        fw.verify_image(b"\x00" * FW85_SIZE, 85)


def test_verify_image_refuses_unknown_version_unless_allowed():
    with pytest.raises(fw.FirmwareImageError, match="never been verified"):
        fw.verify_image(b"abc", 999)
    fw.verify_image(b"abc", 999, allow_unknown=True)


def test_verify_image_accepts_matching_hash(monkeypatch):
    import hashlib
    img = b"fake-image"
    monkeypatch.setitem(fw.KNOWN_IMAGES, 1, (len(img), hashlib.sha256(img).hexdigest()))
    fw.verify_image(img, 1)


# --- session against a fake bootloader ----------------------------------

def _progress_frame(status: int, version: int, size: int, offset: int) -> bytes:
    size_raw = ((size & 0xFFFF) << 16) | (size >> 16)
    payload = struct.pack("<HHHII", 0, status, version, size_raw, offset)
    return fw.build_frame(fw.FLAG_COMMAND, payload)


class FakeBootloader:
    """Minimal BleakClient stand-in that behaves like the HT25 bootloader."""

    def __init__(self, *, status=0, version=0, size=0, offset=0, notify=True, validate=True):
        self.status, self.version, self.size, self.offset = status, version, size, offset
        self.notify = notify
        self.validate = validate
        self.received = bytearray()
        self.installed = False
        self.fail_writes: set[int] = set()  # data-write indices that raise
        self.fail_after_apply: bool = False  # True: the failed write still landed
        self._data_writes = 0
        self._callback = None

    async def start_notify(self, char, cb):
        assert char == READ_CHAR
        self._callback = cb

    async def read_gatt_char(self, char):
        assert char == READ_CHAR
        return bytearray(self._frame())

    def _frame(self) -> bytes:
        return _progress_frame(self.status, self.version, self.size, self.offset)

    async def write_gatt_char(self, char, data, response=None):
        assert char == WRITE_CHAR and response is True
        data = bytes(data)
        assert sum(data[:-2]) & 0xFFFF == int.from_bytes(data[-2:], "little")
        flag, payload = data[0], data[2:-2]
        if flag == fw.FLAG_DATA:
            idx = self._data_writes
            self._data_writes += 1
            if idx in self.fail_writes:
                self.fail_writes.discard(idx)
                if self.fail_after_apply:
                    self._accept(payload)
                raise BleakError("simulated ambiguous write failure")
            self._accept(payload)
            return
        cmd = struct.unpack("<H", payload[:2])[0]
        if cmd == fw.CMD_START:
            _, self.version, self.size = struct.unpack("<HHI", payload)
            self.status, self.offset = fw.STATUS_RECEIVING, 0
            self.received.clear()
        elif cmd == fw.CMD_INSTALL:
            self.installed = True
        elif cmd == fw.CMD_PROGRESS and self.notify and self._callback:
            self._callback(None, bytearray(self._frame()))

    def _accept(self, payload: bytes) -> None:
        self.received += payload
        self.offset += len(payload)
        if self.offset == self.size:
            self.status = fw.STATUS_VALIDATION_PASSED if self.validate else fw.STATUS_VALIDATION_FAILED


IMAGE = bytes(range(256)) * 4 + b"tail"  # 1028 bytes: last block is 4, unpadded


async def _run(dev: FakeBootloader, image=IMAGE, version=85):
    session = fw.BootloaderSession(dev)
    await session.start_notify()
    seen = []
    final = await session.upload(image, version, lambda o, s: seen.append(o))
    return final, seen


def test_upload_fresh_transfer():
    dev = FakeBootloader()
    final, seen = asyncio.run(_run(dev))
    assert bytes(dev.received) == IMAGE
    assert final == fw.Progress(0, 3, 85, len(IMAGE), len(IMAGE))
    assert seen[-1] == len(IMAGE)
    assert not dev.installed


def test_upload_falls_back_to_read_when_no_notification(monkeypatch):
    monkeypatch.setattr(fw, "PROGRESS_TIMEOUT_SEC", 0.01)
    dev = FakeBootloader(notify=False)
    final, _ = asyncio.run(_run(dev))
    assert final.status == fw.STATUS_VALIDATION_PASSED


def test_upload_resumes_matching_partial_transfer():
    dev = FakeBootloader(status=1, version=85, size=len(IMAGE), offset=16)
    dev.received += IMAGE[:16]
    asyncio.run(_run(dev))
    assert bytes(dev.received) == IMAGE


def test_upload_restarts_partial_transfer_for_different_image():
    dev = FakeBootloader(status=1, version=84, size=len(IMAGE), offset=16)
    dev.received += b"x" * 16
    asyncio.run(_run(dev))
    assert dev.version == 85
    assert bytes(dev.received) == IMAGE


def test_upload_skips_already_validated_image():
    dev = FakeBootloader(status=3, version=85, size=len(IMAGE), offset=len(IMAGE))
    final, seen = asyncio.run(_run(dev))
    assert final.status == fw.STATUS_VALIDATION_PASSED
    assert seen == [] and dev.received == bytearray()


def test_upload_raises_on_validation_failure():
    dev = FakeBootloader(validate=False)
    with pytest.raises(fw.BootloaderError, match="rejected"):
        asyncio.run(_run(dev))


def test_upload_retries_write_that_did_not_land():
    dev = FakeBootloader()
    dev.fail_writes = {5}
    asyncio.run(_run(dev))
    assert bytes(dev.received) == IMAGE


def test_upload_does_not_resend_write_that_landed():
    dev = FakeBootloader()
    dev.fail_writes = {5}
    dev.fail_after_apply = True
    asyncio.run(_run(dev))
    assert bytes(dev.received) == IMAGE  # a resend would duplicate block 5


def test_upload_aborts_on_offset_drift():
    dev = FakeBootloader()
    original = dev._accept

    def drop_first(payload):
        dev._accept = original  # silently lose block 0
    dev._accept = drop_first
    with pytest.raises(fw.BootloaderError, match="expected"):
        asyncio.run(_run(dev))


def test_install_swallows_disconnect():
    class Dropping(FakeBootloader):
        async def write_gatt_char(self, char, data, response=None):
            raise BleakError("disconnected")
    asyncio.run(fw.BootloaderSession(Dropping()).install())
