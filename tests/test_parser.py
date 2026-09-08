from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from zha_zhc.exporter import export_python
from zha_zhc.mapping import normalize_device
from zha_zhc.parser import parse_path, parse_source
from zha_zhc.model import ConfigureAction
from zha_zhc.runtime import _apply_configure_actions
from zha_zhc.runtime import apply_report, build_runtime_plan, make_write, register_result
from zha_zhc.runtime import register_with_zha, RuntimeReport


ROOT = Path(__file__).parent


class ParserTests(unittest.TestCase):
    def test_static_definition_becomes_ir(self) -> None:
        source = (ROOT / "fixtures" / "simple_device.ts").read_text()
        result = parse_source(source, "simple_device.ts")
        self.assertEqual(len(result.devices), 1)
        device = result.devices[0]
        self.assertEqual(device.manufacturer, "Example")
        self.assertEqual(device.model, "Test Plug")
        self.assertEqual([item.name for item in device.exposes], ["state", "temperature"])
        self.assertEqual([item.converter for item in device.from_zigbee], ["fz.on_off", "fz.temperature"])

    def test_normalization_adds_standard_clusters(self) -> None:
        source = (ROOT / "fixtures" / "simple_device.ts").read_text()
        device = parse_source(source).devices[0]
        normalized = normalize_device(device)
        self.assertIn("on_off", {item.cluster for item in normalized.from_zigbee})
        self.assertIn("temperature_measurement", {item.cluster for item in normalized.from_zigbee})

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
        prevented_clusters = []

        class Builder:
            def __init__(self, vendor, model):
                pass

            def sensor(self, **kwargs):
                self.sensor_kwargs = kwargs

            def prevent_default_entity_creation(self, **kwargs):
                prevented_clusters.append(kwargs["cluster_id"])

            def add_to_registry(self):
                pass

        register_with_zha(register_result(parse_source(source)), Builder)
        self.assertEqual(prevented_clusters, [0x0702])

    def test_modern_extend_macros_are_expanded_without_execution(self) -> None:
        source = (ROOT / "fixtures" / "modern_extend.ts").read_text()
        result = parse_source(source, "modern_extend.ts")
        self.assertEqual(len(result.devices), 1)
        device = result.devices[0]
        self.assertFalse(device.partial)
        self.assertEqual(device.unsupported_macros, [])
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

        with patch("zha_zhc.runtime._tuya_datapoint_cluster", return_value=object()):
            register_with_zha(register_result(parse_source(source)), Builder)
        self.assertEqual(len(replacements), 1)
        self.assertEqual([item[1]["attribute_name"] for item in entities], ["dp_1", "dp_2"])

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

        with patch("zha_zhc.runtime._tuya_on_off_cluster", return_value=object()), patch(
            "zha_zhc.runtime._tuya3_cluster", return_value=object()
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
        self.assertGreaterEqual(len(device.exposes), 3)

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
        )
        asyncio.run(_apply_configure_actions(Device(), actions))
        self.assertEqual(
            calls,
            [
                ("bind",),
                ("read", ["onOff"]),
                ("reporting", "onOff", 0, 3600, 0),
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


if __name__ == "__main__":
    unittest.main()
