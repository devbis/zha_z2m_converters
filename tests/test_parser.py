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

    def test_static_configure_actions_execute_only_whitelisted_operations(self) -> None:
        calls = []

        class Cluster:
            cluster_id = 0x0006

            async def bind(self):
                calls.append(("bind",))

            async def read_attributes(self, attributes):
                calls.append(("read", attributes))

            async def configure_reporting(self, attribute, minimum, maximum, change):
                calls.append(("reporting", attribute, minimum, maximum, change))

            async def command(self, command, **payload):
                calls.append(("command", command, payload))

        class Endpoint:
            in_clusters = {0x0006: Cluster()}
            out_clusters = {}

        class ZigpyDevice:
            endpoints = {1: Endpoint()}

        class Device:
            _zigpy_device = ZigpyDevice()

        actions = (
            ConfigureAction("bind", 1, "genOnOff"),
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
            ConfigureAction("command", 1, "genOnOff", command="on", payload={"payloadSize": 1}),
        )
        asyncio.run(_apply_configure_actions(Device(), actions))
        self.assertEqual(
            calls,
            [
                ("bind",),
                ("read", ["onOff"]),
                ("reporting", "onOff", 0, 3600, 0),
                ("command", "on", {"payloadSize": 1}),
            ],
        )

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
