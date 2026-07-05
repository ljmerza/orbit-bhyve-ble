# Extracting Your Network Key

*Reverse-engineering notes; imported from the maintainer's research project.*

The Orbit B-Hyve XD encrypts its BLE data channel using an AES-128 key that is **specific to your Orbit account**, not specific to the physical device. Anyone in possession of the key can control all devices on your account, so treat it like a password.

You do **not** normally extract the key by hand — this integration does it for you during setup. The manual paths below are documented for reference and troubleshooting.

## Option 1 — Let the integration fetch it (recommended)

Add the integration and enter your Orbit account email and password (**Settings → Devices & Services → Add Integration → "Orbit B-Hyve BLE"**). The config flow authenticates against the Orbit cloud, lists the devices on your account, and fetches each mesh's `network_key` automatically — you never handle the key yourself.

The cloud logic lives in `custom_components/orbit_bhyve/cloud.py` (`OrbitCloudClient.discover` → `login` → `list_devices` → `get_mesh`). Your credentials are used only for the one cloud authentication request at setup; after that, all control is local BLE.

## Option 2 — Cloud API direct call

To retrieve the key yourself, replicate what the integration does — two requests against the Orbit cloud REST API (base `https://api.orbitbhyve.com/v1`):

```
POST /v1/session
  orbit-app-id: Bhyve Dashboard
  body: {"session": {"email": "you@example.com", "password": "your-password"}}
  -> orbit_api_key: <jwt>

GET /v1/network_topologies        # this integration also tries /meshes and /networks
  orbit-app-id: Bhyve Dashboard
  orbit-api-key: <jwt>
  -> [{ ... "network_key": "<base64 AES-128 key>" ... }, ...]
```

Pick the topology/mesh containing your timer and base64-decode its `network_key` to the 16 raw bytes. See [`hermes-findings.md`](hermes-findings.md) for how this endpoint was discovered.

## Option 3 — From the Mobile App's Storage (advanced)

If you have a rooted Android device with the official B-Hyve app installed and paired, you can extract the key directly from the app's local storage without going through the cloud at all. This is useful for debugging cases where the cloud API path fails (rare, usually due to two-factor authentication or account changes).

The key lives in two places inside the app's data directory:

1. **HTTP cache.** The app caches the cloud response that delivered the key. Look in `/data/data/com.orbit.orbitsmarthome/cache/http-cache/` for files containing JSON like:
   ```json
   {"id":"...","user_id":"...","network_key":"<BASE64_NETWORK_KEY>","bridge_devices":[],"devices":[...]}
   ```
2. **MMKV persistent storage.** Tencent's MMKV format is used by the app for offline state. The relevant store is `mmkv-db-instance-users.<user_id>`. The MMKV format is a key-value flat file; the keys can be enumerated using any of the open-source MMKV reader libraries.

You can pull the cache file over ADB and grep it for `network_key`:

```bash
adb devices                                     # confirm the device is connected
adb shell run-as com.orbit.orbitsmarthome cat cache/http-cache/*   # rooted / debuggable build
```

This requires:
- An ADB-connected Android device with the B-Hyve app installed and signed in to your account.
- Either a rooted device, or `adb shell` access via a debug-enabled build (rare for production apps).

## Format of the Key

The key is **16 bytes (128 bits)**. The cloud API returns it as base64; the integration and CLI accept it as 32 hex characters. To convert base64 to hex:

```python
import base64
b64 = "YOUR_BASE64_KEY"
print(base64.b64decode(b64).hex())
```

Or in shell:

```bash
echo -n 'YOUR_BASE64_KEY' | base64 -d | xxd -p
```

## Security Considerations

- **Treat the key as a password.** Anyone with the key can issue valve commands to all devices on your account, and can decrypt any captured BLE traffic from those devices.
- The key is **regenerated** if you delete and recreate your Orbit account, but is **not** rotated automatically.
- Storing the key in the Home Assistant integration's config entry persists it in HA's config directory (encrypted at rest only if your filesystem is encrypted). If your HA installation is exposed to untrusted users, take normal precautions.
- This project never transmits your key anywhere. The integration uses it only to decrypt notifications from and encrypt commands to your local BLE device.

## Troubleshooting

**Setup says "authentication failed".**
- The Orbit cloud API uses OAuth-style flows that occasionally change. Try logging into the official app once to refresh your account state, then retry setup.
- If you have two-factor authentication enabled on your Orbit account, the cloud-API path will likely fail. Use Option 3 (mobile-app extraction) instead.

**Setup authenticates but reports "no devices found".**
- The cloud account must have at least one B-Hyve device registered.
- The first time you set up a B-Hyve, complete the registration in the official app first.

**The Home Assistant integration says "invalid_key".**
- The key must be exactly 32 hexadecimal characters (16 bytes). Strip any spaces, colons, or `0x` prefix before pasting.
