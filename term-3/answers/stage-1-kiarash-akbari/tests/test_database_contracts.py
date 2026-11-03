"""Offline regression tests; PostgreSQL integration is tested separately."""
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
from data_factory.database import COLUMN_ALIASES, _convert, deduplicate_geolocation, read_csv_batches
from data_factory.errors import DataError


class DatabaseContractTests(unittest.TestCase):
    def csv_path(self, content):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "input.csv"
        path.write_text(content, encoding="utf-8")
        return path

    def test_batches_and_nulls(self):
        path = self.csv_path("id,note\n001,\n002,hello\n003,last\n")
        batches = list(read_csv_batches(path, 2))
        self.assertEqual([len(b) for b in batches], [2, 1])
        self.assertEqual(batches[0][0], {"id": "001", "note": None})

    def test_invalid_batch_sizes(self):
        path = self.csv_path("id\n1\n")
        for size in (0, -1, True):
            with self.subTest(size=size), self.assertRaises(DataError):
                list(read_csv_batches(path, size))

    def test_rejects_duplicate_or_empty_header(self):
        for content in ("", "id,id\n1,2\n"):
            with self.subTest(content=content), self.assertRaises(DataError):
                list(read_csv_batches(self.csv_path(content), 2))

    def test_rejects_short_or_long_rows(self):
        for content in ("a,b\n1\n", "a,b\n1,2,3\n"):
            with self.subTest(content=content), self.assertRaises(DataError):
                list(read_csv_batches(self.csv_path(content), 2))

    def test_product_column_aliases(self):
        self.assertEqual(COLUMN_ALIASES["product_name_length"], "product_name_lenght")
        self.assertEqual(COLUMN_ALIASES["product_description_length"], "product_description_lenght")

    def test_decimal_and_integer_conversion(self):
        money = sa.Column("price", sa.Numeric(10, 2), nullable=False)
        self.assertEqual(_convert("0.10", money), Decimal("0.10"))
        self.assertIsInstance(_convert("0.10", money), Decimal)
        self.assertEqual(_convert("12", sa.Column("n", sa.Integer)), 12)
        with self.assertRaises(ValueError):
            _convert("1.5", sa.Column("n", sa.Integer))

    def test_datetime_null_and_identifier(self):
        self.assertEqual(_convert("2018-01-02 03:04:05", sa.Column("t", sa.DateTime)), datetime(2018, 1, 2, 3, 4, 5))
        self.assertIsNone(_convert(None, sa.Column("t", sa.DateTime, nullable=True)))
        with self.assertRaises(ValueError):
            _convert(None, sa.Column("n", sa.Integer, nullable=False))
        self.assertEqual(_convert("00123", sa.Column("zip", sa.String(8))), "00123")

    def test_nonfinite_numbers_rejected(self):
        for datatype in (sa.Float(), sa.Numeric(10, 2)):
            for value in ("NaN", "Infinity", "-Infinity"):
                with self.subTest(datatype=datatype, value=value), self.assertRaises(ValueError):
                    _convert(value, sa.Column("n", datatype))

    def test_geolocation_duplicates_across_batches(self):
        row = dict(geolocation_zip_code_prefix="00123", geolocation_lat="-23.0", geolocation_lng="-46.0", geolocation_city="city", geolocation_state="SP")
        other = {**row, "geolocation_city": "other"}
        batches = list(deduplicate_geolocation(iter([[row], [dict(row), other]])))
        self.assertEqual(batches, [[row], [other]])


if __name__ == "__main__":
    unittest.main()
