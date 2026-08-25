import importlib.util
import unittest
from pathlib import Path

try:
    import torch
except ModuleNotFoundError:
    torch = None


ROOT = Path(__file__).resolve().parents[1]
NUMERICAL_PATH = (
    ROOT
    / "engines"
    / "openparf"
    / "openparf"
    / "placement"
    / "numerical.py"
)
OPENPARF_ROOT = NUMERICAL_PATH.parents[1]
NUMERICAL = None
if torch is not None:
    SPEC = importlib.util.spec_from_file_location(
        "openparf_numerical", NUMERICAL_PATH
    )
    NUMERICAL = importlib.util.module_from_spec(SPEC)
    assert SPEC.loader is not None
    SPEC.loader.exec_module(NUMERICAL)


@unittest.skipIf(torch is None, "PyTorch is unavailable in this interpreter")
class OpenParfNumericalTest(unittest.TestCase):
    def test_mask_precedes_subgradient_normalization(self) -> None:
        vector = torch.tensor([3.0, 4.0, 1000.0])
        disabled = torch.tensor([False, False, True])
        normalized = NUMERICAL.masked_l2_normalize(vector, disabled)
        self.assertTrue(torch.allclose(normalized, torch.tensor([0.6, 0.8, 0.0])))
        self.assertTrue(torch.isfinite(normalized).all())

    def test_all_locked_subgradient_is_a_finite_noop(self) -> None:
        normalized = NUMERICAL.masked_l2_normalize(
            torch.zeros(4), torch.ones(4, dtype=torch.bool)
        )
        self.assertTrue(torch.equal(normalized, torch.zeros(4)))
        self.assertTrue(torch.isfinite(normalized).all())

    def test_zero_curvature_retains_existing_step(self) -> None:
        step = NUMERICAL.safe_l2_step_size(
            torch.zeros(3), torch.zeros(3), torch.tensor(0.125)
        )
        self.assertEqual(step.item(), 0.125)
        self.assertTrue(torch.isfinite(step))

    def test_regular_secant_step_is_unchanged(self) -> None:
        step = NUMERICAL.safe_l2_step_size(
            torch.tensor([3.0, 4.0]),
            torch.tensor([0.0, 2.0]),
            torch.tensor(0.125),
        )
        self.assertEqual(step.item(), 2.5)

    def test_nonfinite_subgradient_is_not_silently_hidden(self) -> None:
        with self.assertRaises(FloatingPointError):
            NUMERICAL.masked_l2_normalize(
                torch.tensor([float("nan")]), torch.tensor([True])
            )

    def test_live_resource_mask_reaches_electrostatic_solver(self) -> None:
        potential_source = (
            OPENPARF_ROOT
            / "ops"
            / "electric_potential"
            / "electric_potential.py"
        ).read_text(encoding="utf-8")
        inactive_branch = potential_source.index(
            "if not area_type_mask[area_type]:"
        )
        spectral_solve = potential_source.index(
            "self.dct2[area_type].forward", inactive_branch
        )
        self.assertLess(inactive_branch, spectral_solve)
        self.assertIn(
            "density_maps, area_type_mask)", potential_source
        )

        collections_source = (
            OPENPARF_ROOT / "placement" / "data_collections.py"
        ).read_text(encoding="utf-8")
        operators_source = (
            OPENPARF_ROOT / "placement" / "op_collections.py"
        ).read_text(encoding="utf-8")
        legalizer_source = (
            OPENPARF_ROOT / "ops" / "mcf_lg" / "mcf_lg.py"
        ).read_text(encoding="utf-8")
        placer_source = (
            OPENPARF_ROOT / "placement" / "placer.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "self.optimization_area_type_mask = self.area_type_mask.clone()",
            collections_source,
        )
        self.assertIn(
            "self.optimization_area_type_mask[area_types] = False",
            collections_source,
        )
        self.assertIn(
            "area_type_mask=data_cls.optimization_area_type_mask",
            operators_source,
        )
        self.assertIn("lock_area_types", legalizer_source)
        self.assertIn(
            "self.op_cls.density_op.area_type_mask = (",
            placer_source,
        )


if __name__ == "__main__":
    unittest.main()
