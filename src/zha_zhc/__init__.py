"""Safe, declarative zigbee-herdsman-converters bridge for ZHA."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .exporter import export_python
from .coverage import CoverageReport, build_report, format_report
from .model import ConfigureAction, DeviceDefinition, ParseResult
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
DOMAIN = "zha_zhc"
DEFAULT_SOURCE = "/config/zha_zhc/converters"


async def async_setup(hass: Any, config: dict[str, Any]) -> bool:
    """Load the converter snapshot and register safe ZHA quirks."""
    domain_config = config.get(DOMAIN, {})
    source = Path(domain_config.get("source", DEFAULT_SOURCE))
    if not source.exists():
        _LOGGER.error("Converter source directory does not exist: %s", source)
        return False
    result = await hass.async_add_executor_job(parse_path, source)
    manufacturer = domain_config.get("manufacturer")
    model = domain_config.get("model")
    if manufacturer or model:
        result.devices = [
            device
            for device in result.devices
            if (not manufacturer or device.manufacturer == manufacturer)
            and (not model or device.model == model)
            or any(
                (not manufacturer or fingerprint.get("manufacturerName") == manufacturer)
                and (not model or fingerprint.get("modelID") == model)
                for fingerprint in device.fingerprints
            )
        ]
    registry = register_result(result)
    await hass.async_add_executor_job(register_with_zha, registry)
    _LOGGER.info("Registered %d declarative converter definitions from %s", len(registry.devices), source)
    return True

__all__ = [
    "DeviceDefinition",
    "ConfigureAction",
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
