import tempfile
import unittest
from pathlib import Path

from data_factory.config import ReferenceConfig
from data_factory.errors import DataError, FetchError
from data_factory.population import (
    ZipArea,
    build_zip_index,
    fetch_reference,
    read_reference,
    write_reference,
)


def make_reference_config(limit=500):
    return ReferenceConfig(
        url="https://example.test/ref.json",
        columns=("modzcta", "label", "pop_est"),
        limit=limit,
        path=Path("reference.json"),
    )


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get_json(self, url, params=None, headers=None):
        self.calls.append((url, params))
        return self.payload


class BuildZipIndexTests(unittest.TestCase):
    def test_label_with_several_zips_maps_each_to_the_same_area(self):
        index = build_zip_index(
            [{"modzcta": "10001", "label": "10001, 10118", "pop_est": "23072"}]
        )
        self.assertEqual(index["10001"], ZipArea("10001", 23072))
        self.assertEqual(index["10118"], ZipArea("10001", 23072))

    def test_blank_or_zero_population_becomes_none(self):
        index = build_zip_index(
            [
                {"modzcta": "1", "label": "11111", "pop_est": ""},
                {"modzcta": "2", "label": "22222", "pop_est": "0"},
                {"modzcta": "3", "label": "33333"},
            ]
        )
        self.assertTrue(all(index[z].population is None for z in index))

    def test_zip_in_two_areas_is_a_data_error(self):
        rows = [
            {"modzcta": "1", "label": "10001", "pop_est": "5"},
            {"modzcta": "2", "label": "10001", "pop_est": "6"},
        ]
        with self.assertRaises(DataError):
            build_zip_index(rows)

    def test_missing_area_code_is_a_data_error(self):
        with self.assertRaises(DataError):
            build_zip_index([{"label": "10001", "pop_est": "5"}])

    def test_unparseable_population_is_a_data_error(self):
        with self.assertRaises(DataError):
            build_zip_index([{"modzcta": "1", "label": "10001", "pop_est": "many"}])


class ReferenceIoTests(unittest.TestCase):
    def test_fetch_uses_configured_columns_and_limit(self):
        client = FakeClient([{"modzcta": "1", "label": "10001", "pop_est": "5"}])
        rows = fetch_reference(client, make_reference_config())
        self.assertEqual(len(rows), 1)
        url, params = client.calls[0]
        self.assertEqual(url, "https://example.test/ref.json")
        self.assertEqual(params["$select"], "modzcta,label,pop_est")
        self.assertEqual(params["$limit"], 500)

    def test_fetch_rejects_a_full_page_as_possibly_truncated(self):
        client = FakeClient([{"modzcta": str(i)} for i in range(3)])
        with self.assertRaises(FetchError):
            fetch_reference(client, make_reference_config(limit=3))

    def test_write_then_read_round_trips(self):
        rows = [{"modzcta": "1", "label": "10001", "pop_est": "5"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "ref.json"
            write_reference(rows, path)
            self.assertEqual(read_reference(path), rows)

    def test_reading_a_missing_file_is_a_data_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(DataError):
                read_reference(Path(tmp) / "absent.json")