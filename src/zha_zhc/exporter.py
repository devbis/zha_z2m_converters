"""Export normalized IR as reviewable Python metadata."""

from __future__ import annotations

import pprint
from pathlib import Path

from .mapping import normalize_device
from .model import DeviceDefinition


def export_python(devices: list[DeviceDefinition], path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    normalized = [normalize_device(device).to_dict() for device in devices]
    contents = (
        '"""Generated declarative converter metadata; no JavaScript is executed."""\n\n'
        f"DEVICES = {pprint.pformat(normalized, sort_dicts=False, width=120)}\n"
    )
    destination.write_text(contents, encoding="utf-8")
    return destination
