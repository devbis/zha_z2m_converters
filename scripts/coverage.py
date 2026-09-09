#!/usr/bin/env python3
"""Run the converter coverage report directly from a source checkout."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zha_z2m_converters.coverage import main


if __name__ == "__main__":
    raise SystemExit(main())
