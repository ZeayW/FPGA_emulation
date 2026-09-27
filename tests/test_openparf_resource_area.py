import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import torch


class OpenPARFResourceAreaTest(unittest.TestCase):
    @staticmethod
    def _resource_area_class():
        package = types.ModuleType("candidate_resource_area")
        package.__path__ = []
        cpp = types.ModuleType("candidate_resource_area.resource_area_cpp")
        openparf = types.ModuleType("openparf")
        openparf.configure = types.SimpleNamespace(
            compile_configurations={"CUDA_FOUND": "FALSE"}
        )
        source = (
            Path(__file__).resolve().parents[1]
            / "engines/openparf/openparf/ops/resource_area/resource_area.py"
        )
        spec = importlib.util.spec_from_file_location(
            "candidate_resource_area.resource_area", source
        )
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {
            "candidate_resource_area": package,
            "candidate_resource_area.resource_area_cpp": cpp,
            "openparf": openparf,
        }):
            assert spec.loader is not None
            spec.loader.exec_module(module)
        return module.ResourceArea

    def test_ultrascale_accepts_lut1_through_lut6(self):
        resource_area = self._resource_area_class()
        operator = resource_area(
            is_inst_luts=torch.tensor([0, 1, 2, 6], dtype=torch.uint8),
            is_inst_ffs=torch.tensor([0, 0, 0, 0], dtype=torch.uint8),
            ff_ctrlsets=torch.empty((4, 2), dtype=torch.int32),
            num_cksr=1,
            num_ce=1,
            num_bins_x=1,
            num_bins_y=1,
            stddev_x=1.0,
            stddev_y=1.0,
            stddev_trunc=1.0,
            slice_capacity=16,
            gp_adjust_packing_rule="ultrascale",
        )
        self.assertEqual(operator.is_inst_luts.tolist(), [0, 1, 2, 6])

    def test_lut_width_above_six_fails_closed(self):
        resource_area = self._resource_area_class()
        with self.assertRaises(AssertionError):
            resource_area(
                is_inst_luts=torch.tensor([7], dtype=torch.uint8),
                is_inst_ffs=torch.tensor([0], dtype=torch.uint8),
                ff_ctrlsets=torch.empty((1, 2), dtype=torch.int32),
                num_cksr=1,
                num_ce=1,
                num_bins_x=1,
                num_bins_y=1,
                stddev_x=1.0,
                stddev_y=1.0,
                stddev_trunc=1.0,
                slice_capacity=16,
                gp_adjust_packing_rule="ultrascale",
            )


if __name__ == "__main__":
    unittest.main()
