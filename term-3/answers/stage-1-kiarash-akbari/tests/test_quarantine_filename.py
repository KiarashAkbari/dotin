import tempfile
import unittest
from pathlib import Path

from data_factory.contract import ReasonCode
from data_factory.quality import ClassifiedRecord
from data_factory.quarantine import QUARANTINE_FILENAME, VALIDATE_FILENAME, QuarantineWriter
from helpers import make_row


class CustomFilenameTests(unittest.TestCase):
    def test_validate_file_does_not_touch_the_pipeline_quarantine(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pipeline_file = directory / QUARANTINE_FILENAME
            pipeline_file.write_text("keep\n", encoding="utf-8")
            record = ClassifiedRecord(make_row(unique_key="1"), (ReasonCode.BAD_ZIP_FORMAT,))
            with QuarantineWriter(directory, VALIDATE_FILENAME) as writer:
                writer.write(record)
            self.assertEqual(pipeline_file.read_text(encoding="utf-8"), "keep\n")
            self.assertTrue((directory / VALIDATE_FILENAME).exists())
