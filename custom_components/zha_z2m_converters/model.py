"""Versioned, JSON-serializable intermediate representation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Severity = Literal["info", "warning", "error"]


@dataclass(frozen=True)
class Diagnostic:
    severity: Severity
    code: str
    message: str
    filename: str | None = None
    line: int | None = None
    column: int | None = None
    path: str | None = None


@dataclass(frozen=True)
class Expression:
    """Safe expression node; never contains executable source code."""

    op: str
    args: tuple[Any, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "args": [arg.to_dict() if isinstance(arg, Expression) else arg for arg in self.args]}


@dataclass(frozen=True)
class Expose:
    type: str
    name: str
    property: str | None = None
    access: tuple[str, ...] = ()
    endpoint: str | int | None = None
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    value_min: float | int | None = None
    value_max: float | int | None = None
    value_step: float | int | None = None
    values: tuple[Any, ...] = ()
    description: str | None = None
    category: str | None = None


@dataclass(frozen=True)
class Binding:
    converter: str
    cluster: str | int | None = None
    attribute: str | int | None = None
    dp: int | None = None
    data_type: str | None = None
    command: str | None = None
    direction: str = "report"
    endpoint: str | int | None = None
    expression: Expression | None = None
    supported: bool = True


@dataclass(frozen=True)
class ConfigureAction:
    """Safe device configuration operation extracted from a configure callback."""

    operation: str
    endpoint: str | int | None = None
    cluster: str | int | None = None
    attributes: tuple[str | int, ...] = ()
    minimum_interval: int | float | None = None
    maximum_interval: int | float | None = None
    reportable_change: Any = None
    command: str | int | None = None
    payload: dict[str, Any] | None = None
    options: dict[str, Any] | None = None
    target: str = "coordinator"
    manufacturer_code: int | None = None
    destination_endpoint: int | None = None


@dataclass(frozen=True)
class EndpointCluster:
    """Declarative cluster addition to an existing Zigbee endpoint."""

    endpoint: int
    cluster: str | int
    direction: Literal["input", "output"] = "input"


@dataclass(frozen=True)
class CustomClusterSpec:
    """Static custom-cluster schema extracted from a converter definition."""

    name: str
    cluster_id: int
    manufacturer_code: int | None = None
    attributes: tuple[dict[str, Any], ...] = ()
    commands: tuple[dict[str, Any], ...] = ()


@dataclass
class DeviceDefinition:
    manufacturer: str | None = None
    model: str | None = None
    zigbee_models: list[str] = field(default_factory=list)
    fingerprints: list[dict[str, str]] = field(default_factory=list)
    description: str | None = None
    exposes: list[Expose] = field(default_factory=list)
    from_zigbee: list[Binding] = field(default_factory=list)
    to_zigbee: list[Binding] = field(default_factory=list)
    extends: list[str] = field(default_factory=list)
    configure_actions: list[ConfigureAction] = field(default_factory=list)
    endpoint_clusters: list[EndpointCluster] = field(default_factory=list)
    custom_clusters: list[str] = field(default_factory=list)
    custom_cluster_specs: list[CustomClusterSpec] = field(default_factory=list)
    conditional_extends: list[dict[str, Any]] = field(default_factory=list)
    unsupported_macros: list[str] = field(default_factory=list)
    unsupported_fields: list[str] = field(default_factory=list)
    source: str | None = None
    source_line: int | None = None
    partial: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ParseResult:
    devices: list[DeviceDefinition] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    rejected_details: list[Diagnostic] = field(default_factory=list)
    syntax_validated: bool = False
    source_files: int = 0
    rejected_definitions: int = 0

    @property
    def errors(self) -> list[Diagnostic]:
        return [item for item in self.diagnostics if item.severity == "error"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "syntax_validated": self.syntax_validated,
            "source_files": self.source_files,
            "rejected_definitions": self.rejected_definitions,
            "rejected_details": [asdict(item) for item in self.rejected_details],
            "devices": [device.to_dict() for device in self.devices],
            "diagnostics": [asdict(item) for item in self.diagnostics],
        }
