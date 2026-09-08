from __future__ import annotations

import json
import unittest
from pathlib import Path

from zha_zhc.exporter import export_python
from zha_zhc.mapping import normalize_device
from zha_zhc.parser import parse_path, parse_source
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

        class Builder:
            def __init__(self, vendor, model):
                calls.append(("init", vendor, model))

            def switch(self, **kwargs):
                calls.append(("switch", kwargs))

            def sensor(self, **kwargs):
                calls.append(("sensor", kwargs))

            def add_to_registry(self):
                calls.append(("register",))

        register_with_zha(registry, Builder)
        self.assertEqual(calls[0], ("init", "Example", "Test Plug"))
        self.assertEqual([item[0] for item in calls], ["init", "switch", "sensor", "register"])

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
