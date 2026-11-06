"""WT25G2 (B-hyve Indoor/Outdoor sprinkler controller) device class.

`WT` is Orbit's mains-powered multi-station controller line (57946 6-station,
57950 12-station); the cloud record reports `type=sprinkler_timer` with
`num_stations` of 6 or 12. It is unrelated to the `HT25G2` hose valve despite
the similar model string.

It uses the same protobuf protocol as the HT34A XD and HT25G2: frame magic 0x11,
`aa775a0f` inner header, `OrbitPbApi_Message` schema, and the GATT service and
characteristics in `const.py`. Tested on WT25G2-0001, firmware 0096, 12 stations:
handshake, #15/#16 status, zone start (#14) and stop (#30).

Station count comes from the cloud record via `self.stations`.

`has_flow` is False: whether the controller exposes the #57/#59 flow sensor of
the Gen2 hose valve is untested.
"""
from __future__ import annotations

from .protobuf import BHyveProtobufDevice


class BHyveWT25G2Device(BHyveProtobufDevice):
    """Multi-station sprinkler controller (fw0096), protobuf protocol family."""

    log_label = "WT25G2"
    # The WT25G2 refuses ATT long writes at MTU 23, so long messages go as frames.
    SPLIT_WRITES = True
