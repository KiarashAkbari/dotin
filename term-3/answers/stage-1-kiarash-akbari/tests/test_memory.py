import tempfile
import tracemalloc
import unittest
from pathlib import Path

from data_factory.fetch import fetch_all
from data_factory.store import RawPageStore

PAGE_SIZE = 1000
PAYLOAD = "x" * 200
GROWTH_TOLERANCE = 1.5


class _Source:
    page_size = PAGE_SIZE

    def __init__(self, total):
        self.total = total

    def fetch_page(self, offset):
        stop = min(offset + self.page_size, self.total)
        return [{"unique_key": str(i), "payload": PAYLOAD} for i in range(offset, stop)]


def _peak_bytes(total):
    with tempfile.TemporaryDirectory() as tmp:
        store = RawPageStore(Path(tmp) / "raw")
        tracemalloc.start()
        try:
            fetch_all(_Source(total), store)
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()


class MemoryTests(unittest.TestCase):
    def test_peak_memory_does_not_grow_with_total_rows(self):
        small = _peak_bytes(5_000)
        large = _peak_bytes(50_000)
        self.assertLess(large, small * GROWTH_TOLERANCE)


if __name__ == "__main__":
    unittest.main()