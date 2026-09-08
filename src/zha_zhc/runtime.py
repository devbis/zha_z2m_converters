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
    "manuSpecificTuya3": 0xE001,
}

ZHA_ATTRIBUTE_NAMES: dict[str, str] = {
    "onOff": "on_off",
    "onTime": "on_time",
    "moesStartUpOnOff": "moes_start_up_on_off",
    "switchType": "switch_type",
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
    operation: str = "write"
    command: str | None = None
    payload: dict[str, Any] | None = None


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
    binding = _write_binding(plan, entity, property_name)
    if binding is None:
        raise KeyError(f"no writable binding for {property_name!r}")
    if binding.converter == "on_off_countdown":
        if property_name == "countdown":
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 43200:
                raise ValueError("countdown must be an integer between 0 and 43200 seconds")
            return RuntimeWrite(
                entity.cluster,
                entity.attribute,
                value,
                entity.endpoint,
                operation="command",
                command="onWithTimedOff",
                payload={"ctrlbits": 0, "ontime": value, "offwaittime": value},
            )
        if property_name == "state":
            return RuntimeWrite(
                entity.cluster,
                entity.attribute,
                value,
                entity.endpoint,
                operation="command",
                command=_on_off_command(value),
                payload={},
            )
    return RuntimeWrite(entity.cluster, entity.attribute, _apply_write_expression(value, binding), entity.endpoint)


def _write_binding(plan: RuntimePlan, entity: RuntimeEntity, property_name: str) -> Binding | None:
    if property_name in {"state", "countdown"}:
        countdown_binding = next(
            (
                item
                for item in plan.bindings
                if item.converter == "on_off_countdown"
                and _same_cluster(item.cluster, entity.cluster)
                and item.direction == "command"
                and item.command == ("state" if property_name == "state" else "onWithTimedOff")
            ),
            None,
        )
        if countdown_binding is not None:
            return countdown_binding
    return next(
        (
            item
            for item in plan.bindings
            if _same_cluster(item.cluster, entity.cluster)
            and _same_attribute(item.attribute, entity.attribute)
            and item.direction in {"command", "report", "event"}
        ),
        None,
    )


def _on_off_command(value: Any) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, str) and value.lower() in {"on", "off", "toggle"}:
        return value.lower()
    raise ValueError("state must be a boolean or one of on, off, toggle")


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
    }.get(expose.type) or {
        "countdown": "on_off_countdown",
    }.get(expose.name, expose.name)
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
    if binding.expression.op == "lookup" and binding.expression.args:
        lookup = binding.expression.args[0]
        if isinstance(lookup, dict):
            return lookup.get(str(value), value)
    return value


def _apply_write_expression(value: Any, binding: Binding) -> Any:
    if binding.expression is None or not binding.expression.args:
        return value
    if binding.expression.op == "lookup" and isinstance(binding.expression.args[0], dict):
        for raw_value, exposed_value in binding.expression.args[0].items():
            if exposed_value == value:
                try:
                    return int(raw_value)
                except (TypeError, ValueError):
                    return raw_value
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
        signatures = device.fingerprints or [{"manufacturerName": device.manufacturer, "modelID": device.model}]
        plan = build_runtime_plan(device)
        for signature in signatures:
            builder = builder_factory(signature["manufacturerName"], signature["modelID"])
            custom_clusters_ready = _configure_custom_clusters(builder, plan)
            for expose, entity in zip(device.exposes, plan.entities, strict=False):
                if _requires_custom_cluster(plan, entity) and not custom_clusters_ready:
                    continue
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
        kwargs["attribute_name"] = ZHA_ATTRIBUTE_NAMES.get(str(entity.attribute), entity.attribute)
    if expose.device_class:
        kwargs["device_class"] = expose.device_class
    if expose.value_min is not None:
        kwargs["min_value"] = expose.value_min
    if expose.value_max is not None:
        kwargs["max_value"] = expose.value_max
    if expose.value_step is not None:
        kwargs["step"] = expose.value_step
    if expose.endpoint is not None:
        kwargs["endpoint_id"] = expose.endpoint
    if expose.unit:
        kwargs["unit"] = _zha_unit(expose.unit)
    if entity.cluster is not None:
        kwargs["cluster_id"] = ZCL_CLUSTER_IDS.get(entity.cluster, entity.cluster) if isinstance(entity.cluster, str) else entity.cluster
    try:
        method(**kwargs)
    except TypeError:
        # Builder signatures differ slightly between ZHA releases. The
        # portable IR remains available even when optional metadata is new.
        method(fallback_name=expose.name)


def _is_command_backed(plan: RuntimePlan, entity: RuntimeEntity) -> bool:
    return entity.property == "countdown" and any(
        binding.converter == "on_off_countdown"
        and binding.command == "onWithTimedOff"
        and _same_cluster(binding.cluster, entity.cluster)
        for binding in plan.bindings
    )


def _requires_custom_cluster(plan: RuntimePlan, entity: RuntimeEntity) -> bool:
    return _is_command_backed(plan, entity) or (
        entity.cluster in {"genOnOff", "manuSpecificTuya3"}
        and entity.attribute in {"moesStartUpOnOff", "switchType"}
    )


def _configure_custom_clusters(builder: Any, plan: RuntimePlan) -> bool:
    """Install the small Python-only custom clusters required by Tuya entities."""
    required: dict[Any, set[Any]] = {}
    for entity in plan.entities:
        if not _requires_custom_cluster(plan, entity):
            continue
        if entity.cluster == "genOnOff" or _is_command_backed(plan, entity):
            required.setdefault(_tuya_on_off_cluster, set()).add(entity.endpoint or 1)
        elif entity.cluster == "manuSpecificTuya3":
            required.setdefault(_tuya3_cluster, set()).add(entity.endpoint or 1)
    if not required:
        return True
    replaces = getattr(builder, "replaces", None)
    if not callable(replaces):
        return False
    for cluster_factory, endpoints in required.items():
        try:
            cluster = cluster_factory()
        except ImportError:
            return False
        for endpoint in endpoints:
            replaces(cluster, endpoint_id=endpoint)
    return True


def _zha_unit(unit: str) -> Any:
    if unit == "s":
        try:
            from zigpy.quirks.v2.homeassistant import UnitOfTime  # type: ignore

            return UnitOfTime.SECONDS
        except ImportError:
            try:
                from zha.units import UnitOfTime  # type: ignore

                return UnitOfTime.SECONDS
            except ImportError:
                pass
    return unit


def _tuya_on_off_cluster() -> Any:
    """Return a Python-only OnOff replacement for Tuya attributes and countdown."""
    import zigpy.types as t  # type: ignore
    from zigpy.zcl import foundation  # type: ignore
    from zigpy.zcl.clusters.general import OnOff  # type: ignore
    from zigpy.zcl.foundation import ZCLAttributeDef  # type: ignore
    from zhaquirks.clusters import CustomCluster  # type: ignore

    class TuyaOnOffCluster(OnOff, CustomCluster):
        class AttributeDefs(OnOff.AttributeDefs):
            moes_start_up_on_off = ZCLAttributeDef(id=0x8002, type=t.enum8, access="rw")

        async def write_attributes(self, attributes: dict[Any, Any], *args: Any, **kwargs: Any) -> Any:
            on_time = OnOff.AttributeDefs.on_time
            countdown = None
            for attribute, value in attributes.items():
                attribute_id = self.attributes_by_name[attribute].id if isinstance(attribute, str) else getattr(attribute, "id", attribute)
                if attribute_id == on_time.id:
                    countdown = value
            if countdown is None:
                return await super().write_attributes(attributes, *args, **kwargs)
            if not isinstance(countdown, int) or isinstance(countdown, bool) or not 0 <= countdown <= 43200:
                raise ValueError("countdown must be an integer between 0 and 43200 seconds")
            await self.command(
                OnOff.ServerCommandDefs.on_with_timed_off.id,
                on_off_control=0,
                on_time=countdown,
                off_wait_time=countdown,
            )
            self._update_attribute(on_time.id, countdown)
            statuses = [foundation.WriteAttributesStatusRecord(foundation.Status.SUCCESS) for _ in attributes]
            return [statuses]

    return TuyaOnOffCluster


def _tuya3_cluster() -> Any:
    """Return the declarative Tuya manufacturer cluster used by switch options."""
    import zigpy.types as t  # type: ignore
    from zigpy.zcl.foundation import BaseAttributeDefs, ZCLAttributeDef  # type: ignore
    from zhaquirks.clusters import CustomCluster  # type: ignore

    class Tuya3Cluster(CustomCluster):
        cluster_id = 0xE001
        name = "Tuya3Cluster"
        ep_attribute = "tuya3"

        class AttributeDefs(BaseAttributeDefs):
            power_on_behavior = ZCLAttributeDef(id=0xD010, type=t.enum8, access="rw")
            switch_type = ZCLAttributeDef(id=0xD030, type=t.enum8, access="rw")

    return Tuya3Cluster
