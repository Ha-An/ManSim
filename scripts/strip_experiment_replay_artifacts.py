from __future__ import annotations

import argparse
from pathlib import Path


REPLAY_ARTIFACT_NAMES = {
    "operations_replay.html",
    "operations_replay.json",
    "replay_dashboard.html",
    "replay_studio_log.json",
}


def strip_replay_artifacts(result_dir: Path, *, results_root: Path) -> tuple[int, int]:
    root = results_root.resolve()
    target = result_dir.resolve()
    target.relative_to(root)
    removed_count = 0
    removed_bytes = 0
    for path in target.rglob("*"):
        if not path.is_file() or path.name not in REPLAY_ARTIFACT_NAMES:
            continue
        path.resolve().relative_to(target)
        removed_bytes += path.stat().st_size
        path.unlink()
        removed_count += 1
    return removed_count, removed_bytes


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete heavy replay artifacts from one policy experiment result.")
    parser.add_argument("result_dir", type=Path)
    parser.add_argument(
        "--results-root",
        type=Path,
        default=Path("experiments/factory_policy_comparison/results"),
    )
    args = parser.parse_args()
    count, size = strip_replay_artifacts(args.result_dir, results_root=args.results_root)
    print(f"removed_files={count}")
    print(f"reclaimed_bytes={size}")
    print(f"reclaimed_gib={size / (1024 ** 3):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
