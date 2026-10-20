import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from emuflow.errors import ValidationError
from emuflow.global_sta import adopt_opensta_results, build_opensta_timing, compare_system_timing
from emuflow.io import write_json
from emuflow.ir import EmuIR
from emuflow.local_path_timing import path_id_set_sha256, prepare_vpr_local_path_query_inputs
from emuflow.logic_segment_timing import _segment_id, prepare_logic_segment_query_inputs
from emuflow.output_realizations import (
    aggregate_output_realizations, original_path_id, output_realization_index,
)
from emuflow.platform import Platform
from emuflow.routing import SYSTEM_ROUTES_SCHEMA
from emuflow.tdm import reconstruct_tdm_schedule_timing


def registry(originals=("p", "q")):
    copies = []
    for original in originals:
        for owner in ("fpga0", "fpga1"):
            local = owner == "fpga0"
            copies.append({
                "id": f"{original}:{owner}", "original_path": original,
                "fpga": owner, "endpoint": {"port": "out", "bit": 0},
                "terminal_net": "n", "cut_nets": [] if local else ["n"],
                "cut_transitions": [] if local else [{"net": "n", "from": "fpga0", "to": owner}],
                "local": local, "clock_domain": "clk", "clock_period_ns": 10.0,
                "required_time_ns": 10.0, "fixed_delay_ns": 6.0 if original == "p" else 3.0,
            })
    return {"schema": "emuflow.output-realizations/v1", "realizations": copies,
            "original_population": {"paths": len(originals), "path_ids_sha256": path_id_set_sha256(list(originals))}}


def route_value(value=None):
    return {"timing": {"output_realizations": value or registry(), "normalization": {
        "positive_slack_scale_ns": 10.0, "negative_slack_scale_ns": 1.0,
        "max_clock_period_ns": 10.0}}}


class OutputRealizationTimingTest(unittest.TestCase):
    def test_untimed_routes_preserve_existing_behavior(self):
        self.assertEqual(output_realization_index({}), {})
        self.assertEqual(output_realization_index({"timing": None}), {})
        with self.assertRaises(ValidationError):
            output_realization_index({"timing": []})

    def test_p5_expands_compressed_members_and_tns_once_per_original(self):
        routes = route_value()
        record = {"path": "p:fpga1", "compressed_path_ids": ["p:fpga1", "q:fpga1"],
                  "clock_domain": "clk", "clock_period_ns": 10.0, "required_time_ns": 10.0,
                  "transport_delay_ns": 8.0, "delay_ns": 14.0, "slack_ns": -4.0,
                  "normalized_slack": -4.0}
        with mock.patch("emuflow.tdm.reconstruct_tdm_schedule_timing_paths_from_routes", return_value=[record]):
            report = reconstruct_tdm_schedule_timing(routes, SimpleNamespace(links=[]), {})
        self.assertEqual(report["timing_paths"], 2)
        self.assertEqual(report["worst_path"], "p")
        self.assertEqual(report["worst_delay_ns"], 14)
        self.assertEqual(report["negative_slack_paths"], 2)
        # q retains its source fixed delay 3, not the compressed representative's 6.
        self.assertEqual(report["total_negative_slack_ns"], -5)

    def physical_records(self, local_delay=18, remote_delay=14):
        delays = {"p:fpga0": local_delay, "p:fpga1": remote_delay,
                  "q:fpga0": 3, "q:fpga1": 11}
        return [{"path": item["id"], "path_scope": "same-fpga-local" if item["local"] else "cross-fpga",
                 "logical_fpga_sequence": [item["fpga"]] if item["local"] else ["fpga0", item["fpga"]],
                 "target_period_ns": 10.0, "target_required_time_ns": 10.0, "virtual_period_ns": 100.0,
                 "system_delay_bound_ns": delays[item["id"]],
                 "target_clock_slack_bound_ns": 10-delays[item["id"]]}
                for item in registry()["realizations"]]

    def test_physical_worst_copy_can_change_and_original_population_stays_fixed(self):
        for local, remote, winner in ((18, 14, "p:fpga0"), (8, 21, "p:fpga1")):
            result = aggregate_output_realizations(self.physical_records(local, remote), route_value(),
                delay_field="system_delay_bound_ns", physical=True)
            self.assertEqual([item["path"] for item in result], ["p", "q"])
            self.assertEqual(result[0]["worst_realization"], winner)
            self.assertEqual(path_id_set_sha256([item["path"] for item in result]),
                             registry()["original_population"]["path_ids_sha256"])
            self.assertEqual(len(result[0]["realization_ids"]), 2)

    def test_missing_duplicate_wrong_owner_and_original_bypass_rejected(self):
        records = self.physical_records()
        variants = [records[:-1], [*records, records[0]],
                    [{**records[0], "logical_fpga_sequence": ["fpga1"]}, *records[1:]],
                    [{**records[0], "path": "p"}, *records[1:]]]
        for variant in variants:
            with self.assertRaises(ValidationError):
                aggregate_output_realizations(variant, route_value(),
                    delay_field="system_delay_bound_ns", physical=True)

    def test_member_lookup_uses_source_but_segment_identity_uses_copy(self):
        index = output_realization_index(route_value())
        self.assertEqual(original_path_id("p:fpga1", index), "p")
        self.assertEqual(original_path_id("ordinary", index), "ordinary")
        self.assertNotEqual(_segment_id("p:fpga1", 0, "launch"),
                            _segment_id("p:fpga2", 0, "launch"))

    def engine_report(self, local_delay=18, remote_delay=14):
        records = self.physical_records(local_delay, remote_delay)
        metadata = {"paths": {item["path"]: {
            "path_scope": item["path_scope"], "logical_fpga_sequence": item["logical_fpga_sequence"],
            "clock_domain": "clk", "target_period_ns": 10.0,
            "physical_logic_segments_cone_bound": False,
        } for item in records}, "source_binding": {
            "original_paths": 2, "original_path_ids_sha256": path_id_set_sha256(["p", "q"])},
            "compressed_representative_paths": 1, "output_realizations": registry()}
        rows = [{"path": item["path"], "role": role, "event": "capture",
                 "arrival_ns": item["system_delay_bound_ns"], "required_ns": required,
                 "slack_ns": required-item["system_delay_bound_ns"]}
                for item in records for role, required in (("target", 10), ("runtime", 100))]
        runtime = {"virtual_dut_clocks": {"clk": {"nominal_period_ns": 100}}}
        return build_opensta_timing(runtime, metadata, rows), rows

    def test_engine_all_copies_checked_then_tns_once_and_local_can_be_worst(self):
        report, rows = self.engine_report()
        self.assertEqual(report["summary"]["timing_paths"], 2)
        self.assertEqual(report["summary"]["physical_path_realizations"], 4)
        self.assertEqual(report["target_clock"]["tns_bound_ns"], -9)
        self.assertEqual(report["paths"][0]["worst_realization"], "p:fpga0")
        self.assertEqual(report["global_opensta"]["checks"], 8)
        gate = compare_system_timing(rows, report)
        self.assertEqual(gate["metrics"]["target"]["original_path_tns_ns"], -9)
        with self.assertRaises(ValidationError):
            compare_system_timing(rows[:-1], report)
        bad = copy.deepcopy(rows)
        bad[0]["arrival_ns"] += 1
        with self.assertRaises(ValidationError):
            compare_system_timing(bad, report)

    def test_engine_remote_can_be_worst_after_physical_routing(self):
        report, _ = self.engine_report(8, 21)
        self.assertEqual(report["paths"][0]["worst_realization"], "p:fpga1")
        self.assertEqual(report["target_clock"]["tns_bound_ns"], -12)

    def test_engine_adoption_reselects_worst_copy_and_checks_nonworst_copies(self):
        report, rows = self.engine_report(14, 14.0005)
        self.assertEqual(report["paths"][0]["worst_realization"], "p:fpga1")
        for row in rows:
            if row["path"] == "p:fpga0":
                row["arrival_ns"] = 14.0008
                row["slack_ns"] = row["required_ns"] - row["arrival_ns"]
        adopt_opensta_results(report, rows)
        self.assertEqual(report["paths"][0]["worst_realization"], "p:fpga0")
        self.assertAlmostEqual(report["target_clock"]["tns_bound_ns"], -5.0008)
        bad = copy.deepcopy(rows)
        for row in bad:
            if row["path"] == "q:fpga0":
                row["arrival_ns"] += .5
        with self.assertRaises(ValidationError):
            compare_system_timing(bad, report)

    def test_real_local_preparation_keeps_source_local_copy_of_cross_output(self):
        start = {"instance": "launch", "port": "Q", "bit": 0}
        end = {"instance": None, "port": "out", "bit": 0}
        value = {"schema": "emuflow.emuir/v1", "design": {"name": "dut", "top": "dut", "source_format": "test"},
                 "ports": [{"id": "out", "name": "out", "direction": "output", "width": 1}],
                 "instances": [{"id": "launch", "name": "launch", "type": "FDRE", "resources": {"ff": 1},
                                "parameters": {}, "attributes": {}, "constant_connections": []}],
                 "nets": [{"id": "n", "name": "n", "fanout": 1, "cut_class": "register_output",
                           "drivers": [start], "sinks": [end]}], "clocks": [], "warnings": []}
        path = {"id": "p", "startpoint": {"object": "launch/Q", **start},
                "endpoint": {"object": "out[0]", **end}, "clock_domain": "clk", "clock_period_ns": 10.,
                "slack_ns": 4., "fixed_delay_ns": 6., "normalized_slack": .4, "path_nets": ["n"]}
        database = {"schema": "emuflow.sta-path-database/v1", "design": "dut", "paths": [path],
                    "source": {"provider": "test", "input": "test"},
                    "normalization": route_value()["timing"]["normalization"]}
        routes = route_value(registry(("p",)))
        routes.update(schema=SYSTEM_ROUTES_SCHEMA, routes=[])
        routes["timing"]["paths"] = [{"path": "p:fpga1", "compressed_path_ids": ["p:fpga1"]}]
        assignment = {"schema": "emuflow.partition-assignment/v1", "design": "dut",
                      "instance_assignment": {"launch": "fpga0"}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path, assignment_path, database_path, routes_path = [root/name for name in ("ir.emuir.sqlite", "assignment.json", "paths.json", "routes.json")]
            EmuIR(value).save(ir_path)
            for target, content in ((assignment_path, assignment), (database_path, database), (routes_path, routes)):
                write_json(target, content)
            prepared = prepare_vpr_local_path_query_inputs(ir_path, assignment_path, database_path, routes_path)
            self.assertEqual(prepared.local_paths_by_fpga["fpga0"][0]["id"], "p:fpga0")
            self.assertEqual(prepared.source_manifest["original_paths"], 1)
            self.assertEqual(prepared.source_manifest["original_path_ids_sha256"], path_id_set_sha256(["p"]))

    def test_real_author_schedule_and_indexed_physical_preparation_share_copy_ids(self):
        from tests.test_author_routing import author_router
        from tests.test_output_path_projection import OutputPathProjectionTest
        from emuflow.author_routing import route_system_author
        from emuflow.routing import demands_from_assignment, normalize_route_constraints
        from emuflow.timing_routing import load_sta_paths
        from emuflow.tdm import build_tdm_schedule, validate_tdm_schedule
        fixture = OutputPathProjectionTest()
        artifact, _, assignment, ir, platform = fixture._project()
        original_database = fixture._inputs()[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projected = root / "projected.json"
            write_json(projected, artifact)
            timing = load_sta_paths(projected, demands_from_assignment(assignment, platform))
            routes = route_system_author(assignment, platform, normalize_route_constraints(None, platform),
                                         timing, executable=author_router())
            schedule = build_tdm_schedule(routes, platform)
            self.assertEqual(validate_tdm_schedule(routes, platform, schedule)["status"], "pass")
            reconstructed = reconstruct_tdm_schedule_timing(routes, platform, schedule)
            self.assertEqual(reconstructed["timing_paths"], 3)
            ir_path = root / "original.emuir.sqlite"
            ir.save(ir_path)
            assignment_path, original_path, routes_path, schedule_path = [root/name for name in (
                "assignment.json", "original-paths.json", "routes.json", "schedule.json")]
            for target, content in ((assignment_path, assignment), (original_path, original_database),
                                    (routes_path, routes), (schedule_path, schedule)):
                write_json(target, content)
            with mock.patch("emuflow.local_path_timing.EmuIR.load", side_effect=AssertionError("must stay indexed")):
                local = prepare_vpr_local_path_query_inputs(ir_path, assignment_path, original_path, routes_path)
            with mock.patch("emuflow.logic_segment_timing.EmuIR.load", side_effect=AssertionError("must stay indexed")):
                logic = prepare_logic_segment_query_inputs(ir_path, assignment_path, original_path, routes_path,
                                                           schedule_path, platform)
            self.assertEqual(set(logic.database_paths), {"fanout", "terminal-local"})
            expected = {item["id"] for item in artifact["output_realizations"]["realizations"] if item["local"]}
            actual = {path["id"] for paths in local.local_paths_by_fpga.values() for path in paths}
            self.assertEqual(actual, expected)
            remote_members = {member for record in logic.timing_records
                              for member in record["compressed_path_ids"]}
            self.assertEqual(remote_members, {item["id"] for item in artifact["output_realizations"]["realizations"]
                                              if not item["local"]})

    def test_ab_gate_checks_all_copies_originals_and_worst_copy_reduction(self):
        from tests.test_routing_tdm_comparison import RoutingTdmComparisonTest, _sha256
        from emuflow.routing_tdm_comparison import _global_timing_metrics
        from emuflow.timing_routing import ROUTE_TDM_PROVIDER
        from emuflow.tdm import TDM_BASELINE_PROVIDER
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = RoutingTdmComparisonTest()._report(root, ROUTE_TDM_PROVIDER, TDM_BASELINE_PROVIDER, -1.)
            timing = report["runtime"]["system_timing"]
            physical = self.physical_records()
            for item in physical:
                item.update(clock_domain="clk", runtime_clock_slack_bound_ns=100-item["system_delay_bound_ns"],
                            physical_logic_segments_exact=True, physical_logic_segments_cone_bound=False,
                            physical_logic_model="routed-staging-chain-exact")
            timing["output_realizations"] = registry()
            timing["realization_paths"] = physical
            timing["paths"] = aggregate_output_realizations(physical, route_value(),
                delay_field="system_delay_bound_ns", physical=True)
            timing["path_exactness"].update(population="physical-path-realizations", endpoint_exact_logic_paths=4)
            timing["summary"].update(original_local_paths=2, original_cross_fpga_paths=2,
                physical_path_realizations=4, output_copy_realizations=4, output_copy_coverage=1.0,
                original_paths_with_local_and_cross_realizations=2,
                original_path_ids_sha256=path_id_set_sha256(["p", "q"]))
            timing["target_clock"].update(worst_slack_bound_ns=-8., tns_bound_ns=-9., negative_slack_paths=2)
            timing["runtime_clock"].update(worst_slack_bound_ns=82., tns_bound_ns=0., negative_slack_paths=0)
            routes = route_value()
            routes["timing"]["paths"] = [{"path": "p:fpga1", "compressed_path_ids": ["p:fpga1", "q:fpga1"]}]
            write_json(root / "routes.json", routes)
            write_json(root / "timing_path_database.json", {"schema": "emuflow.sta-path-database/v1",
                                                           "paths": [{"id": "p"}, {"id": "q"}]})
            for artifact in ("routes", "timing_path_database"):
                report["artifacts"][artifact]["sha256"] = _sha256(root / (artifact + ".json"))
            timing["source_binding"].update(routes_sha256=report["artifacts"]["routes"]["sha256"],
                path_database_sha256=report["artifacts"]["timing_path_database"]["sha256"],
                original_path_ids_sha256=path_id_set_sha256(["p", "q"]))
            metrics = _global_timing_metrics(root, report)
            self.assertEqual(metrics["original_paths"], 2)
            self.assertEqual(metrics["physical_path_realizations"], 4)
            self.assertEqual(metrics["target_global_tns_ns"], -9)
            variants = []
            for mutate in (
                lambda x: x["realization_paths"].pop(),
                lambda x: x["paths"][0].update(system_delay_bound_ns=14),
                lambda x: x["paths"][0].update(path="wrong-original"),
                lambda x: x["summary"].update(original_paths_with_local_and_cross_realizations=0),
                lambda x: x["path_exactness"].update(endpoint_exact_logic_paths=2),
                lambda x: x["target_clock"].update(tns_bound_ns=-13),
            ):
                altered = copy.deepcopy(report)
                mutate(altered["runtime"]["system_timing"])
                with self.assertRaises(ValidationError):
                    _global_timing_metrics(root, altered)


if __name__ == "__main__":
    unittest.main()
