# nRF52840 BLE Sniffer — Capture-Rig Setup

*Reverse-engineering notes; imported from the maintainer's research project and scrubbed of device-specific identifiers.*

This is the modern workflow for setting up an nRF52840 dongle (PCA10059) as a
Bluetooth LE sniffer for Wireshark, using the Rust-based `nrfutil`.

## Background: skip the manual ZIP download

Older guides tell you to manually download **nRF Sniffer for Bluetooth LE** from
Nordic's license-gated download page:

> https://www.nordicsemi.com/Products/Development-tools/nRF-Sniffer-for-Bluetooth-LE

That URL returns 404 in 2026 — Nordic restructured their site and **deprecated
the manual ZIP download** in favor of fetching the firmware via `nrfutil`. The
steps below auto-fetch everything, so there is no license-click dance and no
broken URLs to chase.

## The workflow

```bash
# 1. Download the modern Rust-based nrfutil (no auth, no license click)
curl -L -o ~/bin/nrfutil \
    https://files.nordicsemi.com/artifactory/swtools/external/nrfutil/executables/x86_64-unknown-linux-gnu/nrfutil
chmod +x ~/bin/nrfutil

# 2. Install the ble-sniffer module (auto-downloads the sniffer firmware too)
nrfutil install ble-sniffer

# 3. Install the device-flash module
nrfutil install device

# 4. Bootstrap the Wireshark extcap shim
nrfutil ble-sniffer bootstrap \
    --extcap-dir ~/.config/wireshark/extcap

# 5. Flash the dongle (in DFU mode: hold SW1 while plugging in)
nrfutil device program \
    --firmware ~/.nrfutil/share/nrfutil-ble-sniffer/firmware/sniffer_nrf52840dongle_nrf52840_4.1.1.zip \
    --serial-number <SERIAL_FROM_DEVICE_LIST> \
    --traits nordicDfu
```

Get `<SERIAL_FROM_DEVICE_LIST>` from `nrfutil device list`.

## Where the firmware lives

After `nrfutil install ble-sniffer`, the dongle firmware is at:

```
~/.nrfutil/share/nrfutil-ble-sniffer/firmware/sniffer_nrf52840dongle_nrf52840_4.1.1.zip
```

Other supported board firmwares (DK variants) are bundled there too — for the
PCA10059 dongle, use only the `_nrf52840dongle_` one.

## Why the manual download still appears in some guides

Older blog posts and older upstream project docs describe the manual ZIP flow
because that was the only option for years. Nordic added the
`nrfutil install ble-sniffer` automation in the Rust-based nrfutil rewrite (v7+,
released ~2023). Stick with the modern path: no version drift, no license-click
dance, no broken URLs.
