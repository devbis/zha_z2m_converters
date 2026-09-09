"""Safe, declarative zigbee-herdsman-converters bridge for ZHA."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .exporter import export_python
from .coverage import CoverageReport, build_report, format_report
from .model import ConfigureAction, DeviceDefinition, EndpointCluster, ParseResult
from .parser import parse_path, parse_source
from .runtime import (
    RuntimePlan,
    RuntimeReport,
    RuntimeWrite,
    apply_report,
    build_runtime_plan,
    make_write,
    register_result,
    register_with_zha,
)
from .source import SourceFile, load_source, load_sources

_LOGGER = logging.getLogger(__name__)
DOMAIN = "zha_z2m_converters"
DEFAULT_SOURCE = "/config/zha_z2m_converters/converters"


def _select_devices(devices: list[DeviceDefinition], selectors: Any) -> list[DeviceDefinition]:
    """Select configured definitions, or keep the complete source when unset."""
    if selectors is None:
        return devices
    if not isinstance(selectors, list):
        _LOGGER.error("The zha_z2m_converters devices option must be a list")
        return []

    normalized: list[tuple[str | None, str | None]] = []
    for selector in selectors:
        if not isinstance(selector, dict):
            _LOGGER.error("Ignoring invalid zha_z2m_converters device selector: %r", selector)
            continue
        manufacturer = selector.get("manufacturer")
        model = selector.get("model")
        if manufacturer is not None and not isinstance(manufacturer, str):
            _LOGGER.error("Ignoring device selector with a non-string manufacturer: %r", selector)
            continue
        if model is not None and not isinstance(model, str):
            _LOGGER.error("Ignoring device selector with a non-string model: %r", selector)
            continue
        if manufacturer is None and model is None:
            _LOGGER.error("Ignoring empty zha_z2m_converters device selector")
            continue
        normalized.append((manufacturer, model))

    def matches(device: DeviceDefinition, manufacturer: str | None, model: str | None) -> bool:
        metadata_match = (
            (not manufacturer or device.manufacturer == manufacturer)
            and (not model or device.model == model or model in device.zigbee_models)
        )
        fingerprint_match = any(
            (not manufacturer or fingerprint.get("manufacturerName") == manufacturer)
            and (not model or fingerprint.get("modelID") == model)
            for fingerprint in device.fingerprints
        )
        return metadata_match or fingerprint_match

    return [device for device in devices if any(matches(device, manufacturer, model) for manufacturer, model in normalized)]


async def async_setup(hass: Any, config: dict[str, Any]) -> bool:
    """Load the converter snapshot and register safe ZHA quirks."""
    domain_config = config.get(DOMAIN, {})
    if "manufacturer" in domain_config or "model" in domain_config:
        _LOGGER.error("Use the zha_z2m_converters devices list; manufacturer/model options are not supported")
        return False
    source = Path(domain_config.get("source", DEFAULT_SOURCE))
    if not source.exists():
        _LOGGER.error("Converter source directory does not exist: %s", source)
        return False
    result = await hass.async_add_executor_job(parse_path, source)
    result.devices = _select_devices(result.devices, domain_config.get("devices"))
    registry = register_result(result)
    await hass.async_add_executor_job(register_with_zha, registry)
    _LOGGER.info("Registered %d declarative converter definitions from %s", len(registry.devices), source)
    return True

__all__ = [
    "DeviceDefinition",
    "ConfigureAction",
    "EndpointCluster",
    "CoverageReport",
    "ParseResult",
    "RuntimePlan",
    "RuntimeReport",
    "RuntimeWrite",
    "apply_report",
    "build_runtime_plan",
    "SourceFile",
    "export_python",
    "build_report",
    "format_report",
    "load_source",
    "load_sources",
    "make_write",
    "parse_path",
    "parse_source",
    "register_result",
    "register_with_zha",
]
