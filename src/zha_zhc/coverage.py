"""Coverage analysis for a converter snapshot."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .mapping import CONVERTER_MAP, EXPOSE_CLUSTER_MAP
from .model import DeviceDefinition, ParseResult
from .parser import parse_path

SUPPORTED_ENTITY_TYPES = {
    "binary",
    "binary_sensor",
    "button",
    "climate",
    "cover",
    "enum",
    "fan",
    "light",
    "lock",
    "numeric",
    "number",
    "select",
    "switch",
    "text",
}
SUPPORTED_EXPOSE_TYPES = SUPPORTED_ENTITY_TYPES | set(EXPOSE_CLUSTER_MAP)


@dataclass
class CoverageReport:
    source_files: int
    total_definitions: int
    definitions: int
    fully_supported: int
    metadata_only: int
    partially_supported: int
    rejected: int
    diagnostics: dict[str, int] = field(default_factory=dict)
    partial_reasons: dict[str, int] = field(default_factory=dict)
    unsupported_macros: dict[str, int] = field(default_factory=dict)
    unsupported_fields: dict[str, int] = field(default_factory=dict)
    unsupported_exposes: dict[str, int] = field(default_factory=dict)
    unsupported_converters: dict[str, int] = field(default_factory=dict)
    devices_with_problems: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_report(result: ParseResult, problem_limit: int = 50) -> CoverageReport:
    diagnostics = Counter(item.code for item in result.diagnostics)
    unsupported_exposes: Counter[str] = Counter()
    unsupported_converters: Counter[str] = Counter()
    unsupported_macros: Counter[str] = Counter()
    unsupported_fields: Counter[str] = Counter()
    partial_reasons: Counter[str] = Counter()
    problem_devices: list[dict[str, Any]] = []
    fully_supported = 0
    partially_supported = 0
    metadata_only = 0

    for device in result.devices:
        device_problems: list[str] = []
        unknown_converter = False
        if device.partial:
            for field_name in device.unsupported_fields:
                unsupported_fields[field_name] += 1
            partially_supported += 1
            for macro in device.unsupported_macros:
                if macro != "dynamic-expression":
                    unsupported_macros[macro] += 1
        else:
            if not device.exposes and not device.from_zigbee and not device.to_zigbee:
                metadata_only += 1
                device_problems.append("metadata-only definition")
            else:
                fully_supported += 1
        for expose in device.exposes:
            if expose.type not in SUPPORTED_EXPOSE_TYPES:
                unsupported_exposes[expose.type] += 1
                device_problems.append(f"unsupported expose type: {expose.type}")
            if expose.type not in EXPOSE_CLUSTER_MAP and expose.type not in SUPPORTED_ENTITY_TYPES:
                device_problems.append(f"no ZHA entity mapping: {expose.name}")
        for binding in [*device.from_zigbee, *device.to_zigbee]:
            converter = binding.converter.rsplit(".", 1)[-1]
            if converter not in CONVERTER_MAP and binding.cluster is None:
                unknown_converter = True
                unsupported_converters[converter] += 1
                device_problems.append(f"unsupported converter: {converter}")
        if unknown_converter and device.partial:
            partial_reasons["unsupported converter binding"] += 1
        if device.partial:
            if device.unsupported_fields:
                for field in device.unsupported_fields:
                    device_problems.append(f"unsupported definition field: {field}")
            if unknown_converter:
                device_problems.append("unsupported converter binding")
            elif device.unsupported_macros and device.unsupported_macros != ["dynamic-expression"]:
                partial_reasons["unsupported extend macro"] += 1
                macros = ", ".join(dict.fromkeys(device.unsupported_macros))
                device_problems.append(f"unsupported extend macro: {macros}")
            elif device.unsupported_fields:
                partial_reasons["unsupported definition field"] += 1
            else:
                partial_reasons["dynamic expose or other expression"] += 1
                device_problems.append("dynamic expose or other expression")
        if device_problems and len(problem_devices) < problem_limit:
            problem_devices.append({
                "manufacturer": device.manufacturer,
                "model": device.model,
                "source": device.source,
                "problems": sorted(set(device_problems)),
            })

    return CoverageReport(
        source_files=result.source_files,
        total_definitions=len(result.devices) + result.rejected_definitions,
        definitions=len(result.devices),
        fully_supported=fully_supported,
        metadata_only=metadata_only,
        partially_supported=partially_supported,
        rejected=result.rejected_definitions,
        diagnostics=dict(diagnostics.most_common()),
        partial_reasons=dict(partial_reasons.most_common()),
        unsupported_macros=dict(unsupported_macros.most_common()),
        unsupported_fields=dict(unsupported_fields.most_common()),
        unsupported_exposes=dict(unsupported_exposes.most_common()),
        unsupported_converters=dict(unsupported_converters.most_common()),
        devices_with_problems=problem_devices,
    )


def format_report(report: CoverageReport) -> str:
    lines = [
        "zigbee-herdsman-converters coverage",
        "====================================",
        f"Source files:          {report.source_files}",
        f"Total definitions:     {report.total_definitions}",
        f"Definitions found:     {report.definitions}",
        f"Fully supported:       {report.fully_supported}",
        f"Metadata only:         {report.metadata_only}",
        f"Partially supported:   {report.partially_supported}",
        f"Rejected:              {report.rejected}",
    ]
    _append_section(lines, "Problems by category", report.diagnostics)
    _append_section(lines, "Partial support reasons", report.partial_reasons)
    if report.unsupported_macros:
        macros = ", ".join(f"{name} ({count})" for name, count in report.unsupported_macros.items())
        lines.append(f"\nUnsupported extend macros: {macros}")
    if report.unsupported_fields:
        fields = ", ".join(f"{name} ({count})" for name, count in report.unsupported_fields.items())
        lines.append(f"\nUnsupported definition fields: {fields}")
    _append_section(lines, "Unsupported expose types", report.unsupported_exposes)
    _append_section(lines, "Unsupported converters", report.unsupported_converters)
    if report.devices_with_problems:
        lines.append("\nDevices with problems:")
        for item in report.devices_with_problems:
            identity = f"{item['manufacturer'] or '<unknown>'} / {item['model'] or '<unknown>'}"
            lines.append(f"  - {identity}: {'; '.join(item['problems'])}")
    return "\n".join(lines)


def _append_section(lines: list[str], title: str, values: dict[str, int]) -> None:
    if not values:
        return
    lines.append(f"\n{title}:")
    for name, count in values.items():
        lines.append(f"  - {name}: {count}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show declarative converter coverage")
    parser.add_argument("source", nargs="?", default="vendor/zigbee-herdsman-converters")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    parser.add_argument("--problem-limit", type=int, default=50)
    args = parser.parse_args(argv)
    report = build_report(parse_path(Path(args.source)), args.problem_limit)
    print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False) if args.json else format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
