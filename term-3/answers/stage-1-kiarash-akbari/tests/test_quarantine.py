import json
import tempfile
import unittest
from pathlib import Path

from data_factory.contract import ReasonCode
from data_factory.quality import ClassifiedRecord
from data_factory.quarantine import QUARANTINE_FILENAME, TMP_SUFFIX, QuarantineWriter
from helpers import make_row


def record(key, *reasons):
    return ClassifiedRecord(make_row(unique_key=key), tuple(reasons))


class QuarantineWriterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.final = self.dir / QUARANTINE_FILENAME
        self.tmp = self.final.with_name(self.final.name + TMP_SUFFIX)

    def _lines(self):
        text = self.final.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]

    def test_each_record_is_written_with_its_reasons(self):
        with QuarantineWriter(self.dir) as writer:
            writer.write(record("1", ReasonCode.BAD_ZIP_FORMAT))
            writer.write(
                record("2", ReasonCode.UNPARSEABLE_DATE, ReasonCode.VALUE_OUT_OF_DOMAIN)
            )
        lines = self._lines()
        self.assertEqual(lines[0]["reasons"], ["BAD_ZIP_FORMAT"])
        self.assertEqual(
            lines[1]["reasons"], ["UNPARSEABLE_DATE", "VALUE_OUT_OF_DOMAIN"]
        )
        self.assertEqual(lines[1]["row"]["unique_key"], "2")
        self.assertEqual(writer.count, 2)

    def test_final_file_appears_only_after_clean_exit(self):
        with QuarantineWriter(self.dir) as writer:
            writer.write(record("1", ReasonCode.BAD_ZIP_FORMAT))
            self.assertFalse(self.final.exists())
        self.assertTrue(self.final.exists())
        self.assertFalse(self.tmp.exists())

    def test_error_keeps_previous_file_and_removes_tmp(self):
        self.final.write_text("old\n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            with QuarantineWriter(self.dir) as writer:
                writer.write(record("1", ReasonCode.BAD_ZIP_FORMAT))
                raise RuntimeError("boom")
        self.assertEqual(self.final.read_text(encoding="utf-8"), "old\n")
        self.assertFalse(self.tmp.exists())

    def test_empty_quarantine_writes_empty_file(self):
        with QuarantineWriter(self.dir):
            pass
        self.assertEqual(self.final.read_text(encoding="utf-8"), "")

    def test_second_run_replaces_previous_file(self):
        with QuarantineWriter(self.dir) as writer:
            writer.write(record("1", ReasonCode.BAD_ZIP_FORMAT))
        with QuarantineWriter(self.dir):
            pass
        self.assertEqual(self.final.read_text(encoding="utf-8"), "")

    def test_output_is_deterministic(self):
        def run():
            with QuarantineWriter(self.dir) as writer:
                writer.write(record("1", ReasonCode.BAD_ZIP_FORMAT))
                writer.write(record("2", ReasonCode.CLOSED_BEFORE_CREATED))
            return self.final.read_bytes()

        self.assertEqual(run(), run())

    def test_write_outside_context_is_an_error(self):
        writer = QuarantineWriter(self.dir)
        with self.assertRaises(RuntimeError):
            writer.write(record("1", ReasonCode.BAD_ZIP_FORMAT))