import unittest

from data_factory.errors import DataError, SchemaDriftError
from data_factory.schema import check_page_schema

COLUMNS = (
    "unique_key", "created_date", "closed_date", "agency", "complaint_type",
    "descriptor", "status", "borough", "incident_zip", "community_board",
)


def page_row(**overrides):
    row = {
        "unique_key": "1", "created_date": "2025-01-03T00:57:07.000",
        "agency": "NYPD", "complaint_type": "Noise - Residential",
        "descriptor": "Loud Music/Party", "status": "Closed", "borough": "BRONX",
        "community_board": "12 BRONX",
    }
    row.update(overrides)
    return {k: v for k, v in row.items() if v is not None}


class CheckPageSchemaTests(unittest.TestCase):
    def test_page_without_null_fields_passes(self):
        check_page_schema([page_row(), page_row(unique_key="2")], COLUMNS)

    def test_empty_page_passes(self):
        check_page_schema([], COLUMNS)

    def test_required_column_absent_from_every_row_is_drift(self):
        rows = [page_row(agency=None), page_row(agency=None, unique_key="2")]
        with self.assertRaises(SchemaDriftError):
            check_page_schema(rows, COLUMNS)

    def test_column_missing_from_some_rows_only_is_not_drift(self):
        check_page_schema([page_row(), page_row(agency=None, unique_key="2")], COLUMNS)

    def test_non_text_value_is_drift(self):
        with self.assertRaises(SchemaDriftError):
            check_page_schema([page_row(unique_key=1)], COLUMNS)

    def test_unexpected_column_is_drift(self):
        with self.assertRaises(SchemaDriftError):
            check_page_schema([page_row(extra="x")], COLUMNS)

    def test_drift_is_a_data_error(self):
        self.assertTrue(issubclass(SchemaDriftError, DataError))