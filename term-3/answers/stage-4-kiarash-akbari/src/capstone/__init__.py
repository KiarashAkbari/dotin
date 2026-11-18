"""The stage-4 capstone: an early-warning system with a time audit in front of it.

This package is deliberately small at first. Every module here is either a check that
can fail loudly or a placeholder that says which phase owns it, so the skeleton can be
run and tested before a single model exists. The obligation each piece serves lives in
``docs/REQUIREMENTS_CHECKLIST.md``, and error messages cite that file by item id.
"""

from __future__ import annotations

__all__ = ["audit", "capacity", "chain", "cli", "config", "doctor", "model", "predict", "timing"]
