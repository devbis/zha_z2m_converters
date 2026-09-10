from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from zha_z2m_converters.status import build_status, render_status


class StatusTests(unittest.TestCase):
    def test_status_splits_configure_problems_into_categories(self) -> None:
        source = """
        export const definitions = [{
            zigbeeModel: ["STATUS_DEVICE"],
            model: "Status device",
            vendor: "Example",
            configure: async (device, coordinatorEndpoint) => {
                const endpoint = device.getEndpoint(1);
                await endpoint.read("genOnOff", ["onOff"]);
                if (device.applicationVersion < 3) {
                    await endpoint.read("genPowerCfg", ["batteryVoltage"]);
                }
            },
            exposes: [e.binary("state", ea.STATE)],
        }];
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "status.ts"
            source_path.write_text(source, encoding="utf-8")
            report = build_status(source_path)

        self.assertEqual(report.configure.definitions, 1)
        self.assertEqual(report.configure.categories["dynamic syntax or control flow"], 1)
        rendered = render_status(report)
        self.assertIn("## Configure gaps", rendered)
        self.assertIn("dynamic syntax or control flow", rendered)


if __name__ == "__main__":
    unittest.main()
