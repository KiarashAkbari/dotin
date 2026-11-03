import tempfile
import unittest
from pathlib import Path

from data_factory.store import TMP_SUFFIX, RawPageStore


class StoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = RawPageStore(Path(tmp.name) / "raw")

    def test_completed_pages_counts_contiguous_from_zero(self):
        for i in (0, 1, 3):
            self.store.write_page(i, [{"k": i}])
        self.assertEqual(self.store.completed_pages(), 2)

    def test_tmp_file_is_not_a_complete_page(self):
        self.store.write_page(0, [{"k": 0}])
        partial = self.store.page_path(1)
        partial = partial.with_name(partial.name + TMP_SUFFIX)
        partial.write_text("partial", encoding="utf-8")
        self.assertEqual(self.store.completed_pages(), 1)

    def test_write_leaves_no_tmp_file(self):
        self.store.write_page(0, [{"k": 1}])
        leftovers = [p for p in self.store.directory.iterdir()
                     if p.name.endswith(TMP_SUFFIX)]
        self.assertEqual(leftovers, [])

    def test_rows_are_serialised_deterministically(self):
        self.store.write_page(0, [{"b": 1, "a": "é"}])
        text = self.store.page_path(0).read_text(encoding="utf-8")
        self.assertEqual(text, '{"a":"é","b":1}\n')

    def test_iter_rows_streams_pages_in_order(self):
        self.store.write_page(0, [{"k": 0}, {"k": 1}])
        self.store.write_page(1, [{"k": 2}])
        self.assertEqual([r["k"] for r in self.store.iter_rows()], [0, 1, 2])

    def test_empty_page_is_complete_with_zero_rows(self):
        self.store.write_page(0, [])
        self.assertEqual(self.store.completed_pages(), 1)
        self.assertEqual(self.store.row_count(0), 0)


if __name__ == "__main__":
    unittest.main()