from __future__ import annotations

import json
import unittest
from pathlib import Path

from zha_zhc.coverage import build_report, format_report
from zha_zhc.parser import parse_path


class CoverageTests(unittest.TestCase):
    def test_report_counts_full_partial_and_rejected(self) -> None:
        result = parse_path(Path(__file__).parent / "fixtures")
        report = build_report(result)
        self.assertEqual(report.source_files, 3)
        self.assertEqual(report.total_definitions, 3)
        self.assertEqual(report.definitions, 3)
        self.assertEqual(report.partially_supported, 1)
        self.assertEqual(report.usable_partial, 0)
        self.assertEqual(report.unusable_partial, 1)
        self.assertEqual(report.rejected, 0)
        self.assertIn("partial-definition", report.diagnostics)

    def test_report_has_text_and_json_forms(self) -> None:
        report = build_report(parse_path(Path(__file__).parent / "fixtures"))
        text = format_report(report)
        self.assertIn("Fully supported:", text)
        self.assertIn("Unusable partial:", text)
        json.dumps(report.to_dict())

    def test_definition_without_supported_data_path_is_unusable_partial(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: [\"TS0601\"],
            model: \"PJ-1203A\",
            vendor: \"Tuya\",
            extend: [tuya.modernExtend.tuyaBase(), m.deviceAddCustomCluster(\"manuSpecificPJ1203A\", {})],
            fromZigbee: [fzLocal.PJ1203A_strict_fz_datapoints],
            toZigbee: [tuya.tz.datapoints],
            exposes: [e.voltage()],
        }];
        """
        from zha_zhc.parser import parse_source

        report = build_report(parse_source(source, "pj1203a.ts"))
        self.assertEqual(report.usable_partial, 0)
        self.assertEqual(report.unusable_partial, 1)
        self.assertIn("no usable data path", report.partial_reasons)
        self.assertIn("no usable data path", format_report(report))

    def test_fingerprint_device_is_fully_supported_with_static_tuya_on_off(self) -> None:
        source = """
        export const definitions = [{
            fingerprint: [...tuya.fingerprint("TS0001", ["_TZ3000_46t1rvdu"])],
            model: "WHD02",
            vendor: "Tuya",
            extend: [tuya.modernExtend.tuyaBase(), tuya.modernExtend.tuyaOnOff({onOffCountdown: true})],
        }];
        """
        from zha_zhc.parser import parse_source

        report = build_report(parse_source(source, "whd02.ts"))
        self.assertEqual(report.fully_supported, 1)
        self.assertEqual(report.usable_partial, 0)
        self.assertEqual(report.unusable_partial, 0)

    def test_unsupported_macros_are_comma_separated(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "modern_extend.ts"
        report = build_report(parse_path(fixture))
        report.unsupported_macros = {"firstMacro": 2, "secondMacro": 1}
        text = format_report(report)
        self.assertIn("Unsupported extend macros:", text)
        self.assertIn(", ", text)

    def test_device_problem_includes_macro_name(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: [\"MACRO-DEVICE\"],
            model: \"Macro Device\",
            vendor: \"Example\",
            extend: [m.unknownMacro()],
        }];
        """
        from zha_zhc.parser import parse_source

        text = format_report(build_report(parse_source(source, "macro_device.ts")))
        self.assertIn("unsupported extend macro: unknownMacro", text)

    def test_modern_extend_fixture_is_fully_supported(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "modern_extend.ts"
        report = build_report(parse_path(fixture))
        self.assertEqual(report.fully_supported, 1)
        self.assertEqual(report.partially_supported, 0)


if __name__ == "__main__":
    unittest.main()
