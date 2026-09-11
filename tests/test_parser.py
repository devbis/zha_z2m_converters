from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from types import ModuleType
import unittest
from pathlib import Path
from unittest.mock import patch

from zha_z2m_converters.exporter import export_python
from zha_z2m_converters import _select_devices
from zha_z2m_converters.mapping import normalize_device
from zha_z2m_converters.parser import parse_path, parse_paths, parse_source
from zha_z2m_converters.model import ConfigureAction, Expose
from zha_z2m_converters.runtime import _apply_configure_actions, _apply_expose, _configure_endpoint_clusters, _make_enum_class
from zha_z2m_converters.runtime import apply_report, build_runtime_plan, make_write, register_result
from zha_z2m_converters.runtime import register_with_zha, RuntimeEntity, RuntimeReport


ROOT = Path(__file__).parent


class ParserTests(unittest.TestCase):
    def test_device_selection_supports_multiple_devices_and_full_source(self) -> None:
        source = """
        export const definitions = [
            {zigbeeModel: ["ONE"], model: "One", vendor: "First"},
            {zigbeeModel: ["TWO"], model: "Two", vendor: "Second"},
        ];
        """
        devices = parse_source(source, "selection.ts").devices
        self.assertEqual(len(_select_devices(devices, None)), 2)
        selected = _select_devices(
            devices,
            [
                {"manufacturer": "First", "model": "One"},
                {"manufacturer": "Second", "model": "Two"},
            ],
        )
        self.assertEqual([device.model for device in selected], ["One", "Two"])

    def test_unsupported_definition_does_not_reject_other_array_items(self) -> None:
        source = """
        export const definitions = [
            {model: "Before", vendor: "Example"},
            {model: "Unsupported", vendor: "Example", extend: [unsupportedFactory().unsupported]},
            {model: "After", vendor: "Example"},
        ];
        """
        result = parse_source(source, "partial-array.ts")
        self.assertEqual([device.model for device in result.devices], ["Before", "After"])
        self.assertEqual(result.rejected_definitions, 1)
        self.assertEqual(result.rejected_details[0].path, "Unsupported")

    def test_identity_scale_is_declarative(self) -> None:
        source = """
        export const definitions = [{
            model: "Identity scale",
            vendor: "Example",
            extend: [modernExtend.illuminance({scale: (value) => value})],
        }];
        """
        result = parse_source(source, "identity-scale.ts")
        self.assertEqual(len(result.devices), 1)
        self.assertEqual(result.rejected_definitions, 0)
        self.assertFalse(result.devices[0].partial)

    def test_static_definition_becomes_ir(self) -> None:
        source = (ROOT / "fixtures" / "simple_device.ts").read_text()
        result = parse_source(source, "simple_device.ts")
        self.assertEqual(len(result.devices), 1)
        device = result.devices[0]
        self.assertEqual(device.manufacturer, "Example")
        self.assertEqual(device.model, "Test Plug")
        self.assertEqual([item.name for item in device.exposes], ["state", "temperature"])
        self.assertEqual([item.converter for item in device.from_zigbee], ["fz.on_off", "fz.temperature"])

    def test_direct_fingerprints_and_tuya_white_labels_are_preserved(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["TS011F"],
            model: "TS011F_plug_1",
            vendor: "Tuya",
            fingerprint: tuya.fingerprint("TS011F", ["_TZ3000_example"]),
            whiteLabel: [tuya.whitelabel("Zbeacon", "TS011F_plug_1_1", "Smart plug", ["Zbeacon"])]
        }];
        """
        device = parse_source(source, "fingerprints.ts").devices[0]
        self.assertEqual(
            device.fingerprints,
            [
                {"modelID": "TS011F", "manufacturerName": "_TZ3000_example"},
                {"modelID": "TS011F", "manufacturerName": "Zbeacon"},
            ],
        )

    def test_white_label_fingerprint_registers_exact_zha_signature(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["TS011F"],
            model: "TS011F_plug_1",
            vendor: "Tuya",
            whiteLabel: [tuya.whitelabel("Zbeacon", "TS011F_plug_1_1", "Smart plug", ["Zbeacon"])]
        }];
        """
        calls = []

        class Builder:
            def __init__(self, manufacturer, model):
                calls.append((manufacturer, model))

            def add_to_registry(self):
                pass

        register_with_zha(register_result(parse_source(source)), Builder)
        self.assertEqual(calls, [("Zbeacon", "TS011F")])

    def test_normalization_adds_standard_clusters(self) -> None:
        source = (ROOT / "fixtures" / "simple_device.ts").read_text()
        device = parse_source(source).devices[0]
        normalized = normalize_device(device)
        self.assertIn("on_off", {item.cluster for item in normalized.from_zigbee})
        self.assertIn("temperature_measurement", {item.cluster for item in normalized.from_zigbee})

    def test_lumi_power_maps_to_analog_input(self) -> None:
        source = """
        export const definitions = [{
            model: "Lumi power",
            vendor: "Aqara",
            fromZigbee: [lumi.fromZigbee.lumi_power],
            exposes: [e.power()],
        }];
        """
        device = parse_source(source, "lumi-power.ts").devices[0]
        plan = build_runtime_plan(device)
        power = next(item for item in plan.entities if item.property == "power")
        self.assertEqual((power.cluster, power.attribute), ("genAnalogInput", "presentValue"))
        self.assertEqual(apply_report(plan, RuntimeReport("genAnalogInput", "presentValue", 42)), {"power": 42})

    def test_simple_lumi_measurements_use_declarative_bindings(self) -> None:
        source = """
        export const definitions = [{
            model: "Lumi measurements",
            vendor: "Aqara",
            fromZigbee: [
                lumi.fromZigbee.lumi_contact,
                lumi.fromZigbee.lumi_co2,
                lumi.fromZigbee.lumi_pm25,
            ],
            exposes: [e.contact(), e.co2(), e.pm25()],
        }];
        """
        plan = build_runtime_plan(parse_source(source, "lumi-measurements.ts").devices[0])
        self.assertEqual(
            [(item.property, item.cluster, item.attribute) for item in plan.entities],
            [
                ("contact", "genOnOff", "onOff"),
                ("co2", "msCO2", "measuredValue"),
                ("pm25", "pm25Measurement", "measuredValue"),
            ],
        )
        self.assertEqual(apply_report(plan, RuntimeReport("genOnOff", "onOff", 0)), {"contact": True})
        self.assertEqual(apply_report(plan, RuntimeReport("msCO2", "measuredValue", 500.9)), {"co2": 500})
        self.assertEqual(apply_report(plan, RuntimeReport("pm25Measurement", "measuredValue", 12)), {"pm25": 12})

    def test_standard_co2_and_pm25_converters_use_safe_expressions(self) -> None:
        source = """
        export const definitions = [{
            model: "Standard air quality",
            vendor: "Example",
            fromZigbee: [fz.co2, fz.pm25],
            exposes: [e.co2(), e.pm25()],
        }];
        """
        plan = build_runtime_plan(parse_source(source, "standard-air-quality.ts").devices[0])
        self.assertEqual(
            [(item.property, item.cluster, item.attribute) for item in plan.entities],
            [("co2", "msCO2", "measuredValue"), ("pm25", "pm25Measurement", "measuredValue")],
        )
        self.assertEqual(apply_report(plan, RuntimeReport("msCO2", "measuredValue", 0.0005019)), {"co2": 501})
        self.assertEqual(apply_report(plan, RuntimeReport("pm25Measurement", "measuredValue", 12)), {"pm25": 12})

    def test_standard_numeric_measurements_use_declarative_bindings(self) -> None:
        source = """
        export const definitions = [{
            model: "Standard measurements",
            vendor: "Example",
            fromZigbee: [fz.flow, fz.soil_moisture, fz.occupancy_timeout],
            exposes: [e.numeric("flow", ea.STATE).withUnit("L/min"), e.numeric("soil_moisture", ea.STATE).withUnit("%"), e.numeric("occupancy_timeout", ea.STATE).withUnit("s")],
        }];
        """
        plan = build_runtime_plan(parse_source(source, "standard-measurements.ts").devices[0])
        self.assertEqual(
            [(item.property, item.cluster, item.attribute) for item in plan.entities],
            [
                ("flow", "msFlowMeasurement", "measuredValue"),
                ("soil_moisture", "msSoilMoisture", "measuredValue"),
                ("occupancy_timeout", "msOccupancySensing", "pirOToUDelay"),
            ],
        )
        self.assertEqual(apply_report(plan, RuntimeReport("msFlowMeasurement", "measuredValue", 25)), {"flow": 2.5})
        self.assertEqual(apply_report(plan, RuntimeReport("msSoilMoisture", "measuredValue", 42)), {"soil_moisture": 42})
        self.assertEqual(apply_report(plan, RuntimeReport("msOccupancySensing", "pirOToUDelay", 90)), {"occupancy_timeout": 90})

    def test_lumi_basic_exposes_use_fixed_attribute_paths(self) -> None:
        source = """
        export const definitions = [{
            model: "Lumi plug",
            vendor: "Aqara",
            fromZigbee: [lumi.fromZigbee.lumi_basic],
            exposes: [e.power(), e.energy(), e.voltage(), e.current(), e.device_temperature()],
        }];
        """
        plan = build_runtime_plan(parse_source(source, "lumi-basic.ts").devices[0])
        self.assertEqual(
            [(item.property, item.cluster, item.attribute) for item in plan.entities],
            [
                ("power", "genBasic", 152),
                ("energy", "genBasic", 149),
                ("voltage", "genBasic", 150),
                ("current", "genBasic", 151),
                ("device_temperature", "genBasic", 3),
            ],
        )
        self.assertEqual(apply_report(plan, RuntimeReport("genBasic", 152, 25)), {"power": 25})
        self.assertEqual(apply_report(plan, RuntimeReport("genBasic", 150, 2300)), {"voltage": 230})
        self.assertEqual(apply_report(plan, RuntimeReport("genBasic", 151, 1250)), {"current": 1.25})

    def test_lumi_switch_writes_use_manufacturer_cluster_and_lookups(self) -> None:
        source = """
        export const definitions = [{
            model: "Lumi switch",
            vendor: "Aqara",
            toZigbee: [
                lumi.toZigbee.lumi_switch_operation_mode_opple,
                lumi.toZigbee.lumi_flip_indicator_light,
                lumi.toZigbee.lumi_switch_mode_switch,
            ],
            exposes: [
                e.enum("operation_mode", ea.ALL, ["control_relay", "decoupled"]).withAccess("STATE_SET"),
                e.binary("flip_indicator_light", ea.ALL, "ON", "OFF").withAccess("STATE_SET"),
                e.enum("mode_switch", ea.ALL, ["quick_mode", "anti_flicker_mode"]).withAccess("STATE_SET"),
            ],
            extend: [lumi.modernExtend.addManuSpecificLumiCluster()],
        }];
        """
        plan = build_runtime_plan(parse_source(source, "lumi-switch.ts").devices[0])
        self.assertEqual(
            [(item.property, item.cluster, item.attribute) for item in plan.entities],
            [
                ("operation_mode", "manuSpecificLumi", 0x0200),
                ("flip_indicator_light", "manuSpecificLumi", 0x00F0),
                ("mode_switch", "manuSpecificLumi", 0x0004),
            ],
        )
        self.assertEqual(make_write(plan, "operation_mode", "control_relay").value, 1)
        self.assertEqual(make_write(plan, "flip_indicator_light", True).value, 1)
        self.assertEqual(make_write(plan, "mode_switch", "anti_flicker_mode").value, 4)

    def test_lumi_simple_settings_use_static_manufacturer_attributes(self) -> None:
        source = """
        export const definitions = [{
            model: "ZNCZ04LM",
            vendor: "Aqara",
            toZigbee: [
                lumi.toZigbee.lumi_switch_type,
                lumi.toZigbee.lumi_button_switch_mode,
                lumi.toZigbee.lumi_socket_button_lock,
                lumi.toZigbee.lumi_auto_off,
                lumi.toZigbee.lumi_motion_sensitivity,
                lumi.toZigbee.lumi_switch_click_mode,
                lumi.toZigbee.lumi_selftest,
                lumi.toZigbee.lumi_overload_protection,
                lumi.toZigbee.lumi_detection_interval,
            ],
            exposes: [
                e.enum("switch_type", ea.ALL, ["toggle", "momentary", "none"]).withAccess("STATE_SET"),
                e.enum("button_switch_mode", ea.ALL, ["relay", "relay_and_usb"]).withAccess("STATE_SET"),
                e.binary("button_lock", ea.ALL, "ON", "OFF").withAccess("STATE_SET"),
                e.binary("auto_off", ea.ALL, "ON", "OFF").withAccess("STATE_SET"),
                e.enum("motion_sensitivity", ea.ALL, ["low", "medium", "high"]).withAccess("STATE_SET"),
                e.enum("click_mode", ea.ALL, ["fast", "multi"]).withAccess("STATE_SET"),
                e.binary("selftest", ea.ALL, "ON", "OFF").withAccess("STATE_SET"),
                e.numeric("overload_protection", ea.ALL).withAccess("STATE_SET"),
                e.numeric("detection_interval", ea.ALL).withAccess("STATE_SET"),
            ],
            extend: [lumi.modernExtend.addManuSpecificLumiCluster()],
        }];
        """
        plan = build_runtime_plan(parse_source(source, "lumi-settings.ts").devices[0])
        self.assertEqual(
            [(item.property, item.cluster, item.attribute) for item in plan.entities],
            [
                ("switch_type", "manuSpecificLumi", 0x000A),
                ("button_switch_mode", "manuSpecificLumi", 0x0226),
                ("button_lock", "manuSpecificLumi", 0x0200),
                ("auto_off", "manuSpecificLumi", 0x0202),
                ("motion_sensitivity", "manuSpecificLumi", 0x010C),
                ("click_mode", "manuSpecificLumi", 0x0125),
                ("selftest", "manuSpecificLumi", 0x0127),
                ("overload_protection", "manuSpecificLumi", 0x020B),
                ("detection_interval", "manuSpecificLumi", 0x0102),
            ],
        )
        self.assertEqual(make_write(plan, "switch_type", "momentary").value, 2)
        self.assertEqual(make_write(plan, "button_switch_mode", "relay_and_usb").value, 1)
        self.assertEqual(make_write(plan, "button_lock", "OFF").value, 1)
        self.assertEqual(make_write(plan, "auto_off", True).value, 1)
        self.assertEqual(make_write(plan, "motion_sensitivity", "high").value, 3)
        self.assertEqual(make_write(plan, "click_mode", "multi").value, 2)
        self.assertEqual(make_write(plan, "selftest", True).value, 1)
        self.assertEqual(make_write(plan, "overload_protection", 2300).value, 2300)
        self.assertEqual(make_write(plan, "detection_interval", 30).value, 30)

    def test_lumi_model_specific_attribute_paths_are_preserved(self) -> None:
        source = """
        export const definitions = [{
            model: "QBKG38LM",
            vendor: "Aqara",
            toZigbee: [lumi.toZigbee.lumi_switch_power_outage_memory],
            exposes: [e.enum("power_outage_memory", ea.ALL, ["electric_appliances_on", "on", "electric_appliances_off", "inverted"]).withAccess("STATE_SET")],
            extend: [lumi.modernExtend.addManuSpecificLumiCluster()],
        }];
        """
        plan = build_runtime_plan(parse_source(source, "lumi-model-specific.ts").devices[0])
        self.assertEqual(plan.entities[0].attribute, 0x0517)
        self.assertEqual(make_write(plan, "power_outage_memory", "electric_appliances_off").value, 2)

    def test_lumi_single_switch_operation_mode_uses_manufacturer_basic_attribute(self) -> None:
        source = """
        export const definitions = [{
            model: "QBKG11LM",
            vendor: "Aqara",
            fromZigbee: [lumi.fromZigbee.lumi_operation_mode_basic],
            toZigbee: [lumi.toZigbee.lumi_switch_operation_mode_basic],
            exposes: [e.enum("operation_mode", ea.ALL, ["control_relay", "decoupled"]).withAccess("STATE_SET")],
        }];
        """
        device = parse_source(source, "lumi-basic-operation-mode.ts").devices[0]
        plan = build_runtime_plan(device)
        self.assertEqual(len(plan.custom_cluster_specs), 1)
        self.assertEqual((plan.entities[0].cluster, plan.entities[0].attribute), ("genBasic", 0xFF22))
        self.assertEqual(make_write(plan, "operation_mode", "decoupled").value, 0xFE)
        self.assertEqual(
            apply_report(plan, RuntimeReport("genBasic", 0xFF22, 0x12)),
            {"operation_mode": "control_relay"},
        )

    def test_lumi_simple_modern_extends_are_static(self) -> None:
        source = """
        export const definitions = [{
            model: "Modern Lumi",
            vendor: "Aqara",
            extend: [
                lumi.lumiModernExtend.lumiButtonLock(),
                lumi.lumiModernExtend.lumiPowerOutageMemory(),
                lumi.lumiModernExtend.lumiClickMode({attribute: {ID: 0x0286, type: 0x20}}),
                lumi.lumiModernExtend.lumiSwitchMode(),
                lumi.lumiModernExtend.lumiDimmingRangeMin(),
                lumi.lumiModernExtend.lumiOffOnDuration(),
                lumi.lumiModernExtend.lumiMultiClick({endpointName: "left"}),
                lumi.lumiModernExtend.lumiMotorSpeed(),
                lumi.lumiModernExtend.lumiTransitionCurveCurvature(),
                lumi.lumiModernExtend.lumiTransitionInitialBrightness(),
                lumi.lumiModernExtend.lumiOverloadProtection({valueMax: 3250, access: "STATE_SET"}),
            ],
        }];
        """
        plan = build_runtime_plan(parse_source(source, "lumi-modern-simple.ts").devices[0])
        self.assertEqual(
            [(item.property, item.attribute, item.access) for item in plan.entities],
            [
                ("button_lock", 0x0200, ("state", "set")),
                ("power_outage_memory", 0x0201, ("state", "set")),
                ("click_mode", 0x0286, ("state", "set")),
                ("mode_switch", 0x0004, ("state", "set")),
                ("dimming_range_minimum", 0x0515, ("state", "set")),
                ("off_on_duration", 0x0012, ("state", "set")),
                ("multi_click", 0x0286, ("state", "set")),
                ("motor_speed", 0x0408, ("state", "set")),
                ("transition_curve_curvature", 0x0528, ("state", "set")),
                ("transition_initial_brightness", 0x052C, ("state", "set")),
                ("overload_protection", 0x020B, ("state", "set")),
            ],
        )
        self.assertEqual(make_write(plan, "button_lock", "OFF").value, 1)
        self.assertEqual(make_write(plan, "power_outage_memory", True).value, 1)
        self.assertEqual(make_write(plan, "click_mode", "multi").value, 2)
        self.assertEqual(make_write(plan, "mode_switch", "anti_flicker_mode").value, 4)
        self.assertEqual(make_write(plan, "dimming_range_minimum", 10).value, 10)
        self.assertEqual(make_write(plan, "off_on_duration", 2.5).value, 25)
        self.assertEqual(make_write(plan, "multi_click", True).value, 2)
        self.assertEqual(make_write(plan, "motor_speed", "high").value, 2)
        self.assertEqual(make_write(plan, "transition_curve_curvature", 1.5).value, 1.5)
        self.assertEqual(make_write(plan, "transition_initial_brightness", 25).value, 25)
        self.assertEqual(make_write(plan, "overload_protection", 3250).value, 3250)

    def test_lumi_on_off_macro_expands_static_features(self) -> None:
        source = """
        export const definitions = [{
            model: "Modern Lumi switch",
            vendor: "Aqara",
            extend: [lumi.lumiModernExtend.lumiOnOff({powerOutageMemory: "binary", operationMode: true, lockRelay: true})],
        }];
        """
        plan = build_runtime_plan(parse_source(source, "lumi-on-off.ts").devices[0])
        self.assertFalse(plan.device.partial)
        self.assertEqual(
            [(item.property, item.cluster, item.attribute) for item in plan.entities],
            [
                ("state", "genOnOff", "onOff"),
                ("device_temperature", "manuSpecificLumi", 3),
                ("power_outage_count", "manuSpecificLumi", 5),
                ("power_outage_memory", "manuSpecificLumi", 0x0201),
                ("operation_mode", "manuSpecificLumi", 0x0200),
                ("lock_relay", "manuSpecificLumi", 0x0285),
            ],
        )
        self.assertEqual(make_write(plan, "power_outage_memory", True).value, 1)
        self.assertEqual(make_write(plan, "operation_mode", "decoupled").value, 0)
        self.assertEqual(make_write(plan, "lock_relay", True).value, 1)
        self.assertEqual(
            apply_report(plan, RuntimeReport("manuSpecificLumi", 5, 4)),
            {"power_outage_count": 3},
        )

    def test_lumi_meter_macros_use_static_attributes_and_scales(self) -> None:
        source = """
        export const definitions = [{
            model: "Modern Lumi meter",
            vendor: "Aqara",
            extend: [lumi.lumiModernExtend.lumiPower(), lumi.lumiModernExtend.lumiElectricityMeter()],
        }];
        """
        plan = build_runtime_plan(parse_source(source, "lumi-meter.ts").devices[0])
        self.assertEqual(
            [(item.property, item.cluster, item.attribute) for item in plan.entities],
            [
                ("power", "genAnalogInput", "presentValue"),
                ("energy", "manuSpecificLumi", 0x0095),
                ("voltage", "manuSpecificLumi", 0x0096),
                ("current", "manuSpecificLumi", 0x0097),
            ],
        )
        self.assertEqual(apply_report(plan, RuntimeReport("manuSpecificLumi", 0x0096, 2300)), {"voltage": 230})
        self.assertEqual(apply_report(plan, RuntimeReport("manuSpecificLumi", 0x0097, 1250)), {"current": 1.25})

    def test_lumi_action_macro_uses_static_lookup_and_endpoint_suffixes(self) -> None:
        source = """
        export const definitions = [{
            model: "Modern Lumi action",
            vendor: "Aqara",
            extend: [
                m.deviceEndpoints({endpoints: {left: 1, right: 2}}),
                lumi.lumiModernExtend.lumiAction({
                    actionLookup: {single: 1, double: 2},
                    endpointNames: ["left", "right"],
                }),
            ],
        }];
        """
        plan = build_runtime_plan(parse_source(source, "lumi-action.ts").devices[0])
        action = next(entity for entity in plan.entities if entity.property == "action")
        self.assertEqual(action.cluster, "genMultistateInput")
        self.assertEqual(action.attribute, "presentValue")
        self.assertEqual(
            action.access,
            ("state",),
        )
        self.assertEqual(
            apply_report(plan, RuntimeReport("genMultistateInput", "presentValue", 1, endpoint=2)),
            {"action": "single_right"},
        )

    def test_lumi_action_macro_supports_button_lookup_and_extra_actions(self) -> None:
        source = """
        export const definitions = [{
            model: "Modern Lumi button action",
            vendor: "Aqara",
            extend: [lumi.lumiModernExtend.lumiAction({
                actionLookup: {single: 1, double: 2},
                buttonLookup: {left: 41, right: 42},
                extraActions: ["slider_single"],
            })],
        }];
        """
        plan = build_runtime_plan(parse_source(source, "lumi-action-button.ts").devices[0])
        self.assertEqual(
            next(entity for entity in plan.entities if entity.property == "action").access,
            ("state",),
        )
        self.assertEqual(
            apply_report(plan, RuntimeReport("genMultistateInput", "presentValue", 2, endpoint=41)),
            {"action": "double_left"},
        )

    def test_lumi_light_macro_expands_static_light_and_lumi_features(self) -> None:
        source = """
        export const definitions = [{
            model: "Modern Lumi light",
            vendor: "Aqara",
            extend: [lumi.lumiModernExtend.lumiLight({
                colorTemp: true,
                colorTempRange: [154, 370],
                powerOutageMemory: "enum",
                powerOutageCount: true,
                deviceTemperature: true,
            })],
        }];
        """
        plan = build_runtime_plan(parse_source(source, "lumi-light.ts").devices[0])
        self.assertFalse(plan.device.partial)
        self.assertEqual(
            [(item.property, item.cluster, item.attribute) for item in plan.entities],
            [
                ("state", "genOnOff", "onOff"),
                ("color_temperature", "lightingColorCtrl", "colorTemperature"),
                ("power_outage_count", "manuSpecificLumi", 5),
                ("device_temperature", "manuSpecificLumi", 3),
                ("power_on_behavior", "manuSpecificLumi", 0x0517),
            ],
        )
        self.assertEqual(
            next(entity for entity in plan.entities if entity.property == "color_temperature").access,
            ("state", "set"),
        )
        self.assertEqual(make_write(plan, "power_on_behavior", "off").value, 2)

    def test_lumi_light_macro_expands_each_named_endpoint(self) -> None:
        source = """
        export const definitions = [{
            model: "Modern Lumi multi light",
            vendor: "Aqara",
            extend: [
                m.deviceEndpoints({endpoints: {white: 1, color: 2}}),
                lumi.lumiModernExtend.lumiLight({colorTemp: true, endpointNames: ["white", "color"]}),
            ],
        }];
        """
        device = parse_source(source, "lumi-light-multi.ts").devices[0]
        plan = build_runtime_plan(device)
        lights = [entity for entity in plan.entities if entity.property == "state"]
        self.assertEqual([entity.endpoint for entity in lights], [1, 2])
        self.assertEqual(
            apply_report(plan, RuntimeReport("genOnOff", "onOff", True, endpoint=2)),
            {"state": True},
        )

    def test_lumi_set_event_mode_is_a_static_configure_write(self) -> None:
        source = """
        export const definitions = [{
            model: "Modern Lumi event mode",
            vendor: "Aqara",
            extend: [lumi.lumiModernExtend.lumiSetEventMode()],
        }];
        """
        device = parse_source(source, "lumi-event-mode.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(
            device.configure_actions,
            [
                ConfigureAction(
                    "write",
                    1,
                    "manuSpecificLumi",
                    payload={"mode": 1},
                    target="device",
                    manufacturer_code=0x115F,
                )
            ],
        )

    def test_lumi_battery_macro_uses_static_attributes_and_curve(self) -> None:
        source = """
        export const definitions = [{
            model: "Modern Lumi battery",
            vendor: "Aqara",
            extend: [lumi.lumiModernExtend.lumiBattery({
                voltageAttribute: 23,
                percentageAttribute: 24,
                voltageToPercentage: {min: 2850, max: 3000},
            })],
        }];
        """
        plan = build_runtime_plan(parse_source(source, "lumi-battery.ts").devices[0])
        self.assertFalse(plan.device.partial)
        self.assertEqual(
            [(item.property, item.attribute) for item in plan.entities],
            [("battery", 23), ("voltage", 23)],
        )
        self.assertEqual(apply_report(plan, RuntimeReport("manuSpecificLumi", 23, 2925)), {"battery": 50, "voltage": 2925})

    def test_lumi_command_mode_is_static_with_optional_configure(self) -> None:
        source = """
        export const definitions = [{
            model: "Modern Lumi command mode",
            vendor: "Aqara",
            extend: [lumi.lumiModernExtend.lumiCommandMode({setEventMode: true})],
        }];
        """
        device = parse_source(source, "lumi-command-mode.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(len(device.configure_actions), 1)
        plan = build_runtime_plan(device)
        self.assertEqual(make_write(plan, "operation_mode", "event").value, 1)
        self.assertEqual(
            apply_report(plan, RuntimeReport("manuSpecificLumi", "mode", 0)),
            {"operation_mode": "command"},
        )

    def test_lumi_slider_uses_static_telemetry_and_action_bindings(self) -> None:
        source = """
        export const definitions = [{
            model: "Modern Lumi slider",
            vendor: "Aqara",
            extend: [
                lumi.lumiModernExtend.lumiAction({extraActions: ["slider_single", "slider_up"]}),
                lumi.lumiModernExtend.lumiSlider(),
            ],
        }];
        """
        plan = build_runtime_plan(parse_source(source, "lumi-slider.ts").devices[0])
        self.assertFalse(plan.device.partial)
        self.assertEqual(
            apply_report(plan, RuntimeReport("manuSpecificLumi", 0x0231, 42)),
            {"action_slide_time": 42},
        )
        self.assertEqual(
            apply_report(plan, RuntimeReport("manuSpecificLumi", 0x028C, 4)),
            {"action": "slider_up"},
        )

    def test_lumi_rgb_effect_macros_use_static_lookups(self) -> None:
        source = """
        export const definitions = [{
            model: "Modern Lumi RGB effect",
            vendor: "Aqara",
            extend: [
                lumi.lumiModernExtend.lumiRGBEffect({off: 0, breathing: 1, candlelight: 2}),
                lumi.lumiModernExtend.lumiRGBEffectSpeed(),
            ],
        }];
        """
        plan = build_runtime_plan(parse_source(source, "lumi-rgb-effect.ts").devices[0])
        self.assertFalse(plan.device.partial)
        self.assertEqual(make_write(plan, "effect", "candlelight").value, 2)
        self.assertEqual(make_write(plan, "effect_speed", 75).value, 75)
        self.assertEqual(
            apply_report(plan, RuntimeReport("manuSpecificLumi", 0x051F, 1)),
            {"effect": "breathing"},
        )

    def test_lumi_air_quality_display_and_voc_are_static(self) -> None:
        source = """
        export const definitions = [{
            model: "Modern Lumi air quality",
            vendor: "Aqara",
            extend: [
                lumi.lumiModernExtend.lumiAirQuality(),
                lumi.lumiModernExtend.lumiDisplayUnit(),
                lumi.lumiModernExtend.lumiVoc(),
            ],
        }];
        """
        plan = build_runtime_plan(parse_source(source, "lumi-air-quality.ts").devices[0])
        self.assertFalse(plan.device.partial)
        self.assertEqual(
            [(item.property, item.cluster, item.attribute, item.access) for item in plan.entities],
            [
                ("air_quality", "manuSpecificLumi", "airQuality", ("state", "get")),
                ("display_unit", "manuSpecificLumi", "displayUnit", ("state", "set")),
                ("voc", "genAnalogInput", "presentValue", ("state", "get")),
            ],
        )
        self.assertEqual(make_write(plan, "display_unit", "ppb_fahrenheit").value, 17)
        self.assertEqual(
            apply_report(plan, RuntimeReport("manuSpecificLumi", "airQuality", 4)),
            {"air_quality": "poor"},
        )

    def test_lumi_vibration_and_sensor_settings_are_static(self) -> None:
        source = """
        export const definitions = [{
            model: "Modern Lumi vibration",
            vendor: "Aqara",
            extend: [
                lumi.lumiModernExtend.lumiVibration(),
                lumi.lumiModernExtend.lumiSensitivityAdjustment(),
                lumi.lumiModernExtend.lumiReportInterval(),
            ],
        }];
        """
        plan = build_runtime_plan(parse_source(source, "lumi-vibration.ts").devices[0])
        self.assertFalse(plan.device.partial)
        self.assertEqual(make_write(plan, "sensitivity_adjustment", "low").value, 3)
        self.assertEqual(make_write(plan, "report_interval", "5s").value, 2)
        self.assertEqual(
            apply_report(plan, RuntimeReport("manuSpecificLumi", "movement", 1)),
            {"action": "movement"},
        )

    def test_lumi_static_state_action_is_a_static_auxiliary_binding(self) -> None:
        source = """
        export const definitions = [{
            model: "Modern Lumi static action",
            vendor: "Aqara",
            extend: [
                lumi.lumiModernExtend.lumiAction({extraActions: ["static"]}),
                lumi.lumiModernExtend.lumiStaticStateAction(),
            ],
        }];
        """
        plan = build_runtime_plan(parse_source(source, "lumi-static-action.ts").devices[0])
        self.assertFalse(plan.device.partial)
        self.assertEqual(
            apply_report(plan, RuntimeReport("manuSpecificLumi", 0x01F3, 1)),
            {"action": "static"},
        )

    def test_standard_converter_aliases_are_normalized_to_zcl(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["STANDARD"],
            model: "Standard",
            vendor: "Example",
            fromZigbee: [
                fz.metering,
                fz.electrical_measurement,
                fz.thermostat,
                fz.thermostat_local_temperature,
                fz.cover_position_tilt,
                fz.lock,
                fz.fan,
            ],
            toZigbee: [
                tz.thermostat_system_mode,
                tz.thermostat_occupied_heating_setpoint,
                tz.cover_state,
                tz.power_on_behavior,
            ],
        }];
        """
        device = normalize_device(parse_source(source, "standard-converters.ts").devices[0])
        self.assertFalse(device.partial)
        bindings = [*device.from_zigbee, *device.to_zigbee]
        self.assertEqual(
            [(item.converter.rsplit(".", 1)[-1], item.cluster, item.attribute) for item in bindings],
            [
                ("metering", "seMetering", None),
                ("electrical_measurement", "haElectricalMeasurement", None),
                ("thermostat", "hvacThermostat", None),
                ("thermostat_local_temperature", "hvacThermostat", "localTemp"),
                ("cover_position_tilt", "closuresWindowCovering", "currentPositionTiltPercentage"),
                ("lock", "closuresDoorLock", "lockState"),
                ("fan", "hvacFanCtrl", "fanMode"),
                ("thermostat_system_mode", "hvacThermostat", "systemMode"),
                ("thermostat_occupied_heating_setpoint", "hvacThermostat", "occupiedHeatingSetpoint"),
                ("cover_state", "closuresWindowCovering", None),
                ("power_on_behavior", "genOnOff", "startUpOnOff"),
            ],
        )

    def test_function_calls_are_not_executed(self) -> None:
        source = (ROOT / "fixtures" / "unsafe_device.ts").read_text()
        result = parse_source(source, "unsafe_device.ts")
        self.assertEqual(len(result.devices), 1)
        self.assertTrue(result.devices[0].partial)
        self.assertEqual(result.devices[0].model, "Unsafe")
        self.assertEqual(result.devices[0].exposes, [])

    def test_export_is_python_data_only(self) -> None:
        source = (ROOT / "fixtures" / "simple_device.ts").read_text()
        device = parse_source(source).devices[0]
        destination = ROOT / ".tmp-generated.py"
        try:
            export_python([device], destination)
            text = destination.read_text()
            self.assertIn("DEVICES =", text)
            self.assertNotIn("eval(", text)
            compile(text, str(destination), "exec")
        finally:
            destination.unlink(missing_ok=True)

    def test_result_is_json_serializable(self) -> None:
        source = (ROOT / "fixtures" / "simple_device.ts").read_text()
        result = parse_source(source)
        json.dumps(result.to_dict())

    def test_runtime_registry_normalizes_devices(self) -> None:
        source = (ROOT / "fixtures" / "simple_device.ts").read_text()
        registry = register_result(parse_source(source))
        self.assertEqual(len(registry.devices), 1)
        self.assertEqual(registry.devices[0].from_zigbee[0].cluster, "on_off")

    def test_runtime_builder_adapter_uses_zha_entity_names(self) -> None:
        source = (ROOT / "fixtures" / "simple_device.ts").read_text()
        registry = register_result(parse_source(source))
        calls = []
        prevented_clusters = []

        class Builder:
            def __init__(self, vendor, model):
                calls.append(("init", vendor, model))

            def switch(self, **kwargs):
                calls.append(("switch", kwargs))

            def sensor(self, **kwargs):
                calls.append(("sensor", kwargs))

            def prevent_default_entity_creation(self, **kwargs):
                prevented_clusters.append(kwargs["cluster_id"])

            def add_to_registry(self):
                calls.append(("register",))

        register_with_zha(registry, Builder)
        self.assertEqual(calls[0], ("init", "Example", "Test Plug"))
        self.assertEqual([item[0] for item in calls], ["init", "switch", "sensor", "register"])
        self.assertEqual(prevented_clusters, [0x0B04, 0x0702])

    def test_supported_measurement_entity_keeps_its_default_cluster(self) -> None:
        source = """
        export const definitions = [{
            model: "Meter",
            vendor: "Example",
            exposes: [{type: "power", name: "power", property: "power", unit: "W"}],
        }];
        """
        prevented = []

        class Builder:
            def __init__(self, vendor, model):
                pass

            def sensor(self, **kwargs):
                self.sensor_kwargs = kwargs

            def prevent_default_entity_creation(self, **kwargs):
                prevented.append(kwargs)

            def add_to_registry(self):
                pass

        register_with_zha(register_result(parse_source(source)), Builder)
        self.assertEqual([item["cluster_id"] for item in prevented], [0x0702, 0x0B04])
        self.assertEqual(prevented[-1]["unique_id_suffix"], "2820-power_factor")

    def test_standard_measurement_entities_are_not_duplicated(self) -> None:
        source = """
        export const definitions = [{
            model: "Meter",
            vendor: "Example",
            exposes: [
                {type: "power", name: "power", property: "power", unit: "W"},
                {type: "current", name: "current", property: "current", unit: "A"},
                {type: "voltage", name: "voltage", property: "voltage", unit: "V"},
                {type: "energy", name: "energy", property: "energy", unit: "kWh"},
            ],
        }];
        """
        calls = []

        class Builder:
            def __init__(self, manufacturer, model):
                pass

            def sensor(self, **kwargs):
                calls.append(kwargs)

            def prevent_default_entity_creation(self, **kwargs):
                pass

            def add_to_registry(self):
                pass

        register_with_zha(register_result(parse_source(source)), Builder)
        self.assertEqual(calls, [])
        from zha_z2m_converters.runtime import RuntimeEntity, _is_default_measurement_entity

        self.assertTrue(_is_default_measurement_entity(RuntimeEntity("power", "numeric", "power", "haElectricalMeasurement", "activePower")))

    def test_modern_extend_macros_are_expanded_without_execution(self) -> None:
        source = (ROOT / "fixtures" / "modern_extend.ts").read_text()
        result = parse_source(source, "modern_extend.ts")
        self.assertEqual(len(result.devices), 1)
        device = result.devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(device.unsupported_macros, [])
        self.assertGreaterEqual(len(device.exposes), 3)
        self.assertIn("light", [item.type for item in device.exposes])
        self.assertIn("battery", [item.name for item in device.exposes])
        self.assertIn("lightingColorCtrl", [item.cluster for item in device.from_zigbee])

    def test_tuya_datapoint_macros_are_declarative_and_runtime_safe(self) -> None:
        source = """
        export const definitions = [{
            model: "DP device",
            vendor: "Tuya",
            extend: [
                tuya.modernExtend.tuyaBase({dp: true}),
                tuya.modernExtend.dpOnOff({dp: 1}),
                tuya.modernExtend.dpNumeric({name: "temperature", dp: 2, type: tuya.dataTypes.number, scale: 10, unit: "°C"}),
                tuya.modernExtend.dpEnumLookup({name: "mode", dp: 3, type: tuya.dataTypes.enum, lookup: {off: 0, on: 1}}),
            ],
        }];
        """
        device = parse_source(source, "tuya_dp.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual([item.type for item in device.exposes], ["switch", "numeric", "enum"])

        plan = build_runtime_plan(device)
        self.assertEqual(apply_report(plan, RuntimeReport("manuSpecificTuya", "dpValues", True, dp=1)), {"state": "ON"})
        self.assertEqual(apply_report(plan, RuntimeReport("manuSpecificTuya", "dpValues", 2150, dp=2)), {"temperature": 215})
        self.assertEqual(apply_report(plan, RuntimeReport("manuSpecificTuya", "dpValues", 1, dp=3)), {"mode": "on"})

        state_write = make_write(plan, "state", "OFF")
        self.assertEqual(state_write.command, "dataRequest")
        self.assertEqual(state_write.payload["dpValues"], [{"dp": 1, "datatype": 1, "data": [0]}])
        temperature_write = make_write(plan, "temperature", 21.5)
        self.assertEqual(temperature_write.payload["dpValues"], [{"dp": 2, "datatype": 2, "data": [0, 0, 0, 215]}])
        mode_write = make_write(plan, "mode", "on")
        self.assertEqual(mode_write.payload["dpValues"], [{"dp": 3, "datatype": 4, "data": [1]}])

        replacements = []
        entities = []

        class Builder:
            def __init__(self, manufacturer, model):
                pass

            def replaces(self, cluster, **kwargs):
                replacements.append((cluster, kwargs))

            def switch(self, **kwargs):
                entities.append(("switch", kwargs))

            def number(self, **kwargs):
                entities.append(("number", kwargs))

            def select(self, **kwargs):
                entities.append(("select", kwargs))

            def add_to_registry(self):
                pass

        with patch("zha_z2m_converters.runtime._tuya_datapoint_cluster", return_value=object()):
            register_with_zha(register_result(parse_source(source)), Builder)
        self.assertEqual(len(replacements), 1)
        self.assertEqual([item[1]["attribute_name"] for item in entities], ["dp_1", "dp_2"])

    def test_tuya_base_configure_options_are_declarative(self) -> None:
        source = """
        export const definitions = [{
            model: "Tuya configure",
            vendor: "Tuya",
            extend: [tuya.modernExtend.tuyaBase({dp: true, queryOnConfigure: true, bindBasicOnConfigure: true})],
        }];
        """
        device = parse_source(source, "tuya_base_configure.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(device.unsupported_macros, [])
        self.assertEqual(
            [(item.operation, item.endpoint, item.cluster, item.command, item.payload) for item in device.configure_actions],
            [
                ("command", 1, "manuSpecificTuya", "dataQuery", {}),
                ("bind", 1, "genBasic", None, None),
            ],
        )
        self.assertEqual([(item.cluster, item.attribute) for item in device.from_zigbee], [("manuSpecificTuya", "dpValues")])

    def test_tuya_base_unsupported_options_remain_partial(self) -> None:
        source = """
        export const definitions = [{
            model: "Tuya time sync",
            vendor: "Tuya",
            extend: [tuya.modernExtend.tuyaBase({queryOnConfigure: true, timeStart: "2000"})],
        }];
        """
        device = parse_source(source, "tuya_base_time.ts").devices[0]
        self.assertTrue(device.partial)
        self.assertIn("tuyaBase", device.unsupported_macros)
        self.assertEqual(device.configure_actions[0].command, "dataQuery")

    def test_tuya_datapoint_wrappers_and_range_scale_are_static(self) -> None:
        source = """
        export const definitions = [{
            model: "DP wrappers",
            vendor: "Tuya",
            extend: [
                tuya.modernExtend.tuyaBase({dp: true}),
                tuya.modernExtend.dpTemperature({dp: 1, endpoint: 2}),
                tuya.modernExtend.dpAction({dp: 2, lookup: {single: 0, double: 1}}),
                tuya.modernExtend.dpNumeric({name: "brightness", dp: 3, type: tuya.dataTypes.number, scale: [0, 254, 0, 1000]}),
            ],
        }];
        """
        device = parse_source(source, "tuya_dp_wrappers.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual([item.type for item in device.exposes], ["temperature", "button", "numeric"])
        plan = build_runtime_plan(device)
        self.assertEqual(plan.entities[0].endpoint, 2)
        self.assertEqual(apply_report(plan, RuntimeReport("manuSpecificTuya", "dpValues", 215, dp=1)), {"temperature": 21.5})
        self.assertEqual(apply_report(plan, RuntimeReport("manuSpecificTuya", "dpValues", 1, dp=2)), {"action": "double"})
        self.assertEqual(apply_report(plan, RuntimeReport("manuSpecificTuya", "dpValues", 127, dp=3)), {"brightness": 500.0})
        self.assertEqual(make_write(plan, "brightness", 500).payload["dpValues"], [{"dp": 3, "datatype": 2, "data": [0, 0, 0, 127]}])

    def test_tuya_fingerprint_and_on_off_extend_are_recovered(self) -> None:
        source = """
        export const definitions = [{
            fingerprint: [...tuya.fingerprint("TS0001", ["_TZ3000_46t1rvdu"])],
            model: "WHD02",
            vendor: "Tuya",
            extend: [tuya.modernExtend.tuyaBase(), tuya.modernExtend.tuyaOnOff({switchType: true, onOffCountdown: true})],
            configure: async (device, coordinatorEndpoint) => {
                await tuya.configureMagicPacket(device, coordinatorEndpoint);
                const endpoint = device.getEndpoint(1);
                await reporting.bind(endpoint, coordinatorEndpoint, ["genOnOff"]);
                await reporting.onOff(endpoint);
            },
        }];
        """
        device = parse_source(source, "whd02.ts").devices[0]
        self.assertEqual(device.fingerprints, [{"modelID": "TS0001", "manufacturerName": "_TZ3000_46t1rvdu"}])
        self.assertFalse(device.partial)
        self.assertIn("state", [item.name for item in device.exposes])
        self.assertIn("countdown", [item.name for item in device.exposes])
        self.assertIn("switch_type", [item.name for item in device.exposes])
        self.assertIn("power_on_behavior", [item.name for item in device.exposes])
        self.assertEqual(device.configure_actions[0].operation, "read")
        self.assertEqual(device.configure_actions[0].target, "device")
        self.assertEqual(device.configure_actions[0].attributes[-1], 0xFFFE)
        self.assertEqual(device.configure_actions[-1].cluster, "genOnOff")

        calls = []
        replacements = []
        numbers = []
        prevented_clusters = []

        class Builder:
            def __init__(self, manufacturer, model):
                calls.append((manufacturer, model))

            def switch(self, **kwargs):
                pass

            def select(self, **kwargs):
                pass

            def replaces(self, cluster, **kwargs):
                replacements.append((cluster, kwargs))

            def prevent_default_entity_creation(self, **kwargs):
                prevented_clusters.append(kwargs["cluster_id"])

            def number(self, **kwargs):
                numbers.append(kwargs)

            def add_to_registry(self):
                pass

        with patch("zha_z2m_converters.runtime._tuya_on_off_cluster", return_value=object()), patch(
            "zha_z2m_converters.runtime._tuya3_cluster", return_value=object()
        ):
            register_with_zha(register_result(parse_source(source)), Builder)
        self.assertEqual(calls, [("_TZ3000_46t1rvdu", "TS0001")])
        self.assertEqual(len(replacements), 2)
        self.assertEqual(numbers[0]["attribute_name"], "on_time")
        self.assertEqual(numbers[0]["cluster_id"], 0x0006)
        self.assertEqual(numbers[0]["max_value"], 43200)
        self.assertEqual(prevented_clusters, [0x0B04, 0x0702])

        plan = build_runtime_plan(device)
        self.assertEqual(apply_report(plan, RuntimeReport("genOnOff", "onTime", 42)), {"countdown": 42})
        self.assertEqual(apply_report(plan, RuntimeReport("manuSpecificTuya3", "switchType", 2)), {"switch_type": "momentary"})
        self.assertEqual(apply_report(plan, RuntimeReport("genOnOff", "moesStartUpOnOff", 1)), {"power_on_behavior": "on"})
        countdown_write = make_write(plan, "countdown", 120)
        self.assertEqual(countdown_write.operation, "command")
        self.assertEqual(countdown_write.command, "onWithTimedOff")
        self.assertEqual(countdown_write.payload, {"ctrlbits": 0, "ontime": 120, "offwaittime": 120})
        state_write = make_write(plan, "state", True)
        self.assertEqual((state_write.operation, state_write.command, state_write.payload), ("command", "on", {}))
        switch_type_write = make_write(plan, "switch_type", "momentary")
        self.assertEqual(switch_type_write.value, 2)
        power_behavior_write = make_write(plan, "power_on_behavior", "previous")
        self.assertEqual(power_behavior_write.value, 2)

    def test_tuya_on_off_static_options_are_expanded(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["TUYA-OPTIONS"],
            model: "Tuya options",
            vendor: "Tuya",
            extend: [tuya.modernExtend.tuyaOnOff({powerOnBehavior2: true, electricalMeasurements: true})],
        }];
        """
        device = parse_source(source, "tuya-options.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(
            [(item.cluster, item.attribute) for item in device.from_zigbee[-5:]],
            [
                ("manuSpecificTuya3", "powerOnBehavior"),
                ("haElectricalMeasurement", "activePower"),
                ("haElectricalMeasurement", "rmsCurrent"),
                ("haElectricalMeasurement", "rmsVoltage"),
                ("seMetering", "currentSummDelivered"),
            ],
        )
        plan = build_runtime_plan(device)
        self.assertEqual(apply_report(plan, RuntimeReport("manuSpecificTuya3", "powerOnBehavior", 2)), {"power_on_behavior": "previous"})
        self.assertEqual(apply_report(plan, RuntimeReport("haElectricalMeasurement", "activePower", 42)), {"power": 42})

    def test_tuya_on_off_backlight_options_are_static(self) -> None:
        source = """
        export const definitions = [
            {
                model: "Tuya backlight modes",
                vendor: "Tuya",
                extend: [tuya.modernExtend.tuyaOnOff({backlightModeOffNormalInverted: true})],
            },
            {
                model: "Tuya backlight levels",
                vendor: "Tuya",
                extend: [tuya.modernExtend.tuyaOnOff({backlightModeLowMediumHigh: true})],
            },
            {
                model: "Tuya backlight switch",
                vendor: "Tuya",
                extend: [tuya.modernExtend.tuyaOnOff({backlightModeOffOn: true})],
            },
        ];
        """
        devices = parse_source(source, "tuya-backlight.ts").devices
        self.assertEqual([device.partial for device in devices], [False, False, False])
        modes_plan = build_runtime_plan(devices[0])
        levels_plan = build_runtime_plan(devices[1])
        switch_plan = build_runtime_plan(devices[2])
        self.assertEqual(apply_report(modes_plan, RuntimeReport("genOnOff", "tuyaBacklightMode", 2)), {"backlight_mode": "inverted"})
        self.assertEqual(apply_report(levels_plan, RuntimeReport("genOnOff", "tuyaBacklightMode", 2)), {"backlight_mode": "high"})
        self.assertEqual(apply_report(switch_plan, RuntimeReport("genOnOff", "tuyaBacklightSwitch", 1)), {"backlight_mode": True})
        self.assertEqual(make_write(modes_plan, "backlight_mode", "normal").value, 1)

    def test_tuya_on_off_remaining_static_options_are_expanded(self) -> None:
        source = """
        export const definitions = [{
            model: "Tuya remaining options",
            vendor: "Tuya",
            endpoint: (device) => ({l1: 1, l2: 2}),
            extend: [tuya.modernExtend.tuyaOnOff({
                endpoints: ["l1", "l2"],
                switchMode: true,
                switchTypeCurtain: true,
                indicatorModeNoneRelayPos: true,
                powerOnBehavior3: true,
            })],
        }];
        """
        device = parse_source(source, "tuya-remaining-options.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(
            [(item.name, item.endpoint) for item in device.exposes],
            [
                ("state", 1),
                ("state", 2),
                ("switch_type_curtain", None),
                ("power_on_behavior", 1),
                ("power_on_behavior", 2),
                ("indicator_mode", None),
                ("switch_mode", 1),
                ("switch_mode", 2),
            ],
        )
        plan = build_runtime_plan(device)
        self.assertEqual(
            apply_report(plan, RuntimeReport("manuSpecificTuya3", "switchMode", 1, endpoint=2)),
            {"switch_mode": "scene"},
        )
        self.assertEqual(
            apply_report(plan, RuntimeReport("manuSpecificTuya3", "switchType", 3)),
            {"switch_type_curtain": "button2-switch"},
        )
        self.assertEqual(
            apply_report(plan, RuntimeReport("genOnOff", "tuyaBacklightMode", 2)),
            {"indicator_mode": "pos"},
        )
        self.assertEqual(
            apply_report(plan, RuntimeReport("manuSpecificTuya", "powerOnBehavior3", 2, endpoint=1)),
            {"power_on_behavior": "previous"},
        )
        self.assertEqual(make_write(plan, "switch_mode", "scene").value, 1)
        self.assertEqual(make_write(plan, "power_on_behavior", "on").value, 1)

    def test_tuya_on_off_endpoints_are_expanded_from_static_maps(self) -> None:
        source = """
        export const definitions = [
            {
                model: "Tuya endpoint callback",
                vendor: "Tuya",
                endpoint: (device) => {
                    return {l1: 1, l2: 2};
                },
                extend: [tuya.modernExtend.tuyaOnOff({
                    endpoints: ["l1", "l2"],
                    powerOnBehavior2: true,
                    onOffCountdown: true,
                })],
            },
            {
                model: "Tuya endpoint metadata",
                vendor: "Tuya",
                extend: [
                    m.deviceEndpoints({endpoints: {l1: 1, l2: 2}}),
                    tuya.modernExtend.tuyaOnOff({endpoints: ["l1", "l2"]}),
                ],
            },
        ];
        """
        devices = parse_source(source, "tuya-endpoints.ts").devices
        self.assertEqual([device.partial for device in devices], [False, False])
        self.assertEqual(
            [(item.name, item.endpoint) for item in devices[0].exposes],
            [
                ("state", 1),
                ("state", 2),
                ("countdown", 1),
                ("countdown", 2),
                ("power_on_behavior", 1),
                ("power_on_behavior", 2),
            ],
        )
        plan = build_runtime_plan(devices[0])
        self.assertEqual(
            apply_report(plan, RuntimeReport("genOnOff", "onOff", True, endpoint=2)),
            {"state": True},
        )
        self.assertEqual([item.endpoint for item in plan.entities], [1, 2, 1, 2, 1, 2])

    def test_tuya_on_off_conditional_options_keep_endpoint_context(self) -> None:
        source = """
        export const definitions = [{
            model: "Tuya conditional endpoint options",
            vendor: "Tuya",
            endpoint: (device) => ({l1: 1, l2: 2}),
            extend: [tuya.modernExtend.tuyaOnOff({
                endpoints: ["l1", "l2"],
                backlightModeOffOn: (manufacturer) => manufacturer !== "_NO_BACKLIGHT",
                powerOnBehavior2: (manufacturer) => manufacturer === "_POWER_BEHAVIOR",
            })],
        }];
        """
        device = parse_source(source, "tuya-conditional-endpoints.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(len(device.conditional_extends), 2)

        backlight = build_runtime_plan(device, "_NO_POWER_BEHAVIOR")
        self.assertIn("backlight_mode", [item.name for item in backlight.device.exposes])
        self.assertNotIn("power_on_behavior", [item.name for item in backlight.device.exposes])
        self.assertEqual(
            apply_report(backlight, RuntimeReport("genOnOff", "tuyaBacklightSwitch", 1)),
            {"backlight_mode": True},
        )

        power_behavior = build_runtime_plan(device, "_POWER_BEHAVIOR")
        self.assertIn("backlight_mode", [item.name for item in power_behavior.device.exposes])
        self.assertEqual(
            [item.endpoint for item in power_behavior.device.exposes if item.name == "power_on_behavior"],
            [1, 2],
        )
        self.assertEqual(
            apply_report(power_behavior, RuntimeReport("manuSpecificTuya3", "powerOnBehavior", 2, endpoint=2)),
            {"power_on_behavior": "previous"},
        )

    def test_tuya_on_off_conditional_options_are_expanded_per_fingerprint(self) -> None:
        source = (
            ROOT.parent
            / "custom_components"
            / "zha_z2m_converters"
            / "converters"
            / "zigbee-herdsman-converters"
            / "src"
            / "devices"
            / "tuya.ts"
        ).read_text()
        result = parse_source(source, "tuya.ts")
        device = next(item for item in result.devices if item.model == "TS011F_plug_1")
        self.assertEqual(device.unsupported_fields, ["configure"])
        self.assertEqual(len(device.conditional_extends), 5)

        zbeacon = build_runtime_plan(device, "Zbeacon")
        self.assertEqual(
            [item.name for item in zbeacon.device.exposes],
            [
                "state",
                "power",
                "current",
                "voltage",
                "energy",
                "power_outage_memory",
                "indicator_mode",
                "child_lock",
                "countdown",
                "switch_type_button",
            ],
        )
        self.assertEqual(
            apply_report(zbeacon, RuntimeReport("genOnOff", "tuyaBacklightMode", 2)),
            {"indicator_mode": "on/off"},
        )
        self.assertEqual(
            apply_report(zbeacon, RuntimeReport("genOnOff", "childLock", True)),
            {"child_lock": "LOCK"},
        )

        green_sun = build_runtime_plan(device, "_TZ3000_cicwjqth")
        self.assertNotIn("power_outage_memory", [item.name for item in green_sun.device.exposes])
        self.assertNotIn("child_lock", [item.name for item in green_sun.device.exposes])
        self.assertNotIn("countdown", [item.name for item in green_sun.device.exposes])

    def test_writable_binary_expose_is_registered_as_switch(self) -> None:
        calls = []

        class Builder:
            def switch(self, **kwargs):
                calls.append(("switch", kwargs))

            def binary_sensor(self, **kwargs):
                calls.append(("binary_sensor", kwargs))

        _apply_expose(
            Builder(),
            Expose("binary", "child_lock", "child_lock", ("state", "set")),
            RuntimeEntity("child_lock", "binary", "child_lock", "genOnOff", "childLock", access=("state", "set")),
        )
        self.assertEqual([name for name, _ in calls], ["switch"])

    def test_enum_expose_uses_integer_values_for_zha_select(self) -> None:
        enum_class = _make_enum_class(
            Expose("enum", "power_outage_memory", values=("off", "previous", "on"))
        )
        self.assertIsNotNone(enum_class)
        self.assertEqual(int(enum_class.PREVIOUS), 1)

    def test_vendor_light_aliases_use_safe_light_expansion(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: [\"LEDVANCE\"],
            model: \"LEDVANCE\",
            vendor: \"Example\",
            extend: [
                ledvanceLight({colorTemp: {range: [153, 370]}, color: true}),
                tuyaLight({colorTemp: {range: [153, 500]}}),
            ],
        }];
        """
        result = parse_source(source, "vendor_aliases.ts")
        self.assertEqual(len(result.devices), 1)
        device = result.devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(device.unsupported_macros, [])

    def test_vendor_light_modern_extend_aliases_keep_static_features(self) -> None:
        source = """
        export const definitions = [
            {
                zigbeeModel: ["SENGLED"],
                model: "SENGLED",
                vendor: "Sengled",
                extend: [sengledLight({colorTemp: {range: [154, 500]}, color: {modes: ["xy"]}})],
            },
            {
                zigbeeModel: ["IKEA"],
                model: "IKEA",
                vendor: "IKEA",
                extend: [ikeaLight({colorTemp: true})],
            },
            {
                zigbeeModel: ["GLEDOPTO"],
                model: "GLEDOPTO",
                vendor: "Gledopto",
                extend: [gledoptoLight({color: true})],
            },
        ];
        """
        devices = parse_source(source, "light_aliases.ts").devices
        self.assertEqual(len(devices), 3)
        for device in devices:
            self.assertFalse(device.partial)
            self.assertEqual(device.unsupported_macros, [])
            self.assertIn("light", [item.name for item in device.exposes])

        ikea_color_temperature = next(item for item in devices[1].exposes if item.name == "color_temperature")
        self.assertEqual((ikea_color_temperature.value_min, ikea_color_temperature.value_max), (250, 454))

    def test_vendor_light_alias_keeps_unsupported_behavior_partial(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["GLEDOPTO"],
            model: "GLEDOPTO",
            vendor: "Gledopto",
            extend: [gledoptoLight({configureReporting: true})],
        }];
        """
        device = parse_source(source, "light_alias_partial.ts").devices[0]
        self.assertTrue(device.partial)
        self.assertEqual(device.unsupported_macros, ["gledoptoLight"])
        self.assertIn("light", [item.name for item in device.exposes])

    def test_lumi_ota_extend_adds_gen_ota_output_cluster(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["LUMI"],
            model: "LUMI",
            vendor: "Lumi",
            extend: [lumiZigbeeOTA()],
        }];
        """
        device = parse_source(source, "lumi_ota.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(
            [(item.endpoint, item.cluster, item.direction) for item in device.endpoint_clusters],
            [(1, "genOta", "output")],
        )

        plan = build_runtime_plan(device)
        calls = []

        class Builder:
            def adds(self, cluster, **kwargs):
                calls.append((cluster, kwargs))

        fake_zcl = ModuleType("zigpy.zcl")
        fake_zcl.ClusterType = type("ClusterType", (), {"Server": "server", "Client": "client"})
        with patch.dict(sys.modules, {"zigpy": ModuleType("zigpy"), "zigpy.zcl": fake_zcl}):
            self.assertTrue(_configure_endpoint_clusters(Builder(), plan))
        self.assertEqual(calls, [(0x0019, {"cluster_type": "client", "endpoint_id": 1})])

    def test_tuya_common_private_cluster_is_declarative(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["TS0002"],
            model: "TS0002",
            vendor: "Tuya",
            extend: [tuya.clusters.addTuyaCommonPrivateCluster()],
        }];
        """
        device = parse_source(source, "tuya-common-private-cluster.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(device.custom_clusters, ["manuSpecificTuya4"])
        self.assertEqual(build_runtime_plan(device).custom_clusters, ["manuSpecificTuya4"])

    def test_literal_custom_cluster_schema_is_declarative(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["CUSTOM"],
            model: "CUSTOM",
            vendor: "Example",
            extend: [m.deviceAddCustomCluster("customCluster", {
                name: "customCluster",
                ID: 0xfc00,
                attributes: {
                    level: {name: "level", ID: 1, type: Zcl.DataType.UINT16, write: true},
                },
                commands: {
                    reset: {name: "reset", ID: 2, parameters: []},
                },
                commandsResponse: {},
            })],
        }];
        """
        device = parse_source(source, "custom-cluster.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(device.unsupported_macros, [])
        self.assertEqual(device.custom_cluster_specs[0].name, "customCluster")
        self.assertEqual(device.custom_cluster_specs[0].cluster_id, 0xFC00)
        self.assertEqual(device.custom_cluster_specs[0].attributes[0]["type"], "uint16_t")

    def test_lumi_custom_cluster_macro_is_declarative(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["LUMI"],
            model: "LUMI",
            vendor: "Aqara",
            extend: [lumi.modernExtend.addManuSpecificLumiCluster()],
        }];
        """
        device = parse_source(source, "lumi-custom-cluster.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(device.custom_cluster_specs[0].name, "manuSpecificLumi")
        self.assertEqual(device.custom_cluster_specs[0].cluster_id, 0xFCC0)
        self.assertEqual(device.custom_cluster_specs[0].manufacturer_code, 0x115F)
        self.assertEqual(len(device.custom_cluster_specs[0].attributes), 49)

    def test_ikea_unknown_cluster_macro_is_declarative(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["IKEA"],
            model: "IKEA",
            vendor: "IKEA",
            extend: [ikea.modernExtend.addCustomClusterManuSpecificIkeaUnknown()],
        }];
        """
        device = parse_source(source, "ikea-unknown-cluster.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(device.custom_cluster_specs[0].cluster_id, 0xFC7C)
        self.assertEqual(device.custom_cluster_specs[0].manufacturer_code, 0x117C)

    def test_develco_basic_cluster_macro_is_declarative(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["DEVELCO"],
            model: "DEVELCO",
            vendor: "Develco",
            extend: [develco.modernExtend.addCustomClusterManuSpecificDevelcoGenBasic()],
        }];
        """
        device = parse_source(source, "develco-basic-cluster.ts").devices[0]
        self.assertFalse(device.partial)
        spec = device.custom_cluster_specs[0]
        self.assertEqual(spec.cluster_id, 0x0000)
        self.assertEqual(spec.attributes[0]["manufacturer_code"], 0x1015)
        self.assertEqual(spec.attributes[0]["type"], "LVBytes")
        self.assertEqual(len(spec.attributes), 4)

    def test_develco_fixed_cluster_macros_are_declarative(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["DEVELCO"],
            model: "DEVELCO",
            vendor: "Develco",
            extend: [
                develco.modernExtend.addCustomClusterManuSpecificDevelcoIasZone(),
                develco.modernExtend.addCustomClusterManuSpecificDevelcoAirQuality(),
                develco.modernExtend.addCustomDevelcoSeMeteringCluster(),
            ],
        }];
        """
        device = parse_source(source, "develco-fixed-clusters.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual([spec.cluster_id for spec in device.custom_cluster_specs], [0x0500, 0xFC03, 0x0702])
        self.assertEqual(device.custom_cluster_specs[1].manufacturer_code, 0x1015)
        self.assertEqual(device.custom_cluster_specs[2].attributes[1]["type"], "uint48_t")

    def test_tuya_inching_switch_is_expanded_into_writable_entities(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["TS0002"],
            model: "TS0002",
            vendor: "Tuya",
            endpoint: (device) => ({l1: 1, l2: 2}),
            extend: [
                tuya.modernExtend.tuyaOnOff({inchingSwitch: true, endpoints: ["l1", "l2"]}),
                tuya.clusters.addTuyaCommonPrivateCluster(),
            ],
        }];
        """
        device = parse_source(source, "tuya-inching.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(
            [item.name for item in device.exposes if item.name.startswith("inching_")],
            ["inching_control_1", "inching_time_1", "inching_control_2", "inching_time_2"],
        )
        plan = build_runtime_plan(device)
        control_write = make_write(plan, "inching_control_2", True)
        self.assertEqual(control_write.command, "setInchingSwitch")
        self.assertEqual(control_write.payload, {"payload": bytes([3, 0, 1])})
        time_write = make_write(plan, "inching_time_2", 30.0)
        self.assertEqual(time_write.payload, {"payload": bytes([2, 0, 30])})
        with self.assertRaises(ValueError):
            make_write(plan, "inching_time_2", 1.5)

    def test_conditional_tuya_inching_switch_is_added_for_matching_manufacturer(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["TS0002"],
            model: "TS0002",
            vendor: "Tuya",
            endpoint: (device) => ({l1: 1, l2: 2}),
            extend: [
                tuya.modernExtend.tuyaOnOff({inchingSwitch: (manufacturerName) => manufacturerName === "_TZ3000_test", endpoints: ["l1", "l2"]}),
                tuya.clusters.addTuyaCommonPrivateCluster(),
            ],
        }];
        """
        device = parse_source(source, "tuya-conditional-inching.ts").devices[0]
        matching = build_runtime_plan(device, "_TZ3000_test")
        non_matching = build_runtime_plan(device, "_TZ3000_other")
        self.assertIn("inching_control_2", {item.property for item in matching.entities})
        self.assertNotIn("inching_control_2", {item.property for item in non_matching.entities})

    def test_fluent_exposes_and_simple_configure_are_recovered_safely(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["CLIMATE"],
            model: "Climate",
            vendor: "Example",
            exposes: [e.climate().withSetpoint("occupied_heating_setpoint", 7, 28, 0.5).withLocalTemperature()],
            configure: async (device, coordinatorEndpoint) => {
                await device.getEndpoint(1).bind(coordinatorEndpoint, "hvacThermostat");
            },
        }];
        """
        result = parse_source(source, "fluent.ts")
        self.assertEqual(result.rejected_definitions, 0)
        self.assertEqual(len(result.devices), 1)
        self.assertEqual(result.devices[0].exposes[0].type, "climate")
        self.assertFalse(result.devices[0].partial)
        self.assertEqual(result.devices[0].configure_actions[0].cluster, "hvacThermostat")

    def test_simple_configure_bind_is_extracted_without_execution(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["THERMOSTAT"],
            model: "Thermostat",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                await device.getEndpoint(1).bind(coordinatorEndpoint, "hvacThermostat");
            },
        }];
        """
        result = parse_source(source, "configure.ts")
        device = result.devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(len(device.configure_actions), 1)
        self.assertEqual(device.configure_actions[0].endpoint, 1)
        self.assertEqual(device.configure_actions[0].cluster, "hvacThermostat")
        plan = build_runtime_plan(device)
        self.assertEqual(plan.configure_actions, device.configure_actions)

    def test_named_configure_function_is_parsed_as_static_callback(self) -> None:
        source = """
        async function configureExample(device, coordinatorEndpoint) {
            const endpoint = device.getEndpoint(1);
            await endpoint.bind(coordinatorEndpoint, "genOnOff");
            await endpoint.read("genBasic", ["modelId"]);
        }
        export const definitions = [{
            zigbeeModel: ["NAMED_CONFIGURE"],
            model: "Named configure",
            vendor: "Example",
            configure: configureExample,
        }];
        """
        device = parse_source(source, "configure-named-function.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(
            device.configure_actions,
            [
                ConfigureAction("bind", 1, "genOnOff"),
                ConfigureAction("read", 1, "genBasic", attributes=("modelId",), target="device"),
            ],
        )

    def test_configure_extracts_coordinator_endpoint_bind(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["COORDINATOR_ENDPOINT_BIND"],
            model: "Coordinator endpoint bind",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                const endpoint = device.getEndpoint(1);
                const coordinatorEndpointB = coordinatorEndpoint.getDevice().getEndpoint(11);
                await reporting.bind(endpoint, coordinatorEndpointB, ["genOnOff"]);
            },
        }];
        """
        device = parse_source(source, "configure-coordinator-endpoint-bind.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(
            device.configure_actions,
            [ConfigureAction("bind", 1, "genOnOff", destination_endpoint=11)],
        )

    def test_static_configure_commands_and_tuya_helpers_are_extracted(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["COMMANDS"],
            model: "Commands",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                const endpoint = device.getEndpoint(1);
                await endpoint.command("genOnOff", "on", {payloadSize: 1, payload: 1});
                await tuya.configureQuery(device, coordinatorEndpoint);
                await tuya.configureBindBasic(device, coordinatorEndpoint);
            },
        }];
        """
        device = parse_source(source, "configure-commands.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(
            [(item.operation, item.endpoint, item.cluster, item.command, item.payload) for item in device.configure_actions],
            [
                ("command", 1, "genOnOff", "on", {"payloadSize": 1, "payload": 1}),
                ("command", 1, "manuSpecificTuya", "dataQuery", {}),
                ("bind", 1, "genBasic", None, None),
            ],
        )

    def test_static_configure_command_supports_buffer_from(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["BUFFER_COMMAND"],
            model: "Buffer command",
            vendor: "Example",
            configure: async (device) => {
                await device.getEndpoint(1).command(
                    "boschSpecific",
                    "pairingCompleted",
                    {data: Buffer.from([0x00, 0x7f])},
                );
            },
        }];
        """
        device = parse_source(source, "configure-buffer-command.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(device.configure_actions[0].payload, {"data": bytes([0x00, 0x7F])})

    def test_unsupported_configure_loop_does_not_hide_following_static_statement(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["LOOP_RECOVERY"],
            model: "Loop recovery",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                for (let i = 1; i <= 2; i++) {
                    const endpoint = device.getEndpoint(i);
                    if (endpoint) await endpoint.bind(coordinatorEndpoint, "genOnOff");
                }
                const mainController = device.getEndpoint(232);
                await mainController.read("genBasic", ["modelId"]);
            },
        }];
        """
        device = parse_source(source, "configure-loop-recovery.ts").devices[0]
        self.assertTrue(device.partial)
        self.assertEqual(
            device.configure_actions,
            [ConfigureAction("read", 232, "genBasic", attributes=("modelId",), target="device")],
        )

    def test_static_configure_command_options_are_extracted(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["COMMAND_OPTIONS"],
            model: "Command options",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                await device.getEndpoint(1).command(
                    "genBasic",
                    "tuyaSetup",
                    {},
                    {disableDefaultResponse: true},
                );
            },
        }];
        """
        device = parse_source(source, "configure-command-options.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(device.configure_actions[0].options, {"disableDefaultResponse": True})

    def test_static_configure_reporting_payload_reassignment_is_extracted(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["REPORTING_PAYLOAD_REASSIGNMENT"],
            model: "Reporting payload reassignment",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                const endpoint = device.getEndpoint(1);
                let payload = reporting.payload("firstAttribute", 1, 120, 1);
                await endpoint.configureReporting("exampleCluster", payload);
                payload = reporting.payload("secondAttribute", 1, 120, 1);
                await endpoint.configureReporting("exampleCluster", payload);
            },
        }];
        """
        device = parse_source(source, "configure-reporting-payload-reassignment.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(
            [action.attributes for action in device.configure_actions],
            [("firstAttribute",), ("secondAttribute",)],
        )

    def test_static_configure_write_supports_numeric_arithmetic(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["WRITE_ARITHMETIC"],
            model: "Write arithmetic",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                const endpoint = device.getEndpoint(1);
                const interval = 100 - 10;
                await endpoint.write("genPollCtrl", {checkinInterval: interval * 4});
            },
        }];
        """
        device = parse_source(source, "configure-write-arithmetic.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(device.configure_actions[0].payload, {"checkinInterval": 360})

    def test_static_configure_actions_execute_only_whitelisted_operations(self) -> None:
        calls = []

        class Cluster:
            cluster_id = 0x0006

            def __init__(self):
                self._attr_cache = {}

            async def bind(self):
                calls.append(("bind",))

            async def read_attributes(self, attributes):
                calls.append(("read", attributes))

            async def configure_reporting(self, attribute, minimum, maximum, change):
                calls.append(("reporting", attribute, minimum, maximum, change))

            async def command(self, command, **payload):
                calls.append(("command", command, payload))

            async def write_attributes(self, attributes, **kwargs):
                calls.append(("write", attributes, kwargs))

        class Endpoint:
            in_clusters = {0x0006: Cluster()}
            out_clusters = {}

        class Destination:
            endpoint = 1

        class Application:
            def get_dst_address(self, cluster):
                return Destination()

        class Zdo:
            async def Bind_req(self, source_ieee, source_endpoint, cluster_id, destination):
                calls.append(("bind_req", source_ieee, source_endpoint, cluster_id, destination.endpoint))

        class ZigpyDevice:
            endpoints = {1: Endpoint()}
            ieee = "00:11:22:33:44:55:66:77"
            application = Application()
            zdo = Zdo()
            power_source = "Unknown"
            type = "Router"
            application_version = 7
            software_build_id = None

        class Device:
            _zigpy_device = ZigpyDevice()

        actions = (
            ConfigureAction("bind", 1, "genOnOff"),
            ConfigureAction("bind", 1, "genOnOff", destination_endpoint=11),
            ConfigureAction("read", 1, "genOnOff", attributes=("onOff",)),
            ConfigureAction(
                "configure_reporting",
                1,
                "genOnOff",
                attributes=("onOff",),
                minimum_interval=0,
                maximum_interval=3600,
                reportable_change=0,
            ),
            ConfigureAction(
                "command",
                1,
                "genOnOff",
                command="on",
                payload={"payloadSize": 1},
                options={"disableDefaultResponse": True},
            ),
            ConfigureAction("write", 1, "genOnOff", payload={"onOff": 1}),
            ConfigureAction("save_cluster_attributes", 1, "genOnOff", payload={"onTime": 10}),
            ConfigureAction(
                "set_device_property",
                payload={"name": "powerSource", "value": "Battery"},
                target="device",
            ),
            ConfigureAction(
                "set_device_property",
                payload={"name": "type", "value": "EndDevice"},
                target="device",
            ),
            ConfigureAction(
                "set_device_property",
                payload={
                    "name": "softwareBuildID",
                    "value": {
                        "__device_property_template__": {
                            "source": "applicationVersion",
                            "prefix": "0.0.0_00",
                        }
                    },
                },
                target="device",
            ),
        )
        asyncio.run(_apply_configure_actions(Device(), actions))
        self.assertEqual(
            calls,
            [
                ("bind",),
                ("bind_req", "00:11:22:33:44:55:66:77", 1, 0x0006, 11),
                ("read", ["onOff"]),
                ("reporting", "onOff", 0, 3600, 0),
                ("command", "on", {"payloadSize": 1, "expect_reply": False}),
                ("write", {"onOff": 1}, {}),
            ],
        )
        self.assertEqual(Endpoint.in_clusters[0x0006]._attr_cache, {"onTime": 10})
        self.assertEqual(Device._zigpy_device.power_source, "Battery")
        self.assertEqual(Device._zigpy_device.type, "EndDevice")
        self.assertEqual(Device._zigpy_device.software_build_id, "0.0.0_007")

    def test_static_configure_actions_pass_manufacturer_code(self) -> None:
        calls = []

        class Cluster:
            cluster_id = 0xFC00

            async def read_attributes(self, attributes, **kwargs):
                calls.append(("read", attributes, kwargs))

            def find_attribute(self, attribute, **kwargs):
                calls.append(("find", attribute, kwargs))
                return f"{attribute}-definition"

            async def configure_reporting(self, attribute, minimum, maximum, change):
                calls.append(("reporting", attribute, minimum, maximum, change))

        class Endpoint:
            in_clusters = {0xFC00: Cluster()}
            out_clusters = {}

        class ZigpyDevice:
            endpoints = {1: Endpoint()}

        class Device:
            _zigpy_device = ZigpyDevice()

        actions = (
            ConfigureAction(
                "read",
                1,
                0xFC00,
                attributes=("status",),
                manufacturer_code=0x115F,
            ),
            ConfigureAction(
                "configure_reporting",
                1,
                0xFC00,
                attributes=("status",),
                minimum_interval=1,
                maximum_interval=3600,
                reportable_change=1,
                manufacturer_code=0x115F,
            ),
        )
        asyncio.run(_apply_configure_actions(Device(), actions))
        self.assertEqual(
            calls,
            [
                ("read", ["status"], {"manufacturer": 0x115F}),
                ("find", "status", {"manufacturer_code": 0x115F}),
                ("reporting", "status-definition", 1, 3600, 1),
            ],
        )

    def test_configure_supports_literal_manufacturer_options(self) -> None:
        source = """
        const manufacturerOptions = {manufacturerCode: 0x115f};
        const reportingInterval = {maximum: 3600};
        export const definitions = [{
            zigbeeModel: ["MANUFACTURER_CONFIG"],
            model: "MANUFACTURER_CONFIG",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                const endpoint = device.getEndpoint(1);
                await endpoint.read("customCluster", ["status"], manufacturerOptions);
                await endpoint.configureReporting("customCluster", [
                    {attribute: "status", minimumReportInterval: 1, maximumReportInterval: reportingInterval.maximum, reportableChange: 1},
                ], manufacturerOptions);
            },
            exposes: [e.numeric("status", ea.STATE)],
        }];
        """
        device = parse_source(source, "manufacturer-configure.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(
            [action.manufacturer_code for action in device.configure_actions],
            [0x115F, 0x115F],
        )

    def test_configure_resolves_zcl_manufacturer_codes(self) -> None:
        source = """
        import {Zcl} from "zigbee-herdsman";
        export const definitions = [{
            zigbeeModel: ["ZCL_MANUFACTURER"],
            model: "ZCL_MANUFACTURER",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                const options = {manufacturerCode: Zcl.ManufacturerCode.DATEK_WIRELESS_AS};
                const endpoint = device.getEndpoint(1);
                await endpoint.read("ssIasZone", ["zoneStatus"], options);
                await endpoint.configureReporting("ssIasZone", [
                    {attribute: "zoneStatus", minimumReportInterval: 1, maximumReportInterval: 3600, reportableChange: 1},
                ], options);
            },
        }];
        """
        device = parse_source(source, "configure-zcl-manufacturer.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual([action.manufacturer_code for action in device.configure_actions], [0x1337, 0x1337])

    def test_configure_resolves_samjin_manufacturer_code(self) -> None:
        source = """
        import {Zcl} from "zigbee-herdsman";
        export const definitions = [{
            zigbeeModel: ["SAMJIN_MANUFACTURER"],
            model: "SAMJIN_MANUFACTURER",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                const endpoint = device.getEndpoint(1);
                await endpoint.read("customCluster", ["status"], {
                    manufacturerCode: Zcl.ManufacturerCode.SAMJIN_CO_LTD,
                });
            },
        }];
        """
        device = parse_source(source, "configure-samjin-manufacturer.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(device.configure_actions[0].manufacturer_code, 0x1241)

    def test_configure_supports_literal_writes(self) -> None:
        source = """
        const options = {manufacturerCode: 0x115f};
        export const definitions = [{
            zigbeeModel: ["WRITE_CONFIGURE"],
            model: "Write configure",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                const endpoint = device.getEndpoint(1);
                await endpoint.write("genBasic", {
                    52: {value: 0, type: 48},
                    powerSource: 1,
                }, options);
            },
        }];
        """
        device = parse_source(source, "configure-write.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(device.configure_actions[0].operation, "write")
        self.assertEqual(device.configure_actions[0].payload, {"52": 0, "powerSource": 1})
        self.assertEqual(device.configure_actions[0].manufacturer_code, 0x115F)

    def test_reporting_bind_and_endpoint_local_are_extracted(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["SENSOR"],
            model: "Sensor",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                const endpoint = device.getEndpoint(2);
                await reporting.bind(endpoint, coordinatorEndpoint, ["genPowerCfg", "msTemperatureMeasurement"]);
            },
        }];
        """
        device = parse_source(source, "configure-reporting.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(
            [(item.endpoint, item.cluster) for item in device.configure_actions],
            [(2, "genPowerCfg"), (2, "msTemperatureMeasurement")],
        )

    def test_static_configure_loops_are_expanded_without_execution(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["LOOP"],
            model: "Loop",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                for (const endpointId of [1, 2]) {
                    const endpoint = device.getEndpoint(endpointId);
                    await reporting.bind(endpoint, coordinatorEndpoint, ["genOnOff"]);
                    await reporting.onOff(endpoint);
                }
            },
        }];
        """
        device = parse_source(source, "configure-loop.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(
            [(item.operation, item.endpoint, item.cluster, item.attributes) for item in device.configure_actions],
            [
                ("bind", 1, "genOnOff", ()),
                ("configure_reporting", 1, "genOnOff", ("onOff",)),
                ("bind", 2, "genOnOff", ()),
                ("configure_reporting", 2, "genOnOff", ("onOff",)),
            ],
        )

    def test_configure_supports_const_assertions_and_endpoint_aliases(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["TYPED_CONFIGURE"],
            model: "Typed configure",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                const clusters = ["genOnOff", "genPowerCfg"] as const;
                await reporting.bind(endpoint1, coordinatorEndpoint, clusters);
                for (const cluster of clusters) {
                    await endpoint1.configureReporting(cluster, [
                        {attribute: "onOff" as const, minimumReportInterval: 0, maximumReportInterval: 3600, reportableChange: 0},
                    ]);
                }
            },
        }];
        """
        device = parse_source(source, "configure-typed.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(
            [(item.operation, item.endpoint, item.cluster) for item in device.configure_actions],
            [
                ("bind", 1, "genOnOff"),
                ("bind", 1, "genPowerCfg"),
                ("configure_reporting", 1, "genOnOff"),
                ("configure_reporting", 1, "genPowerCfg"),
            ],
        )

    def test_configure_extracts_actions_from_try_with_empty_catch(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["TRY_CONFIGURE"],
            model: "Try configure",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                const endpoint = device.getEndpoint(1);
                try {
                    await reporting.bind(endpoint, coordinatorEndpoint, ["genOnOff"]);
                    await reporting.onOff(endpoint);
                } catch {
                }
            },
        }];
        """
        device = parse_source(source, "configure-try.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual([item.operation for item in device.configure_actions], ["bind", "configure_reporting"])

    def test_static_endpoint_object_loops_are_expanded(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["ENDPOINT_OBJECTS"],
            model: "Endpoint objects",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                for (const endpoint of [device.getEndpoint(1), device.getEndpoint(2)]) {
                    await reporting.bind(endpoint, coordinatorEndpoint, ["genPowerCfg"]);
                }
            },
        }];
        """
        device = parse_source(source, "configure-endpoint-loop.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(
            [(item.endpoint, item.cluster) for item in device.configure_actions],
            [(1, "genPowerCfg"), (2, "genPowerCfg")],
        )

    def test_indexed_endpoint_and_static_configure_locals_are_recovered(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["INDEXED"],
            model: "Indexed",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                const endpoint = device.endpoints[0];
                const clusters = ["genOnOff", "genPowerCfg"];
                const reporting = [{attribute: "onOff", minimumReportInterval: 0, maximumReportInterval: 3600, reportableChange: 0}];
                await reporting.bind(endpoint, coordinatorEndpoint, clusters);
                await endpoint.configureReporting("genOnOff", reporting);
            },
        }];
        """
        device = parse_source(source, "configure-locals.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(
            [(item.operation, item.endpoint, item.cluster, item.attributes) for item in device.configure_actions],
            [
                ("bind", "__endpoint_index__:0", "genOnOff", ()),
                ("bind", "__endpoint_index__:0", "genPowerCfg", ()),
                ("configure_reporting", "__endpoint_index__:0", "genOnOff", ("onOff",)),
            ],
        )

    def test_configure_reads_and_reporting_helpers_are_extracted(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["METER"],
            model: "Meter",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                const endpoint = device.getEndpoint(1);
                await endpoint.read("haElectricalMeasurement", ["acPowerDivisor"]);
                await reporting.readMeteringMultiplierDivisor(endpoint);
                await reporting.readEletricalMeasurementMultiplierDivisors(endpoint, true);
            },
        }];
        """
        device = parse_source(source, "configure-read.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(
            [(item.operation, item.cluster, item.attributes) for item in device.configure_actions],
            [
                ("read", "haElectricalMeasurement", ("acPowerDivisor",)),
                ("read", "seMetering", ("multiplier", "divisor")),
                (
                    "read",
                    "haElectricalMeasurement",
                    (
                        "acVoltageMultiplier",
                        "acVoltageDivisor",
                        "acCurrentMultiplier",
                        "acCurrentDivisor",
                        "acPowerMultiplier",
                        "acPowerDivisor",
                        "acFrequencyDivisor",
                        "acFrequencyMultiplier",
                    ),
                ),
            ],
        )

    def test_static_configure_reporting_is_extracted(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["REPORTING"],
            model: "Reporting",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                const endpoint = device.getEndpoint(1);
                await endpoint.configureReporting("genOnOff", [
                    {attribute: "onOff", minimumReportInterval: 0, maximumReportInterval: 3600, reportableChange: 0},
                ]);
            },
        }];
        """
        device = parse_source(source, "configure-reporting-static.ts").devices[0]
        self.assertFalse(device.partial)
        action = device.configure_actions[0]
        self.assertEqual(action.operation, "configure_reporting")
        self.assertEqual(action.endpoint, 1)
        self.assertEqual(action.cluster, "genOnOff")
        self.assertEqual(action.attributes, ("onOff",))
        self.assertEqual(action.minimum_interval, 0)
        self.assertEqual(action.maximum_interval, 3600)
        self.assertEqual(action.reportable_change, 0)

    def test_configure_resolves_literal_rep_interval_constants(self) -> None:
        source = """
        import * as constants from "../lib/constants";
        export const definitions = [{
            zigbeeModel: ["CONSTANTS"],
            model: "Constants",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                const endpoint = device.getEndpoint(1);
                const payload = reporting.payload("onOff", 0, constants.repInterval.HOUR, 0);
                await endpoint.configureReporting("genOnOff", [
                    {attribute: "onOff", minimumReportInterval: 0, maximumReportInterval: constants.repInterval.HOUR, reportableChange: 0},
                ]);
                await endpoint.configureReporting("genOnOff", payload);
                await reporting.onOff(endpoint, {min: 1, max: 0xfffe});
                await reporting.temperature(endpoint, {
                    min: constants.repInterval.MINUTES_10,
                    max: constants.repInterval.MAX,
                    change: 100,
                });
            },
        }];
        """
        device = parse_source(source, "configure-constants.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(
            [
                (item.cluster, item.minimum_interval, item.maximum_interval, item.reportable_change)
                for item in device.configure_actions
            ],
            [
                ("genOnOff", 0, 3600, 0),
                ("genOnOff", 0, 3600, 0),
                ("genOnOff", 1, 65534, 0),
                ("msTemperatureMeasurement", 600, 65000, 100),
            ],
        )

    def test_configure_resolves_direct_rep_interval_import(self) -> None:
        source = """
        import {repInterval} from "../lib/constants";
        export const definitions = [{
            zigbeeModel: ["DIRECT_CONSTANTS"],
            model: "Direct constants",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                const endpoint = device.getEndpoint(1);
                await endpoint.configureReporting("genOnOff", [
                    {attribute: "onOff", minimumReportInterval: 0, maximumReportInterval: repInterval.HOUR, reportableChange: 0},
                ]);
            },
        }];
        """
        device = parse_source(source, "configure-direct-constants.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(device.configure_actions[0].maximum_interval, 3600)

    def test_configure_resolves_static_module_destructuring(self) -> None:
        source = """
        import * as lumi from "../lib/lumi";
        export const definitions = [{
            zigbeeModel: ["LUMI_CONSTANTS"],
            model: "Lumi constants",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                const {manufacturerCode} = lumi;
                const endpoint = device.getEndpoint(1);
                await endpoint.write("manuSpecificLumi", {mode: 1}, {manufacturerCode});
            },
        }];
        """
        device = parse_source(source, "configure-lumi-constants.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(device.configure_actions[0].operation, "write")
        self.assertEqual(device.configure_actions[0].manufacturer_code, 0x115F)

    def test_configure_expands_static_heiman_reporting_helper(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["HEIMAN_REPORTING"],
            model: "Heiman reporting",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                const endpoint = device.getEndpoint(1);
                await heiman.configureReporting.pm25MeasuredValue(endpoint);
            },
        }];
        """
        device = parse_source(source, "configure-heiman-reporting.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(
            device.configure_actions[0],
            ConfigureAction(
                "configure_reporting",
                1,
                "pm25Measurement",
                attributes=("measuredValue",),
                minimum_interval=0,
                maximum_interval=3600,
                reportable_change=1,
                target="device",
            ),
        )

    def test_configure_extracts_static_cluster_attribute_cache(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["CACHE_ATTRIBUTES"],
            model: "Cached attributes",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                const endpoint = device.getEndpoint(1);
                endpoint.saveClusterAttributeKeyValue("seMetering", {divisor: 100, multiplier: 1});
                device.save();
            },
        }];
        """
        device = parse_source(source, "configure-cache-attributes.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(
            device.configure_actions,
            [
                ConfigureAction(
                    "save_cluster_attributes",
                    1,
                    "seMetering",
                    payload={"divisor": 100, "multiplier": 1},
                    target="device",
                )
            ],
        )

    def test_configure_extracts_static_power_source_assignment(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["POWER_SOURCE"],
            model: "Power source",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                device.powerSource = "Mains (single phase)";
                device.save();
            },
        }];
        """
        device = parse_source(source, "configure-power-source.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(
            device.configure_actions,
            [
                ConfigureAction(
                    "set_device_property",
                    payload={"name": "powerSource", "value": "Mains (single phase)"},
                    target="device",
                )
            ],
        )

    def test_configure_extracts_static_device_type_assignment(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["DEVICE_TYPE"],
            model: "Device type",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                device.type = "EndDevice";
                device.save();
            },
        }];
        """
        device = parse_source(source, "configure-device-type.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(
            device.configure_actions,
            [
                ConfigureAction(
                    "set_device_property",
                    payload={"name": "type", "value": "EndDevice"},
                    target="device",
                )
            ],
        )

    def test_configure_extracts_static_software_build_id_templates(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["SOFTWARE_BUILD_ID"],
            model: "Software build id",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                device.softwareBuildID = `0.0.0_00${device.applicationVersion}`;
                device.save();
            },
        }];
        """
        device = parse_source(source, "configure-software-build-id.ts").devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(
            device.configure_actions,
            [
                ConfigureAction(
                    "set_device_property",
                    payload={
                        "name": "softwareBuildID",
                        "value": {
                            "__device_property_template__": {
                                "source": "applicationVersion",
                                "prefix": "0.0.0_00",
                            }
                        },
                    },
                    target="device",
                )
            ],
        )

    def test_custom_electricity_converter_keeps_device_partial(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["METER"],
            model: "Meter",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                const endpoint = device.getEndpoint(1);
                await reporting.readMeteringMultiplierDivisor(endpoint);
            },
            extend: [m.electricityMeter({fzElectricalMeasurement: local.electricalMeasurement})],
        }];
        """
        device = parse_source(source, "custom-meter.ts").devices[0]
        self.assertTrue(device.partial)
        self.assertIn("dynamic-expression", device.unsupported_macros)

    def test_object_and_array_spreads_do_not_reject_static_definition(self) -> None:
        source = """
        const template = {vendor: "Example", exposes: [e.battery()]};
        export const definitions = [{
            ...template,
            zigbeeModel: ["SPREAD"],
            model: "Spread",
            exposes: [...template.exposes, e.contact()],
        }];
        """
        result = parse_source(source, "spread.ts")
        self.assertEqual(result.rejected_definitions, 0)
        self.assertEqual(len(result.devices), 1)
        self.assertEqual(result.devices[0].model, "Spread")

    def test_runtime_temperature_report_is_scaled(self) -> None:
        source = (ROOT / "fixtures" / "simple_device.ts").read_text()
        device = parse_source(source).devices[0]
        plan = build_runtime_plan(device)
        state = apply_report(plan, RuntimeReport("temperature_measurement", "measured_value", 2150))
        self.assertEqual(state, {"temperature": 21.5})

    def test_runtime_on_off_write_uses_zcl_binding(self) -> None:
        source = (ROOT / "fixtures" / "simple_device.ts").read_text()
        device = parse_source(source).devices[0]
        plan = build_runtime_plan(device)
        write = make_write(plan, "state", True)
        self.assertEqual(write.cluster, "on_off")
        self.assertEqual(write.attribute, "on_off")
        self.assertTrue(write.value)

    def test_directory_parsing_reads_all_sources(self) -> None:
        result = parse_path(ROOT / "fixtures")
        self.assertEqual(len(result.devices), 3)
        self.assertEqual({device.model for device in result.devices}, {"Test Plug", "Unsafe", "Modern Light"})

    def test_multiple_source_paths_are_combined(self) -> None:
        definition = """
        export const definitions = [{
            model: "External device",
            vendor: "External",
            exposes: [e.battery()],
        }];
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "external.ts"
            path.write_text(definition, encoding="utf-8")
            result = parse_paths([ROOT / "fixtures" / "simple_device.ts", path])

        self.assertEqual(result.source_files, 2)
        self.assertEqual({device.model for device in result.devices}, {"Test Plug", "External device"})


if __name__ == "__main__":
    unittest.main()
