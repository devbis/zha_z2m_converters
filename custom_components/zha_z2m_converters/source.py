"""Source snapshot loading and manifest handling."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class SourceFile:
    filename: str
    text: str
    root: Path | None = None


SOURCE_MODE_ALL = "all"
SOURCE_MODE_SELECTED = "selected"


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


def source_paths(path: str | Path) -> list[Path]:
    """Return TypeScript source paths in a stable, relative-selection order."""
    source_path = Path(path)
    if source_path.is_file():
        return [source_path] if source_path.suffix == ".ts" else []
    if not source_path.is_dir():
        return []
    candidates = sorted((source_path / "src" / "devices").glob("*.ts"))
    if not candidates:
        candidates = sorted(source_path.rglob("*.ts"))
    return candidates


def source_file_key(path: str | Path, root: str | Path) -> str:
    """Return the portable UI key for a source file relative to its source root."""
    source_path = Path(path)
    root_path = Path(root)
    try:
        return source_path.resolve().relative_to(root_path.resolve()).as_posix()
    except ValueError:
        return source_path.name


def available_source_files(path: str | Path) -> list[str]:
    """List selectable TypeScript files below a source root."""
    return [source_file_key(candidate, path) for candidate in source_paths(path)]


def select_source_paths(
    path: str | Path,
    mode: str = SOURCE_MODE_ALL,
    files: Iterable[str] = (),
) -> list[Path]:
    """Select source files using an allow-list or an all-files exclusion list."""
    source_path = Path(path)
    candidates = source_paths(source_path)
    selected = {str(item) for item in files}
    if mode == SOURCE_MODE_SELECTED:
        return [
            candidate
            for candidate in candidates
            if source_file_key(candidate, source_path) in selected
        ]
    return [
        candidate
        for candidate in candidates
        if source_file_key(candidate, source_path) not in selected
    ]


def load_source(path: str | Path) -> SourceFile:
    """Load one TypeScript file, retaining the original convenience API."""
    return load_sources(path)[0]
