#!/usr/bin/env python
"""Snapshot the operator's policy state into a seed, or freeze golden files from one.

    uv run python scripts/policy_golden.py snapshot [--home ~/.skep] [--out <dir>/seed.json]
    uv run python scripts/policy_golden.py freeze   [--seed <dir>/seed.json] [--out-dir <dir>]

(<dir> defaults to tests/fixtures/policy_golden.)

Thin CLI over ``skep.supervisor.policy_golden`` (the logic lives in src so it
is type-checked and unit-tested). The operator runs ``snapshot`` then
``freeze`` when they change policy on purpose; the PR diff is the review.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from skep.supervisor.policy_golden import freeze, snapshot, write_golden

DEFAULT_DIR = Path("tests/fixtures/policy_golden")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="verb", required=True)
    snap = sub.add_parser("snapshot", help="export a machine-free seed from a skep home")
    snap.add_argument("--home", type=Path, default=Path("~/.skep"))
    snap.add_argument("--out", type=Path, default=DEFAULT_DIR / "seed.json")
    frz = sub.add_parser("freeze", help="write golden files from a seed")
    frz.add_argument("--seed", type=Path, default=DEFAULT_DIR / "seed.json")
    frz.add_argument("--out-dir", type=Path, default=DEFAULT_DIR)
    args = parser.parse_args(argv)
    if args.verb == "snapshot":
        seed = snapshot(args.home.expanduser().resolve())
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(seed, indent=2, sort_keys=True) + "\n")
        print(
            f"seed: {args.out} ({len(seed['projects'])} projects, "
            f"{len(seed['templates'])} templates, {len(seed['policy_groups'])} stored groups)"
        )
        return 0
    seed = json.loads(args.seed.read_text())
    with tempfile.TemporaryDirectory(prefix="skep-golden-") as tmp:
        golden = freeze(seed, Path(tmp) / "home")
    for path in write_golden(golden, args.out_dir):
        print(f"golden: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
