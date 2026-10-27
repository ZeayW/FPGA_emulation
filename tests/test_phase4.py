from tests.native_route_fixture import write_assignment_cuts
from tests.native_route_fixture import route_system_native
from contextlib import closing
import copy
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.errors import EmuFlowError, ValidationError
from emuflow.io import file_sha256, read_json
from emuflow.partition import (PARTITION_ASSIGNMENT_SCHEMA)
from tests.native_partition_facts import (build_clusters, normalize_partition_constraints)
from tests.partition_reference import assign_clusters
from emuflow.phase4 import run_phase4
from emuflow.platform import Platform
from emuflow.routing import (
    SYSTEM_ROUTES_SCHEMA,
    demands_from_assignment,
    normalize_route_constraints,
    validate_system_routes,
)
from tests.routing_reference import exact_route_tree_selection
from emuflow.timing_routing import (GLOBAL_CANDIDATE_PROVIDER, compress_sta_paths, load_sta_paths, normalize_sta_paths, reconstruct_system_route_timing, validate_native_system_routes)
from emuflow.system_route_storage import SYSTEM_ROUTE_STORAGE_SCHEMA, SYSTEM_ROUTE_USER_VERSION, SystemRoutesView, read_system_routes
from tests.native_route_seal_fixture import write_route_store_fixture
from emuflow.yosys import import_yosys_json
from tests.native_build import emuflow_native, tlr_router


ROOT = Path(__file__).resolve().parents[1]
PLATFORM_PATH = ROOT / "platforms" / "virtual" / "xcvu3p_2fpga_p2p.json"


def _storage_routes(generation: int) -> dict:
    route = {
        "id": "demand-0",
        "net": "n0",
        "source": "a",
        "sinks": ["b"],
        "width_bits": 1,
        "tree_edges": [{"link": "ab", "from": "a", "to": "b"}],
    }
    demand = {
        key: copy.deepcopy(route[key])
        for key in ("id", "net", "source", "sinks", "width_bits")
    }
    return {
        "schema": SYSTEM_ROUTES_SCHEMA,
        "design": "route-store-lifecycle",
        "generation": generation,
        "demands": [demand],
        "routes": [route],
    }


def _downgrade_route_relation_to_v2(store_path: Path) -> None:
    with closing(sqlite3.connect(store_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        rows = list(connection.execute("SELECT * FROM routes ORDER BY seq"))
        connection.executescript(
            "DROP INDEX routes_net_order;"
            "DROP INDEX routes_demand_order;"
            "ALTER TABLE routes RENAME TO routes_v3;"
            "CREATE TABLE routes("
            "seq INTEGER PRIMARY KEY,demand_id TEXT NOT NULL UNIQUE,"
            "net_id TEXT NOT NULL UNIQUE,source_fpga TEXT NOT NULL,"
            "dependency_level INTEGER,"
            "combinational_dependency_depth INTEGER,"
            "record_json TEXT NOT NULL,CHECK(json_valid(record_json)));"
        )
        for row in rows:
            scalar = {"width_bits": int(row["width_bits"])}
            for field in (
                "transport_round",
                "cut_class",
                "max_latency_cycles",
                "predicted_max_delay_ns",
            ):
                if row[field] is not None:
                    scalar[field] = row[field]
            connection.execute(
                "INSERT INTO routes VALUES(?,?,?,?,?,?,?)",
                (
                    row["seq"],
                    row["demand_id"],
                    row["net_id"],
                    row["source_fpga"],
                    row["dependency_level"],
                    row["combinational_dependency_depth"],
                    json.dumps(scalar, separators=(",", ":"), sort_keys=True),
                ),
            )
        connection.executescript(
            "DROP TABLE routes_v3;"
            "CREATE INDEX routes_net_order ON routes(net_id,seq);"
            "CREATE INDEX routes_demand_order ON routes(demand_id,seq);"
            "PRAGMA user_version=2;"
        )


def _platform_value(name, fpga_ids, links):
    return {
        "schema": "emuflow.boarddb/v1",
        "platform": {
            "name": name,
            "kind": "virtual",
            "description": "Phase 4 test topology",
        },
        "fpgas": [
            {
                "id": fpga_id,
                "part": "xcvu3p-ffvc1517-2-e",
                "utilization_limit": 1.0,
                "capacity": {"lut": 100, "ff": 100},
            }
            for fpga_id in fpga_ids
        ],
        "links": links,
    }


def _link(
    link_id,
    left,
    right,
    lanes=1,
    direction="full_duplex",
    latency=1,
):
    return {
        "id": link_id,
        "endpoints": [left, right],
        "direction": direction,
        "mode": "abstract",
        "data_lanes_per_direction": lanes,
        "fabric_clock_mhz": 250.0,
        "latency_cycles": latency,
    }


def _assignment(platform, cuts):
    return {
        "schema": PARTITION_ASSIGNMENT_SCHEMA,
        "design": "route_test",
        "platform": platform.name,
        "cut_nets": [
            {
                "net": net,
                "cut_class": "register_output",
                "source_fpgas": [source],
                "sink_fpgas": list(sinks),
                "sink_endpoints": len(sinks),
            }
            for net, source, sinks in sorted(cuts)
        ],
    }


class Phase4Test(unittest.TestCase):

    def test_route_store_has_no_python_production_publisher(self) -> None:
        from emuflow import system_route_storage
        self.assertFalse(hasattr(system_route_storage, 'write_system_route_storage'))
        self.assertFalse(hasattr(system_route_storage, '_typed_route_scalar_values'))

    def test_route_store_v1_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            selector_path = root / "routes.json"
            logical = _storage_routes(0)
            manifest = write_route_store_fixture(selector_path, logical)
            store_path = root / manifest["artifact"]
            _downgrade_route_relation_to_v2(store_path)
            legacy_header = {
                key: value
                for key, value in logical.items()
                if key not in {"demands", "routes"}
            }
            legacy_sections = {
                key: manifest["sections"][key]
                for key in ("demands", "routes", "timing_paths")
            }
            with closing(sqlite3.connect(store_path)) as connection, connection:
                connection.executescript(
                    "DROP TABLE route_master_selections;"
                    "DROP TABLE refinement_batch_paths;"
                    "PRAGMA user_version=1;"
                )
                connection.executemany(
                    "UPDATE metadata SET value=? WHERE key=?",
                    (
                        (json.dumps(legacy_header), "header"),
                        (json.dumps(legacy_sections), "sections"),
                    ),
                )
            store_sha256 = file_sha256(store_path)
            legacy_store = root / f"routes.{store_sha256}.sqlite"
            store_path.rename(legacy_store)
            legacy_manifest = {
                **manifest,
                "user_version": 1,
                "header": legacy_header,
                "sections": legacy_sections,
                "artifact": legacy_store.name,
                "store_bytes": legacy_store.stat().st_size,
                "store_sha256": store_sha256,
            }
            selector_path.write_text(
                json.dumps(legacy_manifest), encoding="utf-8"
            )
            self.assertEqual(read_json(selector_path), logical)

    def test_route_store_v2_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            selector_path = root / "routes.json"
            logical = _storage_routes(0)
            manifest = write_route_store_fixture(selector_path, logical)
            store_path = root / manifest["artifact"]
            _downgrade_route_relation_to_v2(store_path)
            store_sha256 = file_sha256(store_path)
            legacy_store = root / f"routes.{store_sha256}.sqlite"
            store_path.rename(legacy_store)
            legacy_manifest = {
                **manifest,
                "user_version": 2,
                "artifact": legacy_store.name,
                "store_bytes": legacy_store.stat().st_size,
                "store_sha256": store_sha256,
            }
            selector_path.write_text(
                json.dumps(legacy_manifest), encoding="utf-8"
            )
            self.assertEqual(read_json(selector_path), logical)

    def test_route_store_v3_owns_route_scalars_as_typed_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            selector_path = root / "routes.json"
            logical = _storage_routes(0)
            logical["routes"][0].update(
                {
                    "transport_round": 2,
                    "cut_class": "register_output",
                    "dependency_level": 0,
                    "combinational_dependency_depth": 0,
                    "predecessor_cut_nets": [],
                    "max_latency_cycles": 3,
                    "predicted_max_delay_ns": 4.5,
                }
            )
            logical["demands"][0].update(
                {
                    "transport_round": 2,
                    "cut_class": "register_output",
                    "dependency_level": 0,
                    "combinational_dependency_depth": 0,
                    "predecessor_cut_nets": [],
                }
            )
            manifest = write_route_store_fixture(selector_path, logical)
            self.assertEqual(manifest["user_version"], SYSTEM_ROUTE_USER_VERSION)
            with closing(sqlite3.connect(root / manifest["artifact"])) as connection, connection:
                columns = tuple(
                    row[1] for row in connection.execute("PRAGMA table_info(routes)")
                )
                row = connection.execute(
                    "SELECT width_bits,transport_round,cut_class,"
                    "dependency_level,combinational_dependency_depth,"
                    "max_latency_cycles,predicted_max_delay_ns FROM routes"
                ).fetchone()
            self.assertNotIn("record_json", columns)
            self.assertEqual(row, (1, 2, "register_output", 0, 0, 3, 4.5))
            self.assertEqual(read_json(selector_path), logical)


    def test_route_store_v3_rejects_schema_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            selector_path = root / "routes.json"
            manifest = write_route_store_fixture(
                selector_path, _storage_routes(0)
            )
            store_path = root / manifest["artifact"]
            with closing(sqlite3.connect(store_path)) as connection, connection:
                connection.execute("ALTER TABLE routes ADD COLUMN hidden_json TEXT")
            store_sha256 = file_sha256(store_path)
            extended_path = root / f"routes.{store_sha256}.sqlite"
            store_path.rename(extended_path)
            extended_manifest = {
                **manifest,
                "artifact": extended_path.name,
                "store_bytes": extended_path.stat().st_size,
                "store_sha256": store_sha256,
            }
            selector_path.write_text(
                json.dumps(extended_manifest), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                ValidationError, "routes relation schema"
            ):
                read_system_routes(selector_path)


    def test_route_store_rejects_renamed_content_addressed_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            selector_path = root / "routes.json"
            manifest = write_route_store_fixture(
                selector_path, _storage_routes(0)
            )
            original = root / manifest["artifact"]
            renamed = root / f"routes.{'0' * 64}.sqlite"
            renamed.write_bytes(original.read_bytes())
            forged = dict(manifest)
            forged["artifact"] = renamed.name
            selector_path.write_text(json.dumps(forged), encoding="utf-8")

            with self.assertRaisesRegex(
                ValidationError, "artifact identity disagrees"
            ):
                read_system_routes(selector_path)

    def test_route_store_reader_rejects_artifact_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            selector_path = root / "routes.json"
            manifest = write_route_store_fixture(
                selector_path, _storage_routes(0)
            )
            victim = root.parent / "route-store-victim.sqlite"
            victim.write_bytes(b"keep")
            forged = dict(manifest)
            forged["artifact"] = f"../{victim.name}"
            selector_path.write_text(json.dumps(forged), encoding="utf-8")
            old_selector = selector_path.read_bytes()
            try:
                with self.assertRaisesRegex(
                    ValidationError, "store path is invalid"
                ):
                    read_system_routes(selector_path)
                self.assertEqual(selector_path.read_bytes(), old_selector)
                self.assertEqual(victim.read_bytes(), b"keep")
            finally:
                victim.unlink(missing_ok=True)

    def test_native_router_materializes_valid_zero_demand_artifact(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "zero-demand",
                ["a", "b"],
                [_link("ab", "a", "b", direction="half_duplex")],
            )
        )
        assignment = _assignment(platform, [])
        constraints = normalize_route_constraints(None, platform)
        routes = route_system_native(
            assignment,
            platform,
            constraints,
            executable=str(tlr_router()),
        )

        checked = validate_native_system_routes(assignment, platform, routes)
        self.assertEqual(routes["routes"], [])
        self.assertEqual(routes["demands"], [])
        self.assertEqual(routes["metrics"]["estimated_max_tdm_ratio"], 0)
        self.assertEqual(
            routes["direction_locks"],
            [{"group": 0, "link": "ab", "from": "a", "to": "b"}],
        )
        self.assertEqual(checked["status"], "pass")
        self.assertEqual(checked["demands"], 0)
        self.assertEqual(checked["total_link_bit_hops"], 0)


    def test_path_compression_keeps_distinct_required_times(self) -> None:
        normalized = normalize_sta_paths(
            {
                "schema": "emuflow.sta-paths/v1",
                "design": "required_time_compression",
                "paths": [
                    {
                        "id": "p0",
                        "clock_domain": "clk",
                        "clock_period_ns": 10.0,
                        "slack_ns": 1.0,
                        "fixed_delay_ns": 5.0,
                        "cut_nets": ["n0"],
                    },
                    {
                        "id": "p1",
                        "clock_domain": "clk",
                        "clock_period_ns": 10.0,
                        "slack_ns": 1.0,
                        "fixed_delay_ns": 6.0,
                        "cut_nets": ["n0"],
                    },
                ],
            },
            [{"net": "n0", "source": "a", "sinks": ["b"]}],
        )
        compressed = compress_sta_paths(normalized)
        self.assertEqual(compressed["compression"]["compressed_paths"], 2)
        self.assertIn("required-time", compressed["compression"]["lossless_by"])

    def test_timing_oblivious_route_retains_independent_timing_records(
        self,
    ) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "evaluated_baseline",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=4)],
            )
        )
        assignment = _assignment(platform, [("n0", "a", ["b"])])
        timing = {
            "schema": "emuflow.sta-paths/v1",
            "design": "route_test",
            "paths": [
                {
                    "id": "p0",
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "slack_ns": 9.0,
                    "fixed_delay_ns": 0.0,
                    "cut_nets": ["n0"],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            assignment_path = root / "assignment.json"
            platform_path = root / "platform.json"
            timing_path = root / "timing.json"
            write_assignment_cuts(root / "assignment.sqlite", assignment, platform)
            assignment_path.write_text(json.dumps(assignment), encoding="utf-8")
            platform_path.write_text(
                json.dumps(platform.to_dict()), encoding="utf-8"
            )
            timing_path.write_text(json.dumps(timing), encoding="utf-8")
            report = run_phase4(
                root / "assignment.sqlite",
                platform_path,
                root / "phase4",
                timing_paths_path=timing_path,
                timing_driven=False,
                provider=GLOBAL_CANDIDATE_PROVIDER,
                router=str(tlr_router()),
            )
            routes = read_json(root / "phase4/routes.json")
            self.assertEqual(
                routes["provider"], GLOBAL_CANDIDATE_PROVIDER
            )
            self.assertEqual(
                routes["timing_evaluation"],
                {
                    "mode": "post-route-evaluation-only",
                    "optimization_enabled": False,
                },
            )
            self.assertEqual(len(routes["timing"]["paths"]), 1)
            self.assertEqual(report["validation"]["timing_paths_original"], 1)
            self.assertEqual(
                report["candidate_generation"],
                {
                    "requested_workers": 1,
                    "ordering": "demand-index-then-generator-index",
                    "route_artifact_deterministic": True,
                    "validation_dataflow": (
                        "streamed-candidate-domain-footprint-v1"
                    ),
                },
            )
            normalized = load_sta_paths(timing_path, routes["demands"])
            self.assertEqual(
                validate_native_system_routes(
                    assignment, platform, routes, normalized
                )["status"],
                "pass",
            )
            tampered = copy.deepcopy(routes)
            tampered["timing_evaluation"]["optimization_enabled"] = True
            with self.assertRaisesRegex(
                ValidationError, "evaluation metadata"
            ):
                validate_native_system_routes(
                    assignment, platform, tampered, normalized
                )

    def test_cpp_router_enforces_source_to_sink_hop_limit(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "hop_bounded_routes",
                ["a", "b", "c", "d"],
                [
                    _link("ab", "a", "b", lanes=4),
                    _link("bc", "b", "c", lanes=4),
                    _link("cd", "c", "d", lanes=4),
                    _link("ad", "a", "d", lanes=4),
                ],
            )
        )
        assignment = _assignment(platform, [("n0", "a", ["d"])])
        base = {
            "schema": "emuflow.system-route-constraints/v1",
            "frame_slots": 8,
            "link_delay_ns": {
                "ab": 1.0,
                "bc": 1.0,
                "cd": 1.0,
                "ad": 10.0,
            },
        }
        unconstrained = route_system_native(
            assignment,
            platform,
            normalize_route_constraints(base, platform),
            executable=str(tlr_router()),
        )
        self.assertLessEqual(len(unconstrained["routes"][0]["tree_edges"]), 3)

        bounded = route_system_native(
            assignment,
            platform,
            normalize_route_constraints(
                {**base, "max_route_hops": 2}, platform
            ),
            executable=str(tlr_router()),
        )
        self.assertEqual(
            [edge["link"] for edge in bounded["routes"][0]["tree_edges"]],
            ["ad"],
        )
        self.assertEqual(bounded["metrics"]["max_route_hops_observed"], 1)
        self.assertEqual(
            validate_system_routes(assignment, platform, bounded)["status"],
            "pass",
        )
        timing = compress_sta_paths(
            normalize_sta_paths(
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": "route_test",
                    "paths": [
                        {
                            "id": "n0_path",
                            "clock_domain": "clk",
                            "clock_period_ns": 20.0,
                            "slack_ns": 10.0,
                            "fixed_delay_ns": 0.0,
                            "cut_nets": ["n0"],
                        }
                    ],
                },
                bounded["demands"],
            )
        )
        oracle = exact_route_tree_selection(
            assignment, platform, bounded["constraints"], timing
        )
        self.assertEqual(oracle["trees"]["n0"], [["ad", "a", "d"]])

        forged = copy.deepcopy(unconstrained)
        forged["constraints"] = normalize_route_constraints(
            {**base, "max_route_hops": 2}, platform
        )
        forged["routes"][0]["tree_edges"] = [
            {"link": "ab", "from": "a", "to": "b"},
            {"link": "bc", "from": "b", "to": "c"},
            {"link": "cd", "from": "c", "to": "d"},
        ]
        forged["metrics"]["max_route_hops_observed"] = 3
        with self.assertRaisesRegex(ValidationError, "above maximum"):
            validate_system_routes(assignment, platform, forged)

        line_platform = Platform.from_dict(
            _platform_value(
                "hop_bounded_infeasible",
                ["a", "b", "c", "d"],
                [
                    _link("ab", "a", "b", lanes=4),
                    _link("bc", "b", "c", lanes=4),
                    _link("cd", "c", "d", lanes=4),
                ],
            )
        )
        with self.assertRaisesRegex(EmuFlowError, "infeasible"):
            route_system_native(
                _assignment(line_platform, [("n0", "a", ["d"])]),
                line_platform,
                normalize_route_constraints(
                    {
                        "schema": "emuflow.system-route-constraints/v1",
                        "max_route_hops": 2,
                    },
                    line_platform,
                ),
                executable=str(tlr_router()),
            )

    def test_route_constraint_rejects_invalid_hop_limit(self) -> None:
        platform = Platform.from_dict(
            _platform_value("hop_limit", ["a", "b"], [_link("ab", "a", "b")])
        )
        for invalid in (0, -1, True, 1.5, "2"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValidationError, "max_route_hops"
            ):
                normalize_route_constraints(
                    {
                        "schema": "emuflow.system-route-constraints/v1",
                        "max_route_hops": invalid,
                    },
                    platform,
                )

    def test_cpp_router_preserves_direction_exact_link_delays(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "asymmetric_diamond",
                ["a", "b", "c", "d"],
                [
                    _link("ab", "a", "b"),
                    _link("bd", "b", "d"),
                    _link("ac", "a", "c"),
                    _link("cd", "c", "d"),
                ],
            )
        )
        assignment = _assignment(
            platform,
            [("forward", "a", ["d"]), ("reverse", "d", ["a"])],
        )
        timing = compress_sta_paths(
            normalize_sta_paths(
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": "route_test",
                    "paths": [
                        {
                            "id": net,
                            "clock_domain": "clk",
                            "clock_period_ns": 20.0,
                            "slack_ns": 10.0,
                            "fixed_delay_ns": 0.0,
                            "cut_nets": [net],
                        }
                        for net in ("forward", "reverse")
                    ],
                },
                demands_from_assignment(assignment, platform),
            )
        )
        constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 32,
                "directed_link_delay_ns": {
                    "ab": {"a": {"b": 1.0}, "b": {"a": 5.0}},
                    "bd": {"b": {"d": 1.0}, "d": {"b": 5.0}},
                    "ac": {"a": {"c": 5.0}, "c": {"a": 1.0}},
                    "cd": {"c": {"d": 5.0}, "d": {"c": 1.0}},
                },
            },
            platform,
        )
        routes = route_system_native(
            assignment,
            platform,
            constraints,
            timing,
            executable=str(tlr_router()),
            provider=GLOBAL_CANDIDATE_PROVIDER,
        )
        by_net = {route["net"]: route for route in routes["routes"]}
        self.assertEqual(
            {edge["link"] for edge in by_net["forward"]["tree_edges"]},
            {"ab", "bd"},
        )
        self.assertEqual(
            {edge["link"] for edge in by_net["reverse"]["tree_edges"]},
            {"ac", "cd"},
        )
        self.assertEqual(by_net["forward"]["predicted_max_delay_ns"], 2.0)
        self.assertEqual(by_net["reverse"]["predicted_max_delay_ns"], 2.0)

    def test_tree_edge_sum_tdm_counts_each_multicast_edge_once(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "tree_edge_sum",
                ["a", "b", "c", "d"],
                [
                    _link("ab", "a", "b"),
                    _link("bc", "b", "c"),
                    _link("bd", "b", "d"),
                ],
            )
        )
        assignment = _assignment(
            platform, [("multicast", "a", ["c", "d"])]
        )
        timing = compress_sta_paths(
            normalize_sta_paths(
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": "route_test",
                    "paths": [
                        {
                            "id": "multicast_path",
                            "clock_domain": "clk",
                            "clock_period_ns": 10.0,
                            "slack_ns": 10.0,
                            "fixed_delay_ns": 0.0,
                            "cut_nets": ["multicast"],
                        }
                    ],
                },
                demands_from_assignment(assignment, platform),
            )
        )
        base_value = {
            "schema": "emuflow.system-route-constraints/v1",
            "frame_slots": 8,
            "reroute_rounds": 0,
            "link_delay_ns": {"ab": 1.0, "bc": 1.0, "bd": 1.0},
        }
        longest_sink = route_system_native(
            assignment,
            platform,
            normalize_route_constraints(base_value, platform),
            timing,
            executable=str(tlr_router()),
            provider=GLOBAL_CANDIDATE_PROVIDER,
        )
        edge_sum_value = {**base_value, "tree_edge_sum_tdm": True}
        edge_sum = route_system_native(
            assignment,
            platform,
            normalize_route_constraints(edge_sum_value, platform),
            timing,
            executable=str(tlr_router()),
            provider=GLOBAL_CANDIDATE_PROVIDER,
        )

        self.assertEqual(len(edge_sum["routes"][0]["tree_edges"]), 3)
        self.assertAlmostEqual(
            longest_sink["metrics"]["estimated_worst_tdm_slack_ns"],
            8.0,
        )
        self.assertAlmostEqual(
            edge_sum["metrics"]["estimated_worst_tdm_slack_ns"],
            7.0,
        )
        checked = validate_native_system_routes(
            assignment, platform, edge_sum, timing
        )
        self.assertEqual(checked["status"], "pass")
        self.assertAlmostEqual(
            checked["estimated_worst_tdm_slack_ns"], 7.0
        )

    def test_router_preserves_member_specific_multicast_sink(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "member_sink",
                ["a", "b", "c"],
                [
                    _link("ab", "a", "b"),
                    _link("ac", "a", "c"),
                ],
            )
        )
        assignment = _assignment(
            platform, [("multicast", "a", ["b", "c"])]
        )
        timing = compress_sta_paths(
            normalize_sta_paths(
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": "route_test",
                    "paths": [
                        {
                            "id": "to-c",
                            "clock_domain": "clk",
                            "clock_period_ns": 10.0,
                            "slack_ns": 1.0,
                            "fixed_delay_ns": 9.0,
                            "cut_nets": ["multicast"],
                            "cut_transitions": [
                                {
                                    "net": "multicast",
                                    "from": "a",
                                    "to": "c",
                                }
                            ],
                        }
                    ],
                },
                demands_from_assignment(assignment, platform),
            )
        )
        constraints = normalize_route_constraints(None, platform)
        routes = route_system_native(
            assignment,
            platform,
            constraints,
            timing,
            executable=str(tlr_router()),
            provider=GLOBAL_CANDIDATE_PROVIDER,
        )
        self.assertEqual(
            routes["timing"]["paths"][0]["cut_transitions"],
            [{"net": "multicast", "from": "a", "to": "c"}],
        )
        self.assertEqual(
            validate_native_system_routes(
                assignment, platform, routes, timing
            )["status"],
            "pass",
        )

    def test_sll_capacity_is_not_multiplied_by_frame_slots(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "sll_capacity",
                ["a", "b"],
                [_link("sll", "a", "b", lanes=1)],
            )
        )
        constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 64,
                "sll_links": ["sll"],
            },
            platform,
        )
        legal_assignment = _assignment(
            platform, [("n0", "a", ["b"])]
        )
        timing = compress_sta_paths(
            normalize_sta_paths(
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": "route_test",
                    "paths": [
                        {
                            "id": "sll_path",
                            "clock_domain": "clk",
                            "clock_period_ns": 20.0,
                            "slack_ns": 10.0,
                            "fixed_delay_ns": 0.0,
                            "cut_nets": ["n0"],
                        }
                    ],
                },
                demands_from_assignment(legal_assignment, platform),
            )
        )
        routes = route_system_native(
            legal_assignment,
            platform,
            constraints,
            timing,
            executable=str(tlr_router()),
            provider=GLOBAL_CANDIDATE_PROVIDER,
        )
        self.assertEqual(
            routes["metrics"]["estimated_max_tdm_ratio"], 1
        )
        self.assertEqual(
            validate_native_system_routes(
                legal_assignment, platform, routes, timing
            )["status"],
            "pass",
        )

        overfull_assignment = _assignment(
            platform,
            [("n0", "a", ["b"]), ("n1", "a", ["b"])],
        )
        with self.assertRaisesRegex(
            EmuFlowError, "routing infeasible"
        ):
            route_system_native(
                overfull_assignment,
                platform,
                constraints,
                timing,
                executable=str(tlr_router()),
                provider=GLOBAL_CANDIDATE_PROVIDER,
            )

    def test_native_router_accepts_capacity_above_signed_32_bit(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "wide_tdm_capacity",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=5000)],
            )
        )
        assignment = _assignment(platform, [("n0", "a", ["b"])])
        constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 837876,
            },
            platform,
        )
        routes = route_system_native(
            assignment,
            platform,
            constraints,
            executable=str(tlr_router()),
            provider=GLOBAL_CANDIDATE_PROVIDER,
        )
        self.assertEqual(routes["metrics"]["total_link_bit_hops"], 1)
        self.assertEqual(
            validate_native_system_routes(
                assignment, platform, routes
            )["status"],
            "pass",
        )

    def test_hybrid_router_selects_delay_demand_balanced_multicast(
        self,
    ) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "balanced_multicast",
                ["a", "b", "c", "d", "e"],
                [
                    _link("ab", "a", "b"),
                    _link("bd", "b", "d"),
                    _link("be", "b", "e"),
                    _link("ac", "a", "c"),
                    _link("ce", "c", "e"),
                ],
            )
        )
        assignment = _assignment(
            platform, [("multicast", "a", ["d", "e"])]
        )
        timing = compress_sta_paths(
            normalize_sta_paths(
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": "route_test",
                    "paths": [
                        {
                            "id": "multicast_path",
                            "clock_domain": "clk",
                            "clock_period_ns": 20.0,
                            "slack_ns": 10.0,
                            "fixed_delay_ns": 0.0,
                            "cut_nets": ["multicast"],
                        }
                    ],
                },
                demands_from_assignment(assignment, platform),
            )
        )
        constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 8,
                "reroute_rounds": 0,
                "link_delay_ns": {
                    "ab": 1.0,
                    "bd": 1.0,
                    "be": 1.0,
                    "ac": 0.995,
                    "ce": 0.995,
                },
            },
            platform,
        )
        routes = route_system_native(
            assignment,
            platform,
            constraints,
            timing,
            executable=str(tlr_router()),
            provider=GLOBAL_CANDIDATE_PROVIDER,
        )
        self.assertEqual(len(routes["routes"][0]["tree_edges"]), 3)
        oracle = exact_route_tree_selection(
            assignment, platform, constraints, timing
        )
        self.assertEqual(
            {
                (edge["link"], edge["from"], edge["to"])
                for edge in routes["routes"][0]["tree_edges"]
            },
            {
                tuple(edge)
                for edge in oracle["trees"]["multicast"]
            },
        )
        self.assertGreater(oracle["enumerated_combinations"], 1)
        self.assertEqual(
            validate_native_system_routes(
                assignment, platform, routes, timing
            )["status"],
            "pass",
        )

    def test_common_timing_checker_keeps_multiclock_extrema_separate(
        self,
    ) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "multiclock",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=1, latency=1)],
            )
        )
        assignment = _assignment(
            platform,
            [("long_period", "a", ["b"]), ("short_period", "a", ["b"])],
        )
        routes = route_system_native(
            assignment,
            platform,
            normalize_route_constraints(
                {
                    "schema": "emuflow.system-route-constraints/v1",
                    "frame_slots": 64,
                },
                platform,
            ),
            executable=str(tlr_router()),
        )
        timing = compress_sta_paths(
            normalize_sta_paths(
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": "route_test",
                    "paths": [
                        {
                            "id": "raw_slack_worst",
                            "clock_domain": "slow",
                            "clock_period_ns": 100.0,
                            "slack_ns": -2.0,
                            "fixed_delay_ns": 110.0,
                            "cut_nets": ["long_period"],
                        },
                        {
                            "id": "normalized_worst",
                            "clock_domain": "fast",
                            "clock_period_ns": 10.0,
                            "slack_ns": -1.0,
                            "fixed_delay_ns": 10.0,
                            "cut_nets": ["short_period"],
                        },
                    ],
                },
                routes["demands"],
            )
        )
        checked = reconstruct_system_route_timing(
            assignment, platform, routes, timing
        )
        self.assertEqual(
            checked["worst_slack_path"], "raw_slack_worst"
        )
        self.assertEqual(
            checked["worst_normalized_path"], "normalized_worst"
        )
        self.assertEqual(
            checked["estimated_worst_tdm_slack_path"],
            "raw_slack_worst",
        )
        self.assertEqual(
            checked["estimated_worst_tdm_normalized_path"],
            "normalized_worst",
        )

    def test_timing_aware_cpp_router_prioritizes_critical_clock_domain(
        self,
    ) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "timing_diamond",
                ["a", "b", "c", "d"],
                [
                    _link("ab", "a", "b", latency=1),
                    _link("bd", "b", "d", latency=1),
                    _link("ac", "a", "c", latency=5),
                    _link("cd", "c", "d", latency=5),
                ],
            )
        )
        assignment = _assignment(
            platform,
            [
                ("a_low_priority", "a", ["d"]),
                ("z_critical", "a", ["d"]),
            ],
        )
        timing_value = {
            "schema": "emuflow.sta-paths/v1",
            "design": "route_test",
            "paths": [
                {
                    "id": "critical_0",
                    "clock_domain": "fast_clk",
                    "clock_period_ns": 20.0,
                    "slack_ns": -1.0,
                    "fixed_delay_ns": 10.0,
                    "cut_nets": ["z_critical"],
                    "cut_signature": ["a->d"],
                },
                {
                    "id": "critical_duplicate",
                    "clock_domain": "fast_clk",
                    "clock_period_ns": 20.0,
                    "slack_ns": 0.0,
                    "fixed_delay_ns": 9.0,
                    "cut_nets": ["z_critical"],
                    "cut_signature": ["a->d"],
                },
                {
                    "id": "relaxed_0",
                    "clock_domain": "slow_clk",
                    "clock_period_ns": 100.0,
                    "slack_ns": 80.0,
                    "fixed_delay_ns": 0.0,
                    "cut_nets": ["a_low_priority"],
                    "cut_signature": ["slow:a->d"],
                },
            ],
        }
        constraints_value = {
            "schema": "emuflow.system-route-constraints/v1",
            "frame_slots": 1,
            "max_iterations": 12,
            "reroute_rounds": 4,
            "link_delay_ns": {
                "ab": 1.0,
                "bd": 1.0,
                "ac": 5.0,
                "cd": 5.0,
            },
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            executable = tlr_router()
            runtime = root / "runtime"
            binary = runtime / "bin" / "emuflow-native"
            binary.parent.mkdir(parents=True)
            binary.symlink_to(emuflow_native())
            assignment_path = root / "assignment.json"
            platform_path = root / "platform.json"
            timing_path = root / "timing.json"
            constraints_path = root / "constraints.json"
            write_assignment_cuts(root / "assignment.sqlite", assignment, platform)
            assignment_path.write_text(
                json.dumps(assignment), encoding="utf-8"
            )
            platform_path.write_text(
                json.dumps(platform.to_dict()), encoding="utf-8"
            )
            timing_path.write_text(
                json.dumps(timing_value), encoding="utf-8"
            )
            constraints_path.write_text(
                json.dumps(constraints_value), encoding="utf-8"
            )
            with mock.patch.dict(
                "os.environ",
                {"EMUFLOW_NATIVE_ROOT": str(runtime)},
                clear=False,
            ):
                report = run_phase4(
                    assignment_path=root / "assignment.sqlite",
                    platform_path=platform_path,
                    output_dir=root / "phase4",
                    constraints_path=constraints_path,
                    timing_paths_path=timing_path,
                )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(
                report["route_storage"]["dataflow"],
                "native-sql-route-arc-delay-selection-publication-v1",
            )
            self.assertEqual(report["validation"]["timing_paths_original"], 3)
            self.assertEqual(report["validation"]["timing_paths_compressed"], 2)
            selector = json.loads(
                (root / "phase4" / "routes.json").read_text(
                    encoding="utf-8"
                )
            )
            stored_joint = selector["header"]["joint_optimization"]
            self.assertNotIn(
                "master_selection",
                stored_joint["candidate_generation"],
            )
            self.assertNotIn(
                "batches", stored_joint["refinement_batches"]
            )
            self.assertEqual(
                selector["sections"]["master_selections"]["records"], 2
            )
            self.assertEqual(
                selector["sections"]["refinement_batch_paths"]["records"],
                2,
            )
            route_view = read_system_routes(root / "phase4" / "routes.json")
            self.assertIsInstance(route_view, SystemRoutesView)
            lazy_selection = route_view["joint_optimization"][
                "candidate_generation"
            ]["master_selection"]
            lazy_batches = route_view["joint_optimization"][
                "refinement_batches"
            ]["batches"]
            self.assertEqual(list.__len__(lazy_selection), 0)
            self.assertEqual(list.__len__(lazy_batches), 0)
            routes = read_json(root / "phase4" / "routes.json")
            self.assertEqual(
                routes["provider"],
                GLOBAL_CANDIDATE_PROVIDER,
            )
            self.assertIn("candidate_generation", report)
            self.assertEqual(
                routes["joint_optimization"]["method"],
                "checked-candidate-pool+"
                "deterministic-batch-conflict-lns-v2",
            )
            route_by_net = {
                route["net"]: route for route in routes["routes"]
            }
            self.assertEqual(
                {
                    edge["link"]
                    for edge in route_by_net["z_critical"]["tree_edges"]
                },
                {"ab", "bd"},
            )
            self.assertEqual(
                route_by_net["z_critical"]["predicted_max_delay_ns"], 2.0
            )
            self.assertEqual(
                {
                    edge["link"]
                    for edge in route_by_net["a_low_priority"]["tree_edges"]
                },
                {"ac", "cd"},
            )
            normalized = load_sta_paths(
                timing_path,
                routes["demands"],
            )
            checked = validate_native_system_routes(
                assignment, platform, routes, normalized
            )
            self.assertEqual(checked["status"], "pass")
            self.assertFalse(
                (root / "phase4" / "timing_paths.normalized.json").exists()
            )
            corrupted = copy.deepcopy(routes)
            corrupted["routes"][0]["predicted_max_delay_ns"] += 0.25
            with self.assertRaisesRegex(
                ValidationError, "independent edge-delay recomputation"
            ):
                validate_native_system_routes(
                    assignment, platform, corrupted, normalized
                )
            corrupted = copy.deepcopy(routes)
            corrupted["metrics"]["estimated_max_tdm_ratio"] += 1
            with self.assertRaisesRegex(
                ValidationError, "route/TDM proxy recomputation"
            ):
                validate_native_system_routes(
                    assignment, platform, corrupted, normalized
                )

            lock_platform = Platform.from_dict(
                _platform_value(
                    "direction_lock",
                    ["a", "b", "c"],
                    [
                        _link(
                            "ab",
                            "a",
                            "b",
                            lanes=4,
                            direction="half_duplex",
                        ),
                        _link("ac", "a", "c", lanes=4, latency=2),
                        _link("bc", "b", "c", lanes=4, latency=2),
                    ],
                )
            )
            lock_assignment = _assignment(
                lock_platform,
                [
                    ("forward_0", "a", ["b"]),
                    ("forward_1", "a", ["b"]),
                    ("reverse_0", "b", ["a"]),
                ],
            )
            lock_timing = {
                "schema": "emuflow.sta-paths/v1",
                "design": "route_test",
                "paths": [
                    {
                        "id": f"path_{net}",
                        "clock_domain": "clk",
                        "clock_period_ns": 50.0,
                        "slack_ns": 20.0,
                        "fixed_delay_ns": 0.0,
                        "cut_nets": [net],
                        "cut_signature": [signature],
                    }
                    for net, signature in (
                        ("forward_0", "a->b:0"),
                        ("forward_1", "a->b:1"),
                        ("reverse_0", "b->a"),
                    )
                ],
            }
            lock_assignment_path = root / "lock" / "assignment.sqlite"
            write_assignment_cuts(lock_assignment_path, lock_assignment, lock_platform)
            lock_platform_path = root / "lock-platform.json"
            lock_timing_path = root / "lock-timing.json"
            lock_platform_path.write_text(
                json.dumps(lock_platform.to_dict()), encoding="utf-8"
            )
            lock_timing_path.write_text(
                json.dumps(lock_timing), encoding="utf-8"
            )
            lock_report = run_phase4(
                assignment_path=lock_assignment_path,
                platform_path=lock_platform_path,
                output_dir=root / "lock-phase4",
                frame_slots=1,
                provider=GLOBAL_CANDIDATE_PROVIDER,
                timing_paths_path=lock_timing_path,
                router=str(executable),
            )
            self.assertEqual(lock_report["validation"]["direction_locks"], 1)
            lock_routes = read_json(root / "lock-phase4" / "routes.json")
            self.assertEqual(
                lock_routes["provider"], GLOBAL_CANDIDATE_PROVIDER
            )
            self.assertIn("joint_optimization", lock_routes)
            self.assertEqual(
                lock_routes["direction_locks"][0]["from"], "a"
            )
            reverse_route = next(
                route
                for route in lock_routes["routes"]
                if route["net"] == "reverse_0"
            )
            self.assertNotIn(
                "ab", {edge["link"] for edge in reverse_route["tree_edges"]}
            )

    def test_pipeline_routes_real_counter_partition(self) -> None:
        ir = import_yosys_json(
            ROOT / "examples" / "yosys" / "counter.json",
            top="counter",
            clocks=["clk"],
        )
        platform = Platform.load(PLATFORM_PATH)
        constraints = normalize_partition_constraints(None, ir, platform)
        clusters = build_clusters(ir, constraints, cut_mode="sequential-only")
        assignment = assign_clusters(
            ir,
            platform,
            clusters,
            constraints,
            seed=4,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            assignment_path = root / "assignment.json"
            write_assignment_cuts(root / "assignment.sqlite", assignment, platform)
            assignment_path.write_text(
                json.dumps(assignment), encoding="utf-8"
            )
            report = run_phase4(
                assignment_path=root / "assignment.sqlite",
                platform_path=PLATFORM_PATH,
                output_dir=root / "phase4",
                frame_slots=1,
                timing_driven=False,
                router=str(tlr_router()),
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["provider"], GLOBAL_CANDIDATE_PROVIDER)
            self.assertGreater(report["validation"]["demands"], 0)
            self.assertGreater(report["validation"]["routed_sinks"], 0)
            self.assertEqual(report["validation"]["overloaded_links"], 0)
            for filename in (
                "route_constraints.normalized.json",
                "routes.json",
                "phase4_report.json",
            ):
                self.assertTrue((root / "phase4" / filename).is_file())
            self.assertNotIn("candidate_pool", report["artifacts"])
            self.assertFalse(
                (root / "phase4/route_candidate_pool.json").exists()
            )
            selector = json.loads(
                (root / "phase4/routes.json").read_text(encoding="utf-8")
            )
            self.assertEqual(selector["schema"], SYSTEM_ROUTE_STORAGE_SCHEMA)
            self.assertEqual(
                report["artifacts"]["route_store"], selector["artifact"]
            )
            self.assertTrue(
                (root / "phase4" / selector["artifact"]).is_file()
            )
            route_view = read_system_routes(root / "phase4/routes.json")
            self.assertIsInstance(route_view, SystemRoutesView)
            self.assertEqual(list.__len__(route_view["routes"]), 0)
            self.assertEqual(
                len(read_json(root / "phase4/routes.json")["routes"]),
                report["validation"]["demands"],
            )
            detached = root / "detached"
            detached.mkdir()
            (detached / "routes.json").write_text(
                json.dumps(selector), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValidationError, "store is missing"):
                read_system_routes(detached / "routes.json")

    def test_native_router_uses_both_diamond_paths(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "diamond",
                ["a", "b", "c", "d"],
                [
                    _link("ab", "a", "b"),
                    _link("bd", "b", "d"),
                    _link("ac", "a", "c"),
                    _link("cd", "c", "d"),
                ],
            )
        )
        assignment = _assignment(
            platform,
            [("n0", "a", ["d"]), ("n1", "a", ["d"])],
        )
        constraints = normalize_route_constraints(
            {"schema": "emuflow.system-route-constraints/v1", "frame_slots": 1},
            platform,
        )
        routes = route_system_native(
            assignment,
            platform,
            constraints,
            executable=str(tlr_router()),
        )
        validation = validate_native_system_routes(
            assignment, platform, routes
        )
        self.assertEqual(validation["status"], "pass")
        used_links = {
            edge["link"]
            for route in routes["routes"]
            for edge in route["tree_edges"]
        }
        self.assertEqual(used_links, {"ab", "bd", "ac", "cd"})
        self.assertEqual(validation["max_link_utilization"], 1.0)

    def test_multicast_is_a_reachable_acyclic_tree(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "multicast",
                ["a", "b", "c", "d"],
                [
                    _link("ab", "a", "b", lanes=4),
                    _link("ac", "a", "c", lanes=4),
                    _link("bd", "b", "d", lanes=4),
                    _link("cd", "c", "d", lanes=4),
                ],
            )
        )
        assignment = _assignment(
            platform,
            [("multicast_net", "a", ["b", "c", "d"])],
        )
        constraints = normalize_route_constraints(None, platform, frame_slots=1)
        routes = route_system_native(
            assignment,
            platform,
            constraints,
            executable=str(tlr_router()),
        )
        validation = validate_native_system_routes(
            assignment, platform, routes
        )
        self.assertEqual(validation["routed_sinks"], 3)
        self.assertEqual(validation["demands"], 1)
        self.assertLessEqual(validation["tree_edges"], 3)

    def test_unavailable_links_report_unreachable_sink(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "line",
                ["a", "b", "c"],
                [_link("ab", "a", "b"), _link("bc", "b", "c")],
            )
        )
        assignment = _assignment(platform, [("n0", "a", ["c"])])
        constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "unavailable_links": ["bc"],
            },
            platform,
        )
        with self.assertRaisesRegex(
            (EmuFlowError, ValidationError), "unreachable|cannot reach"
        ):
            route_system_native(
                assignment,
                platform,
                constraints,
                executable=str(tlr_router()),
            )

    def test_infeasible_link_capacity_is_rejected(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "single",
                ["a", "b"],
                [_link("ab", "a", "b")],
            )
        )
        assignment = _assignment(
            platform,
            [("n0", "a", ["b"]), ("n1", "a", ["b"])],
        )
        constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 1,
                "max_iterations": 2,
            },
            platform,
        )
        with self.assertRaisesRegex(EmuFlowError, "infeasible"):
            route_system_native(
                assignment,
                platform,
                constraints,
                executable=str(tlr_router()),
            )

    def test_insufficient_node_cut_capacity_is_rejected(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "single-node-cut",
                ["a", "b", "c"],
                [
                    _link("ab", "a", "b", lanes=1),
                    _link("ac", "a", "c", lanes=1),
                ],
            )
        )
        assignment = _assignment(
            platform,
            [(f"n{index}", "a", ["b"]) for index in range(3)],
        )
        constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 1,
            },
            platform,
        )
        with self.assertRaisesRegex(
            EmuFlowError,
            r"routing infeasible",
        ):
            route_system_native(
                assignment,
                platform,
                constraints,
                executable=str(tlr_router()),
            )

    def test_half_duplex_capacity_is_shared(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "half",
                ["a", "b"],
                [_link("ab", "a", "b", direction="half_duplex")],
            )
        )
        assignment = _assignment(
            platform,
            [("n0", "a", ["b"]), ("n1", "b", ["a"])],
        )
        constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 1,
                "max_iterations": 2,
            },
            platform,
        )
        with self.assertRaisesRegex(EmuFlowError, "unreachable|infeasible|could not span"):
            route_system_native(
                assignment,
                platform,
                constraints,
                executable=str(tlr_router()),
            )

    def test_cycle_in_route_artifact_is_rejected(self) -> None:
        platform = Platform.from_dict(
            _platform_value(
                "cycle",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=4)],
            )
        )
        assignment = _assignment(platform, [("n0", "a", ["b"])])
        constraints = normalize_route_constraints(None, platform, frame_slots=1)
        routes = route_system_native(
            assignment,
            platform,
            constraints,
            executable=str(tlr_router()),
        )
        broken = copy.deepcopy(routes)
        broken["routes"][0]["tree_edges"].append(
            {"link": "ab", "from": "b", "to": "a"}
        )
        with self.assertRaisesRegex(ValidationError, "cycle"):
            validate_system_routes(assignment, platform, broken)


if __name__ == "__main__":
    unittest.main()
