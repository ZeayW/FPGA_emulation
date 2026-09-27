import importlib.util
import math
import unittest
from pathlib import Path


class OpenPARFFillerTest(unittest.TestCase):
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
