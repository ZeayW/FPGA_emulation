import importlib.util
import math
import unittest
from pathlib import Path


class OpenPARFFillerTest(unittest.TestCase):
    def test_charge_matches_target_with_fixed_cells_and_blockages(self):
        # The electrostatic operator scales unavailable and fixed charge by
        # tau. All charge, including fillers, must therefore sum to tau*A.
        for tau in (0.4, 0.75, 1.0):
            die, blocked, fixed, movable = 1000.0, 200.0, 50.0, 150.0
            filler = self._module().compute_filler_area(
                die - blocked - fixed, movable, tau
            )
            charge = tau * (blocked + fixed) + movable + filler
            self.assertTrue(math.isclose(charge, tau * die))

    def test_low_density_does_not_fill_whole_device(self):
        module = self._module()
        movable, capacity, tau = 98049.1171875, 510720.0, 0.4
        area = module.compute_filler_area(capacity, movable, tau)
        count, size = module.compute_filler_geometry(
            area, (math.sqrt(0.125), math.sqrt(0.125)), 65536,
            (0.876953125 / 2, 1.21484375 / 2),
        )
        self.assertAlmostEqual(movable + count * size[0] * size[1],
                               capacity * tau)
        self.assertAlmostEqual(area, 106238.8828125)

    def test_overfull_target_has_no_negative_fillers(self):
        self.assertEqual(self._module().compute_filler_area(100, 60, 0.4), 0)

    def test_invalid_density_budget_rejected(self):
        for args in [(1, 0, 0), (1, 0, 1.1), (-1, 0, 0.4),
                     (1, -1, 0.4), (1, 0, float('nan'))]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self._module().compute_filler_area(*args)

    @staticmethod
    def _module():
        source = (
            Path(__file__).resolve().parents[1]
            / "engines/openparf/openparf/placement/filler.py"
        )
        spec = importlib.util.spec_from_file_location(
            "candidate_openparf_filler", source
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def test_cap_preserves_total_area_and_aspect_ratio(self):
        count, size = self._module().compute_filler_geometry(
            511088.0, (0.25, 0.25), 65536
        )
        self.assertEqual(count, 65536)
        self.assertTrue(math.isclose(count * size[0] * size[1], 511088.0))
        self.assertTrue(math.isclose(size[0], size[1]))

    def test_uncapped_path_preserves_historical_count(self):
        count, size = self._module().compute_filler_geometry(
            16.0, (0.25, 0.25)
        )
        self.assertEqual(count, 256)
        self.assertTrue(math.isclose(count * size[0] * size[1], 16.0))

    def test_cap_yields_to_density_bin_geometry(self):
        count, size = self._module().compute_filler_geometry(
            284990.8828125,
            (0.3535533905932738, 0.3535533905932738),
            65536,
            (0.876953, 1.21484),
        )
        self.assertGreater(count, 65536)
        self.assertLessEqual(size[0], 0.876953)
        self.assertLessEqual(size[1], 1.21484)
        self.assertTrue(
            math.isclose(count * size[0] * size[1], 284990.8828125)
        )

    def test_half_bin_sampling_increases_spatial_resolution(self):
        module = self._module()
        full_count, _ = module.compute_filler_geometry(
            284990.8828125,
            (0.3535533905932738, 0.3535533905932738),
            65536,
            (0.876953, 1.21484),
        )
        half_count, half_size = module.compute_filler_geometry(
            284990.8828125,
            (0.3535533905932738, 0.3535533905932738),
            65536,
            (0.876953 * 0.5, 1.21484 * 0.5),
        )
        self.assertGreaterEqual(half_count, full_count * 4 - 3)
        self.assertLessEqual(half_size[0], 0.876953 * 0.5)
        self.assertLessEqual(half_size[1], 1.21484 * 0.5)
        self.assertTrue(
            math.isclose(
                half_count * half_size[0] * half_size[1],
                284990.8828125,
            )
        )

    def test_zero_area_needs_no_filler(self):
        self.assertEqual(
            self._module().compute_filler_geometry(0.0, (1.0, 1.0), 1),
            (0, (0.0, 0.0)),
        )

    def test_invalid_cap_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "positive integer"):
            self._module().compute_filler_geometry(1.0, (1.0, 1.0), 0)


if __name__ == "__main__":
    unittest.main()
