import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

from data_factory.clean import CLEAN_COLUMNS
from data_factory.errors import DataError
from data_factory.outputs import (
    AreaMonthAggregator,
    DatasetWriter,
    months_between,
    sha256_file,
    write_wide,
)

MONTHS = ("2025-01", "2025-02")


def cleaned(**overrides):
    row = {column: None for column in CLEAN_COLUMNS}
    row.update(
        unique_key="1",
        created_at="2025-01-10T10:00:00",
        complaint_type="Noise - Residential",
        modzcta="10466",
        population=10000,
        is_ambiguous=False,
    )
    row.update(overrides)
    return row


def read_csv(path):
    with Path(path).open(encoding="utf-8", newline="") as fh:
        return list(csv.reader(fh))


class MonthsTests(unittest.TestCase):
    def test_eighteen_month_window(self):
        months = months_between(date(2025, 1, 1), date(2026, 6, 30))
        self.assertEqual((len(months), months[0], months[-1]), (18, "2025-01", "2026-06"))


class DatasetWriterTests(unittest.TestCase):
    def test_writes_header_and_formats_empty_and_boolean_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out" / "cases.csv"
            with DatasetWriter(path) as writer:
                writer.write(cleaned(is_ambiguous=True))
            rows = read_csv(path)
            self.assertEqual(rows[0], list(CLEAN_COLUMNS))
            record = dict(zip(rows[0], rows[1]))
            self.assertEqual((record["is_ambiguous"], record["closed_at"]), ("true", ""))
            self.assertFalse(Path(str(path) + ".tmp").exists())

    def test_failed_block_leaves_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.csv"
            with self.assertRaises(RuntimeError):
                with DatasetWriter(path) as writer:
                    writer.write(cleaned())
                    raise RuntimeError("boom")
            self.assertFalse(path.exists())
            self.assertFalse(Path(str(path) + ".tmp").exists())


class AggregatorTests(unittest.TestCase):
    def test_counts_per_area_type_and_month_in_sorted_order(self):
        agg = AreaMonthAggregator(MONTHS)
        agg.add(cleaned(unique_key="1"))
        agg.add(cleaned(unique_key="2"))
        agg.add(cleaned(unique_key="3", created_at="2025-02-01T00:00:00"))
        agg.add(cleaned(unique_key="4", modzcta="10001", population=5000))
        rows = list(agg.rows())
        self.assertEqual([r["modzcta"] for r in rows], ["10001", "10466"])
        self.assertEqual((rows[1]["count_2025-01"], rows[1]["count_2025-02"]), (2, 1))
        self.assertEqual(rows[0]["count_2025-02"], 0)

    def test_rate_per_ten_thousand_and_blank_without_population(self):
        agg = AreaMonthAggregator(MONTHS)
        agg.add(cleaned(unique_key="1"))
        agg.add(cleaned(unique_key="2", modzcta=None, population=None))
        rows = {r["modzcta"]: r for r in agg.rows()}
        self.assertEqual(rows["10466"]["rate_per_10k_2025-01"], "1.0000")
        self.assertEqual(rows["UNKNOWN"]["rate_per_10k_2025-01"], "")
        self.assertEqual(rows["UNKNOWN"]["count_2025-01"], 1)

    def test_record_outside_window_is_a_data_error(self):
        agg = AreaMonthAggregator(MONTHS)
        with self.assertRaises(DataError):
            agg.add(cleaned(created_at="2025-05-01T00:00:00"))

    def test_same_input_gives_identical_file_hash(self):
        hashes = []
        for _ in range(2):
            agg = AreaMonthAggregator(MONTHS)
            for key in ("1", "2", "3"):
                agg.add(cleaned(unique_key=key))
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "wide.csv"
                write_wide(path, agg)
                hashes.append(sha256_file(path))
        self.assertEqual(hashes[0], hashes[1])