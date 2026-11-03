import unittest

from data_factory.contract import CONTRACT
from data_factory.errors import SchemaDriftError
from data_factory.schema import NULLABLE_CHECK_MIN_ROWS, check_page_schema

COLUMNS = [spec.name for spec in CONTRACT]


def page(size, drop=()):
    row = {name: "x" for name in COLUMNS if name not in drop}
    return [dict(row) for _ in range(size)]


class NullableColumnDriftTests(unittest.TestCase):
    def test_large_page_without_a_nullable_column_is_drift(self):
        for column in ("descriptor", "community_board", "incident_zip"):
            with self.subTest(column=column):
                with self.assertRaises(SchemaDriftError):
                    check_page_schema(page(NULLABLE_CHECK_MIN_ROWS, drop=(column,)), COLUMNS)

    def test_small_page_may_lack_a_nullable_column(self):
        check_page_schema(page(10, drop=("descriptor",)), COLUMNS)

    def test_all_open_page_may_lack_closed_date(self):
        check_page_schema(page(NULLABLE_CHECK_MIN_ROWS, drop=("closed_date",)), COLUMNS)

    def test_complete_large_page_passes(self):
        check_page_schema(page(NULLABLE_CHECK_MIN_ROWS), COLUMNS)
