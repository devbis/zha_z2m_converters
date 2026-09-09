"""Source snapshot loading and manifest handling."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SourceFile:
    filename: str
    text: str
    root: Path | None = None


def load_sources(path: str | Path) -> list[SourceFile]:
    """Load one TypeScript file or every device source in a snapshot."""
    source_path = Path(path)
    if source_path.is_file():
        return [SourceFile(str(source_path), source_path.read_text(encoding="utf-8"), source_path.parent)]
    if not source_path.is_dir():
        raise FileNotFoundError(source_path)
    candidates = sorted((source_path / "src" / "devices").glob("*.ts"))
    if not candidates:
        candidates = sorted(source_path.rglob("*.ts"))
    if not candidates:
        raise FileNotFoundError(f"no TypeScript source found under {source_path}")
    return [SourceFile(str(candidate), candidate.read_text(encoding="utf-8"), source_path) for candidate in candidates]


def load_source(path: str | Path) -> SourceFile:
    """Load one TypeScript file, retaining the original convenience API."""
    return load_sources(path)[0]
