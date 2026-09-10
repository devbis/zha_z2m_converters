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
    "lumi_basic": ("genBasic", None, "report"),
    "lumi_switch_operation_mode_opple": ("manuSpecificLumi", 0x0200, "command"),
    "lumi_operation_mode_opple": ("manuSpecificLumi", 0x0009, "command"),
    "lumi_operation_mode_basic": ("genBasic", 0xFF22, "report"),
    "lumi_switch_operation_mode_basic": ("genBasic", 0xFF22, "command"),
    "lumi_flip_indicator_light": ("manuSpecificLumi", 0x00F0, "command"),
    "lumi_led_disabled_night": ("manuSpecificLumi", 0x0203, "command"),
    "lumi_switch_mode_switch": ("manuSpecificLumi", 0x0004, "command"),
    "lumi_switch_power_outage_memory": ("manuSpecificLumi", 0x0201, "command"),
    "lumi_switch_type": ("manuSpecificLumi", 0x000A, "command"),
    "lumi_button_switch_mode": ("manuSpecificLumi", 0x0226, "command"),
    "lumi_socket_button_lock": ("manuSpecificLumi", 0x0200, "command"),
    "lumi_auto_off": ("manuSpecificLumi", 0x0202, "command"),
    "lumi_motion_sensitivity": ("manuSpecificLumi", 0x010C, "command"),
    "lumi_switch_click_mode": ("manuSpecificLumi", 0x0125, "command"),
    "lumi_selftest": ("manuSpecificLumi", 0x0127, "command"),
    "lumi_overload_protection": ("manuSpecificLumi", 0x020B, "command"),
    "lumi_detection_interval": ("manuSpecificLumi", 0x0102, "command"),
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

LUMI_BASIC_ATTRIBUTE_MAP = {
    "device_temperature": (3, None),
    "energy": (149, None),
    "power": (152, None),
    "voltage": (150, 10),
    "current": (151, 1000),
}

LUMI_WRITE_EXPRESSIONS = {
    "lumi_switch_operation_mode_opple": {"0": "decoupled", "1": "control_relay"},
    "lumi_operation_mode_opple": {"0": "command", "1": "event"},
    "lumi_operation_mode_basic": {"18": "control_relay", "254": "decoupled"},
    "lumi_switch_operation_mode_basic": {"18": "control_relay", "254": "decoupled"},
    "lumi_flip_indicator_light": {"0": False, "1": True},
    "lumi_led_disabled_night": {"0": False, "1": True},
    "lumi_switch_mode_switch": {"1": "quick_mode", "4": "anti_flicker_mode"},
    "lumi_switch_power_outage_memory": {"0": False, "1": True},
    "lumi_switch_type": {"1": "toggle", "2": "momentary", "3": "none"},
    "lumi_button_switch_mode": {"0": "relay", "1": "relay_and_usb"},
    "lumi_socket_button_lock": {"0": "ON", "1": "OFF"},
    "lumi_auto_off": {"0": False, "1": True},
    "lumi_motion_sensitivity": {"1": "low", "2": "medium", "3": "high"},
    "lumi_switch_click_mode": {"1": "fast", "2": "multi"},
    "lumi_selftest": {"0": False, "1": True},
}

LUMI_POWER_OUTAGE_MODELS = {
    "QBKG38LM",
    "QBKG39LM",
    "QBKG40LM",
    "QBKG41LM",
}

LUMI_SINGLE_OPERATION_MODE_BASIC_MODELS = {"QBKG04LM", "QBKG11LM", "QBKG21LM", "QBKG23LM"}

LUMI_POWER_OUTAGE_MEMORY_MANU_MODELS = {
    "SP-EUC01", "ZNCZ04LM", "ZNCZ15LM", "QBCZ14LM", "QBCZ15LM", "SSM-U01", "SSM-U02",
    "DLKZMK11LM", "DLKZMK12LM", "WS-EUK01", "WS-EUK02", "WS-EUK03", "WS-EUK04",
    "QBKG17LM", "QBKG18LM", "QBKG19LM", "QBKG20LM", "QBKG25LM", "QBKG26LM", "QBKG27LM",
    "QBKG28LM", "QBKG29LM", "QBKG30LM", "QBKG31LM", "QBKG32LM", "QBKG33LM", "QBKG34LM",
    "QBKG38LM", "QBKG39LM", "QBKG40LM", "QBKG41LM", "ZNDDMK11LM", "ZNLDP13LM", "ZNQBKG31LM",
    "WS-USC02", "WS-USC03", "WS-USC04", "ZNQBKG24LM", "ZNQBKG25LM", "ZNQBKG26LM", "JWDL001A",
    "SSWQD02LM", "SSWQD03LM", "XDD11LM", "XDD12LM", "XDD13LM", "ZNLDP12LM", "ZNXDD01LM", "WS-USC01",
}

LUMI_AUTO_OFF_MANU_MODELS = {"ZNCZ04LM", "ZNCZ12LM", "SP-EUC01"}
LUMI_CLICK_MODE_ALT_MODELS = {"ZNQBKG38LM", "ZNQBKG39LM", "ZNQBKG40LM", "ZNQBKG41LM"}
LUMI_SELFTEST_ENCODED_MODELS = {"JTYJ-GD-01LM/BW", "JTQJ-BF-01LM/BW"}

LUMI_LED_DISABLED_NIGHT_UNSUPPORTED_MODELS = {"ZNCZ11LM"}


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
            if converter_name in {"lumi_operation_mode_basic", "lumi_switch_operation_mode_basic"} and device.model not in LUMI_SINGLE_OPERATION_MODE_BASIC_MODELS:
                normalized_from.append(binding)
                continue
            cluster, attribute, direction = mapped
            scale = CONVERTER_SCALES.get(converter_name)
            expression = Expression("divide", (scale,)) if scale else binding.expression
            if converter_name == "lumi_contact" and direction == "report":
                expression = Expression("lookup", ({"0": True, "1": False},))
            elif converter_name == "lumi_co2" and direction == "report":
                expression = Expression("floor")
            elif converter_name == "co2" and direction == "report":
                expression = Expression("floor", (Expression("multiply", (1_000_000,)),))
            elif converter_name == "lumi_operation_mode_basic" and direction == "report":
                expression = Expression("lookup", (LUMI_WRITE_EXPRESSIONS[converter_name],))
            normalized_from.append(
                replace(binding, cluster=cluster, attribute=attribute, direction=direction, expression=expression)
            )
        else:
            normalized_from.append(binding)
    for binding in bindings:
        if binding.converter.rsplit(".", 1)[-1] != "lumi_basic":
            continue
        for expose in exposes:
            mapping = LUMI_BASIC_ATTRIBUTE_MAP.get(expose.name)
            if mapping is None:
                continue
            attribute, divisor = mapping
            expression = Expression("divide", (divisor,)) if divisor else None
            normalized_from.append(
                Binding(
                    f"lumi_basic_{expose.name}",
                    "genBasic",
                    attribute,
                    direction="report",
                    endpoint=binding.endpoint,
                    expression=expression,
                )
            )
    normalized_to = []
    for binding in device.to_zigbee:
        converter_name = binding.converter.rsplit(".", 1)[-1]
        mapped = CONVERTER_MAP.get(converter_name)
        if mapped and binding.cluster is None:
            if converter_name == "lumi_switch_operation_mode_basic" and device.model not in LUMI_SINGLE_OPERATION_MODE_BASIC_MODELS:
                normalized_to.append(binding)
                continue
            cluster, attribute, _ = mapped
            expression = None
            if converter_name == "lumi_switch_power_outage_memory":
                if device.model in LUMI_POWER_OUTAGE_MODELS:
                    attribute = 0x0517
                    expression = Expression(
                        "lookup",
                        ({"0": "electric_appliances_on", "1": "on", "2": "electric_appliances_off", "3": "inverted"},),
                    )
                elif device.model in LUMI_POWER_OUTAGE_MEMORY_MANU_MODELS:
                    expression = Expression("lookup", (LUMI_WRITE_EXPRESSIONS[converter_name],))
                else:
                    normalized_to.append(binding)
                    continue
            elif converter_name == "lumi_led_disabled_night" and device.model in LUMI_LED_DISABLED_NIGHT_UNSUPPORTED_MODELS:
                normalized_to.append(binding)
                continue
            elif converter_name == "lumi_auto_off" and device.model not in LUMI_AUTO_OFF_MANU_MODELS:
                normalized_to.append(binding)
                continue
            elif converter_name == "lumi_switch_click_mode" and device.model in LUMI_CLICK_MODE_ALT_MODELS:
                attribute = 0x0286
            elif converter_name == "lumi_selftest" and device.model in LUMI_SELFTEST_ENCODED_MODELS:
                normalized_to.append(binding)
                continue
            elif converter_name in LUMI_WRITE_EXPRESSIONS:
                expression = Expression("lookup", (LUMI_WRITE_EXPRESSIONS[converter_name],))
            normalized_to.append(
                replace(
                    binding,
                    cluster=cluster,
                    attribute=attribute,
                    direction="command",
                    expression=expression,
                )
            )
        else:
            normalized_to.append(binding)
    return replace(device, exposes=exposes, from_zigbee=normalized_from, to_zigbee=normalized_to)
