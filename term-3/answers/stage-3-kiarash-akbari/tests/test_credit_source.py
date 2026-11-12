"""The file source against stage 1's real machinery: same protocol, same reuse.

Nothing here reads the Kaggle file. The synthetic fixture has the contract's
header, so what these tests prove is the seam - that ``fetch_all`` and the raw
page store accept a file exactly as they accept the API.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
for candidate in (
    HERE,
    HERE.parent / "src",
    HERE.parents[1] / "stage-1" / "src",
):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from data_factory.errors import DataError, SchemaDriftError
from data_factory.fetch import fetch_all
from data_factory.store import RawPageStore
from fixtures import write_csv

from credit.columns import CSV_COLUMNS, db_for
from credit.source import (
    CsvFileSource,
    count_data_rows,
    file_fingerprint,
    file_sha256,
    read_header,
)

ROWS = 37


class HeaderContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.csv = write_csv(self.dir / "cs-training.csv", rows=ROWS)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_header_is_the_contract(self):
        self.assertEqual(read_header(self.csv), CSV_COLUMNS)

    def test_renamed_column_fails_loudly(self):
        broken = self.dir / "broken.csv"
        lines = self.csv.read_text(encoding="utf-8").splitlines()
        lines[0] = lines[0].replace("MonthlyIncome", "Income")
        broken.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with (
            self.assertRaises(SchemaDriftError) as caught,
            CsvFileSource(broken, page_size=10) as source,
        ):
            source.fetch_page(0)
        context = caught.exception.context
        self.assertIn("MonthlyIncome", context["missing"])
        self.assertIn("Income", context["unexpected"])

    def test_blank_field_reads_as_none_not_empty_string(self):
        with CsvFileSource(self.csv, page_size=5) as source:
            page = source.fetch_page(0)
        self.assertIn("MonthlyIncome", page[0])
        self.assertTrue(any(row["MonthlyIncome"] is None for row in page))

    def test_the_id_column_may_be_spelled_either_way(self):
        """The first header cell is the one name this contract does not demand.

        A draft of the contract took the id column's name from the competition's title
        instead of from the file, so a header spelled that way is a naming difference
        with no consequence: same positions, same values, same ``row_id``. Every other
        column stays exact, because a wrong name there changes what the model is fed.
        """
        text = self.csv.read_text(encoding="utf-8")
        legacy = text.replace("Id,", "Give.Me.Some.Credits,", 1)
        self.assertNotEqual(legacy, text, "the fixture header no longer starts with Id")
        other = self.csv.with_name("legacy.csv")
        other.write_text(legacy, encoding="utf-8")
        with CsvFileSource(other) as source:
            rows = list(source.iter_rows())
            self.assertEqual(source.header[0], "Give.Me.Some.Credits")
        self.assertEqual([row["Id"] for row in rows[:2]], ["1", "2"])
        self.assertEqual(db_for("Give.Me.Some.Credits"), "row_id")
        self.assertEqual(db_for("Id"), "row_id")

    def test_an_unnamed_id_column_is_the_file_people_actually_have(self):
        """The Kaggle export's first header cell is empty, and that must load.

        Both prior guesses at this name came from prose about the competition rather
        than from the file, so the file gets the benefit of the doubt here: the slot
        is present, the other eleven names are exact, and the row index is discarded.
        """
        lines = self.csv.read_text(encoding="utf-8").splitlines()
        names = lines[0].split(",")
        names[0] = ""
        other = self.csv.with_name("kaggle.csv")
        other.write_text(
            "\r\n".join([",".join(names), *lines[1:]]) + "\r\n", encoding="utf-8"
        )
        with CsvFileSource(other) as source:
            rows = list(source.iter_rows())
            self.assertEqual(source.header[0], "")
        with CsvFileSource(self.csv) as reference:
            expected = list(reference.iter_rows())
        self.assertEqual([row["Id"] for row in rows[:2]], ["1", "2"])
        self.assertEqual(rows, expected)
        self.assertEqual(db_for(""), "row_id")
        # provenance identity is the contract plus the name, so a differently spelled
        # header must not look like a different dataset to the resumability check
        twin = self.csv.parent / "sub" / self.csv.name
        twin.parent.mkdir()
        twin.write_text(
            "\r\n".join([",".join(names), *lines[1:]]) + "\r\n", encoding="utf-8"
        )
        self.assertEqual(file_fingerprint(self.csv), file_fingerprint(twin))

    def test_a_reordered_header_is_drift_even_though_the_id_name_is_free(self):
        """Leniency about one name must not turn into leniency about order."""
        lines = self.csv.read_text(encoding="utf-8").splitlines()
        names = lines[0].split(",")
        names[1], names[2] = names[2], names[1]
        other = self.csv.with_name("swapped.csv")
        other.write_text(
            "\r\n".join([",".join(names), *lines[1:]]) + "\r\n", encoding="utf-8"
        )
        # entering the source is the check: __enter__ validates the header, and the
        # contextlib one-liner keeps ruff's nested-with rule off the test
        with self.assertRaises(SchemaDriftError), CsvFileSource(other):
            pass

    def test_a_blank_name_somewhere_else_is_drift(self):
        """Accepting an empty id cell must not mean accepting empty cells generally."""
        lines = self.csv.read_text(encoding="utf-8").splitlines()
        names = lines[0].split(",")
        names[3] = ""
        other = self.csv.with_name("hollow.csv")
        other.write_text(
            "\r\n".join([",".join(names), *lines[1:]]) + "\r\n", encoding="utf-8"
        )
        with self.assertRaises(SchemaDriftError), CsvFileSource(other):
            pass


class PagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.csv = write_csv(Path(self._tmp.name) / "cs-training.csv", rows=ROWS)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_pages_respect_the_requested_size(self):
        with CsvFileSource(self.csv, page_size=10) as source:
            sizes = [len(source.fetch_page(offset)) for offset in (0, 10, 20, 30, 40)]
        self.assertEqual(sizes, [10, 10, 10, 7, 0])

    def test_every_row_appears_once_in_order(self):
        with CsvFileSource(self.csv, page_size=6) as source:
            rows = list(source.iter_rows())
        self.assertEqual(len(rows), ROWS)
        self.assertEqual(
            [row["Id"] for row in rows],
            [str(i + 1) for i in range(ROWS)],
        )

    def test_out_of_order_access_rewinds_and_still_answers(self):
        with CsvFileSource(self.csv, page_size=10) as source:
            first = source.fetch_page(0)
            source.fetch_page(20)
            again = source.fetch_page(0)
        self.assertEqual(first, again)

    def test_negative_offset_is_a_programming_error(self):
        with (
            CsvFileSource(self.csv, page_size=10) as source,
            self.assertRaises(ValueError),
        ):
            source.fetch_page(-1)

    def test_ragged_row_reports_its_line(self):
        broken = self.csv.parent / "ragged.csv"
        lines = self.csv.read_text(encoding="utf-8").splitlines()
        lines[3] = lines[3] + ",extra"
        broken.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with (
            self.assertRaises(DataError) as caught,
            CsvFileSource(broken, page_size=2) as source,
        ):
            source.fetch_page(2)
        self.assertEqual(caught.exception.context["line"], 4)

    def test_page_size_must_be_positive(self):
        with self.assertRaises(ValueError):
            CsvFileSource(self.csv, page_size=0)


class StageOneReuseTests(unittest.TestCase):
    """The point of the adapter: the factory pipeline is unchanged."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.csv = write_csv(self.dir / "cs-training.csv", rows=ROWS)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_fetch_all_pages_a_file_with_no_changes(self):
        store = RawPageStore(self.dir / "raw")
        with CsvFileSource(self.csv, page_size=10) as source:
            result = fetch_all(source, store)
        self.assertEqual(result.rows_fetched, ROWS)
        self.assertEqual(result.pages_total, 4)
        self.assertEqual(result.pages_fetched, 4)
        self.assertFalse(result.already_complete)

    def test_second_run_reports_completion_without_refetching(self):
        store = RawPageStore(self.dir / "raw")
        with CsvFileSource(self.csv, page_size=10) as source:
            fetch_all(source, store)
            again = fetch_all(source, store)
        self.assertTrue(again.already_complete)
        self.assertEqual(again.pages_fetched, 0)

    def test_stored_pages_match_the_source_row_for_row(self):
        store = RawPageStore(self.dir / "raw")
        with CsvFileSource(self.csv, page_size=10) as source:
            fetch_all(source, store)
            expected = list(source.iter_rows())
        self.assertEqual(list(store.iter_rows()), expected)

    def test_the_store_keeps_the_memory_bound(self):
        store = RawPageStore(self.dir / "raw")
        with CsvFileSource(self.csv, page_size=10) as source:
            fetch_all(source, store)
        sizes = sorted(
            len(p.read_text(encoding="utf-8").splitlines())
            for p in store.directory.glob("*.jsonl")
        )
        self.assertEqual(sizes, [7, 10, 10, 10])


class ProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.csv = write_csv(self.dir / "cs-training.csv", rows=ROWS)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_sha256_matches_a_direct_hash(self):
        expected = hashlib.sha256(self.csv.read_bytes()).hexdigest()
        self.assertEqual(file_sha256(self.csv), expected)

    def test_fingerprint_tracks_header_and_name_not_values(self):
        twin = write_csv(self.dir / "cs-training.csv", rows=ROWS + 5)
        renamed = write_csv(self.dir / "cs-test.csv", rows=ROWS)
        self.assertEqual(
            file_fingerprint(self.csv, CSV_COLUMNS), file_fingerprint(twin, CSV_COLUMNS)
        )
        self.assertNotEqual(
            file_fingerprint(self.csv, CSV_COLUMNS),
            file_fingerprint(renamed, CSV_COLUMNS),
        )

    def test_fingerprint_is_twelve_hex(self):
        token = file_fingerprint(self.csv, CSV_COLUMNS)
        self.assertEqual(len(token), 12)
        int(token, 16)  # raises if not hex

    def test_row_count_and_hash_agree_with_the_file_bytes(self):
        self.assertEqual(count_data_rows(self.csv), ROWS)
        self.assertTrue(file_sha256(self.csv))


if __name__ == "__main__":
    unittest.main()
