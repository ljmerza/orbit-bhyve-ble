"""HT34A / HT34 (XD timer) device class.

Protobuf-over-CRC16 protocol (the `OrbitPbApi_Message` schema from the APK),
shared across the XD family. HT34A-0001 (fw0107, originally ported from
upstream `wxfield/Orbit_B-Hyve_4Port_Controller`) is community-verified against
firmware 0107 (2026-06): zone start/stop actuate over BLE via Home Assistant,
both through an ESPHome Bluetooth proxy and a direct adapter. Also covers
HT34-0001 (fw0058) and the 2-port HT32A-0001 (fw0107), routed here as the XD
sibling of the 4-port HT34A — station count comes from the cloud record, so the
same class drives a 2-port unit unchanged (issue #13, untested on hardware). The
cipher/handshake is shared with HT25; only the inner plaintext (protobuf) and
magic byte (0x11) differ.

The protocol logic (framing, cipher, timerMode start/stop, confirm-and-retry,
protobuf RX status decode) lives in `protobuf.BHyveProtobufDevice`; this class
only carries the model's log label. Station addressing comes from
`self.stations` (4 here).
"""
from __future__ import annotations

from .protobuf import BHyveProtobufDevice


class BHyveHT34ADevice(BHyveProtobufDevice):
    """4-port XD timer. Ported from upstream; verified on fw0107 over BLE."""

    log_label = "HT34A"
