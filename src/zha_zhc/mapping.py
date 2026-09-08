"""Declarative converter-to-ZCL mappings."""

from __future__ import annotations

from dataclasses import replace

from .model import Binding, DeviceDefinition, Expose, Expression


EXPOSE_CLUSTER_MAP: dict[str, tuple[str, str | None]] = {
    "temperature": ("temperature_measurement", "measured_value"),
    "humidity": ("relative_humidity", "measured_value"),
    "pressure": ("pressure_measurement", "measured_value"),
    "illuminance": ("illuminance_measurement", "measured_value"),
    "occupancy": ("occupancy", "occupancy"),
    "contact": ("binary_input", "present_value"),
    "battery": ("power_configuration", "battery_percentage_remaining"),
    "voltage": ("electrical_measurement", "rms_voltage"),
    "current": ("electrical_measurement", "rms_current"),
    "power": ("electrical_measurement", "active_power"),
    "energy": ("metering", "current_summ_delivered"),
    "switch": ("on_off", "on_off"),
    "light": ("on_off", "on_off"),
    "cover": ("window_covering", "current_position_lift_percentage"),
    "lock": ("door_lock", "lock_state"),
}


CONVERTER_MAP: dict[str, tuple[str, str | None, str]] = {
    "on_off": ("on_off", "on_off", "report"),
    "light": ("on_off", "on_off", "report"),
    "brightness": ("level_control", "current_level", "report"),
    "color_temp": ("color_control", "color_temperature", "report"),
    "color_xy": ("color_control", None, "report"),
    "temperature": ("temperature_measurement", "measured_value", "report"),
    "humidity": ("relative_humidity", "measured_value", "report"),
    "pressure": ("pressure_measurement", "measured_value", "report"),
    "occupancy": ("occupancy", "occupancy", "report"),
    "battery": ("power_configuration", "battery_percentage_remaining", "report"),
    "read": (None, None, "read"),
}

CONVERTER_SCALES = {
    "temperature": 100,
    "humidity": 100,
    "pressure": 10,
    "battery": 2,
}


def normalize_device(device: DeviceDefinition) -> DeviceDefinition:
    """Fill standard cluster bindings from expose and converter names."""
    exposes = [replace(expose) for expose in device.exposes]
    bindings = list(device.from_zigbee)
    known = {(item.cluster, item.attribute, item.converter) for item in bindings}
    for expose in exposes:
        cluster, attribute = EXPOSE_CLUSTER_MAP.get(expose.type, (None, None))
        if cluster and (cluster, attribute, expose.type) not in known:
            bindings.append(Binding(expose.type, cluster, attribute, direction="report", endpoint=expose.endpoint))
    normalized_from = []
    for binding in bindings:
        converter_name = binding.converter.rsplit(".", 1)[-1]
        mapped = CONVERTER_MAP.get(converter_name)
        if mapped and binding.cluster is None:
            cluster, attribute, direction = mapped
            scale = CONVERTER_SCALES.get(converter_name)
            expression = Expression("divide", (scale,)) if scale else binding.expression
            normalized_from.append(
                replace(binding, cluster=cluster, attribute=attribute, direction=direction, expression=expression)
            )
        else:
            normalized_from.append(binding)
    normalized_to = []
    for binding in device.to_zigbee:
        converter_name = binding.converter.rsplit(".", 1)[-1]
        mapped = CONVERTER_MAP.get(converter_name)
        if mapped and binding.cluster is None:
            cluster, attribute, _ = mapped
            normalized_to.append(replace(binding, cluster=cluster, attribute=attribute, direction="command"))
        else:
            normalized_to.append(binding)
    return replace(device, exposes=exposes, from_zigbee=normalized_from, to_zigbee=normalized_to)
