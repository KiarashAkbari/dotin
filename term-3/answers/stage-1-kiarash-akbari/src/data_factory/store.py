"""Raw page storage: one JSON Lines file per page, written atomically."""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

PAGE_PREFIX = "page-"
PAGE_SUFFIX = ".jsonl"
TMP_SUFFIX = ".tmp"
PAGE_INDEX_WIDTH = 6


class RawPageStore:
    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)

    @property
    def directory(self) -> Path:
        return self._dir

    def page_path(self, index: int) -> Path:
        return self._dir / f"{PAGE_PREFIX}{index:0{PAGE_INDEX_WIDTH}d}{PAGE_SUFFIX}"

    def completed_pages(self) -> int:
        """Number of contiguous complete pages starting at index 0."""
        index = 0
        while self.page_path(index).is_file():
            index += 1
        return index

    def row_count(self, index: int) -> int:
        with self.page_path(index).open("r", encoding="utf-8") as fh:
            return sum(1 for _ in fh)

    def write_page(self, index: int, rows: list[dict]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        final = self.page_path(index)
        tmp = final.with_name(final.name + TMP_SUFFIX)
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")))
                fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)

    def iter_rows(self) -> Iterator[dict]:
        for index in range(self.completed_pages()):
            with self.page_path(index).open("r", encoding="utf-8") as fh:
                for line in fh:
                    yield json.loads(line)