from __future__ import annotations

from pathlib import Path
from typing import Any

from .series_dashboard import (
    build_series_analysis as _build_series_analysis,
    export_series_dashboard as _export_series_dashboard,
)


def build_series_analysis(*, parent_output_dir: Path, summary_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return _build_series_analysis(parent_output_dir=parent_output_dir, summary_payload=summary_payload)


def export_series_dashboard(
    *,
    parent_output_dir: Path,
    analysis: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
    manifest_path: Path | None = None,
) -> Path | None:
    return _export_series_dashboard(
        parent_output_dir=parent_output_dir,
        analysis=analysis,
        manifest=manifest,
        manifest_path=manifest_path,
    )
