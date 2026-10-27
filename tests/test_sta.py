import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.sta_path_reference import import_sta_path_database_tsv
from emuflow.errors import ValidationError
from emuflow.io import read_json, write_json
from emuflow.ir import EmuIR
from emuflow.partition import PARTITION_ASSIGNMENT_SCHEMA
from emuflow.sta import (
    EMUIR_NET_MAP_HEADER,
    PARTITION_NET_WEIGHTS_INDEX_SCHEMA,
    PARTITION_NET_WEIGHTS_SCHEMA,
    STA_PATH_DATABASE_TSV_HEADER,
    STA_PATH_DATABASE_SCHEMA,
    derive_partition_net_weights,
    import_sta_path_database_tsv_streaming,
    iter_sta_path_database_paths,
    load_partition_net_weights_indexed,
    project_sta_path_database,
    write_emuir_net_map,
)
from emuflow.yosys import import_yosys_json


ROOT = Path(__file__).resolve().parents[1]


def _write_assignment_facts(path, value):
    """Encode small projection inputs in the current selector/index contract."""
    from types import SimpleNamespace
    from tests.phase3_store_fixture import write_current_assignment_fixture
    owners = dict(value.get('instance_assignment', {}))
    assert len(owners) <= 256 and len(value['cut_nets']) <= 4096
    cuts = [dict(row, source_fpgas=row.get('source_fpgas', ['a']),
                 sink_fpgas=row.get('sink_fpgas', ['b']),
                 cut_class=row.get('cut_class', 'register_output'),
                 sink_endpoints=max(1, len(row.get('sink_fpgas', ['b']))))
            for row in value['cut_nets']]
    parts = sorted(set(owners.values()) | {part for row in cuts
                   for part in row['source_fpgas']+row['sink_fpgas']})
    facts = dict(value, provider='explicit-projection-facts', seed=0, constraints={},
        instance_assignment=owners, cut_nets=sorted(cuts,key=lambda row:row['net']),
        cluster_assignment={f'c{i:06d}':owners[name] for i,name in enumerate(sorted(owners))},
        partitions=[dict(fpga=part, clusters=[]) for part in parts])
    source = SimpleNamespace(value={'instances':[{'id':name} for name in owners]})
    write_current_assignment_fixture(path, source, facts)


class StaAdapterTest(unittest.TestCase):
    def test_retired_vivado_provider_is_rejected_before_loading_sources(self) -> None:
        for importer in (import_sta_path_database_tsv, import_sta_path_database_tsv_streaming):
            with self.subTest(importer=importer.__name__), \
                 patch("emuflow.sta.EmuIR.load", side_effect=AssertionError("source was loaded")):
                with self.assertRaisesRegex(ValidationError, "provider is invalid"):
                    importer(Path("missing.tsv"), Path("missing.emuir"), Path("out.json"),
                             provider="vivado-get-timing-path-database-v1")

    def test_unknown_source_net_is_rejected_by_current_import(self) -> None:
        ir = import_yosys_json(ROOT / "examples/yosys/counter.json", top="counter", clocks=["clk"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, measured, output = root / "ir.emuir.sqlite", root / "paths.tsv", root / "out.json"
            ir.save(source)
            measured.write_text(STA_PATH_DATABASE_TSV_HEADER + "\n" + "\t".join([
                "path0".encode().hex(), "clk".encode().hex(), "10", "0", "1",
                "missing".encode().hex(),
            ]) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "unknown EmuIR nets"):
                import_sta_path_database_tsv_streaming(
                    measured, source, output, provider="opensta-fpga-path-database-v1",
                )
            self.assertFalse(output.exists())

    def test_indexed_timing_handoff_has_no_engine_audit_or_redundant_ir_load(self) -> None:
        ir = import_yosys_json(ROOT / "examples/yosys/counter.json", top="counter", clocks=["clk"])
        net_id = ir.value["nets"][0]["id"]
        connect = sqlite3.connect

        def without_audits(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_authorizer(
                lambda action, name, *unused: sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_PRAGMA
                and name.lower() in {"integrity_check", "quick_check"}
                else sqlite3.SQLITE_OK
            )
            return connection

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "design.emuir.sqlite"
            ir.save(ir_path)
            tsv = root / "paths.tsv"
            tsv.write_text(STA_PATH_DATABASE_TSV_HEADER + "\n" + "\t".join([
                "path0".encode().hex(), "clk".encode().hex(), "10", "-2", "1", net_id.encode().hex()
            ]) + "\n", encoding="utf-8")
            database_path = root / "paths.json"
            expected_weights = root / "expected.json"
            actual_weights = root / "actual.json"
            with patch("emuflow.sta.sqlite3.connect", without_audits):
                imported = import_sta_path_database_tsv_streaming(
                    tsv, ir_path, database_path,
                    provider="opensta-fpga-path-database-v1", _ir=ir,
                )
                records = list(iter_sta_path_database_paths(database_path, read_json(database_path)))
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["path_nets"], [net_id])
                from emuflow import sta
                self.assertFalse(hasattr(sta, 'iter_selected_sta_path_database_paths'))
                derive_partition_net_weights(database_path, ir_path, expected_weights)
                with patch("emuflow.sta.EmuIR.load", side_effect=AssertionError("unused whole-graph load")):
                    derive_partition_net_weights(
                        database_path, ir_path, actual_weights, _validation=imported.validation,
                    )
                self.assertEqual(load_partition_net_weights_indexed(expected_weights), load_partition_net_weights_indexed(actual_weights))
                self.assertEqual(
                    (root / read_json(expected_weights)["payloads"]["weights"]["path"]).read_bytes(),
                    (root / read_json(actual_weights)["payloads"]["weights"]["path"]).read_bytes(),
                )

    def test_net_map_reuses_validated_graph_without_reloading(self) -> None:
        ir = import_yosys_json(
            ROOT / "examples" / "yosys" / "counter.json",
            top="counter", clocks=["clk"],
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "design.emuir.sqlite"
            ir.save(ir_path)
            ordinary = root / "ordinary.tsv"
            reused = root / "reused.tsv"
            report = write_emuir_net_map(ir_path, ordinary)
            with patch(
                "emuflow.sta.EmuIR.load",
                side_effect=AssertionError("duplicate graph load"),
            ):
                reused_report = write_emuir_net_map(ir_path, reused, _ir=ir)
            self.assertEqual(ordinary.read_bytes(), reused.read_bytes())
            self.assertEqual(report["nets"], reused_report["nets"])
            self.assertEqual(report["design"], reused_report["design"])

    def test_opensta_escaped_hierarchy_resolves_structured_endpoints(
        self,
    ) -> None:
        ir = EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "escaped_hierarchy",
                    "top": "escaped_hierarchy",
                    "source_format": "test",
                },
                "ports": [],
                "instances": [
                    {
                        "id": "$flatten\\u.launch",
                        "name": "$flatten\\u.launch",
                        "type": "FDRE",
                        "resources": {"ff": 1},
                        "parameters": {},
                        "attributes": {},
                        "constant_connections": [],
                    },
                    {
                        "id": "$flatten\\u.capture",
                        "name": "$flatten\\u.capture",
                        "type": "FDRE",
                        "resources": {"ff": 1},
                        "parameters": {},
                        "attributes": {},
                        "constant_connections": [],
                    },
                ],
                "nets": [
                    {
                        "id": "data",
                        "name": "data",
                        "width": 1,
                        "cut_class": "register_output",
                        "drivers": [
                            {
                                "instance": "$flatten\\u.launch",
                                "port": "Q",
                                "bit": 0,
                            }
                        ],
                        "sinks": [
                            {
                                "instance": "$flatten\\u.capture",
                                "port": "D",
                                "bit": 0,
                            }
                        ],
                        "attributes": {},
                    }
                ],
                "clocks": [],
                "warnings": [],
            }
        )
        canonical_start = "$flatten\\u.launch/Q"
        canonical_end = "$flatten\\u.capture/D"
        opensta_start = canonical_start.replace("\\", "\\\\")
        opensta_end = canonical_end.replace("\\", "\\\\")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "ir.json"
            input_path = root / "paths.tsv"
            output_path = root / "paths.json"
            write_json(ir_path, ir.value)
            input_path.write_text(
                STA_PATH_DATABASE_TSV_HEADER
                + "\n"
                + "\t".join(
                    (
                        (
                            f"{opensta_start}->{opensta_end}#00000000"
                        ).encode().hex(),
                        "clk".encode().hex(),
                        "4.0",
                        "-0.5",
                        "4.5",
                        "data".encode().hex(),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            report = import_sta_path_database_tsv(
                input_path,
                ir_path,
                output_path,
                provider="opensta-fpga-path-database-v1",
            )
            self.assertNotIn("_value", report)
            with patch(
                "emuflow.sta.EmuIR.load",
                side_effect=AssertionError("preloaded EmuIR was reparsed"),
            ):
                reused = import_sta_path_database_tsv(
                    input_path,
                    ir_path,
                    output_path,
                    provider="opensta-fpga-path-database-v1",
                    _ir=ir,
                    _return_value=True,
                )
            self.assertIsInstance(reused.pop("_value"), dict)
            path = json.loads(output_path.read_text(encoding="utf-8"))[
                "paths"
            ][0]
        self.assertEqual(report["structured_endpoint_paths"], 1)
        self.assertEqual(
            path["startpoint"],
            {
                "object": canonical_start,
                "instance": "$flatten\\u.launch",
                "port": "Q",
                "bit": 0,
            },
        )
        self.assertEqual(path["endpoint"]["object"], canonical_end)
        self.assertEqual(path["endpoint"]["instance"], "$flatten\\u.capture")

    def test_ambiguous_opensta_escaped_hierarchy_is_rejected(self) -> None:
        instances = []
        for instance_id in ("unit\\ff", "unit\\\\ff"):
            instances.append(
                {
                    "id": instance_id,
                    "name": instance_id,
                    "type": "LUT1",
                    "resources": {"lut": 1},
                    "parameters": {"INIT": "10"},
                    "attributes": {},
                    "constant_connections": [
                        {"port": "I0", "bit": 0, "value": "0"}
                    ],
                }
            )
        ir = EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "ambiguous_escape",
                    "top": "ambiguous_escape",
                    "source_format": "test",
                },
                "ports": [],
                "instances": instances,
                "nets": [],
                "clocks": [],
                "warnings": [],
            }
        )
        ir.value['nets'].append(dict(id='data', drivers=[], sinks=[], cut_class='undriven'))
        ambiguous = 'unit' + '\\' * 2 + 'ff/I0'
        identity = f'{ambiguous}->{ambiguous}#00000000'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "ir.emuir.sqlite"
            input_path = root / "paths.tsv"
            ir.save(ir_path)
            input_path.write_text(
                STA_PATH_DATABASE_TSV_HEADER + "\n" + "\t".join([
                    identity.encode().hex(), b'clk'.hex(), '10', '-1', '0', b'data'.hex(),
                ]) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "alias is ambiguous"):
                import_sta_path_database_tsv_streaming(
                    input_path,
                    ir_path,
                    root / "paths.json",
                    provider="opensta-fpga-path-database-v1",
                )
            self.assertFalse((root / "paths.json").exists())


    def test_projection_keeps_member_specific_multicast_sinks(self) -> None:
        normalization = {
            "positive_slack_scale_ns": 1.0,
            "negative_slack_scale_ns": 1.0,
            "max_clock_period_ns": 10.0,
        }
        base = {
            "clock_domain": "clk",
            "clock_period_ns": 10.0,
            "slack_ns": -1.0,
            "fixed_delay_ns": 11.0,
            "path_nets": ["cut"],
            "normalized_slack": -0.1,
            "startpoint": {
                "object": "src/Q",
                "instance": "src",
                "port": "Q",
                "bit": 0,
            },
        }
        database = {
            "schema": STA_PATH_DATABASE_SCHEMA,
            "design": "multicast",
            "source": {"provider": "opensta-fpga-path-database-v1"},
            "normalization": normalization,
            "paths": [
                {
                    **base,
                    "id": "src/Q->sink_b/D#00000000",
                    "endpoint": {
                        "object": "sink_b/D",
                        "instance": "sink_b",
                        "port": "D",
                        "bit": 0,
                    },
                },
                {
                    **base,
                    "id": "src/Q->sink_c/D#00000001",
                    "endpoint": {
                        "object": "sink_c/D",
                        "instance": "sink_c",
                        "port": "D",
                        "bit": 0,
                    },
                },
                {
                    **base,
                    "id": "src/Q->local/D#00000002",
                    "endpoint": {
                        "object": "local/D",
                        "instance": "local",
                        "port": "D",
                        "bit": 0,
                    },
                },
            ],
        }
        assignment = {
            "schema": PARTITION_ASSIGNMENT_SCHEMA,
            "design": "multicast",
            "platform": "fixture",
            "instance_assignment": {
                "src": "a",
                "sink_b": "b",
                "sink_c": "c",
                "local": "a",
            },
            "cut_nets": [
                {
                    "net": "cut",
                    "source_fpgas": ["a"],
                    "sink_fpgas": ["b", "c"],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database_path = root / "database.json"
            assignment_path = root / "assignment.json"
            output_path = root / "projected.json"
            write_json(database_path, database)
            _write_assignment_facts(assignment_path, assignment)
            report = project_sta_path_database(
                database_path, assignment_path, output_path
            )
            projected = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(report["projected_paths"], 2)
        self.assertEqual(report["compressed_paths"], 2)
        self.assertEqual(
            {
                path["cut_transitions"][0]["to"]
                for path in projected["paths"]
            },
            {"b", "c"},
        )

    def test_partition_independent_database_projects_candidate_cuts(
        self,
    ) -> None:
        ir = import_yosys_json(
            ROOT / "examples" / "yosys" / "counter.json",
            top="counter",
            clocks=["clk"],
        )
        net_ids = [net["id"] for net in ir.value["nets"][:3]]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "ir.json"
            map_path = root / "net-map.tsv"
            tsv_path = root / "database.tsv"
            database_path = root / "database.json"
            assignment_a_path = root / "assignment-a" / "assignment.json"
            assignment_b_path = root / "assignment-b" / "assignment.json"
            projected_a_path = root / "projected-a.json"
            projected_b_path = root / "projected-b.json"
            write_json(ir_path, ir.value)
            map_report = write_emuir_net_map(ir_path, map_path)
            self.assertEqual(map_report["nets"], len(ir.value["nets"]))
            map_lines = map_path.read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(map_lines[0], EMUIR_NET_MAP_HEADER)
            self.assertEqual(
                bytes.fromhex(map_lines[1].split("\t")[1]).decode(),
                net_ids[0],
            )
            rows = [
                [
                    "q_reg[0]/Q->q_reg[1]/D#00000000".encode().hex(),
                    "clk".encode().hex(),
                    "10.0",
                    "-0.5",
                    "9.5",
                    net_ids[0].encode().hex(),
                ],
                [
                    "path db 1".encode().hex(),
                    "clk".encode().hex(),
                    "5.0",
                    "-2.0",
                    "4.0",
                    net_ids[1].encode().hex(),
                ],
                [
                    "path db 2".encode().hex(),
                    "clk".encode().hex(),
                    "8.0",
                    "1.0",
                    "7.0",
                    net_ids[2].encode().hex(),
                ],
            ]
            tsv_path.write_text(
                STA_PATH_DATABASE_TSV_HEADER
                + "\n"
                + "\n".join("\t".join(row) for row in rows)
                + "\n",
                encoding="utf-8",
            )
            imported = import_sta_path_database_tsv(
                tsv_path, ir_path, database_path, provider="opensta-fpga-path-database-v1"
            )
            indexed_ir, indexed_database = root / "weights.emuir.sqlite", root / "weights-paths.json"
            ir.save(indexed_ir)
            import_sta_path_database_tsv_streaming(
                tsv_path, indexed_ir, indexed_database, provider="opensta-fpga-path-database-v1"
            )
            weights_path = root / "weights.json"
            weights_report = derive_partition_net_weights(
                indexed_database,
                indexed_ir,
                weights_path,
                criticality_scale=9.0,
                criticality_exponent=2.0,
            )
            weights = json.loads(weights_path.read_text(encoding="utf-8"))
            indexed_weights = load_partition_net_weights_indexed(weights_path)
            database = json.loads(
                database_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                database["schema"], STA_PATH_DATABASE_SCHEMA
            )
            self.assertEqual(
                database["paths"][0]["path_nets"], [net_ids[0]]
            )
            self.assertEqual(
                database["paths"][0]["startpoint"]["instance"],
                "q_reg[0]",
            )
            self.assertEqual(
                database["paths"][0]["endpoint"]["instance"],
                "q_reg[1]",
            )
            self.assertEqual(
                database["normalization"],
                {
                    "positive_slack_scale_ns": 1.0,
                    "negative_slack_scale_ns": 2.0,
                    "max_clock_period_ns": 10.0,
                },
            )
            for path, cut_net in (
                (assignment_a_path, net_ids[0]),
                (assignment_b_path, net_ids[1]),
            ):
                assignment = {
                    "schema": PARTITION_ASSIGNMENT_SCHEMA,
                    "design": "counter",
                    "platform": "fixture",
                    "cut_nets": [{"net": cut_net}],
                }
                _write_assignment_facts(path, assignment)
            report_a = project_sta_path_database(
                database_path, assignment_a_path, projected_a_path
            )
            report_b = project_sta_path_database(
                database_path, assignment_b_path, projected_b_path
            )
            projected_a = json.loads(
                projected_a_path.read_text(encoding="utf-8")
            )
            projected_b = json.loads(
                projected_b_path.read_text(encoding="utf-8")
            )
        self.assertEqual(imported["paths"], 3)
        self.assertEqual(
            weights["schema"], PARTITION_NET_WEIGHTS_INDEX_SCHEMA
        )
        self.assertEqual(weights_report["weighted_nets"], 3)
        self.assertEqual(indexed_weights[net_ids[0]], 10.0)
        self.assertEqual(indexed_weights[net_ids[1]], 10.0)
        self.assertAlmostEqual(
            indexed_weights[net_ids[2]], 7.890625
        )
        self.assertEqual(report_a["projected_paths"], 1)
        self.assertEqual(report_b["projected_paths"], 1)
        self.assertEqual(
            projected_a["paths"][0]["cut_nets"], [net_ids[0]]
        )
        self.assertEqual(
            projected_b["paths"][0]["cut_nets"], [net_ids[1]]
        )
        self.assertEqual(
            projected_a["normalization"], database["normalization"]
        )
        self.assertEqual(
            projected_b["normalization"], database["normalization"]
        )
        self.assertEqual(
            projected_a["paths"][0]["normalized_slack"], -0.025
        )
        self.assertEqual(
            projected_b["paths"][0]["normalized_slack"], -0.2
        )

    def test_projection_identity_survives_atomic_checkpoint_move(self) -> None:
        database = {
            "schema": STA_PATH_DATABASE_SCHEMA,
            "design": "movable",
            "source": {"provider": "fixture"},
            "normalization": {
                "positive_slack_scale_ns": 1.0,
                "negative_slack_scale_ns": 1.0,
                "max_clock_period_ns": 10.0,
            },
            "paths": [
                {
                    "id": "a/Q->b/D#00000000",
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "slack_ns": 1.0,
                    "fixed_delay_ns": 9.0,
                    "normalized_slack": 1.0,
                    "path_nets": ["cut"],
                }
            ],
        }
        assignment = {
            "schema": PARTITION_ASSIGNMENT_SCHEMA,
            "design": "movable",
            "platform": "fixture",
            "cut_nets": [
                {
                    "net": "cut",
                    "source_fpgas": ["a"],
                    "sink_fpgas": ["b"],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "staging"
            staging.mkdir()
            write_json(staging / "database.json", database)
            _write_assignment_facts(staging / "assignment.json", assignment)
            project_sta_path_database(
                staging / "database.json",
                staging / "assignment.json",
                staging / "projected.json",
            )
            original = read_json(staging / "projected.json")
            objects = root / "objects"
            staging.rename(objects)
            project_sta_path_database(
                objects / "database.json",
                objects / "assignment.json",
                objects / "rebuilt.json",
            )
            rebuilt = read_json(objects / "rebuilt.json")
        self.assertEqual(original, rebuilt)
        self.assertEqual(
            set(original["source"]), {"provider", "input_sha256"}
        )


if __name__ == "__main__":
    unittest.main()
    derive_partition_net_weights,
