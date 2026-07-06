"""Test bootstrap.

The integration's package __init__ pulls in Home Assistant (voluptuous, etc.),
which we don't want to require just to exercise the pure protocol/dispatch
logic. Register a lightweight `orbit_bhyve` namespace package whose __path__
points at the real source dir, so relative imports inside the submodules resolve
without executing the HA-heavy __init__. connection.py's only HA import is
function-local, so importing the device/protocol modules needs just bleak +
cryptography, both available.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

_CC = Path(__file__).resolve().parent.parent / "custom_components"

if "orbit_bhyve" not in sys.modules:
    pkg = types.ModuleType("orbit_bhyve")
    pkg.__path__ = [str(_CC / "orbit_bhyve")]
    sys.modules["orbit_bhyve"] = pkg
