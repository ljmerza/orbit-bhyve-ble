"""HT25A-0001 (Gen2 hose-tap timer, 90205Z, fw0098) device class.

Same protobuf protocol family as the HT25G2 (frame magic 0x11, AES-CTR, #16
status decode) — but its firmware handles the `timerMode` messages differently
from the fw0111 Gen2 and the HT34A XD. Hardware-verified 2026-09-14 on a
fw0098 unit (see docs/ble-reliability-and-behavior.md, "HT25A-0001 fw0098
quirks"):

- The shared STOP (`timerMode{mode=manualMode, manualModeParams={}}`) does NOT
  stop a run. It (re)starts a manual run of the device's default 1800 s —
  on a running device it replaces the run, on an idle device it opens the valve.
- `timerMode{mode=offMode, manualModeParams={}}` DOES stop the run, so that is
  this class's stop frame. A zero-second manual run also closed the valve and
  is kept as the fallback.
- The `runTimeSec` we send in a manual run is ignored: every run lasts the
  device default (1800 s). The protobuf base enforces the requested duration
  from the host with a wall-clock timer that sends the stop.
"""
from __future__ import annotations

from .ht25g2 import BHyveHT25G2Device
from .protobuf import _build_set_timer_mode_pb, _build_start_pb


class BHyveHT25ADevice(BHyveHT25G2Device):
    """Gen2 hose-tap timer HT25A-0001 (fw0098) — offMode stop, host-timed runs."""

    log_label = "HT25A"
    # The device runs its default 1800 s regardless of the requested duration.
    duration_honored = False

    def _stop_frames(self) -> list[bytes]:
        # offMode is the verified stop; the zero-second run is the fallback.
        return [_build_set_timer_mode_pb(0), _build_start_pb(0, 0)]
