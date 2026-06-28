"""Constants for the Orbit B-Hyve integration."""
from __future__ import annotations

DOMAIN = "orbit_bhyve"

# GATT characteristics — same across all BHyve BLE models seen so far.
AES_CHAR = "00006c71-fe32-4f58-8b78-98e42b2c047f"
WRITE_CHAR = "00006c72-fe32-4f58-8b78-98e42b2c047f"
READ_CHAR = "00006c73-fe32-4f58-8b78-98e42b2c047f"
# 4th char on the 0xfe32 service. Write-only. v1 hypothesis was that the
# phone writes [0x01 0x00 || network_key] here before the AES handshake
# (commit fad91eae) and that this is what unblocks fw0041 — but in
# practice the char is firmware-locked ("Write not permitted") on every
# device tested. Left here as documentation; do not write to it without
# new evidence about how the phone actually unlocks fw0041.
NETWORK_CHAR = "00006c76-fe32-4f58-8b78-98e42b2c047f"

SERVICE_UUID = "0000fe32-0000-1000-8000-00805f9b34fb"

# Cloud API.
CLOUD_API_BASE = "https://api.orbitbhyve.com/v1"
CLOUD_APP_ID = "Bhyve-App"
# Orbit's WAF 403s any request whose User-Agent contains "HomeAssistant"
# (which HA's shared aiohttp session sets by default). Send a browser UA on
# cloud calls so setup isn't rejected. See sebr/bhyve-home-assistant#427.
CLOUD_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/72.0.3626.81 Safari/537.36"
)
CLOUD_KEY_PATHS = (
    "/meshes/{mesh_id}",
    "/network_topologies/{mesh_id}",
    "/networks/{mesh_id}",
)
CLOUD_KEY_FIELDS = ("ble_network_key", "network_key")

# Config / options keys.
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_DEVICES = "devices"
CONF_INCLUDE = "include"
CONF_DEFAULT_DURATION = "default_duration_sec"
CONF_IDLE_DISCONNECT = "idle_disconnect_sec"
CONF_POLL_IDLE = "poll_idle_sec"
CONF_POLL_WATERING = "poll_watering_sec"

DEFAULT_DURATION = 600
DEFAULT_IDLE_DISCONNECT = 60
DEFAULT_POLL_IDLE = 300
DEFAULT_POLL_WATERING = 30

# Stored shape per device under entry.data["devices"]:
#   {
#     "cloud_id":       str,
#     "name":           str,
#     "mac":            str (XX:XX:XX:XX:XX:XX, upper),
#     "type":           "sprinkler_timer" | "bridge",
#     "hardware":       str (e.g. "HT25-0000"),
#     "firmware":       str (canonical 4-digit, e.g. "0085"),
#     "stations":       int,
#     "mesh_id":        str,
#     "mesh_device_id": int | None,
#     "bridge_device_id": str | None,
#     "network_key":    str (32 hex chars),
#     "battery_pct":    int | None,
#     "battery_mv":     int | None,
#   }
