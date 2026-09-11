"""Generate a human-readable status report for the converter snapshot."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .coverage import CoverageReport, build_report
from .lexer import tokenize
from .parser import (
    _REPORTING_HELPERS,
    _call_name,
    _configure_actions,
    _find_assignments,
    _find_static_constants,
    _find_static_configure_functions,
    _resolve_configure_reference,
    _string,
    parse_path,
)
from .source import load_sources


DEFAULT_SOURCE = Path(__file__).resolve().parent / "converters" / "zigbee-herdsman-converters"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class ConfigureStatus:
    definitions: int = 0
    categories: Counter[str] = field(default_factory=Counter)
    calls: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))


@dataclass
class StatusReport:
    coverage: CoverageReport
    configure: ConfigureStatus


def _raw_definition_identity(raw: dict[str, Any]) -> tuple[str | None, str | None]:
    vendor = _string(raw.get("vendor")) or _string(raw.get("manufacturer"))
    model = _string(raw.get("model"))
    if model is None:
        zigbee_model = raw.get("zigbeeModel")
        if isinstance(zigbee_model, list) and zigbee_model and isinstance(zigbee_model[0], str):
            model = zigbee_model[0]
        elif isinstance(zigbee_model, str):
            model = zigbee_model
    return vendor, model


def _configure_issue_categories(value: Any, constants: dict[str, Any]) -> dict[str, Counter[str]]:
    """Classify unsupported configure fragments without evaluating JavaScript."""
    actions, unsupported = _configure_actions(value, constants)
    del actions
    if not unsupported:
        return {}

    categories: dict[str, Counter[str]] = defaultdict(Counter)
    if not isinstance(value, dict) or "__configure__" not in value:
        call = _call_name(value) if isinstance(value, dict) else None
        categories["direct or unsupported configure expression"][call or "<unknown>"] += 1
        return categories

    if "__unsupported__" in value:
        categories["dynamic syntax or control flow"]["<parser-marked>"] += 1

    local_values = value.get("__locals__", {})
    if not isinstance(local_values, dict):
        local_values = {}
    for statement in value.get("__configure__", []):
        if not isinstance(statement, dict):
            categories["dynamic syntax or control flow"]["<unknown>"] += 1
            continue
        _, statement_unsupported = _configure_actions(
            {"__configure__": [statement], "__locals__": local_values},
            constants,
        )
        if not statement_unsupported:
            continue
        if "__unsupported__" in statement:
            categories["dynamic syntax or control flow"]["<parser-marked>"] += 1
            continue
        call = _call_name(statement)
        if call is None:
            categories["dynamic syntax or control flow"]["<unknown>"] += 1
            continue

        method = call.rsplit(".", 1)[-1]
        if call.startswith("reporting."):
            if method in {"bind", "read", "write", "command", "configureReporting"} or method in _REPORTING_HELPERS:
                category = "unsupported reporting arguments"
            else:
                category = "unsupported reporting helper"
        elif method in {"bind", "read", "write", "command", "configureReporting"}:
            category = "unsupported endpoint operation arguments"
        elif call.startswith(("endpoint", "device.")):
            category = "unsupported endpoint/device operation"
        else:
            category = "unsupported configure call"
        categories[category][call] += 1
    # A definition can contain the same unsupported call more than once. The
    # status report counts affected definitions, not call occurrences.
    return {category: Counter(set(calls)) for category, calls in categories.items()}


def _iter_configure_problems(source: Path, result: Any):
    """Yield configure problems for parsed, non-rejected definitions only."""
    parsed_keys: Counter[tuple[str, str | None, str | None]] = Counter()
    # Use the same identity that the parser exposes for each device.
    for device in result.devices:
        parsed_keys[(device.source or "", device.manufacturer, device.model)] += 1

    for source_file in load_sources(source):
        tokens = tokenize(source_file.text)
        constants = _find_static_constants(tokens)
        assignments = _find_assignments(tokens, {"definitions", "definition"}, constants)
        configure_names = {
            str(raw["configure"]["__identifier__"])
            for _token, assigned in assignments
            for raw in (assigned if isinstance(assigned, list) else [assigned])
            if isinstance(raw, dict)
            and isinstance(raw.get("configure"), dict)
            and set(raw["configure"]) == {"__identifier__"}
        }
        configure_functions = _find_static_configure_functions(tokens, configure_names, constants)
        for token, assigned in assignments:
            raw_definitions = assigned if isinstance(assigned, list) else [assigned]
            for raw in raw_definitions:
                if not isinstance(raw, dict):
                    continue
                configure = raw.get("configure")
                if configure is None:
                    continue
                if isinstance(configure, dict) and set(configure) == {"__identifier__"}:
                    configure = configure_functions.get(str(configure["__identifier__"]), configure)
                    configure = _resolve_configure_reference(configure) or configure
                vendor, model = _raw_definition_identity(raw)
                key = (source_file.filename, vendor, model)
                if not parsed_keys[key]:
                    continue
                parsed_keys[key] -= 1
                categories = _configure_issue_categories(configure, constants)
                if categories:
                    yield token, vendor, model, categories


def build_status(source: Path = DEFAULT_SOURCE) -> StatusReport:
    """Build the aggregate status report for a converter source tree."""
    result = parse_path(source)
    coverage = build_report(result, problem_limit=0)
    configure = ConfigureStatus()
    for _token, _vendor, _model, categories in _iter_configure_problems(source, result):
        configure.definitions += 1
        for category, calls in categories.items():
            configure.categories[category] += 1
            configure.calls[category].update(calls)
    return StatusReport(coverage=coverage, configure=configure)


def _percentage(value: int, total: int) -> str:
    return f"{value / total * 100:.1f}%" if total else "0.0%"


def _append_counter_table(lines: list[str], title: str, counter: Counter[str], limit: int = 20) -> None:
    if not counter:
        return
    lines.extend([f"### {title}", "", "| Item | Definitions |", "|---|---:|"])
    for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:limit]:
        lines.append(f"| `{name}` | {count:,} |")
    if len(counter) > limit:
        lines.append(f"| *{len(counter) - limit:,} more items* | — |")
    lines.append("")


def _display_path(value: str) -> str:
    """Prefer repository-relative paths in generated reports."""
    try:
        return str(Path(value).resolve().relative_to(REPOSITORY_ROOT))
    except ValueError:
        return value


def render_status(report: StatusReport, top: int = 20) -> str:
    """Render the status report as Markdown."""
    coverage = report.coverage
    configure = report.configure
    total = coverage.total_definitions
    lines = [
        "# Converter Status",
        "",
        "This file is generated by `python3 scripts/status.py` from the bundled",
        "`zigbee-herdsman-converters` snapshot. It describes the remaining gaps in",
        "the safe declarative translation to ZHA.",
        "",
        "## Overview",
        "",
        "| Status | Definitions | Share |",
        "|---|---:|---:|",
        f"| Fully supported | {coverage.fully_supported:,} | {_percentage(coverage.fully_supported, total)} |",
        f"| Usable partial | {coverage.usable_partial:,} | {_percentage(coverage.usable_partial, total)} |",
        f"| Metadata only | {coverage.metadata_only:,} | {_percentage(coverage.metadata_only, total)} |",
        f"| Unusable partial | {coverage.unusable_partial:,} | {_percentage(coverage.unusable_partial, total)} |",
        f"| Rejected | {coverage.rejected:,} | {_percentage(coverage.rejected, total)} |",
        f"| **Total** | **{total:,}** | **100%** |",
        "",
        "## Remaining problem areas",
        "",
        "| Problem | Definitions |",
        "|---|---:|",
    ]
    for name, count in coverage.partial_reasons.items():
        lines.append(f"| {name} | {count:,} |")
    lines.append("")

    _append_counter_table(lines, "Unsupported extend macros", Counter(coverage.unsupported_macros), top)
    _append_counter_table(lines, "Unsupported converter bindings", Counter(coverage.unsupported_converters), top)
    _append_counter_table(lines, "Unsupported definition fields", Counter(coverage.unsupported_fields), top)
    _append_counter_table(lines, "Unsupported expose types", Counter(coverage.unsupported_exposes), top)

    lines.extend([
        "## Configure gaps",
        "",
        "Configure is reported separately because a definition may have usable",
        "entities while its startup configuration is only partially translated.",
        "Categories can overlap when one definition has several different gaps.",
        "",
        "| Configure problem | Definitions |",
        "|---|---:|",
        f"| Any unsupported configure part | {configure.definitions:,} |",
    ])
    for category, count in sorted(configure.categories.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {category} | {count:,} |")
    lines.append("")
    for category, calls in configure.calls.items():
        _append_counter_table(lines, f"Configure: {category}", calls, top)

    if coverage.rejected_definitions:
        lines.extend(["## Rejected definitions", "", "| Definition | Source | Reason |", "|---|---|---|"])
        for item in coverage.rejected_definitions:
            identity = item.get("path") or "<unknown>"
            source_name = _display_path(item.get("filename") or "<unknown>")
            if item.get("line") is not None:
                source_name = f"{source_name}:{item['line']}"
            reason = item.get("message") or "unsupported syntax"
            lines.append(f"| `{identity}` | `{source_name}` | {reason} |")
        lines.append("")

    lines.extend([
        "## Regeneration",
        "",
        "```shell",
        "python3 scripts/status.py",
        "```",
        "",
    ])
    return "\n".join(lines)


def update_status(output: Path, source: Path = DEFAULT_SOURCE, top: int = 20) -> None:
    """Generate the status Markdown file."""
    output.write_text(render_status(build_status(source), top=top) + "\n", encoding="utf-8")
