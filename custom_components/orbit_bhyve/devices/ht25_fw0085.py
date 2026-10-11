"""HT25-0000 fw0085 — thin variant of the base HT25 d7-47 device class.

The original standalone fw0085 implementation hardcoded the developer's own
timer mesh_id (0x47D7 → on-wire prefix "d747") into every frame, so the timer's
application parser silently dropped commands whose prefix didn't match its own
id — while the BLE link layer still ack'd every write, masking the fault during
init. That fix already lives in ht25.BHyveHT25Device (dynamic per-device
mesh_address). This class now INHERITS that single source of truth. Its one
override, a rebind "sid delta" of 3, was dropped once bind/rebind were found to
be clock writes (2026-10-11); the class is kept so routing and tests stay put.
"""
from __future__ import annotations

from .ht25 import BHyveHT25Device


class BHyveHT25Fw0085Device(BHyveHT25Device):
    """HT25 single-station timer, firmware 0085.
    Identical to the base HT25 protocol in every frame, payload, and opcode.
    """
