#!/usr/bin/env python3
"""Generate STATUS.md for the bundled converter snapshot."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components"))

from zha_z2m_converters.status import DEFAULT_SOURCE, update_status  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate converter STATUS.md")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=ROOT / "STATUS.md")
    parser.add_argument("--top", type=int, default=20, help="Number of top items per problem table")
    args = parser.parse_args(argv)
    update_status(args.output, args.source, top=max(1, args.top))
    print(f"Status report written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
