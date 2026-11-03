import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from data_factory.timeutil import closed_duration, possible_instants

NY = ZoneInfo("America/New_York")


def local(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")


class PossibleInstantsTests(unittest.TestCase):
    def test_ordinary_time_has_one_instant(self):
        self.assertEqual(len(possible_instants(local("2025-06-01T12:00:00"), NY)), 1)

    def test_repeated_fall_back_hour_has_two_instants(self):
        options = possible_instants(local("2025-11-02T01:30:00"), NY)
        self.assertEqual(len(options), 2)
        self.assertEqual((options[1] - options[0]).total_seconds(), 3600)

    def test_skipped_spring_forward_hour_maps_to_two_instants(self):
        options = possible_instants(local("2025-03-09T02:30:00"), NY)
        self.assertEqual(len(options), 2)
        self.assertEqual((options[1] - options[0]).total_seconds(), 3600)


class ClosedDurationTests(unittest.TestCase):
    def test_ordinary_duration(self):
        result = closed_duration(
            local("2025-06-01T10:00:00"), local("2025-06-01T10:30:00"), NY
        )
        self.assertEqual((result.seconds, result.is_ambiguous), (1800, False))

    def test_closed_equal_to_created_is_zero(self):
        t = local("2025-06-01T10:00:00")
        self.assertEqual(closed_duration(t, t, NY).seconds, 0)

    def test_closed_before_created_outside_dst_has_no_duration(self):
        result = closed_duration(
            local("2025-06-01T10:30:00"), local("2025-06-01T10:00:00"), NY
        )
        self.assertIsNone(result.seconds)

    def test_fall_back_inversion_resolves_to_positive_duration(self):
        result = closed_duration(
            local("2025-11-02T01:04:03"), local("2025-11-02T01:03:22"), NY
        )
        self.assertEqual(result.seconds, 3559)
        self.assertTrue(result.is_ambiguous)

    def test_fall_back_same_hour_picks_smallest_consistent_duration(self):
        result = closed_duration(
            local("2025-11-02T01:10:00"), local("2025-11-02T01:50:00"), NY
        )
        self.assertEqual(result.seconds, 2400)
        self.assertTrue(result.is_ambiguous)

    def test_span_across_fall_back_is_one_hour_longer_than_naive(self):
        result = closed_duration(
            local("2025-11-01T23:00:00"), local("2025-11-02T03:00:00"), NY
        )
        self.assertEqual(result.seconds, 5 * 3600)
        self.assertFalse(result.is_ambiguous)

    def test_span_across_spring_forward_is_one_hour_shorter_than_naive(self):
        result = closed_duration(
            local("2025-03-09T01:30:00"), local("2025-03-09T03:30:00"), NY
        )
        self.assertEqual(result.seconds, 3600)
        self.assertFalse(result.is_ambiguous)