import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from emuflow.author_routing import route_system_author, validate_author_system_routes
from emuflow.cross_layer_timing import build_cross_layer_timing_contract
from emuflow.errors import ValidationError
from emuflow.ir import EmuIR
from emuflow.platform import Platform
from emuflow.routing import demands_from_assignment, normalize_route_constraints
from emuflow.sta import project_sta_path_database, project_sta_path_database_value
from emuflow.timing_routing import load_sta_paths


def _ep(instance, port):
    return {"instance": instance, "port": port, "bit": 0}


class OutputPathProjectionTest(unittest.TestCase):
    def _inputs(self):
        from tests.test_phase4 import _link, _platform_value
        platform = Platform.from_dict(_platform_value("output-copies", ["a", "b", "c"],
            [_link("ab", "a", "b", lanes=4), _link("ac", "a", "c", lanes=4)]))
        instances = [{"id": name, "type": "LUT1", "resources": {"lut": 1}}
                     for name in ("q", "lb", "lc")]
        nets = [
            {"id": "cut", "name": "cut", "cut_class": "register_output",
             "drivers": [_ep("q", "Q")], "sinks": [_ep("lb", "I"), _ep("lc", "I"), _ep(None, "out")]},
            {"id": "tail", "name": "tail", "cut_class": "combinational",
             "drivers": [_ep("lb", "O")], "sinks": [_ep(None, "tail_out")]},
            {"id": "local", "name": "local", "cut_class": "combinational",
             "drivers": [_ep("lc", "O")], "sinks": [_ep(None, "local_out")]},
        ]
        ir = EmuIR({"schema": "emuflow.emuir/v1", "design": {"name": "outputs", "top": "outputs", "source_format": "test"},
                    "instances": instances, "nets": nets, "clocks": [],
                    "ports": [{"id": port, "direction": "output", "width": 1}
                              for port in ("out", "tail_out", "local_out")]})
        assignment = {"schema": "emuflow.partition-assignment/v1", "design": "outputs", "platform": platform.name,
            "instance_assignment": {"q": "a", "lb": "b", "lc": "c"},
            "cut_nets": [{"net": "cut", "cut_class": "register_output", "source_fpgas": ["a"], "sink_fpgas": ["b", "c"], "width_bits": 1}]}
        paths = []
        for identity, start, endpoint, path_nets in (
                ("fanout", "q", "out", ["cut"]),
                ("terminal-local", "q", "tail_out", ["cut", "tail"]),
                ("pure-local", "lc", "local_out", ["local"])):
            paths.append({"id": identity, "clock_domain": "clk", "clock_period_ns": 10.0,
                          "slack_ns": 9.0, "fixed_delay_ns": 1.0, "normalized_slack": 1.0,
                          "path_nets": path_nets, "startpoint": {**_ep(start, "Q"), "object": start + "/Q"},
                          "endpoint": {**_ep(None, endpoint), "object": endpoint}})
        database = {"schema": "emuflow.sta-path-database/v1", "design": "outputs",
                    "normalization": {"positive_slack_scale_ns": 9.0, "negative_slack_scale_ns": 1.0, "max_clock_period_ns": 10.0},
                    "paths": paths}
        return database, assignment, ir, platform

    def _project(self):
        db, assignment, ir, platform = self._inputs()
        artifact, summary = project_sta_path_database_value(db, assignment, source_input_sha256="a" * 64,
            original_nets={net["id"]: net for net in ir.value["nets"]})
        return artifact, summary, assignment, ir, platform

    def test_source_remote_terminal_local_and_pure_local_output_copies(self):
        artifact, summary, *_ = self._project()
        records = artifact["output_realizations"]["realizations"]
        self.assertEqual({(r["original_path"], r["fpga"], r["local"]) for r in records},
                         {("fanout", "a", True), ("fanout", "b", False), ("fanout", "c", False),
                          ("terminal-local", "b", False), ("pure-local", "c", True)})
        self.assertEqual(artifact["output_realizations"]["original_population"]["paths"], 3)
        self.assertEqual(artifact["compression"]["original_paths"], 2)
        self.assertEqual(artifact["compression"]["realized_paths"], 3)
        self.assertEqual(summary["database_paths"], 3)
        self.assertEqual(len({r["id"] for r in records}), 5)

    def test_compact_original_ir_projection_uses_indexed_view(self):
        db, assignment, ir, _ = self._inputs()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.emuir.sqlite"
            ir.save(original)
            (root / "db.json").write_text(json.dumps(db))
            (root / "assignment.json").write_text(json.dumps(assignment))
            with patch("emuflow.sta.EmuIR.load", side_effect=AssertionError("compact projection must stay indexed")):
                project_sta_path_database(root / "db.json", root / "assignment.json", root / "projected.json", original_ir_path=original)
            self.assertEqual(len(json.loads((root / "projected.json").read_text())["output_realizations"]["realizations"]), 5)

    def test_author_route_and_cross_layer_preserve_registry(self):
        from tests.test_author_routing import author_router
        artifact, _, assignment, _, platform = self._project()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "timing.json"
            path.write_text(json.dumps(artifact))
            timing = load_sta_paths(path, demands_from_assignment(assignment, platform))
            routed = route_system_author(assignment, platform, normalize_route_constraints(None, platform), timing, executable=author_router())
            validate_author_system_routes(assignment, platform, routed, timing)
            registry = artifact["output_realizations"]
            self.assertEqual(routed["timing"]["output_realizations"], registry)
            self.assertEqual(build_cross_layer_timing_contract(routed)["output_realizations"], registry)
            altered = copy.deepcopy(routed)
            altered["timing"]["output_realizations"]["realizations"].pop()
            with self.assertRaisesRegex(ValidationError, "timing records"):
                validate_author_system_routes(assignment, platform, altered, timing)

    def test_json_original_ir_projection_retains_output_copies(self):
        db, assignment, ir, _ = self._inputs()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, value in (("db", db), ("assignment", assignment), ("original", ir.value)):
                (root / (name + ".json")).write_text(json.dumps(value))
            project_sta_path_database(root / "db.json", root / "assignment.json", root / "projected.json",
                                      original_ir_path=root / "original.json")
            self.assertEqual(len(json.loads((root / "projected.json").read_text())["output_realizations"]["realizations"]), 5)

    def test_missing_or_rebound_realization_is_rejected(self):
        artifact, _, assignment, _, platform = self._project()
        for mutation in (lambda x: x.pop("output_realizations"),
                         lambda x: x["output_realizations"]["realizations"].pop(1),
                         lambda x: x["output_realizations"]["realizations"][1].update(fpga="c")):
            changed = copy.deepcopy(artifact)
            mutation(changed)
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "timing.json"
                path.write_text(json.dumps(changed))
                with self.assertRaises(ValidationError):
                    load_sta_paths(path, demands_from_assignment(assignment, platform))

    def test_public_schemas_validate_registry_and_normalized_projection(self):
        try:
            import jsonschema
            from referencing import Registry, Resource
        except ImportError:
            self.skipTest("optional JSON Schema validator is not installed")
        root = Path(__file__).resolve().parents[1] / "schemas"
        registry_schema = json.loads((root / "output-realizations-v1.schema.json").read_text())
        resources = Registry().with_resource(registry_schema["$id"], Resource.from_contents(registry_schema))
        artifact, _, assignment, _, platform = self._project()
        schema = json.loads((root / "sta-paths-v1.schema.json").read_text())
        jsonschema.Draft202012Validator(schema, registry=resources).validate(artifact)
        broken = copy.deepcopy(artifact)
        broken["output_realizations"]["realizations"][0].pop("fixed_delay_ns")
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema, registry=resources).validate(broken)
        from emuflow.tdm import build_tdm_schedule
        from tests.test_author_routing import author_router
        routes = route_system_author(assignment, platform, normalize_route_constraints(None, platform), artifact, executable=author_router())
        contract = build_cross_layer_timing_contract(routes, build_tdm_schedule(routes, platform))
        cross_schema = json.loads((root / "cross-layer-timing-v1.schema.json").read_text())
        jsonschema.Draft202012Validator(cross_schema, registry=resources).validate(contract)

    def test_replication_fails_closed_only_when_output_realization_is_needed(self):
        db, assignment, ir, _ = self._inputs()
        with self.assertRaisesRegex(ValidationError, "nonreplicated"):
            project_sta_path_database_value(db, assignment, source_input_sha256="a" * 64,
                original_nets={net["id"]: net for net in ir.value["nets"]}, replication_present=True)
        db["paths"] = [db["paths"][0]]
        db["paths"][0]["endpoint"] = {**_ep("lb", "I"), "object": "lb/I"}
        projected, _ = project_sta_path_database_value(db, assignment, source_input_sha256="a" * 64,
            original_nets={net["id"]: net for net in ir.value["nets"]}, replication_present=True)
        self.assertEqual(projected["output_realizations"]["realizations"], [])


if __name__ == "__main__":
    unittest.main()
