"""HT25G2 (Gen2 single-station hose timer) device class.

Protobuf protocol family — the SAME framing, cipher, and timerMode start
message as the HT34A XD timer (frame magic 0x11), NOT the d7-47 mesh
protocol the older HT25-0000 hose timers (fw0041/0085) speak. Sharing a
"HT25" hardware prefix with those mesh devices is the only thing they have
in common; the dispatcher in __init__.py disambiguates by hardware-suffix /
firmware so these land here instead of on BHyveHT25Device.

The protocol logic lives in `protobuf.BHyveProtobufDevice`, shared with the
HT34A XD: both are protobuf-family valves differing only in log label and
station count (1 here, via self.stations). Keeping a distinct class — rather
than routing the Gen2 prefix straight to the base — preserves the per-model
docstring/verification record and a stable name for the dispatcher.

Hardware-verified start AND stop on fw0111 valves (BTValve01-04) via the
standalone CLI, which drives byte-identical protobuf frames. Single station:
the device exposes one valve, addressed as wire station_id 0 (station 1 - 1).
"""
from __future__ import annotations

from .protobuf import BHyveProtobufDevice


class BHyveHT25G2Device(BHyveProtobufDevice):
    """Gen2 single-station valve (fw0111), protobuf protocol family."""

    log_label = "HT25G2"
    # Gen2 exposes an inline flow sensor (#57/#59); the XD does not. Verify on
    # hardware (`bhyve.py flow`) before trusting the reading — see the Phase 3
    # note in the capability-buildout plan.
    has_flow = True
