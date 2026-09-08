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
        self.assertEqual(report.rejected, 0)
        self.assertIn("partial-definition", report.diagnostics)

    def test_report_has_text_and_json_forms(self) -> None:
        report = build_report(parse_path(Path(__file__).parent / "fixtures"))
        text = format_report(report)
        self.assertIn("Fully supported:", text)
        json.dumps(report.to_dict())

    def test_modern_extend_fixture_is_fully_supported(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "modern_extend.ts"
        report = build_report(parse_path(fixture))
        self.assertEqual(report.fully_supported, 1)
        self.assertEqual(report.partially_supported, 0)


if __name__ == "__main__":
    unittest.main()
