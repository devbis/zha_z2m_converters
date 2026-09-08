"""Command-line interface."""

from __future__ import annotations

import argparse
import json

from .exporter import export_python
from .parser import parse_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse zigbee-herdsman-converters without executing JavaScript")
    parser.add_argument("source", help="TypeScript file or converter snapshot directory")
    parser.add_argument("--json", action="store_true", help="Print the IR and diagnostics as JSON")
    parser.add_argument("--export", help="Write generated Python metadata to this path")
    args = parser.parse_args(argv)
    result = parse_path(args.source)
    if args.export:
        export_python(result.devices, args.export)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(f"devices={len(result.devices)} diagnostics={len(result.diagnostics)}")
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
