#!/usr/bin/env python
"""Run the v12 autonomy scorecard and write output/scorecard/scorecard.{json,md}.

Thin CLI over ``skep.supervisor.scorecard_runner`` (the logic lives in src so it
is type-checked and unit-tested). Exits non-zero if any required metric fails.
"""

from __future__ import annotations

from skep.supervisor.scorecard_runner import main

if __name__ == "__main__":
    raise SystemExit(main())
