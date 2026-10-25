import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

import torch


class ISMCapacityTests(unittest.TestCase):
    def module(self):
        package = ModuleType("candidate_ism")
        package.__path__ = []
        cpp = ModuleType("candidate_ism.ism_dp_cpp")
        source = Path(__file__).resolve().parents[1] / "engines/openparf/openparf/ops/ism_dp/ism_dp.py"
        spec = importlib.util.spec_from_file_location("candidate_ism.ism_dp", source)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"candidate_ism": package, "candidate_ism.ism_dp_cpp": cpp}):
            spec.loader.exec_module(module)
        return module, cpp

    def params(self, **changes):
        values = dict(unit_horizontal_routing_capacity=209.0,
                      unit_vertical_routing_capacity=239.0,
                      unit_pin_capacity=50.0, wirelength_weights=[0.7, 1.2])
        values.update(changes)
        return SimpleNamespace(**values)

    def test_native_forward_receives_configured_capacities(self):
        module, cpp = self.module()
        placedb = SimpleNamespace(siteMapDim=lambda: SimpleNamespace(x=lambda: 4, y=lambda: 8))
        op = module.ISMDetailedPlace(placedb, self.params())
        cpp.forward = mock.Mock(side_effect=lambda db, param, clocks, halves, fixed, pos: pos)
        pos = torch.zeros((2, 3))
        self.assertTrue(torch.equal(op(pos), pos))
        param = cpp.forward.call_args.args[1]
        self.assertEqual((param.unitHoriRouteCap, param.unitVertRouteCap, param.unitPinCap),
                         (209.0, 239.0, 50.0))
        # A bin with zero demand has finite zero utilization and can retain
        # a whitespace candidate, unlike 0 * (1/0) from the old defaults.
        self.assertLess(0.0 / param.unitPinCap, param.pinUtilToFixWhiteSpace)

    def test_zero_nonfinite_and_negative_capacity_rejected_before_native_call(self):
        module, _ = self.module()
        for name in ["unit_horizontal_routing_capacity", "unit_vertical_routing_capacity", "unit_pin_capacity"]:
            for value in [0, -1, float("nan"), float("inf")]:
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, "finite and positive"):
                    module.ISMDetailedPlaceParam(self.params(**{name: value}))
