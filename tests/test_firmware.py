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

    def __init__(self, *, status=0, version=0, size=0, offset=0, notify=True, validate=True,
                 require_response=True):
        self.status, self.version, self.size, self.offset = status, version, size, offset
        self.require_response = require_response
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
        assert char == WRITE_CHAR
        assert response is True or not self.require_response
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


# --- helpers -------------------------------------------------------------

def test_identify_image(monkeypatch):
    import hashlib
    img = b"fake-image"
    monkeypatch.setitem(fw.KNOWN_IMAGES, 7, (len(img), hashlib.sha256(img).hexdigest()))
    assert fw.identify_image(img) == 7
    assert fw.identify_image(b"other") is None


def test_find_download_urls_walks_nested_metadata():
    meta = [{"version": 85, "file": {"url": "https://x/fw?sig=1", "name": "fw"}}, "nope"]
    assert fw.find_download_urls(meta) == ["https://x/fw?sig=1"]
    assert fw.find_download_urls({"a": 1}) == []


@pytest.mark.parametrize(
    "hardware,ok",
    [("HT25-0000", True), ("HT25A-0001", False), ("HT34A-0001", False), (None, False)],
)
def test_recovery_supported(hardware, ok):
    assert fw.recovery_supported(hardware) is ok


# --- write-response detection + unacknowledged mode ---------------------

class SilentAckBootloader(FakeBootloader):
    """Like an ESPHome proxy: a Write Request never gets its response relayed."""

    async def write_gatt_char(self, char, data, response=None):
        self.modes = getattr(self, "modes", set()) | {response}
        if response:
            await asyncio.sleep(3600)  # never acked
        await FakeBootloader.write_gatt_char(self, char, data, response)


def test_detect_write_response_true_on_direct_link(monkeypatch):
    monkeypatch.setattr(fw, "WRITE_RESPONSE_PROBE_SEC", 0.05)
    session = fw.BootloaderSession(FakeBootloader())
    assert asyncio.run(session.detect_write_response()) is True
    assert session.write_response is True


def test_unacked_transfer_over_proxy(monkeypatch):
    monkeypatch.setattr(fw, "WRITE_RESPONSE_PROBE_SEC", 0.05)
    dev = SilentAckBootloader(require_response=False)

    async def go():
        session = fw.BootloaderSession(dev)
        await session.start_notify()
        assert await session.detect_write_response() is False
        dev.modes = set()
        return await session.upload(IMAGE, 85)

    final = asyncio.run(go())
    assert dev.modes == {False}
    assert final.status == fw.STATUS_VALIDATION_PASSED
    assert bytes(dev.received) == IMAGE


def _dropping(dev: FakeBootloader, drop_indices: set[int]):
    """Silently lose the given data writes, like an overrun proxy queue."""
    original = dev._accept
    count = {"n": 0}

    def accept(payload):
        n = count["n"]
        count["n"] += 1
        if n not in drop_indices:
            original(payload)
    dev._accept = accept


def test_unacked_transfer_restarts_after_dropped_block():
    dev = FakeBootloader(require_response=False)
    _dropping(dev, {20})  # mid-transfer, between offset checks
    session = fw.BootloaderSession(dev, write_response=False)

    async def go():
        await session.start_notify()
        return await session.upload(IMAGE, 85)

    final = asyncio.run(go())
    assert final.status == fw.STATUS_VALIDATION_PASSED
    assert bytes(dev.received) == IMAGE  # restart cleared the shifted data


def test_unacked_transfer_restarts_after_drop_in_last_blocks():
    dev = FakeBootloader(require_response=False)
    _dropping(dev, {64})  # the final 4-byte block (index 64 of 65): no checkpoint after it
    session = fw.BootloaderSession(dev, write_response=False)

    async def go():
        await session.start_notify()
        return await session.upload(IMAGE, 85)

    assert asyncio.run(go()).status == fw.STATUS_VALIDATION_PASSED
    assert bytes(dev.received) == IMAGE


def test_unacked_transfer_gives_up_after_max_restarts():
    dev = FakeBootloader(require_response=False)
    _dropping(dev, set(range(0, 10_000, 30)))  # drops every attempt
    session = fw.BootloaderSession(dev, write_response=False)

    async def go():
        await session.start_notify()
        await session.upload(IMAGE, 85)

    with pytest.raises(fw.BootloaderError):
        asyncio.run(go())


def test_acked_transfer_does_not_restart_on_drift():
    dev = FakeBootloader()
    _dropping(dev, {20})
    session = fw.BootloaderSession(dev, write_response=True)

    async def go():
        await session.start_notify()
        await session.upload(IMAGE, 85)

    with pytest.raises(fw.BootloaderError, match="expected"):
        asyncio.run(go())


def test_restart_rejected_by_device_says_to_pull_batteries():
    class NoRestart(FakeBootloader):
        async def write_gatt_char(self, char, data, response=None):
            if data[0] == fw.FLAG_COMMAND and data[2] == fw.CMD_START and self.status:
                return  # ignore start while a transfer is in progress
            await super().write_gatt_char(char, data, response)

    dev = NoRestart(require_response=False)
    _dropping(dev, {20})
    session = fw.BootloaderSession(dev, write_response=False)

    async def go():
        await session.start_notify()
        await session.upload(IMAGE, 85)

    with pytest.raises(fw.BootloaderError, match="batteries"):
        asyncio.run(go())


# --- bootloader detection ------------------------------------------------

def _client_with_chars(*uuids):
    from types import SimpleNamespace
    from orbit_bhyve.const import SERVICE_UUID

    service = SimpleNamespace(characteristics=[SimpleNamespace(uuid=u) for u in uuids])
    services = SimpleNamespace(get_service=lambda uuid: service if uuid == SERVICE_UUID else None)
    return SimpleNamespace(services=services)


def test_is_bootloader():
    from orbit_bhyve.const import AES_CHAR, NETWORK_CHAR

    assert fw.is_bootloader(_client_with_chars(WRITE_CHAR, READ_CHAR))
    assert not fw.is_bootloader(_client_with_chars(AES_CHAR, WRITE_CHAR, READ_CHAR, NETWORK_CHAR))
    assert not fw.is_bootloader(object())  # no service table: never break a connect


def test_connection_raises_bootloader_mode_without_retrying(monkeypatch):
    import sys
    import types as _types

    from orbit_bhyve import connection as conn_mod

    for name in ("homeassistant", "homeassistant.components",
                 "homeassistant.components.bluetooth"):
        monkeypatch.setitem(sys.modules, name, _types.ModuleType(name))
    monkeypatch.setattr(
        sys.modules["homeassistant.components.bluetooth"],
        "async_ble_device_from_address",
        lambda *a, **k: object(),
        raising=False,
    )
    calls = {"connect": 0}

    async def _fake_establish(*_a, **_k):
        calls["connect"] += 1
        client = _client_with_chars(WRITE_CHAR, READ_CHAR)
        client.is_connected = True

        async def _noop(*_a):
            pass
        client.disconnect = _noop
        client.stop_notify = _noop
        return client

    async def _no_sleep(*_a, **_k):
        pass

    monkeypatch.setattr(conn_mod, "establish_connection", _fake_establish)
    monkeypatch.setattr(conn_mod.asyncio, "sleep", _no_sleep)
    conn = conn_mod.BHyveBleConnection(None, "AA:BB:CC:DD:EE:FF", "00" * 16)

    with pytest.raises(conn_mod.BleBootloaderMode):
        asyncio.run(conn.ensure_connected())
    assert calls["connect"] == 1
    assert conn.in_bootloader is True
    # Still a BleHandshakeError, so existing handlers keep catching it.
    assert issubclass(conn_mod.BleBootloaderMode, conn_mod.BleHandshakeError)
