import unittest

from data_factory.errors import ConfigError, DataError, FetchError, PipelineError
from data_factory.exit_codes import ExitCode


class ErrorTests(unittest.TestCase):
    def test_exit_codes(self):
        cases = [
            (ConfigError, ExitCode.CONFIG),
            (FetchError, ExitCode.NETWORK),
            (DataError, ExitCode.DATA),
            (PipelineError, ExitCode.UNEXPECTED),
        ]
        for cls, code in cases:
            with self.subTest(cls=cls.__name__):
                self.assertEqual(cls("x").exit_code, code)

    def test_context_default(self):
        self.assertEqual(PipelineError("x").context, {})

    def test_context_passed(self):
        err = FetchError("boom", context={"offset": 50000})
        self.assertEqual(err.context["offset"], 50000)
        self.assertEqual(str(err), "boom")


if __name__ == "__main__":
    unittest.main()