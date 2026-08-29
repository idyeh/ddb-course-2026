"""Deterministic cluster simulator for the distributed database course.

The package keeps one rule above all others: a run is fully described by
(scenario, params, seed).  Nothing here reads the wall clock, and nothing here
uses module level randomness, so the same inputs always produce the same trace.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The lab reuses the modules that appear on the slides (examples/raft_rules.py and
# friends), so the repository root has to be importable no matter how the lab was
# started: `python3 lab/run.py`, `unittest discover -s lab -t lab`, or an editor.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

__all__ = ["__version__"]
__version__ = "0.1.0"
