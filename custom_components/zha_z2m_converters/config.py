"""Configuration keys shared by the Home Assistant flows and runtime."""

from __future__ import annotations

from typing import Any

from .source import SOURCE_MODE_ALL, SOURCE_MODE_SELECTED

CONF_BUNDLED_MODE = "bundled_mode"
CONF_BUNDLED_FILES = "bundled_files"
CONF_EXTERNAL_MODE = "external_mode"
CONF_EXTERNAL_FILES = "external_files"


def default_settings() -> dict[str, Any]:
    """Return settings that enable every discovered converter file."""
    return {
        CONF_BUNDLED_MODE: SOURCE_MODE_ALL,
        CONF_BUNDLED_FILES: [],
        CONF_EXTERNAL_MODE: SOURCE_MODE_ALL,
        CONF_EXTERNAL_FILES: [],
    }


def normalize_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    """Merge persisted settings with defaults and discard malformed values."""
    normalized = default_settings()
    if not isinstance(settings, dict):
        return normalized
    for key in (CONF_BUNDLED_MODE, CONF_EXTERNAL_MODE):
        value = settings.get(key)
        if value in {SOURCE_MODE_ALL, SOURCE_MODE_SELECTED}:
            normalized[key] = value
    for key in (CONF_BUNDLED_FILES, CONF_EXTERNAL_FILES):
        value = settings.get(key)
        if isinstance(value, list):
            normalized[key] = sorted({item for item in value if isinstance(item, str)})
    return normalized
