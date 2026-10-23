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
    def test_regular_arithmetic_uses_fixed_adam_step(self):
        for psi in (0.0, 5.0, 999.0):
            for delta in (-100.0, 0.0, 100.0):
                for moment in (-20.0, 0.0, 20.0):
                    candidate = psi + 0.002 * moment / (math.sqrt(9.0) + 1e-8)
                    expected = max(0, candidate) if candidate < 1000 else psi
                    self.assertEqual(update(psi, 0.002, delta, moment, 9.0, 1e-8), expected)

    def test_absolute_sll_delta_is_not_exponentiated(self):
        expected = update(5, 0.002, 1, 10, 100, 1e-8)
        self.assertEqual(update(5, 0.002, 1_000_000, 10, 100, 1e-8), expected)
        self.assertEqual(update(5, 0.002, -1_000_000, 10, 100, 1e-8), expected)

    def test_native_bounds_reject_positive_overshoot_and_clamp_negative(self):
        self.assertEqual(update(999, 0.002, 1, 1e6, 1, 1e-8), 999)
        self.assertEqual(update(5, 0.002, -1, -1e6, 1, 1e-8), 0)
        self.assertEqual(update(5, 0.002, 1, 0, 1, 1e-8), 5)

    def test_nonrepresentable_candidate_preserves_native_bounds(self):
        self.assertEqual(update(5, 0.002, 1, 1e308, 0, 1e-8), 5)
        self.assertEqual(update(5, 0.002, -1, -1e308, 0, 1e-8), 0)
        self.assertEqual(update(5, 0.002, 1_000_000, 0, 100, 1e-8), 5)

    def test_invalid_state_is_not_hidden_by_the_overflow_guard(self):
        for args in [(5, 0, 1, 1, 1, 1e-8), (5, 0.002, math.inf, 1, 1, 1e-8),
                     (5, 0.002, 1, math.nan, 1, 1e-8), (5, 0.002, 1, 1, -1, 1e-8)]:
            with self.assertRaises(FloatingPointError):
                update(*args)
