import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import torch

from emuflow.openparf_native_driver import (
    OPENPARF_NATIVE_CONVERGENCE_SCHEMA,
    bounded_native_area_adjustment_condition,
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
            logic_area_type_names=["LUT", "FF"],
            io_legalization_flag=False,
            emuflow_relative_hpwl_improvement=0.0,
            emuflow_min_feasible_iterations=minimum,
            emuflow_convergence_patience=patience,
            emuflow_post_adjustment_iteration_reserve=30,
        ),
        data_cls=SimpleNamespace(
            pos=[torch.tensor([[1.0, 2.0]], dtype=torch.float64)],
            optimization_area_type_mask=torch.tensor([True, True, False]),
            area_type_inst_groups=[list(range(20)), list(range(20)), list(range(20))],
        ),
        num_gp_adjust_area=1,
        gp_adjust_area=False,
        last_area_inflation_iter=10,
        placedb=SimpleNamespace(
            getAreaTypeIndexFromName=lambda name: {"LUT": 0, "FF": 1}[name]
        ),
    )


class OpenparfNativeDriverTest(unittest.TestCase):
    def test_declines_area_adjustment_without_post_reset_budget(self):
        engine = _engine()
        metric = _metric(80, [2, 2])
        self.assertFalse(
            bounded_native_area_adjustment_condition(
                engine, metric, lambda _engine, _metric: True
            )
        )
        self.assertFalse(engine.gp_adjust_area)
        self.assertEqual(
            engine._emuflow_area_adjustment_cutoff,
            {
                "iteration": 80,
                "remaining_iterations": 20,
                "required_reserve": 30,
                "completed_adjustments": 1,
            },
        )

    def test_allows_area_adjustment_with_reserved_budget(self):
        engine = _engine()
        engine.gp_adjust_area = True
        self.assertTrue(
            bounded_native_area_adjustment_condition(
                engine,
                _metric(60, [2, 2]),
                lambda _engine, _metric: True,
            )
        )
        self.assertTrue(engine.gp_adjust_area)

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

    def test_sparse_exact_resources_do_not_block_logic_convergence(self):
        engine = _engine(patience=0, minimum=1)
        self.assertTrue(stable_native_stop_condition(
            engine, [_metric(20, [2, 2], overflow=(0.05, 0.04, 0.99))]
        ))
        self.assertEqual(
            engine._emuflow_stable_convergence["terminal_logic_overflow"],
            {"LUT": 0.05, "FF": 0.04},
        )

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
