"""Publish a run's output files as one set, with the manifest as the commit point.

Each writer used to replace its own file as soon as it finished: dataset and
quarantine first, then the wide table, the profile and finally the manifest. A kill
between two of those replaces left a new cases.csv beside an old wide table and an
old manifest whose hashes no longer matched anything.

Now every file of a run is written into a `.staging` folder inside its final folder
(same filesystem, so a rename is atomic), nothing is renamed until all of them are
complete and fsynced, the files are then renamed in a fixed order, and the manifest
is written last. A run killed before the renames leaves the previous set intact; a
run killed during them leaves a manifest that does not match the files, which
`verify_outputs` reports and a rerun repairs.
"""
from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path

from .errors import DataError
from .outputs import sha256_file

STAGING_DIR = ".staging"


class OutputStaging:
    """Staging folders for one run, one per distinct final folder."""

    def __init__(self, *final_dirs: Path) -> None:
        self._dirs = {Path(d): Path(d) / STAGING_DIR for d in dict.fromkeys(final_dirs)}
        for staging in self._dirs.values():
            # leftovers of a run that was killed before it could publish
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)

    def folder(self, final_dir: Path) -> Path:
        return self._dirs[Path(final_dir)]

    def path_for(self, final: Path) -> Path:
        final = Path(final)
        return self._dirs[final.parent] / final.name

    def commit(self, finals: Iterable[Path], *, remove: Iterable[Path] = ()) -> None:
        """Rename every staged file onto its final name, then drop the staging folders."""
        for final in finals:
            staged = self.path_for(final)
            if staged.exists():
                os.replace(staged, final)
        for path in remove:
            Path(path).unlink(missing_ok=True)
        self.discard()

    def move_out(self, staged: Path, final: Path) -> None:
        os.replace(staged, final)

    def discard(self) -> None:
        for staging in self._dirs.values():
            shutil.rmtree(staging, ignore_errors=True)


def verify_outputs(manifest_path: Path, root: Path | None, files: Mapping[str, Path]) -> None:
    """Refuse output files that are not the ones the manifest describes.

    `files` maps a manifest output name to the file a reader is about to use. A
    missing manifest is not an error here (a reader may be pointed at curated files
    produced elsewhere); a manifest that lists the same file with another hash is,
    because that is what an interrupted publish looks like.
    """
    manifest_path = Path(manifest_path)
    if not manifest_path.is_file():
        return
    try:
        outputs = json.loads(manifest_path.read_text(encoding="utf-8")).get("outputs", {})
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        raise DataError("Run manifest is unreadable", context={"path": str(manifest_path)}) from exc
    mismatched = []
    for name, path in sorted(files.items()):
        entry = outputs.get(name)
        if not isinstance(entry, dict) or "path" not in entry:
            continue
        listed = Path(entry["path"])
        if not listed.is_absolute() and root is not None:
            listed = root / listed
        if os.path.normpath(listed) != os.path.normpath(Path(path).absolute()):
            continue
        if sha256_file(path) != entry.get("sha256"):
            mismatched.append(str(path))
    if mismatched:
        raise DataError(
            "Output files do not match the run manifest; the last publish was "
            "interrupted. Rerun 'data-factory run' (or 'clean') to publish a complete set",
            context={"manifest": str(manifest_path), "files": mismatched},
        )
