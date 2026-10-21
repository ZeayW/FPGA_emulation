import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.cross_layer_timing import (
    CROSS_LAYER_PHYSICAL_BINDING_SCHEMA,
    CROSS_LAYER_TIMING_CONTRACT_SCHEMA,
    REGISTERED_BOUNDARY,
    SAMPLED_VIRTUAL_WIRE,
    build_cross_layer_physical_binding,
    build_cross_layer_timing_contract,
    validate_cross_layer_physical_binding,
    validate_cross_layer_timing_contract,
)
from emuflow.errors import ValidationError
from emuflow.platform import Platform
from emuflow.phase3_assignment_index import (
    ASSIGNMENT_INDEX_APPLICATION_ID,
)
from emuflow.tdm_schedule_storage import (
    ScheduleRecordSequence,
    read_tdm_schedule,
    write_tdm_schedule_storage,
)


def _semantic_contract():
    return {
        "schema": "emuflow.static-exact-combinational-cut/v3",
        "mode": "static-exact-combinational",
        "logic_segments": [],
    }


def _routes(*, exact=False, semantic_contract=None):
    value = {
        "schema": "emuflow.system-routes/v1",
        "design": "cross-layer-fixture",
        "platform": "three",
        "provider": "route-fixture",
        "constraints": {"frame_slots": 16},
        "routes": [
            {
                "id": "d000000",
                "net": "n0",
                "source": "a",
                "sinks": ["c"],
                "tree_edges": [
                    {"link": "ab", "from": "a", "to": "b"},
                    {"link": "bc", "from": "b", "to": "c"},
                ],
            }
        ],
        "timing": {
            "schema": "emuflow.sta-paths/v1",
            "paths": [
                {
                    "path": "p0",
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "fixed_delay_ns": 2.0,
                    "compressed_path_ids": ["p0", "p1"],
                    "cut_nets": ["n0"],
                    "cut_transitions": [
                        {"net": "n0", "from": "a", "to": "c"}
                    ],
                }
            ],
        },
    }
    if exact:
        from emuflow.combinational_cut import semantic_contract_sha256

        semantic = semantic_contract or _semantic_contract()
        value["semantic_contract_schema"] = semantic["schema"]
        value["semantic_contract_sha256"] = semantic_contract_sha256(semantic)
    return value


def _schedule(*, exact=False):
    return {
        "schema": "emuflow.tdm-schedule/v1",
        "design": "cross-layer-fixture",
        "platform": "three",
        "provider": "arbitrary-unified-provider",
        "transport_semantics": (
            SAMPLED_VIRTUAL_WIRE if exact else REGISTERED_BOUNDARY
        ),
        "entries": [
            {
                "id": "s000000",
                "demand": "d000000",
                "link": "ab",
                "from": "a",
                "to": "b",
            },
            {
                "id": "s000001",
                "demand": "d000000",
                "link": "bc",
                "from": "b",
                "to": "c",
            },
        ],
    }


class CrossLayerTimingContractTest(unittest.TestCase):
    def test_indexed_schedule_hop_binding_avoids_record_decode(self):
        routes = _routes()
        schedule = {
            **_schedule(),
            "routes": [],
            "demand_completions": [],
            "domain_schedules": [],
        }
        expected = build_cross_layer_timing_contract(routes, schedule)
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "schedule.json"
            write_tdm_schedule_storage(path, schedule)
            indexed = read_tdm_schedule(path)
            with mock.patch.object(
                ScheduleRecordSequence,
                "__iter__",
                side_effect=AssertionError(
                    "indexed certificate decoded schedule record objects"
                ),
            ):
                actual = build_cross_layer_timing_contract(routes, indexed)
                validation = validate_cross_layer_timing_contract(
                    routes, actual, indexed
                )
        self.assertEqual(actual, expected)
        self.assertEqual(validation["status"], "pass")

    def test_indexed_schedule_hop_binding_fails_closed(self):
        routes = _routes()
        schedule = {
            **_schedule(),
            "routes": [],
            "demand_completions": [],
            "domain_schedules": [],
        }
        schedule["entries"][1]["from"] = "wrong"
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "schedule.json"
            write_tdm_schedule_storage(path, schedule)
            indexed = read_tdm_schedule(path)
            with self.assertRaisesRegex(ValidationError, "does not cover"):
                build_cross_layer_timing_contract(routes, indexed)

    def test_indexed_schedule_hop_binding_rejects_malformed_record(self):
        routes = _routes()
        schedule = {
            **_schedule(),
            "routes": [],
            "demand_completions": [],
            "domain_schedules": [],
        }
        del schedule["entries"][1]["from"]
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "schedule.json"
            write_tdm_schedule_storage(path, schedule)
            indexed = read_tdm_schedule(path)
            with self.assertRaisesRegex(ValidationError, "malformed"):
                build_cross_layer_timing_contract(routes, indexed)

    def test_indexed_semantic_contract_stays_compact(self):
        semantic = {
            "schema": "emuflow.static-exact-combinational-cut/v3",
            "mode": "static-exact-combinational",
            "qualification": "structural-partition-legality",
            "indexed": True,
        }
        routes = _routes(exact=True, semantic_contract=semantic)
        # Indexed bindings use the externally supplied Phase 3 digest rather
        # than hashing the compact locator itself.
        routes["semantic_contract_sha256"] = "a" * 64
        contract = build_cross_layer_timing_contract(
            routes,
            _schedule(exact=True),
            semantic,
        )
        self.assertEqual(contract["logic_segment_bindings"], [])
        self.assertEqual(
            contract["logic_segment_binding"]["mode"],
            "phase3-assignment-index",
        )
        self.assertEqual(
            validate_cross_layer_timing_contract(
                routes, contract, _schedule(exact=True), semantic
            )["status"],
            "pass",
        )

    def test_indexed_physical_binding_reads_exact_segments_from_sqlite(self):
        semantic = {
            "schema": "emuflow.static-exact-combinational-cut/v3",
            "mode": "static-exact-combinational",
            "qualification": "structural-partition-legality",
            "indexed": True,
        }
        routes = _routes(exact=True, semantic_contract=semantic)
        routes["semantic_contract_sha256"] = "a" * 64
        schedule = _schedule(exact=True)
        contract = build_cross_layer_timing_contract(
            routes, schedule, semantic
        )
        segment = {
            "id": "segment000000",
            "kind": "launch_to_tx",
            "fpga": "a",
            "sink_cut_net": "n0",
            "source_semantics": "configuration-stable-constant",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            index = Path(temporary_directory) / "assignment.sqlite"
            with sqlite3.connect(index) as connection:
                connection.executescript(
                    f"""
                    PRAGMA application_id={ASSIGNMENT_INDEX_APPLICATION_ID};
                    PRAGMA user_version=1;
                    CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                    CREATE TABLE parts(part_index INTEGER PRIMARY KEY,fpga TEXT NOT NULL);
                    CREATE TABLE exact_segment_bindings(
                      segment_id TEXT PRIMARY KEY,kind TEXT NOT NULL,
                      part_index INTEGER NOT NULL,payload_json TEXT NOT NULL);
                    CREATE TABLE exact_capture_requirements(
                      capture_id TEXT PRIMARY KEY,part_index INTEGER NOT NULL,
                      payload_json TEXT NOT NULL);
                    """
                )
                connection.executemany(
                    "INSERT INTO metadata VALUES (?,?)",
                    [
                        ("semantic_contract_schema", json.dumps(semantic["schema"])),
                        ("semantic_contract_sha256", json.dumps("a" * 64)),
                    ],
                )
                connection.executemany(
                    "INSERT INTO parts VALUES (?,?)",
                    enumerate(("a", "b", "c")),
                )
                connection.execute(
                    "INSERT INTO exact_segment_bindings VALUES (?,?,?,?)",
                    (
                        segment["id"],
                        segment["kind"],
                        0,
                        json.dumps(segment, sort_keys=True),
                    ),
                )
            binding = build_cross_layer_physical_binding(
                contract,
                schedule,
                self._physical(schedule),
                self._platform(),
                index,
            )
            self.assertEqual(binding["status"], "pass")
            self.assertEqual(binding["metrics"]["required_logic_segments"], 1)
            self.assertEqual(binding["metrics"]["measured_logic_segments"], 1)
            self.assertEqual(
                binding["logic_segment_bindings"][0]["measurement"],
                "structural-configuration-stable-constant",
            )

    def _platform(self):
        return Platform.from_dict(
            {
                "schema": "emuflow.boarddb/v1",
                "platform": {
                    "name": "three",
                    "kind": "virtual",
                    "description": "cross-layer fixture",
                },
                "fpgas": [
                    {
                        "id": fpga,
                        "part": "fixture",
                        "utilization_limit": 0.8,
                        "capacity": {"lut": 10, "ff": 10},
                    }
                    for fpga in ("a", "b", "c")
                ],
                "links": [
                    {
                        "id": link,
                        "endpoints": endpoints,
                        "direction": "full_duplex",
                        "mode": "abstract",
                        "data_lanes_per_direction": 4,
                        "fabric_clock_mhz": 250.0,
                        "latency_cycles": 2,
                    }
                    for link, endpoints in (
                        ("ab", ["a", "b"]),
                        ("bc", ["b", "c"]),
                    )
                ],
            }
        )

    def _physical(self, schedule):
        identities = {}
        timing = {}
        for fpga in ("a", "b", "c"):
            endpoints = []
            delays = []
            for entry in schedule["entries"]:
                for kind, owner in (("tx", entry["from"]), ("rx", entry["to"])):
                    if owner != fpga:
                        continue
                    endpoint = f"__emuflow_{kind}_{entry['id']}"
                    endpoints.append(
                        {
                            "id": endpoint,
                            "kind": kind,
                            "schedule_entry": entry["id"],
                        }
                    )
                    delays.append(
                        {
                            "id": endpoint,
                            "kind": kind,
                            "schedule_entry": entry["id"],
                            "delay_ns": 0.25,
                            "start_object": "start",
                            "end_object": "end",
                            "measurement": (
                                "logical-source-to-tx-port"
                                if kind == "tx"
                                else "rx-port-to-shadow-capture"
                            ),
                        }
                    )
            coverage = {
                "endpoints": len(endpoints),
                "tx": sum(item["kind"] == "tx" for item in endpoints),
                "rx": sum(item["kind"] == "rx" for item in endpoints),
            }
            identities[fpga] = {
                "schema": "emuflow.boundary-identity/v1",
                "status": "pass",
                "design": schedule["design"],
                "platform": schedule["platform"],
                "fpga": fpga,
                "provider": "fixture",
                "coverage": {**coverage, "external_port_nets": len(endpoints)},
                "endpoints": endpoints,
            }
            timing[fpga] = {
                "schema": "emuflow.boundary-timing/v1",
                "status": "pass",
                "design": schedule["design"],
                "platform": schedule["platform"],
                "fpga": fpga,
                "provider": "fixture",
                "qualification": "endpoint-exact",
                "coverage": coverage,
                "endpoints": delays,
            }
        return {
            "provider": "fixture-physical",
            "qualification": "routed-endpoint-exact",
            "boundary_identities": identities,
            "boundary_timing": timing,
        }

    def test_registered_contract_binds_paths_routes_and_schedule(self):
        routes = _routes()
        schedule = _schedule()
        contract = build_cross_layer_timing_contract(routes, schedule)
        self.assertEqual(contract["schema"], CROSS_LAYER_TIMING_CONTRACT_SCHEMA)
        self.assertEqual(contract["transport_semantics"], REGISTERED_BOUNDARY)
        self.assertEqual(
            contract["path_bindings"],
            [
                {
                    "path": "p0",
                    "members": ["p0", "p1"],
                    "clock_domain": "clk",
                    "required_time_ns": 10.0,
                    "estimated_logic_delay_ns": 2.0,
                    "cuts": [
                        {
                            "cut": "p0:cut:0",
                            "index": 0,
                            "logical_net": "n0",
                            "demand": "d000000",
                            "from": "a",
                            "to": "c",
                        }
                    ],
                }
            ],
        )
        self.assertEqual(contract["metrics"]["scheduled_hops"], 2)
        self.assertEqual(
            validate_cross_layer_timing_contract(routes, contract, schedule)[
                "status"
            ],
            "pass",
        )

    def test_static_exact_selects_sampled_virtual_wire_semantics(self):
        semantic = _semantic_contract()
        routes = _routes(exact=True, semantic_contract=semantic)
        contract = build_cross_layer_timing_contract(
            routes, _schedule(exact=True), semantic
        )
        self.assertEqual(
            contract["transport_semantics"], SAMPLED_VIRTUAL_WIRE
        )
        self.assertEqual(
            contract["semantic_binding"]["schema"],
            "emuflow.static-exact-combinational-cut/v3",
        )

    def test_configuration_stable_source_needs_no_physical_logic_record(self):
        semantic = _semantic_contract()
        semantic["logic_segments"] = [
            {
                "id": "constant-launch",
                "kind": "launch_to_tx",
                "fpga": "a",
                "source_semantics": "configuration-stable-constant",
            }
        ]
        routes = _routes(exact=True, semantic_contract=semantic)
        schedule = _schedule(exact=True)
        contract = build_cross_layer_timing_contract(
            routes, schedule, semantic
        )
        physical = self._physical(schedule)
        binding = build_cross_layer_physical_binding(
            contract, schedule, physical, self._platform()
        )
        self.assertEqual(binding["status"], "pass")
        self.assertEqual(
            binding["logic_segment_bindings"],
            [
                {
                    "segment": "constant-launch",
                    "fpga": "a",
                    "measured_min_delay_ns": 0.0,
                    "measured_max_delay_ns": 0.0,
                    "measurement": "structural-configuration-stable-constant",
                }
            ],
        )

    def test_schedule_provider_is_not_semantic_dispatch(self):
        semantic = _semantic_contract()
        routes = _routes(exact=True, semantic_contract=semantic)
        schedule = _schedule(exact=True)
        first = build_cross_layer_timing_contract(routes, schedule, semantic)
        schedule["provider"] = "different-legal-solver"
        second = build_cross_layer_timing_contract(routes, schedule, semantic)
        self.assertEqual(
            first["transport_semantics"], second["transport_semantics"]
        )
        self.assertNotEqual(
            first["schedule_binding"], second["schedule_binding"]
        )

    def test_tampered_or_incomplete_binding_is_rejected(self):
        routes = _routes()
        schedule = _schedule()
        contract = build_cross_layer_timing_contract(routes, schedule)
        tampered = copy.deepcopy(contract)
        tampered["hop_bindings"][0]["entry"] = "wrong"
        with self.assertRaisesRegex(ValidationError, "canonical inputs"):
            validate_cross_layer_timing_contract(routes, tampered, schedule)
        incomplete = copy.deepcopy(schedule)
        incomplete["entries"].pop()
        with self.assertRaisesRegex(ValidationError, "does not cover"):
            build_cross_layer_timing_contract(routes, incomplete)

    def test_multicast_path_requires_explicit_sink_transition(self):
        routes = _routes()
        routes["routes"][0]["sinks"] = ["b", "c"]
        routes["timing"]["paths"][0].pop("cut_transitions")
        with self.assertRaisesRegex(ValidationError, "explicit transitions"):
            build_cross_layer_timing_contract(routes)

    def test_physical_binding_has_exact_endpoint_coverage(self):
        routes = _routes()
        schedule = _schedule()
        contract = build_cross_layer_timing_contract(routes, schedule)
        physical = self._physical(schedule)
        binding = build_cross_layer_physical_binding(
            contract, schedule, physical, self._platform()
        )
        self.assertEqual(binding["schema"], CROSS_LAYER_PHYSICAL_BINDING_SCHEMA)
        self.assertEqual(binding["status"], "pass")
        self.assertEqual(binding["metrics"]["required_endpoints"], 4)
        self.assertEqual(binding["metrics"]["model_only_board_hops"], 2)
        self.assertEqual(binding["metrics"]["measured_board_hops"], 0)
        self.assertFalse(
            binding["metrics"]["final_board_link_timing_signoff"]
        )
        self.assertTrue(
            all(
                item["delay_bound_ns"] == 8.0
                and item["measurement"] == "model-only"
                for item in binding["board_bindings"]
            )
        )
        self.assertEqual(
            validate_cross_layer_physical_binding(
                contract, schedule, physical, self._platform(), binding
            )["status"],
            "pass",
        )

    def test_public_schemas_cover_complete_contract_shapes(self):
        root = Path(__file__).resolve().parents[1] / "schemas"
        routes = _routes()
        schedule = _schedule()
        contract = build_cross_layer_timing_contract(routes, schedule)
        physical = self._physical(schedule)
        binding = build_cross_layer_physical_binding(
            contract, schedule, physical, self._platform()
        )
        for filename, artifact in (
            ("cross-layer-timing-v1.schema.json", contract),
            ("cross-layer-physical-binding-v1.schema.json", binding),
        ):
            schema = json.loads((root / filename).read_text(encoding="utf-8"))
            self.assertEqual(schema["properties"]["schema"]["const"], artifact["schema"])
            self.assertTrue(set(schema["required"]).issubset(artifact))
            self.assertFalse(set(artifact) - set(schema["properties"]))

    def test_missing_or_tampered_physical_evidence_fails_closed(self):
        routes = _routes()
        schedule = _schedule()
        contract = build_cross_layer_timing_contract(routes, schedule)
        physical = self._physical(schedule)
        physical.pop("boundary_timing")
        physical.pop("boundary_identities")
        binding = build_cross_layer_physical_binding(
            contract, schedule, physical, self._platform()
        )
        self.assertEqual(binding["status"], "incomplete")
        tampered = copy.deepcopy(binding)
        tampered["missing"]["endpoints"] = []
        with self.assertRaisesRegex(ValidationError, "canonical inputs"):
            validate_cross_layer_physical_binding(
                contract, schedule, physical, self._platform(), tampered
            )


if __name__ == "__main__":
    unittest.main()
