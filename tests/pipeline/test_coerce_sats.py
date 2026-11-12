import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PROJECT_ROOT))

from exporters.common import coerce_sats  # noqa: E402


class CoerceSatsTests(unittest.TestCase):
    def test_sats_mode_keeps_integers_and_integral_floats(self):
        self.assertEqual(coerce_sats(150000), 150000)
        self.assertEqual(coerce_sats(2.0), 2)
        self.assertEqual(coerce_sats("100000"), 100000)

    def test_sats_mode_rejects_fractional_amounts(self):
        # The pre-fix code silently read this as 1.5 BTC = 150_000_000 sats.
        with self.assertRaises(ValueError):
            coerce_sats(1.5)
        with self.assertRaises(ValueError):
            coerce_sats("0.0015")

    def test_btc_mode_multiplies_by_1e8(self):
        self.assertEqual(coerce_sats(0.0015, unit="btc"), 150000)
        self.assertEqual(coerce_sats(1.0, unit="btc"), 100_000_000)

    def test_none_and_empty_string_pass_through(self):
        self.assertIsNone(coerce_sats(None))
        self.assertIsNone(coerce_sats("  "))

    def test_unknown_unit_is_rejected(self):
        with self.assertRaises(ValueError):
            coerce_sats(1, unit="wei")


if __name__ == "__main__":
    unittest.main()
