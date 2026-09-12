"""Home Assistant configuration and options flows."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import DEFAULT_EXTERNAL_SOURCE, DEFAULT_SOURCE, DOMAIN
from .config import (
    CONF_BUNDLED_FILES,
    CONF_BUNDLED_MODE,
    CONF_EXTERNAL_FILES,
    CONF_EXTERNAL_MODE,
    normalize_settings,
)
from .source import SOURCE_MODE_ALL, SOURCE_MODE_SELECTED, available_source_files


def _mode_selector() -> selector.SelectSelector:
    """Create the selector used for all-files versus selected-files mode."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                {"value": SOURCE_MODE_ALL, "label": "All files"},
                {"value": SOURCE_MODE_SELECTED, "label": "Selected files only"},
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _file_selector(options: list[str]) -> selector.SelectSelector:
    """Create a multi-select selector for relative TypeScript file names."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[{"value": item, "label": item} for item in options],
            multiple=True,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


class ZhaZ2mConvertersConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure converter file selection through the Home Assistant UI."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> ZhaZ2mConvertersOptionsFlow:
        """Return the options flow for an existing config entry."""
        return ZhaZ2mConvertersOptionsFlow()

    def __init__(self) -> None:
        self._settings = normalize_settings(None)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> Any:
        """Select file-selection modes for the initial config entry."""
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            self._settings = normalize_settings({**self._settings, **user_input})
            return await self.async_step_files()
        return self.async_show_form(step_id="user", data_schema=self._mode_schema(self._settings))

    async def async_step_files(self, user_input: dict[str, Any] | None = None) -> Any:
        """Select included or excluded files for both converter sources."""
        if user_input is not None:
            self._settings = normalize_settings({**self._settings, **user_input})
            return self.async_create_entry(title="ZHA Z2M Converters", data=self._settings)
        schema = await self._async_file_schema(self._settings)
        return self.async_show_form(
            step_id="files",
            data_schema=schema,
        )

    def _mode_schema(self, settings: dict[str, Any]) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_BUNDLED_MODE, default=settings[CONF_BUNDLED_MODE]): _mode_selector(),
                vol.Required(CONF_EXTERNAL_MODE, default=settings[CONF_EXTERNAL_MODE]): _mode_selector(),
            }
        )

    async def _async_file_schema(self, settings: dict[str, Any]) -> vol.Schema:
        bundled_files, external_files = await self.hass.async_add_executor_job(
            _available_file_options,
            DEFAULT_SOURCE,
            self.hass.config.path(DEFAULT_EXTERNAL_SOURCE),
        )
        return _file_schema(settings, bundled_files, external_files)


class ZhaZ2mConvertersOptionsFlow(config_entries.OptionsFlowWithReload):
    """Edit converter file selection for an existing config entry."""

    def __init__(self) -> None:
        self._settings: dict[str, Any] | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> Any:
        """Select file-selection modes before choosing individual files."""
        if self._settings is None:
            self._settings = normalize_settings(self.config_entry.options or self.config_entry.data)
        if user_input is not None:
            self._settings = normalize_settings({**self._settings, **user_input})
            return await self.async_step_files()
        return self.async_show_form(step_id="init", data_schema=self._mode_schema(self._settings))

    async def async_step_files(self, user_input: dict[str, Any] | None = None) -> Any:
        """Select included or excluded files for both converter sources."""
        if self._settings is None:
            self._settings = normalize_settings(self.config_entry.options or self.config_entry.data)
        if user_input is not None:
            self._settings = normalize_settings({**self._settings, **user_input})
            return self.async_create_entry(title="", data=self._settings)
        schema = await self._async_file_schema(self._settings)
        return self.async_show_form(step_id="files", data_schema=schema)

    def _mode_schema(self, settings: dict[str, Any]) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_BUNDLED_MODE, default=settings[CONF_BUNDLED_MODE]): _mode_selector(),
                vol.Required(CONF_EXTERNAL_MODE, default=settings[CONF_EXTERNAL_MODE]): _mode_selector(),
            }
        )

    async def _async_file_schema(self, settings: dict[str, Any]) -> vol.Schema:
        bundled_files, external_files = await self.hass.async_add_executor_job(
            _available_file_options,
            DEFAULT_SOURCE,
            self.hass.config.path(DEFAULT_EXTERNAL_SOURCE),
        )
        return _file_schema(settings, bundled_files, external_files)


def _valid_defaults(values: Any, available: list[str]) -> list[str]:
    """Keep persisted selections that still exist on disk."""
    if not isinstance(values, list):
        return []
    available_set = set(available)
    return sorted({value for value in values if isinstance(value, str) and value in available_set})


def _available_file_options(bundled_source: Any, external_source: Any) -> tuple[list[str], list[str]]:
    """Discover both file lists outside Home Assistant's event loop."""
    return available_source_files(bundled_source), available_source_files(external_source)


def _file_schema(settings: dict[str, Any], bundled_files: list[str], external_files: list[str]) -> vol.Schema:
    """Build a file-selection schema from already-discovered file names."""
    return vol.Schema(
        {
            vol.Required(
                CONF_BUNDLED_FILES,
                default=_valid_defaults(settings[CONF_BUNDLED_FILES], bundled_files),
            ): _file_selector(bundled_files),
            vol.Required(
                CONF_EXTERNAL_FILES,
                default=_valid_defaults(settings[CONF_EXTERNAL_FILES], external_files),
            ): _file_selector(external_files),
        }
    )
