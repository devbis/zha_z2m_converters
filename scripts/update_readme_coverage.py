#!/usr/bin/env python3
"""Update the generated coverage block in README.md."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components"))

from zha_z2m_converters.coverage import DEFAULT_SOURCE, build_report  # noqa: E402
from zha_z2m_converters.parser import parse_path  # noqa: E402


START_MARKER = "<!-- coverage:start -->"
END_MARKER = "<!-- coverage:end -->"


def _percentage(value: int, total: int) -> str:
    return f"{value / total * 100:.1f}%" if total else "0.0%"


def render_coverage_block(report: object) -> str:
    """Render the README section managed by this script."""
    total = report.total_definitions
    usable = report.fully_supported + report.usable_partial
    needs_work = report.metadata_only + report.unusable_partial
    return "\n".join(
        [
            START_MARKER,
            "| Status | Definitions | Share | Meaning |",
            "|:---:|---:|---:|---|",
            f"| ✅ Fully supported | **{report.fully_supported:,}** | **{_percentage(report.fully_supported, total)}** | Static definition and all extracted features are supported. |",
            f"| 🟡 Usable partial | **{report.usable_partial:,}** | **{_percentage(report.usable_partial, total)}** | Some features are missing, but at least one reliable data path remains. |",
            f"| 🟠 Metadata only | **{report.metadata_only:,}** | **{_percentage(report.metadata_only, total)}** | Device metadata was recovered, but no usable entity or data binding exists. |",
            f"| 🔴 Unusable partial | **{report.unusable_partial:,}** | **{_percentage(report.unusable_partial, total)}** | The remaining functionality depends on unsupported converter logic or has no usable data path. |",
            f"| ⛔ Rejected | **{report.rejected:,}** | **{_percentage(report.rejected, total)}** | The definition could not be recovered by the static parser. |",
            f"| **Total** | **{total:,}** | **100%** | All definitions found in the snapshot, including rejected definitions. |",
            "",
            "### At a glance",
            "",
            f"- **{usable:,} definitions ({_percentage(usable, total)})** have either full support or at least one usable",
            "  supported data path.",
            f"- **{needs_work:,} definitions** still need additional implementation or have no usable",
            "  entity path (`metadata-only` + `unusable partial`).",
            f"- **{report.rejected:,} definitions** are currently rejected by the parser.",
            END_MARKER,
        ]
    )


def update_readme(readme: Path, check: bool = False) -> bool:
    """Update README and return whether its generated block is current."""
    content = readme.read_text()
    start = content.find(START_MARKER)
    end = content.find(END_MARKER)
    if start < 0 or end < 0 or end < start:
        raise ValueError(f"README must contain {START_MARKER} and {END_MARKER}")
    report = build_report(parse_path(DEFAULT_SOURCE), problem_limit=0)
    block = render_coverage_block(report)
    updated = content[:start] + block + content[end + len(END_MARKER) :]
    if updated == content:
        return True
    if check:
        return False
    readme.write_text(updated)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update the generated README coverage block")
    parser.add_argument("--check", action="store_true", help="Fail when README coverage is stale")
    args = parser.parse_args(argv)
    try:
        current = update_readme(ROOT / "README.md", check=args.check)
    except (OSError, ValueError) as exc:
        print(f"README coverage update failed: {exc}", file=sys.stderr)
        return 1
    if args.check and not current:
        print("README coverage is stale; run python3 scripts/update_readme_coverage.py")
        return 1
    if not args.check:
        print("README coverage updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
