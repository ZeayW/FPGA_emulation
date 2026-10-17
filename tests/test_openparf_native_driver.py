import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

import torch

import emuflow.openparf_native_driver as native_driver

from emuflow.openparf_native_driver import (
    OPENPARF_NATIVE_CONVERGENCE_SCHEMA,
    stable_native_stop_condition,
    validate_openparf_native_metrics,
)


def _metric(iteration, hpwl, overflow=(0.05, 0.04, 0.9)):
    return SimpleNamespace(
        opt_iter=SimpleNamespace(iteration=iteration),
        hpwl=torch.tensor(hpwl, dtype=torch.float64),
        overflow=torch.tensor(overflow, dtype=torch.float64),
    )


def _engine(*, patience=2, minimum=3):
    return SimpleNamespace(
        params=SimpleNamespace(
            wirelength_weights=[1.0, 1.0],
            max_global_place_iters=100,
            gp_adjust_area=True,
            gp_max_adjust_area_iters=6,
            stop_overflow=0.1,
            io_legalization_flag=False,
            emuflow_relative_hpwl_improvement=0.0,
            emuflow_min_feasible_iterations=minimum,
            emuflow_convergence_patience=patience,
        ),
        data_cls=SimpleNamespace(
            pos=[torch.tensor([[1.0, 2.0]], dtype=torch.float64)],
            optimization_area_type_mask=torch.tensor([True, True, False]),
            area_type_inst_groups=[list(range(20)), list(range(20)), list(range(20))],
        ),
        num_gp_adjust_area=1,
        gp_adjust_area=False,
        last_area_inflation_iter=10,
    )


class OpenparfNativeDriverTest(unittest.TestCase):
    def test_native_entrypoint_applies_fabric_weights_before_optimization(self):
        created = []

        class Data:
            def __init__(self):
                self.net_pin_map = SimpleNamespace(
                    b_starts=torch.tensor([0, 1024, 2048], dtype=torch.int32),
                    b2as=torch.repeat_interleave(torch.arange(2, dtype=torch.int32), 1024),
                )
                self.pin_signal_directs = torch.zeros(2048, dtype=torch.uint8)
                self.pin_signal_directs[0] = 1
                self.net_weights = torch.ones(2, dtype=torch.float64)
                self.net_mask = torch.ones(2, dtype=torch.uint8)
                self.net_mask_ignore_large = torch.zeros(2, dtype=torch.uint8)
                self.pos = [torch.zeros((1, 2), dtype=torch.float64)]
                self.preconditioner_updates = 0

            def compute_wl_precond(self, *_args):
                self.preconditioner_updates += 1

        class Engine:
            def __init__(self, params, placedb):
                self.params, self.placedb = params, placedb
                self.data_cls = Data()
                self.dtype, self.device = torch.float64, torch.device("cpu")
                self.operator_mask = self.data_cls.net_mask_ignore_large
                self.op_cls = SimpleNamespace(hpwl_op=lambda _pos: [1.0, 2.0])
                created.append(self)

            def __call__(self):
                # Assert at the real entrypoint's optimizer boundary, not
                # merely by invoking the weighting helper directly.
                if self.data_cls.net_weights.tolist() != [4.0, 1.0]:
                    raise AssertionError("native optimization began with unit weights")
                if self.operator_mask.tolist() != [1, 0]:
                    raise AssertionError("fabric and ideal clocks were not distinguished")
                self._emuflow_stable_convergence = {"stop_reason": "empty-active-subspace"}
                self.cur_metric_record = _metric(1, [1.0, 2.0])

            def write(self, filename):
                Path(filename).write_text("fixture\n", encoding="utf-8")

        class Params:
            def load(self, filename):
                self.__dict__.update(json.loads(Path(filename).read_text()))

            def design_name(self):
                return "fixture"

        package = ModuleType("openparf")
        package.__path__ = []
        package.openparf = SimpleNamespace(SignalDirection=SimpleNamespace(kOutput=1))
        flow = ModuleType("openparf.flow")
        flow.route = mock.Mock(side_effect=AssertionError("routing not requested"))

        def place(params, filename):
            engine = Engine(params, object())
            engine()
            engine.write(filename)

        flow.place = place
        params_module = ModuleType("openparf.params")
        params_module.Params = Params
        placement = ModuleType("openparf.placement")
        placement.placer = SimpleNamespace(Placer=Engine)
        modules = {"openparf": package, "openparf.flow": flow,
                   "openparf.params": params_module, "openparf.placement": placement}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            config.write_text(json.dumps({
                "emuflow_stable_global_placement": True, "num_threads": 1,
                "route_flag": 0, "result_dir": str(root),
                "wirelength_weights": [1.0, 1.0],
                "emuflow_min_feasible_iterations": 96,
                "emuflow_convergence_patience": 64,
                "emuflow_relative_hpwl_improvement": 1e-4,
                "emuflow_high_fanout_weight_threshold": 64,
                "emuflow_high_fanout_weight_exponent": 0.5,
                "emuflow_high_fanout_weight_maximum": 4.0,
            }))
            argv = ["native-driver", "--config", str(config), "--log", str(root / "log")]
            with mock.patch.dict(sys.modules, modules), mock.patch.object(sys, "argv", argv):
                self.assertEqual(native_driver.main(), 0)
            metrics = json.loads((root / "fixture.native-metrics.json").read_text())
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].data_cls.preconditioner_updates, 1)
        self.assertIs(created[0].operator_mask, created[0].data_cls.net_mask_ignore_large)
        self.assertEqual(metrics["high_fanout_net_weighting"]["reenabled_driven_nets"], 1)
        self.assertEqual(metrics["high_fanout_net_weighting"]["masked_driverless_nets"], 1)
        flow.route.assert_not_called()

    def test_waits_for_patience_and_restores_best_feasible_position(self):
        engine = _engine()
        engine.data_cls.pos[0].data.fill_(11.0)
        self.assertFalse(stable_native_stop_condition(engine, [_metric(11, [6, 4])]))
        engine.data_cls.pos[0].data.fill_(12.0)
        self.assertFalse(stable_native_stop_condition(engine, [_metric(12, [5, 4])]))
        engine.data_cls.pos[0].data.fill_(13.0)
        self.assertFalse(stable_native_stop_condition(engine, [_metric(13, [6, 4])]))
        engine.data_cls.pos[0].data.fill_(14.0)
        self.assertTrue(stable_native_stop_condition(engine, [_metric(15, [7, 4])]))
        self.assertTrue(torch.equal(
            engine.data_cls.pos[0], torch.full((1, 2), 12.0, dtype=torch.float64)
        ))
        self.assertEqual(
            engine._emuflow_stable_convergence["stop_reason"],
            "feasible-hpwl-patience",
        )

    def test_does_not_stop_before_routability_adjustment_finishes(self):
        engine = _engine(patience=0, minimum=0)
        engine.gp_adjust_area = True
        self.assertFalse(stable_native_stop_condition(engine, [_metric(20, [1, 1])]))

    def test_adjustment_limit_starts_stable_window(self):
        engine = _engine(patience=0, minimum=1)
        engine.gp_adjust_area = True
        engine.num_gp_adjust_area = 6
        self.assertTrue(stable_native_stop_condition(engine, [_metric(20, [1, 1])]))

    def test_infeasible_iterate_resets_stable_feasible_window(self):
        engine = _engine(patience=0, minimum=2)
        self.assertFalse(stable_native_stop_condition(engine, [_metric(11, [2, 2])]))
        self.assertFalse(stable_native_stop_condition(
            engine, [_metric(12, [2, 2], overflow=(0.2, 0.04, 0.9))]
        ))
        self.assertFalse(stable_native_stop_condition(engine, [_metric(13, [2, 2])]))
        self.assertTrue(stable_native_stop_condition(engine, [_metric(14, [2, 2])]))

    def test_maximum_iteration_restores_best_feasible_position(self):
        engine = _engine(patience=100, minimum=100)
        engine.data_cls.pos[0].data.fill_(7.0)
        self.assertFalse(stable_native_stop_condition(engine, [_metric(20, [2, 2])]))
        engine.data_cls.pos[0].data.fill_(9.0)
        self.assertTrue(stable_native_stop_condition(engine, [_metric(100, [3, 3])]))
        self.assertTrue(torch.equal(
            engine.data_cls.pos[0], torch.full((1, 2), 7.0, dtype=torch.float64)
        ))
        self.assertEqual(
            engine._emuflow_stable_convergence["stop_reason"],
            "maximum-iterations-feasible",
        )

    def test_first_feasible_sample_at_iteration_limit_is_accepted(self):
        engine = _engine(patience=100, minimum=100)
        engine.data_cls.pos[0].data.fill_(7.0)
        self.assertFalse(stable_native_stop_condition(
            engine, [_metric(99, [2, 2], overflow=(0.2, 0.2, 0.9))]
        ))
        engine.data_cls.pos[0].data.fill_(9.0)
        self.assertTrue(stable_native_stop_condition(
            engine, [_metric(100, [3, 3], overflow=(0.1, 0.09, 0.9))]
        ))
        self.assertTrue(torch.equal(
            engine.data_cls.pos[0], torch.full((1, 2), 9.0, dtype=torch.float64)
        ))
        state = engine._emuflow_stable_convergence
        self.assertEqual(state["first_feasible_iteration"], 100)
        self.assertEqual(state["best_feasible_iteration"], 100)
        self.assertEqual(state["stop_reason"], "maximum-iterations-feasible")

    def test_validates_compact_native_certificate(self):
        value = {
            "schema": OPENPARF_NATIVE_CONVERGENCE_SCHEMA,
            "status": "pass",
            "stop_reason": "feasible-hpwl-patience",
            "iterations": 42,
            "restored_best_feasible": True,
            "best_feasible_hpwl": 10.0,
            "final_legal_hpwl": 11.0,
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "metrics.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(validate_openparf_native_metrics(path), value)

    def test_accepts_bounded_feasible_maximum_iteration(self):
        value = {
            "schema": OPENPARF_NATIVE_CONVERGENCE_SCHEMA,
            "status": "pass",
            "stop_reason": "maximum-iterations-feasible",
            "iterations": 100,
            "restored_best_feasible": True,
            "best_feasible_hpwl": 10.0,
            "final_legal_hpwl": 11.0,
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "metrics.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(validate_openparf_native_metrics(path), value)

    def test_rejects_bounded_infeasible_maximum_iteration(self):
        value = {
            "schema": OPENPARF_NATIVE_CONVERGENCE_SCHEMA,
            "status": "fail",
            "stop_reason": "maximum-iterations-infeasible",
            "iterations": 100,
            "restored_best_feasible": False,
            "best_feasible_hpwl": None,
            "final_legal_hpwl": 11.0,
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "metrics.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "certificate is invalid"):
                validate_openparf_native_metrics(path)
            self.assertEqual(
                validate_openparf_native_metrics(
                    path, allow_detailed_legalization_handoff=True
                ),
                value,
            )

    def test_infeasible_handoff_rejects_false_restored_feasible_claim(self):
        value = {
            "schema": OPENPARF_NATIVE_CONVERGENCE_SCHEMA,
            "status": "fail",
            "stop_reason": "maximum-iterations-infeasible",
            "iterations": 100,
            "restored_best_feasible": True,
            "best_feasible_hpwl": None,
            "final_legal_hpwl": 11.0,
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "metrics.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "cannot claim"):
                validate_openparf_native_metrics(
                    path, allow_detailed_legalization_handoff=True
                )


if __name__ == "__main__":
    unittest.main()
