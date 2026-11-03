"""Validation: reasons for every rejection, and no silent conversion of junk."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
for candidate in (
    HERE,
    HERE.parent / "src",
    HERE.parents[1] / "stage-1-kiarash-akbari" / "src",
):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from fixtures import row

from credit.columns import DB_COLUMNS, as_records
from credit.validate import (
    CreditReasonCode,
    count_reasons,
    parse_row,
    parse_stream,
)

VALID = row(
    **{
        "Give.Me.Some.Credits": "1",
        "SeriousDlqin2yrs": "0",
        "RevolvingUtilizationOfUnsecuredLines": "0.81",
        "age": "45",
        "NumberOfTime30-59DaysPastDueNotWorse": "96",
        "DebtRatio": "1.2",
        "MonthlyIncome": "5000",
        "NumberOfOpenCreditLinesAndLoans": "7",
        "NumberOfTimes90DaysLate": "0",
        "NumberRealEstateLoansOrLines": "1",
        "NumberOfTime60-89DaysPastDueNotWorse": "0",
        "NumberOfDependents": "2",
    }
)


class HappyPathTests(unittest.TestCase):
    def test_a_complete_row_has_no_reasons(self):
        parsed = parse_row(VALID)
        self.assertTrue(parsed.is_valid)
        self.assertEqual(parsed.reasons, ())

    def test_record_is_returned_under_database_names(self):
        parsed = parse_row(VALID)
        self.assertEqual(tuple(parsed.record), DB_COLUMNS)

    def test_types_follow_the_contract_not_the_text(self):
        record = parse_row(VALID).record
        self.assertIsInstance(record["row_id"], int)
        self.assertIsInstance(record["serious_dlqin_2yrs"], int)
        self.assertIsInstance(record["monthly_income"], float)
        self.assertEqual(record["age"], 45.0)

    def test_integral_text_with_a_decimal_point_is_still_integral(self):
        parsed = parse_row({**VALID, "NumberOfDependents": "2.0"})
        self.assertTrue(parsed.is_valid)
        self.assertEqual(parsed.record["num_dependents"], 2.0)

    def test_sentinel_codes_pass_validation_untouched(self):
        # a code is not a defect of the file; the analysis decides what it means
        parsed = parse_row(VALID)
        self.assertEqual(parsed.record["num_late_30_59"], 96.0)


class ReasonTests(unittest.TestCase):
    def test_missing_label_is_required(self):
        parsed = parse_row({**VALID, "SeriousDlqin2yrs": ""})
        self.assertEqual(parsed.reasons, (CreditReasonCode.MISSING_REQUIRED_FIELD,))

    def test_missing_optional_column_is_not_a_reason(self):
        parsed = parse_row({**VALID, "MonthlyIncome": None, "age": "  "})
        self.assertTrue(parsed.is_valid)
        self.assertIsNone(parsed.record["monthly_income"])
        self.assertIsNone(parsed.record["age"])

    def test_text_number_is_unparseable(self):
        for value in ("not available", "1e999", "inf", "1,000"):
            with self.subTest(value=value):
                parsed = parse_row({**VALID, "MonthlyIncome": value})
                self.assertEqual(parsed.reasons, (CreditReasonCode.UNPARSEABLE_NUMBER,))

    def test_placeholder_text_reads_as_absence_not_as_junk(self):
        # pandas would call these NaN; a loader that disagrees invents missing data
        for value in ("NaN", "nan", "NA", "N/A", "null", "  ", ""):
            with self.subTest(value=value):
                parsed = parse_row({**VALID, "MonthlyIncome": value})
                self.assertTrue(parsed.is_valid)
                self.assertIsNone(parsed.record["monthly_income"])

    def test_the_same_placeholder_in_the_label_is_a_missing_required_field(self):
        parsed = parse_row({**VALID, "SeriousDlqin2yrs": "NaN"})
        self.assertEqual(parsed.reasons, (CreditReasonCode.MISSING_REQUIRED_FIELD,))

    def test_fractional_label_is_out_of_domain(self):
        parsed = parse_row({**VALID, "SeriousDlqin2yrs": "0.5"})
        self.assertEqual(parsed.reasons, (CreditReasonCode.NOT_INTEGRAL,))

    def test_wrong_label_is_out_of_domain(self):
        parsed = parse_row({**VALID, "SeriousDlqin2yrs": "2"})
        self.assertEqual(parsed.reasons, (CreditReasonCode.VALUE_OUT_OF_DOMAIN,))

    def test_fractional_row_id_is_not_integral(self):
        parsed = parse_row({**VALID, "Give.Me.Some.Credits": "12.5"})
        self.assertEqual(parsed.reasons, (CreditReasonCode.NOT_INTEGRAL,))

    def test_reasons_are_deduplicated_and_ordered(self):
        parsed = parse_row({**VALID, "MonthlyIncome": "x", "SeriousDlqin2yrs": ""})
        self.assertEqual(
            parsed.reasons,
            (CreditReasonCode.MISSING_REQUIRED_FIELD, CreditReasonCode.UNPARSEABLE_NUMBER),
        )

    def test_negative_counts_are_reported_not_rejected(self):
        # stage 2's DDL lesson: an unmeasured invariant must not abort a load
        parsed = parse_row({**VALID, "NumberOfDependents": "-1"})
        self.assertTrue(parsed.is_valid)
        self.assertEqual(parsed.record["num_dependents"], -1.0)


class StreamTests(unittest.TestCase):
    def test_duplicates_are_flagged_by_the_stream_not_the_row(self):
        rows = [VALID, {**VALID, "SeriousDlqin2yrs": "1"}]
        parsed = list(parse_stream(rows))
        self.assertTrue(parsed[0][0].is_valid)
        self.assertEqual(parsed[1][0].reasons, (CreditReasonCode.DUPLICATE_ROW_ID,))

    def test_duplicate_detection_can_be_turned_off(self):
        parsed = list(parse_stream([VALID, VALID], detect_duplicates=False))
        self.assertEqual([item[0].reasons for item in parsed], [(), ()])

    def test_reason_counts_include_zeroes(self):
        counts = count_reasons([parse_row({**VALID, "MonthlyIncome": "x"}), parse_row(VALID)])
        self.assertEqual(counts[CreditReasonCode.UNPARSEABLE_NUMBER.value], 1)
        self.assertEqual(counts[CreditReasonCode.DUPLICATE_ROW_ID.value], 0)
        self.assertEqual(len(counts), len(CreditReasonCode))

    def test_as_records_is_a_bijection(self):
        raw = row(MonthlyIncome="1")
        self.assertEqual(len(as_records(raw)), len(DB_COLUMNS))
        self.assertNotIn("MonthlyIncome", as_records(raw))


if __name__ == "__main__":
    unittest.main()
