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
    "co2": ("msCO2", "measuredValue"),
    "pm25": ("pm25Measurement", "measuredValue"),
    "flow": ("msFlowMeasurement", "measuredValue"),
    "soil_moisture": ("msSoilMoisture", "measuredValue"),
    "occupancy_timeout": ("msOccupancySensing", "pirOToUDelay"),
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
    "metering": ("seMetering", None, "report"),
    "electrical_measurement": ("haElectricalMeasurement", None, "report"),
    "lumi_contact": ("genOnOff", "onOff", "report"),
    "lumi_co2": ("msCO2", "measuredValue", "report"),
    "lumi_pm25": ("pm25Measurement", "measuredValue", "report"),
    "lumi_power": ("genAnalogInput", "presentValue", "report"),
    "co2": ("msCO2", "measuredValue", "report"),
    "pm25": ("pm25Measurement", "measuredValue", "report"),
    "flow": ("msFlowMeasurement", "measuredValue", "report"),
    "soil_moisture": ("msSoilMoisture", "measuredValue", "report"),
    "occupancy_timeout": ("msOccupancySensing", "pirOToUDelay", "report"),
    "device_temperature": ("genDeviceTempCfg", "currentTemperature", "report"),
    "thermostat": ("hvacThermostat", None, "report"),
    "thermostat_local_temperature": ("hvacThermostat", "localTemp", "report"),
    "thermostat_local_temperature_calibration": ("hvacThermostat", "localTemperatureCalibration", "report"),
    "thermostat_occupied_heating_setpoint": ("hvacThermostat", "occupiedHeatingSetpoint", "report"),
    "thermostat_unoccupied_heating_setpoint": ("hvacThermostat", "unoccupiedHeatingSetpoint", "report"),
    "thermostat_occupied_cooling_setpoint": ("hvacThermostat", "occupiedCoolingSetpoint", "report"),
    "thermostat_unoccupied_cooling_setpoint": ("hvacThermostat", "unoccupiedCoolingSetpoint", "report"),
    "thermostat_system_mode": ("hvacThermostat", "systemMode", "report"),
    "thermostat_running_state": ("hvacThermostat", "runningState", "report"),
    "thermostat_running_mode": ("hvacThermostat", "runningMode", "report"),
    "thermostat_occupancy": ("hvacThermostat", "occupancy", "report"),
    "thermostat_pi_heating_demand": ("hvacThermostat", "pIHeatingDemand", "report"),
    "thermostat_control_sequence_of_operation": ("hvacThermostat", "controlSequenceOfOperation", "report"),
    "thermostat_weekly_schedule": ("hvacThermostat", None, "event"),
    "thermostat_clear_weekly_schedule": ("hvacThermostat", None, "command"),
    "thermostat_remote_sensing": ("hvacThermostat", "remoteSensing", "report"),
    "thermostat_temperature_display_mode": ("hvacUserInterfaceCfg", "tempDisplayMode", "report"),
    "thermostat_outdoor_temperature": ("hvacThermostat", "outdoorTemp", "report"),
    "thermostat_setpoint_raise_lower": ("hvacThermostat", None, "command"),
    "thermostat_min_heat_setpoint_limit": ("hvacThermostat", "minHeatSetpointLimit", "report"),
    "thermostat_max_heat_setpoint_limit": ("hvacThermostat", "maxHeatSetpointLimit", "report"),
    "thermostat_min_cool_setpoint_limit": ("hvacThermostat", "minCoolSetpointLimit", "report"),
    "thermostat_max_cool_setpoint_limit": ("hvacThermostat", "maxCoolSetpointLimit", "report"),
    "thermostat_temperature_setpoint_hold": ("hvacThermostat", "tempSetpointHold", "report"),
    "thermostat_temperature_setpoint_hold_duration": ("hvacThermostat", "tempSetpointHoldDuration", "report"),
    "thermostat_ac_louver_position": ("hvacThermostat", "acLouverPosition", "report"),
    "hvac_user_interface": ("hvacUserInterfaceCfg", None, "report"),
    "thermostat_keypad_lockout": ("hvacUserInterfaceCfg", "keypadLockout", "report"),
    "fan": ("hvacFanCtrl", "fanMode", "report"),
    "fan_mode": ("hvacFanCtrl", "fanMode", "report"),
    "cover_position_tilt": ("closuresWindowCovering", "currentPositionTiltPercentage", "report"),
    "cover_state": ("closuresWindowCovering", None, "report"),
    "cover_position_via_brightness": ("genLevelCtrl", "currentLevel", "report"),
    "cover_via_brightness": ("genLevelCtrl", "currentLevel", "report"),
    "cover_state_via_onoff": ("genOnOff", "onOff", "report"),
    "lock": ("closuresDoorLock", "lockState", "report"),
    "lock_operation_event": ("closuresDoorLock", None, "event"),
    "identify": ("genIdentify", None, "event"),
    "power_on_behavior": ("genOnOff", "startUpOnOff", "report"),
    "power_on_behavior_1": ("genOnOff", "moesStartUpOnOff", "report"),
    "power_on_behavior_2": ("manuSpecificTuya3", "powerOnBehavior", "report"),
    "power_outage_memory": ("genOnOff", "moesStartUpOnOff", "report"),
    "switch_type": ("manuSpecificTuya3", "switchType", "report"),
    "on_off_action": ("genOnOff", None, "event"),
    "ias_occupancy_alarm_2": ("ssIasZone", "zoneStatus", "event"),
    "warning": ("ssIasWd", None, "command"),
    "warning_simple": ("ssIasWd", None, "command"),
    "occupancy_timeout": ("msOccupancySensing", "pirOToUDelay", "report"),
    "level_config": ("genLevelCtrl", None, "report"),
    "fan_speed": ("genLevelCtrl", "currentLevel", "report"),
    "electrical_measurement_power": ("haElectricalMeasurement", "activePower", "report"),
    "electrical_measurement_power_phase_b": ("haElectricalMeasurement", "activePowerPhB", "report"),
    "electrical_measurement_power_phase_c": ("haElectricalMeasurement", "activePowerPhC", "report"),
    "electrical_measurement_power_reactive": ("haElectricalMeasurement", "reactivePower", "report"),
    "metering_power": ("seMetering", "instantaneousDemand", "report"),
    "metering_status": ("seMetering", "status", "report"),
    "metering_extended_status": ("seMetering", "extendedStatus", "report"),
    "currentsummdelivered": ("seMetering", "currentSummDelivered", "report"),
    "read": (None, None, "read"),
    "command_on": ("genOnOff", None, "event"),
    "command_off": ("genOnOff", None, "event"),
    "command_toggle": ("genOnOff", None, "event"),
    "command_move": ("genLevelCtrl", None, "event"),
    "command_stop": ("genLevelCtrl", None, "event"),
    "command_step": ("genLevelCtrl", None, "event"),
    "command_move_to_level": ("genLevelCtrl", None, "event"),
    "command_move_to_color_temp": ("lightingColorCtrl", None, "event"),
    "command_move_color_temperature": ("lightingColorCtrl", None, "event"),
    "command_step_color_temperature": ("lightingColorCtrl", None, "event"),
    "command_enhanced_move_to_hue_and_saturation": ("lightingColorCtrl", None, "event"),
    "command_move_to_color": ("lightingColorCtrl", None, "event"),
    "command_recall": ("genScenes", None, "event"),
    "command_store": ("genScenes", None, "event"),
    "command_arm": ("ssIasAce", None, "event"),
    "command_panic": ("ssIasAce", None, "event"),
    "ias_occupancy_alarm_1": ("ssIasZone", "zoneStatus", "event"),
    "ias_occupancy_alarm_1_with_timeout": ("ssIasZone", "zoneStatus", "event"),
    "ias_contact_alarm_1": ("ssIasZone", "zoneStatus", "event"),
    "ias_water_leak_alarm_1": ("ssIasZone", "zoneStatus", "event"),
    "ias_smoke_alarm_1": ("ssIasZone", "zoneStatus", "event"),
    "ias_carbon_monoxide_alarm_1_gas_alarm_2": ("ssIasZone", "zoneStatus", "event"),
    "linkquality_from_basic": ("genBasic", "zclVersion", "report"),
    "tuya_cover": ("closuresWindowCovering", "currentPositionLiftPercentage", "report"),
    "tuya_cover_control": ("closuresWindowCovering", None, "event"),
}

CONVERTER_SCALES = {
    "temperature": 100,
    "humidity": 100,
    "pressure": 10,
    "battery": 2,
    "flow": 10,
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
            if converter_name == "lumi_contact" and direction == "report":
                expression = Expression("lookup", ({"0": True, "1": False},))
            elif converter_name == "lumi_co2" and direction == "report":
                expression = Expression("floor")
            elif converter_name == "co2" and direction == "report":
                expression = Expression("floor", (Expression("multiply", (1_000_000,)),))
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
