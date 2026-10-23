from decimal import Decimal, localcontext
import importlib.util
import math
from pathlib import Path
import unittest


PATH = Path(__file__).resolve().parents[1] / "engines/openparf/openparf/placement/sll_weight.py"
SPEC = importlib.util.spec_from_file_location("openparf_sll_weight", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
update = MODULE.bounded_sll_psi


class SllMultiplierTests(unittest.TestCase):
    def test_regular_arithmetic_and_native_bounds_are_unchanged(self):
        for psi in (0.0, 5.0, 999.0):
            for delta in (-100.0, 0.0, 100.0):
                for moment in (-20.0, 0.0, 20.0):
                    candidate = psi + math.pow(1.002, abs(delta)) * moment / (math.sqrt(9.0) + 1e-8)
                    expected = max(0, candidate) if candidate < 1000 else psi
                    self.assertEqual(update(psi, 1.002, delta, moment, 9.0, 1e-8), expected)

    def test_overflowing_growth_preserves_existing_acceptance_rule(self):
        with self.assertRaises(OverflowError):
            math.pow(1.002, 1_000_000)
        self.assertEqual(update(5, 1.002, 1_000_000, 10, 100, 1e-8), 5)
        self.assertEqual(update(5, 1.002, -1_000_000, -10, 100, 1e-8), 0)
        self.assertEqual(update(5, 1.002, 1_000_000, 0, 100, 1e-8), 5)

    def test_log_domain_keeps_small_finite_product_of_overflowing_power(self):
        # The power cannot be represented as a double, while its product with
        # a tiny moment can. Blindly replacing the power with infinity fails.
        with localcontext() as context:
            context.prec = 80
            rate = Decimal.from_float(1.002)
            exponent = 360_000
            moment = 1e-310
            expected = float(Decimal(5) + rate ** exponent * Decimal.from_float(moment) / (Decimal(1) + Decimal.from_float(1e-8)))
        actual = update(5, 1.002, exponent, moment, 1, 1e-8)
        self.assertAlmostEqual(actual, expected, places=9)
        self.assertGreater(actual, 5)
        self.assertLess(actual, 1000)

    def test_invalid_state_is_not_hidden_by_the_overflow_guard(self):
        for args in [(5, 0, 1, 1, 1, 1e-8), (5, 1.002, math.inf, 1, 1, 1e-8),
                     (5, 1.002, 1, math.nan, 1, 1e-8), (5, 1.002, 1, 1, -1, 1e-8)]:
            with self.assertRaises(FloatingPointError):
                update(*args)
