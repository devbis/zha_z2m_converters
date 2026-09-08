"""Runtime registry and optional ZHA QuirkBuilder integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .mapping import normalize_device
from .model import Binding, ConfigureAction, DeviceDefinition, Diagnostic, Expose, ParseResult


ZCL_CLUSTER_IDS: dict[str, int] = {
    "genOnOff": 0x0006,
    "on_off": 0x0006,
    "genLevelCtrl": 0x0008,
    "level_control": 0x0008,
    "genPowerCfg": 0x0001,
    "power_configuration": 0x0001,
    "genDeviceTempCfg": 0x0002,
    "msTemperatureMeasurement": 0x0402,
    "temperature_measurement": 0x0402,
    "msRelativeHumidity": 0x0405,
    "relative_humidity": 0x0405,
    "msPressureMeasurement": 0x0403,
    "pressure_measurement": 0x0403,
    "msOccupancySensing": 0x0406,
    "occupancy": 0x0406,
    "msIlluminanceMeasurement": 0x0400,
    "illuminance_measurement": 0x0400,
    "lightingColorCtrl": 0x0300,
    "color_control": 0x0300,
    "electricalMeasurement": 0x0702,
    "electrical_measurement": 0x0702,
    "metering": 0x0700,
    "closuresWindowCovering": 0x0102,
    "window_covering": 0x0102,
    "closuresDoorLock": 0x0101,
    "door_lock": 0x0101,
}


@dataclass(frozen=True)
class RuntimeEntity:
    name: str
    type: str
    property: str
    cluster: str | int | None
    attribute: str | int | None
    endpoint: str | int | None = None
    access: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeReport:
    cluster: str | int
    attribute: str | int
    value: Any


@dataclass(frozen=True)
class RuntimeWrite:
    cluster: str | int
    attribute: str | int
    value: Any
    endpoint: str | int | None = None


@dataclass
class RuntimePlan:
    device: DeviceDefinition
    entities: list[RuntimeEntity] = field(default_factory=list)
    bindings: list[Binding] = field(default_factory=list)
    configure_actions: list[ConfigureAction] = field(default_factory=list)


@dataclass
class RuntimeRegistry:
    """In-memory registry used by tests and by a ZHA adapter."""

    devices: list[DeviceDefinition] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def add(self, device: DeviceDefinition) -> None:
        self.devices.append(normalize_device(device))


def build_runtime_plan(device: DeviceDefinition) -> RuntimePlan:
    """Build a controller-independent plan for reports and writes."""
    normalized = normalize_device(device)
    entities = []
    for expose in normalized.exposes:
        binding = _binding_for_expose(expose, normalized.from_zigbee)
        entities.append(
            RuntimeEntity(
                name=expose.name,
                type=expose.type,
                property=expose.property or expose.name,
                cluster=binding.cluster if binding else None,
                attribute=binding.attribute if binding else None,
                endpoint=expose.endpoint,
                access=expose.access,
            )
        )
    return RuntimePlan(
        normalized,
        entities,
        [*normalized.from_zigbee, *normalized.to_zigbee],
        list(normalized.configure_actions),
    )


def apply_report(plan: RuntimePlan, report: RuntimeReport) -> dict[str, Any]:
    """Convert one Zigbee attribute report into ZHA entity state."""
    result: dict[str, Any] = {}
    for entity in plan.entities:
        if not _same_cluster(entity.cluster, report.cluster) or not _same_attribute(entity.attribute, report.attribute):
            continue
        for binding in plan.bindings:
            if binding.direction not in {"report", "event"}:
                continue
            if _same_cluster(binding.cluster, entity.cluster) and _same_attribute(binding.attribute, entity.attribute):
                result[entity.property] = _apply_expression(report.value, binding)
                break
    return result


def make_write(plan: RuntimePlan, property_name: str, value: Any) -> RuntimeWrite:
    """Create a declarative Zigbee write for a writable entity."""
    entity = next((item for item in plan.entities if item.property == property_name or item.name == property_name), None)
    if entity is None or "set" not in entity.access or entity.cluster is None or entity.attribute is None:
        raise KeyError(f"no writable binding for {property_name!r}")
    binding = next(
        (item for item in plan.bindings if _same_cluster(item.cluster, entity.cluster) and _same_attribute(item.attribute, entity.attribute)),
        None,
    )
    if binding is None:
        raise KeyError(f"no writable binding for {property_name!r}")
    return RuntimeWrite(entity.cluster, entity.attribute, value, entity.endpoint)


def _binding_for_expose(expose: Expose, bindings: list[Binding]) -> Binding | None:
    semantic = {
        "temperature": "temperature",
        "humidity": "humidity",
        "pressure": "pressure",
        "battery": "battery",
        "occupancy": "occupancy",
        "contact": "contact",
        "switch": "on_off",
        "light": "light",
    }.get(expose.type, expose.name)
    return next((item for item in bindings if item.converter.rsplit(".", 1)[-1] == semantic), None) or next(
        (item for item in bindings if item.attribute == expose.property), None
    )


def _same_cluster(left: str | int | None, right: str | int | None) -> bool:
    if left == right:
        return True
    if isinstance(left, str) and left in ZCL_CLUSTER_IDS:
        return ZCL_CLUSTER_IDS[left] == right
    if isinstance(right, str) and right in ZCL_CLUSTER_IDS:
        return ZCL_CLUSTER_IDS[right] == left
    return False


def _same_attribute(left: str | int | None, right: str | int | None) -> bool:
    return left == right or (isinstance(left, str) and isinstance(right, str) and left.lower() == right.lower())


def _apply_expression(value: Any, binding: Binding) -> Any:
    if binding.expression is None:
        return value
    if binding.expression.op == "divide" and binding.expression.args:
        divisor = binding.expression.args[0]
        if isinstance(value, (int, float)) and isinstance(divisor, (int, float)) and divisor != 0:
            return value / divisor
    return value


def register_result(result: ParseResult) -> RuntimeRegistry:
    registry = RuntimeRegistry(diagnostics=list(result.diagnostics))
    for device in result.devices:
        registry.add(device)
    return registry


def register_with_zha(registry: RuntimeRegistry, builder_factory: Any | None = None) -> RuntimeRegistry:
    """Register the portable IR through a supplied QuirkBuilder factory.

    The adapter is deliberately duck-typed so importing this package does not
    require Home Assistant. A production integration supplies the current
    ``zhaquirks.builder.QuirkBuilder`` implementation.
    """
    if builder_factory is None:
        try:
            from zhaquirks.builder import QuirkBuilder  # type: ignore
        except ImportError as exc:
            raise RuntimeError("ZHA is not installed; pass builder_factory explicitly") from exc
        builder_factory = QuirkBuilder
    for device in registry.devices:
        if not device.manufacturer or not device.model:
            continue
        builder = builder_factory(device.manufacturer, device.model)
        plan = build_runtime_plan(device)
        for expose, entity in zip(device.exposes, plan.entities, strict=False):
            _apply_expose(builder, expose, entity)
        add_to_registry = getattr(builder, "add_to_registry", None)
        if callable(add_to_registry):
            add_to_registry()
    return registry


def _apply_expose(builder: Any, expose: Any, entity: RuntimeEntity) -> None:
    method_name = {
        "binary": "binary_sensor",
        "button": "button",
        "enum": "select",
        "numeric": "number" if "set" in expose.access else "sensor",
        "battery": "sensor",
        "contact": "binary_sensor",
        "occupancy": "binary_sensor",
        "temperature": "sensor",
        "humidity": "sensor",
        "pressure": "sensor",
        "illuminance": "sensor",
        "voltage": "sensor",
        "current": "sensor",
        "power": "sensor",
        "energy": "sensor",
    }.get(expose.type, expose.type)
    method = getattr(builder, method_name, None)
    if not callable(method):
        return
    kwargs = {"fallback_name": expose.name}
    if entity.attribute is not None:
        kwargs["attribute_name"] = entity.attribute
    if expose.unit:
        kwargs["unit"] = expose.unit
    if expose.device_class:
        kwargs["device_class"] = expose.device_class
    if expose.value_min is not None:
        kwargs["min_value"] = expose.value_min
    if expose.value_max is not None:
        kwargs["max_value"] = expose.value_max
    if entity.cluster is not None:
        kwargs["cluster_id"] = ZCL_CLUSTER_IDS.get(entity.cluster, entity.cluster) if isinstance(entity.cluster, str) else entity.cluster
    try:
        method(**kwargs)
    except TypeError:
        # Builder signatures differ slightly between ZHA releases. The
        # portable IR remains available even when optional metadata is new.
        method(fallback_name=expose.name)
