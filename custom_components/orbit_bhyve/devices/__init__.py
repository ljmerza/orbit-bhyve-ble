"""Device-class registry + dispatcher.

Adding a future model: drop a new module under devices/ exposing a subclass
of BHyveBleDeviceBase with the right frame_magic / trailer_const / actuation
methods, then add a clause to resolve_device_class().
"""
from __future__ import annotations

from .base import BHyveBleDeviceBase, DeviceState, UnsupportedModel
from .hub import BHyveHubDevice
from .ht25 import BHyveHT25Device
from .ht25_fw0085 import BHyveHT25Fw0085Device
from .ht25g2 import BHyveHT25G2Device
from .ht34a import BHyveHT34ADevice

__all__ = [
    "BHyveBleDeviceBase",
    "DeviceState",
    "UnsupportedModel",
    "BHyveHubDevice",
    "BHyveHT25Device",
    "BHyveHT25Fw0085Device",
    "BHyveHT25G2Device",
    "BHyveHT34ADevice",
    "resolve_device_class",
    "build_device",
]


def resolve_device_class(*, hardware: str, firmware: str, type_: str) -> type[BHyveBleDeviceBase]:
    if type_ == "bridge":
        return BHyveHubDevice
    if (hardware or "").startswith("HT25"):
        # Gen2 HT25G2 valves (fw0111) share the "HT25" hardware prefix but
        # speak the protobuf protocol (frame magic 0x11) like the HT34A XD,
        # NOT the d7-47 mesh protocol of the HT25-0000 hose timers. Route
        # them away from the mesh classes before falling through. Match on
        # the hardware suffix or fw so HT25-0000 (fw0041/0085) is untouched.
        if (hardware or "").startswith("HT25G2") or firmware == "0111":
            return BHyveHT25G2Device
        # fw0085 (Deck) keeps the pre-fix code path that empirically actuated
        # on 2026-05-03. fw0041 (Hill, Corner) and any future fw use the
        # parameterized BHyveHT25Device.
        if firmware == "0085":
            return BHyveHT25Fw0085Device
        return BHyveHT25Device
    if (hardware or "").startswith("HT34"):
        # Both HT34A-0001 and HT34-0001 (fw0058) use the protobuf XD protocol.
        # The older HT34 sharing it is the stuartdenne fork's claim (2026-06-27),
        # not independently verified on hardware here.
        return BHyveHT34ADevice
    if (hardware or "").startswith("HT32"):
        # HT32A-0001 (fw0107) is the 2-port XD sibling of the HT34A: same
        # firmware, same protobuf-over-CRC16 protocol (magic 0x11), fewer
        # stations. Station count flows from the cloud record, so the 4-port
        # class handles a 2-port unit unchanged. Untested on hardware here
        # (issue #13) — same caveat as HT34A.
        return BHyveHT34ADevice
    raise UnsupportedModel(hardware or "?", firmware or "?")


def build_device(hass, record, **kwargs) -> BHyveBleDeviceBase:
    cls = resolve_device_class(
        hardware=record.get("hardware", ""),
        firmware=record.get("firmware", ""),
        type_=record.get("type", ""),
    )
    return cls(hass, record, **kwargs)
