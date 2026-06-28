"""HT25-0000 fw0085 — thin variant of the base HT25 d7-47 device class.

The original standalone fw0085 implementation hardcoded the developer's own
timer mesh_id (0x47D7 → on-wire prefix "d747") into every frame, so the timer's
application parser silently dropped commands whose prefix didn't match its own
id — while the BLE link layer still ack'd every write, masking the fault during
init. That fix already lives in ht25.BHyveHT25Device (dynamic per-device
mesh_address). This class now INHERITS that single source of truth and overrides
only the one value that is claimed to differ for fw0085.
"""
from __future__ import annotations

from .ht25 import BHyveHT25Device


class BHyveHT25Fw0085Device(BHyveHT25Device):
    """HT25 single-station timer, firmware 0085.
    Identical to the base HT25 protocol in every frame, payload, and opcode.
    """

    # Base = 2 (fw0041 Hill, BTSnoop-confirmed). 3 retained from the legacy
    # fw0085 path purely to minimise change from the deployed version — NOT
    # confirmed. Flip to 2 if START is dropped. See module docstring.
    _rebind_sid_delta = 3
