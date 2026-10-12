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
# Every cloud_id ever offered in a device picker — kept AND excluded. Lets the
# reconfigure flow tell "you unchecked this on purpose" apart from "this is new
# hardware", which CONF_DEVICES alone can't express (it only holds the keepers).
# Append-only, so a bug degrades to a device showing up pre-checked in a form
# the user is looking at, never to a device being permanently hidden.
CONF_KNOWN_CLOUD_IDS = "known_cloud_ids"
CONF_INCLUDE = "include"
# True on an entry set up without the Orbit cloud (import or manual entry). It
# has no email/password, and its device list only changes through Reconfigure.
CONF_LOCAL = "local"
CONF_IMPORT = "devices_import"
CONF_DEFAULT_DURATION = "default_duration_sec"
CONF_IDLE_DISCONNECT = "idle_disconnect_sec"
CONF_POLL_IDLE = "poll_idle_sec"
CONF_POLL_WATERING = "poll_watering_sec"
CONF_FLOW_COUNTS_PER_GALLON = "flow_counts_per_gallon"
CONF_MESH_STATUS_POLL = "mesh_status_poll"
CONF_HUB_MESH_OVERRIDES = "hub_mesh_overrides"

DEFAULT_DURATION = 600
DEFAULT_IDLE_DISCONNECT = 60
DEFAULT_POLL_IDLE = 900  # 15 min — an idle status poll (#15) connects over BLE, so keep
                         # the cadence gentle on the AA-powered valves; most program runs
                         # last longer and get picked up at least once. Tunable in the
                         # Configure dialog (Poll idle).
DEFAULT_POLL_WATERING = 30
# Opt-in, default OFF: an idle mesh (HT25) status poll must CONNECT over BLE
# (~96 connects/day at the default 15-min idle cadence, each a full bind/init
# on AA cells), unlike the protobuf family whose poll is already a connect.
# Without it, mesh idle state stays event-driven (acks + wall clock).
DEFAULT_MESH_STATUS_POLL = False

# HT25's magic2 init step references the paired hub's mesh id. The cloud
# /api/networks response doesn't always surface bridge_device_id, and without
# it the init falls back to a 0x0000 placeholder (see ht25.hub_mesh_address).
# This option lets a user supply the value per device rather than the
# integration shipping a lookup table. Format: comma-separated MAC=id pairs,
# id decimal or 0x-hex, e.g. "AA:BB:CC:DD:EE:FF=0xEB42, 11:22:33:44:55:66=9021".
# Read it off a BTSnoop capture of the vendor app's magic2 frame.
DEFAULT_HUB_MESH_OVERRIDES = ""

# Flow (Gen2 #59.#3) is a CUMULATIVE per-run volume counter in raw device units,
# not gpm. To convert the counter to gallons, divide by this factor (exposed as
# the "Flow calibration" option so a user can re-calibrate their own install).
# DEFAULT MEASURED 2026-07-03 on BTValve01: a 44.5 s flow window logged +1443
# counts while a bucket collected 3.33 gal over the same window → 1443 / 3.33 ≈
# 433 counts/gal (self-consistent: 3.33 gal / 44.5 s = 4.49 gpm matches the slope).
DEFAULT_FLOW_COUNTS_PER_GALLON = 433

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
#     "hub_mesh_device_id": int | None,
#     "network_key":    str (32 hex chars),
#     "battery_pct":    int | None,
#     "battery_mv":     int | None,
#   }
#
# Sibling of "devices" under entry.data:
#   "known_cloud_ids": list[str] — see CONF_KNOWN_CLOUD_IDS. Absent on entries
#   created before the reconfigure flow existed; readers must tolerate None.
#   "local": True — see CONF_LOCAL. Its records may lack the cloud-only fields
#   (mesh_id, bridge_device_id, battery_*), and a cloud_id of "local_<mac>"
#   stands in for one that never came from the cloud.
