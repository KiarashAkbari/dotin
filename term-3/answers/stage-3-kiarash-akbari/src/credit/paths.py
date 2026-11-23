"""Where the stage-3 folder and its stage-1 sibling are, resolved from this file.

The delivery layout is ``term-3/answers/stage-<n>-<name>/``, so the stage-1 package
lives next to this folder as ``stage-1-kiarash-akbari``. The paths that used to point
at a sibling called plain ``stage-1`` depended on two things that are not true on a
grader's machine: a folder with that short name, and a shell whose current directory is
the stage-3 folder. Both are removed here: the paths are computed from this file's own
location, so they hold whatever the current directory is.

``CREDIT_STAGE1_CONFIG`` is the one override. Stage 1's ``config/default.toml`` is a
tracked file that names the database; a machine whose database differs points this
variable at an untracked copy instead of editing the tracked one.
"""

from __future__ import annotations

import os
from pathlib import Path

#: the stage-3 delivery folder (``src/credit/paths.py`` -> two levels up from ``src``)
STAGE3_DIR: Path = Path(__file__).resolve().parents[2]
#: the sibling folder the guide's ``stage-<n>-<name>`` layout gives stage 1
STAGE1_DIRNAME = "stage-1-kiarash-akbari"
STAGE1_DIR: Path = STAGE3_DIR.parent / STAGE1_DIRNAME
STAGE1_CONFIG_ENV = "CREDIT_STAGE1_CONFIG"


def stage1_config() -> Path:
    """Stage 1's config file: the override when it is set, else the sibling's default."""
    override = os.environ.get(STAGE1_CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    return STAGE1_DIR / "config" / "default.toml"


def credit_config() -> Path:
    """The untracked stage-3 source config, next to the tracked example."""
    return STAGE3_DIR / "config" / "credit.toml"
