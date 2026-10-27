"""Optional learned-delay libraries must not become runtime requirements."""
from pathlib import Path
import unittest


class OpenparfOptionalDependenciesTest(unittest.TestCase):
    def test_optional_learned_delay_dependencies_are_lazy(self):
        root = Path(__file__).resolve().parents[1]
        collections = (
            root / "engines/openparf/openparf/placement/op_collections.py"
        ).read_text(encoding="utf-8")
        prefix, delay_builder = collections.split(
            "def build_estimate_delay_op", maxsplit=1
        )
        self.assertNotIn("from hummingbird", prefix)
        self.assertIn("from hummingbird.ml import load", delay_builder)
        delay_op = (
            root
            / "engines/openparf/openparf/ops/delay_estimation/delay_estimation.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("from hummingbird", delay_op)
