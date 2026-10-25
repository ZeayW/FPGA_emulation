from contextlib import closing
import tempfile
import tracemalloc
import unittest
import hashlib
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from emuflow.io import read_json, write_json
from emuflow.ir import EmuIR
from emuflow.errors import ValidationError
from emuflow.logic_segment_timing import (
    LogicSegmentQueryInputs,
    _architectural_launch_endpoints,
    _index_exact_logic_segments,
    _incoming_transported_cut_nets,
    _normalize_xilinx_logical_endpoint,
    _route_timing_paths,
    _vivado_object,
    import_vivado_logic_segment_timing,
    prepare_logic_segment_query_inputs,
)
from emuflow.logic_segment_timing_storage import (
    read_logic_segment_timing_storage,
)
from emuflow.logic_segment_identity_storage import (
    LogicSegmentIdentitySpool,
    read_logic_segment_identity_storage,
    write_logic_segment_identity_storage,
)
import emuflow.logic_segment_identity_storage as identity_storage
import emuflow.logic_segment_timing as logic_segment_timing
from emuflow.local_path_timing import (
    LOCAL_PATH_IDENTITY_STORAGE_QUALIFICATION,
    _resolve_local_path_fpga,
    local_path_timing_header,
    path_id_set_sha256,
    prepare_local_path_query_inputs,
    read_local_path_identity,
    validate_local_path_identity,
    validate_local_path_timing,
    write_xilinx_local_path_query,
)
from emuflow.local_path_timing_storage import (
    LOCAL_PATH_TIMING_STORAGE_SCHEMA,
    read_local_path_timing_storage,
    write_local_path_timing_storage,
)
from emuflow.routing import SYSTEM_ROUTES_SCHEMA, system_route_timing_paths
from emuflow.platform import Platform


def _import_fixture_logic_measurements(raw, identity, output, **kwargs):
    """Encode small test rows using the maintained eight-column vendor protocol."""
    from emuflow.logic_segment_timing import VIVADO_LOGIC_SEGMENT_TIMING_HEADER
    with tempfile.TemporaryDirectory(dir=raw.parent) as temporary:
        native = Path(temporary) / "measurements.tsv"
        with raw.open() as source, native.open("w") as target:
            header = next(source, "")
            if header != "endpoint\tkind\tdelay_ns\tstart_pin\tend_pin\n":
                target.write(header)
            else:
                target.write(VIVADO_LOGIC_SEGMENT_TIMING_HEADER + "\n")
            for row in source:
                fields = row.rstrip("\n").split("\t")
                if len(fields) != 5:
                    target.write(row)
                    continue
                segment, kind, delay, start, end = fields
                target.write("\t".join((segment.encode().hex(), kind, delay,
                    start.encode().hex(), end.encode().hex(), "endpoint-exact",
                    start.encode().hex(), end.encode().hex())) + "\n")
        return import_vivado_logic_segment_timing(native, identity, output, **kwargs)


class LogicSegmentTimingTest(unittest.TestCase):
    def test_xilinx_ramb18_physical_wea_alias_uses_logical_unisim_bit(self):
        endpoint = {
            "instance": "ram0", "port": "WEA", "bit": 2,
            "object": "ram0/WEA__2",
        }
        instances = {"ram0": {"id": "ram0", "type": "RAMB18E2"}}
        self.assertEqual(
            _normalize_xilinx_logical_endpoint(
                endpoint, {"ram0": {("WEA", 0), ("WEA", 1)}}, instances
            )["bit"],
            0,
        )

    def test_xilinx_ramb18_wea_alias_keeps_distinct_physical_bit(self):
        endpoint = {"instance": "ram0", "port": "WEA", "bit": 2}
        instances = {"ram0": {"id": "ram0", "type": "RAMB18E2"}}
        self.assertEqual(
            _normalize_xilinx_logical_endpoint(
                endpoint,
                {"ram0": {("WEA", 0), ("WEA", 2)}},
                instances,
            )["bit"],
            2,
        )

    def test_compact_local_path_preparation_never_loads_whole_graph(self):
        value = {
            "schema": "emuflow.emuir/v1",
            "design": {"name": "dut", "top": "dut", "source_format": "test"},
            "ports": [],
            "instances": [
                {
                    "id": name,
                    "name": name,
                    "type": "FDRE" if name != "logic" else "LUT1",
                    "resources": {"ff": 1} if name != "logic" else {"lut": 1},
                    "parameters": {},
                    "attributes": {},
                    "constant_connections": [],
                }
                for name in ("launch", "logic", "capture")
            ],
            "nets": [
                {
                    "id": "n0", "name": "n0", "fanout": 1,
                    "cut_class": "register_output",
                    "drivers": [{"instance": "launch", "port": "Q", "bit": 0}],
                    "sinks": [{"instance": "logic", "port": "I0", "bit": 0}],
                },
                {
                    "id": "n1", "name": "n1", "fanout": 1,
                    "cut_class": "combinational",
                    "drivers": [{"instance": "logic", "port": "O", "bit": 0}],
                    "sinks": [{"instance": "capture", "port": "D", "bit": 0}],
                },
            ],
            "clocks": [],
            "warnings": [],
        }
        database = {
            "schema": "emuflow.sta-path-database/v1",
            "design": "dut",
            "source": {"provider": "opensta-fpga-path-database-v1", "input": "test"},
            "normalization": {
                "positive_slack_scale_ns": 10.0,
                "negative_slack_scale_ns": 1.0,
                "max_clock_period_ns": 10.0,
            },
            "paths": [{
                "id": "p0",
                "startpoint": {"object": "launch/Q", "instance": "launch", "port": "Q", "bit": 0},
                "endpoint": {"object": "capture/D", "instance": "capture", "port": "D", "bit": 0},
                "clock_domain": "clk",
                "clock_period_ns": 10.0,
                "slack_ns": 1.0,
                "fixed_delay_ns": 9.0,
                "path_nets": ["n0", "n1"],
                "normalized_slack": 0.1,
            }],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "dut.emuir.sqlite"
            EmuIR(value).save(ir_path)
            assignment = root / "assignment.json"
            write_json(assignment, {
                "schema": "emuflow.partition-assignment/v1",
                "design": "dut",
                "instance_assignment": {
                    "launch": "fpga0", "logic": "fpga0", "capture": "fpga0"
                },
            })
            paths = root / "paths.json"
            write_json(paths, database)
            routes = root / "routes.json"
            write_json(routes, {"schema": SYSTEM_ROUTES_SCHEMA, "routes": []})
            with mock.patch(
                "emuflow.local_path_timing.EmuIR.load",
                side_effect=AssertionError("whole-graph loading forbidden"),
            ):
                prepared = prepare_local_path_query_inputs(
                    ir_path, assignment, paths, routes
                )
            schedule = root / "schedule.json"
            write_json(schedule, {
                "schema": "emuflow.tdm-schedule/v1",
                "design": "dut",
                "platform": "test-platform",
                "entries": [],
            })
            platform = Platform.from_dict({
                "schema": "emuflow.boarddb/v1",
                "platform": {
                    "name": "test-platform", "kind": "virtual", "description": "test"
                },
                "fpgas": [{
                    "id": "fpga0", "part": "test", "utilization_limit": 0.75,
                    "capacity": {"lut": 10, "ff": 10},
                }],
                "links": [],
            })
            with mock.patch(
                "emuflow.logic_segment_timing.EmuIR.load",
                side_effect=AssertionError("whole-graph loading forbidden"),
            ):
                logic = prepare_logic_segment_query_inputs(
                    ir_path, assignment, paths, routes, schedule, platform,
                    allow_legacy_schedule=True,
                )
            value["design"]["name"] = "dut__fpga0"
            merged = root / "merged.emuir.sqlite"
            EmuIR(value).save(merged)
            query = root / "local-path-query.tsv"
            identity_path = root / "local-path-identity.json"
            query_report = write_xilinx_local_path_query(
                ir_path,
                assignment,
                paths,
                routes,
                merged,
                "fpga0",
                query,
                identity_path,
                prepared_inputs=prepared,
            )
            self.assertEqual(query_report["local_paths"], 1)
            selector = read_json(identity_path)
            self.assertEqual(selector["schema"], LOCAL_PATH_TIMING_STORAGE_SCHEMA)
            self.assertNotIn("paths", selector)
            with read_local_path_identity(identity_path) as identity:
                self.assertEqual(
                    validate_local_path_identity(identity)["local_paths"], 1
                )
                self.assertEqual(identity["paths"][0]["id"], "p0")
            self.assertEqual(
                query.read_text(encoding="utf-8").splitlines()[0],
                "endpoint\tkind\tstart_pin\tend_pin\tpath_pins",
            )
        self.assertIsNone(prepared.original_ir)
        self.assertNotIsInstance(prepared.local_paths_by_fpga, dict)
        self.assertNotIsInstance(prepared.cross_path_ids, (set, frozenset))
        self.assertFalse(hasattr(prepared, "original_nets"))
        self.assertEqual(
            prepared.source_manifest["original_path_ids_sha256"],
            path_id_set_sha256(["p0"]),
        )
        self.assertEqual(prepared.path_endpoints["p0"][0]["instance"], "launch")
        self.assertIsNone(logic.original_ir)
        self.assertEqual(set(logic.original_nets), {"n0", "n1"})

    def test_local_path_owner_uses_selected_drivers_for_top_level_ports(self):
        nets = {
            "input": {
                "drivers": [
                    {"instance": None, "port": "address", "bit": 0}
                ],
                "sinks": [
                    {"instance": "logic", "port": "A", "bit": 0},
                    {"instance": "unrelated", "port": "A", "bit": 0},
                ],
            },
            "output": {
                "drivers": [
                    {"instance": "logic", "port": "Y", "bit": 0}
                ],
                "sinks": [
                    {"instance": None, "port": "read_data", "bit": 0}
                ],
            },
        }
        self.assertEqual(
            _resolve_local_path_fpga(
                {"id": "port-to-port", "path_nets": ["input", "output"]},
                {"instance": None, "port": "address", "bit": 0},
                {"instance": None, "port": "read_data", "bit": 0},
                nets,
                {"logic": "fpga0", "unrelated": "fpga1"},
            ),
            "fpga0",
        )

    def test_local_path_owner_rejects_unreported_cross_partition_path(self):
        nets = {
            "n0": {
                "drivers": [
                    {"instance": "launch", "port": "Q", "bit": 0}
                ],
                "sinks": [],
            },
            "n1": {
                "drivers": [
                    {"instance": "logic", "port": "Y", "bit": 0}
                ],
                "sinks": [],
            },
        }
        with self.assertRaisesRegex(
            ValidationError,
            "cross-partition but absent from the Phase 4 timing population",
        ):
            _resolve_local_path_fpga(
                {"id": "missing-cross", "path_nets": ["n0", "n1"]},
                {"instance": "launch", "port": "Q", "bit": 0},
                {"instance": None, "port": "result", "bit": 0},
                nets,
                {"launch": "fpga0", "logic": "fpga1"},
            )

    def test_local_path_owner_keeps_pure_port_passthrough_unresolved(self):
        self.assertIsNone(
            _resolve_local_path_fpga(
                {"id": "passthrough", "path_nets": ["n0"]},
                {"instance": None, "port": "a", "bit": 0},
                {"instance": None, "port": "y", "bit": 0},
                {
                    "n0": {
                        "drivers": [
                            {"instance": None, "port": "a", "bit": 0}
                        ],
                        "sinks": [
                            {"instance": None, "port": "y", "bit": 0}
                        ],
                    }
                },
                {},
            )
        )

    def test_zero_route_flow_has_no_logic_segment_route_timing(self):
        empty_routes = {"schema": SYSTEM_ROUTES_SCHEMA, "routes": []}
        self.assertEqual(_route_timing_paths(empty_routes), [])
        self.assertEqual(
            system_route_timing_paths(
                empty_routes,
                context="local path system routes",
            ),
            [],
        )
        with self.assertRaisesRegex(
            ValidationError, "with demands require timing records"
        ):
            _route_timing_paths(
                {
                    "schema": SYSTEM_ROUTES_SCHEMA,
                    "routes": [{"id": "d0"}],
                }
            )

    def test_exact_capture_index_distinguishes_memory_port_bits(self):
        segment_by_key, captures = _index_exact_logic_segments(
            {
                "capture_requirements": [
                    {
                        "id": f"capture{bit}",
                        "kind": "architectural-state",
                        "endpoint": "ram0",
                        "port": "WEBWE",
                        "bit": bit,
                    }
                    for bit in range(2)
                ],
                "logic_segments": [
                    {
                        "id": f"segment{bit}",
                        "kind": "rx_to_capture",
                        "source_cut_net": "cut0",
                        "fpga": "fpga0",
                        "capture_requirement": f"capture{bit}",
                    }
                    for bit in range(2)
                ],
            }
        )
        self.assertEqual(len(captures), 2)
        self.assertEqual(
            segment_by_key[
                ("capture", "cut0", None, "fpga0", ("ram0", "WEBWE", 0))
            ],
            "segment0",
        )
        self.assertEqual(
            segment_by_key[
                ("capture", "cut0", None, "fpga0", ("ram0", "WEBWE", 1))
            ],
            "segment1",
        )

    def test_exact_launch_cone_keeps_all_local_launches_and_stops_at_cuts(self):
        ir = EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "dut",
                    "top": "dut",
                    "source_format": "test",
                },
                "ports": [
                    {
                        "id": "a",
                        "name": "a",
                        "direction": "input",
                        "width": 1,
                        "clock": False,
                    }
                ],
                "instances": [
                    {
                        "id": "state",
                        "name": "state",
                        "type": "$_DFF_P_",
                        "resources": {"ff": 1},
                        "parameters": {},
                        "attributes": {},
                        "constant_connections": [],
                    },
                    {
                        "id": "remote",
                        "name": "remote",
                        "type": "$lut",
                        "resources": {"lut": 1},
                        "parameters": {},
                        "attributes": {},
                        "constant_connections": [],
                    },
                    {
                        "id": "logic",
                        "name": "logic",
                        "type": "$lut",
                        "resources": {"lut": 1},
                        "parameters": {},
                        "attributes": {},
                        "constant_connections": [],
                    },
                ],
                "nets": [
                    {
                        "id": "primary",
                        "name": "primary",
                        "drivers": [
                            {"instance": None, "port": "a", "bit": 0}
                        ],
                        "sinks": [
                            {"instance": "logic", "port": "A", "bit": 0}
                        ],
                        "fanout": 1,
                        "cut_class": "primary_input",
                    },
                    {
                        "id": "state_net",
                        "name": "state_net",
                        "drivers": [
                            {"instance": "state", "port": "Q", "bit": 0}
                        ],
                        "sinks": [
                            {"instance": "logic", "port": "A", "bit": 1}
                        ],
                        "fanout": 1,
                        "cut_class": "register_output",
                    },
                    {
                        "id": "incoming_cut",
                        "name": "incoming_cut",
                        "drivers": [
                            {"instance": "remote", "port": "Y", "bit": 0}
                        ],
                        "sinks": [
                            {"instance": "logic", "port": "A", "bit": 2}
                        ],
                        "fanout": 1,
                        "cut_class": "combinational",
                    },
                    {
                        "id": "sink_cut",
                        "name": "sink_cut",
                        "drivers": [
                            {"instance": "logic", "port": "Y", "bit": 0}
                        ],
                        "sinks": [],
                        "fanout": 0,
                        "cut_class": "combinational",
                    },
                ],
                "clocks": [],
                "warnings": [],
            }
        )
        result = _architectural_launch_endpoints(
            ir,
            {"state": "fpga0", "logic": "fpga0", "remote": "fpga1"},
            "sink_cut",
            "fpga0",
            _incoming_transported_cut_nets(
                {
                    "cut_nodes": [
                        {
                            "net": "incoming_cut",
                            "source_fpgas": ["fpga1"],
                            "sink_fpgas": ["fpga0"],
                        },
                        {
                            # This net is transported globally, but it is a
                            # local architectural launch on fpga0 and must not
                            # stop the source-cone walk there.
                            "net": "state_net",
                            "source_fpgas": ["fpga0"],
                            "sink_fpgas": ["fpga2"],
                        },
                        {
                            "net": "sink_cut",
                            "source_fpgas": ["fpga0"],
                            "sink_fpgas": ["fpga3"],
                        },
                    ]
                },
                "fpga0",
            ),
        )
        self.assertEqual(
            result,
            [
                {"instance": None, "port": "a", "bit": 0},
                {"instance": "state", "port": "Q", "bit": 0},
            ],
        )
        nets = {item["id"]: item for item in ir.value["nets"]}
        instances = {
            item["id"]: item for item in ir.value["instances"]
        }
        incoming = {}
        for net in ir.value["nets"]:
            for endpoint in net["sinks"]:
                instance = endpoint["instance"]
                if instance is not None:
                    incoming.setdefault(instance, []).append(net["id"])
        self.assertEqual(
            _architectural_launch_endpoints(
                ir,
                {"state": "fpga0", "logic": "fpga0", "remote": "fpga1"},
                "sink_cut",
                "fpga0",
                {"incoming_cut"},
                nets=nets,
                instances=instances,
                incoming=incoming,
            ),
            result,
        )

    def test_local_path_timing_accepts_repeatable_lazy_path_sequence(self):
        record = {
            "id": "local-a",
            "kind": "local",
            "fpga": "fpga0",
            "clock_domain": "clk",
            "clock_period_ns": 10.0,
            "required_time_ns": 9.0,
            "start_pin": "i0.Q[0]",
            "end_pin": "i1.D[0]",
            "measurement": "endpoint-longest-path-fallback",
            "path_pins": [],
            "delay_ns": 2.5,
        }
        database = {
            "schema": "emuflow.local-path-timing/v1",
            "status": "pass",
            "design": "dut",
            "fpga": "fpga0",
            "provider": "test",
            "qualification": "endpoint-exact",
            "identity_schema": "emuflow.local-path-identity/v2",
            "source": {
                "path_database_sha256": "a" * 64,
                "original_ir_sha256": "b" * 64,
                "assignment_sha256": "c" * 64,
                "routes_sha256": "d" * 64,
                "original_paths": 1,
                "original_path_ids_sha256": path_id_set_sha256(["local-a"]),
            },
            "coverage": {"local_paths": 1},
            "paths": (record,),
        }
        self.assertEqual(
            validate_local_path_timing(database),
            {
                "status": "pass",
                "fpga": "fpga0",
                "local_paths": 1,
                "maximum_delay_ns": 2.5,
                "measurement_counts": {
                    "explicit-routed-path-chain": 0,
                    "endpoint-longest-path-fallback": 1,
                    "optimized-away": 0,
                },
                "implemented_paths": 1,
                "optimized_away_paths": 0,
            },
        )

    def test_vivado_pin_mapping_covers_logic_ff_and_memory_endpoints(self):
        ir = EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "dut__fpga0",
                    "top": "dut__fpga0",
                    "source_format": "test",
                },
                "ports": [
                    {
                        "id": "input_bus",
                        "name": "input_bus",
                        "direction": "input",
                        "width": 2,
                        "clock": False,
                    }
                ],
                "instances": [
                    {
                        "id": "lut",
                        "name": "lut",
                        "type": "$lut",
                        "resources": {"lut": 1},
                        "parameters": {"WIDTH": 2, "LUT": "4'b1000"},
                        "attributes": {},
                        "constant_connections": [],
                    },
                    {
                        "id": "ff",
                        "name": "ff",
                        "type": "$_DFF_P_",
                        "resources": {"ff": 1},
                        "parameters": {},
                        "attributes": {},
                        "constant_connections": [],
                    },
                    {
                        "id": "ram",
                        "name": "ram",
                        "type": "RAMB36E2",
                        "resources": {"bram18k": 2},
                        "parameters": {"ADDR_WIDTH": 4, "DATA_WIDTH": 8},
                        "attributes": {},
                        "constant_connections": [],
                    },
                ],
                "nets": [],
                "clocks": [],
                "warnings": [],
            }
        )
        instances = {item["id"]: item for item in ir.value["instances"]}
        pins = {
            "lut": {("A", 0), ("A", 1), ("Y", 0)},
            "ff": {("C", 0), ("D", 0), ("Q", 0)},
            "ram": {
                *(("ADDRARDADDR", bit) for bit in range(4)),
                ("DOUTBDOUT", 3),
            },
        }
        self.assertEqual(
            _vivado_object(
                ir,
                {"instance": "lut", "port": "A", "bit": 1},
                pins,
                instances,
            ),
            ("pin", "lut/I1"),
        )
        self.assertEqual(
            _vivado_object(
                ir,
                {"instance": "ff", "port": "D", "bit": 0},
                pins,
                instances,
            ),
            ("pin", "ff/D"),
        )
        self.assertEqual(
            _vivado_object(
                ir,
                {"instance": "ff", "port": "C", "bit": 0},
                pins,
                instances,
            ),
            ("pin", "ff/C"),
        )
        self.assertEqual(
            _vivado_object(
                ir,
                {"instance": "ram", "port": "ADDRARDADDR", "bit": 3},
                pins,
                instances,
            ),
            ("pin", "ram/ADDRARDADDR[3]"),
        )
        self.assertEqual(
            _vivado_object(
                ir,
                {
                    "object": "ram/memory_reg_bram_0/CLKBWRCLK",
                    "instance": "ram",
                    "port": "DOUTBDOUT",
                    "bit": 3,
                },
                pins,
                instances,
            ),
            ("pin", "ram/memory_reg_bram_0/CLKBWRCLK"),
        )
        self.assertEqual(
            _vivado_object(
                ir,
                {"instance": None, "port": "input_bus", "bit": 1},
            ),
            ("port", "input_bus[1]"),
        )

    def test_import_is_identity_and_coverage_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity_path = root / "identity.json"
            write_json(
                identity_path,
                {
                    "schema": "emuflow.logic-segment-identity/v1",
                    "system_routes_sha256": "a" * 64,
                    "tdm_schedule_sha256": "b" * 64,
                    "status": "pass",
                    "design": "dut",
                    "platform": "board",
                    "fpga": "fpga0",
                    "provider": "test",
                    "coverage": {
                        "segments": 1,
                        "system_paths": 1,
                        "member_paths": 1,
                        "unsupported_member_paths": 0,
                    },
                    "unsupported_member_paths": [],
                    "segments": [
                        {
                            "id": "logic0",
                            "kind": "launch",
                            "system_path": "path0",
                            "member_path": "member0",
                            "cut_index": 0,
                            "fpga": "fpga0",
                            "replace_tx_endpoint": "tx0",
                            "start_pin": "i0.Q[0]",
                            "end_pin": "out:n0.outpad[0]",
                        }
                    ],
                },
            )
            raw = root / "timing.tsv"
            raw.write_text(
                "endpoint\tkind\tdelay_ns\tstart_pin\tend_pin\n"
                "logic0\tlaunch\t2.5\ti0.Q[0]\tout:n0.outpad[0]\n",
                encoding="utf-8",
            )
            output = root / "timing.json"
            report = _import_fixture_logic_measurements(
                raw, identity_path, output, allow_legacy_identity=True
            )
            self.assertEqual(report["segments"], 1)
            self.assertEqual(report["maximum_delay_ns"], 2.5)

    def test_vivado_import_is_identity_and_coverage_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity_path = root / "identity.json"
            segment = {
                "id": "logic0",
                "kind": "capture",
                "system_path": "path0",
                "member_path": "member0",
                "cut_index": 1,
                "fpga": "fpga0",
                "replace_tx_endpoint": None,
                "start_pin": "rx_shadow/Q",
                "end_pin": "dut_reg/D",
                "start_object_kind": "pin",
                "end_object_kind": "pin",
            }
            write_json(
                identity_path,
                {
                    "schema": "emuflow.logic-segment-identity/v1",
                    "system_routes_sha256": "a" * 64,
                    "tdm_schedule_sha256": "b" * 64,
                    "status": "pass",
                    "design": "dut",
                    "platform": "board",
                    "fpga": "fpga0",
                    "provider": "test",
                    "coverage": {
                        "segments": 1,
                        "system_paths": 1,
                        "member_paths": 1,
                        "unsupported_member_paths": 0,
                    },
                    "unsupported_member_paths": [],
                    "segments": [segment],
                },
            )
            raw = root / "timing.tsv"
            raw.write_text(
                "endpoint_hex\tkind\tdelay_ns\tstart_object_hex\t"
                "end_object_hex\n"
                + "\t".join(
                    (
                        segment["id"].encode().hex(),
                        segment["kind"],
                        "3.25",
                        segment["start_pin"].encode().hex(),
                        segment["end_pin"].encode().hex(),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            output = root / "timing.json"
            report = import_vivado_logic_segment_timing(
                raw, identity_path, output, allow_legacy_identity=True,
                allow_legacy_timing_format=True,
            )
            self.assertEqual(report["segments"], 1)
            self.assertEqual(report["maximum_delay_ns"], 3.25)
            missing_raw = root / "missing.tsv"
            missing_raw.write_text(
                "endpoint_hex\tkind\tdelay_ns\tstart_object_hex\t"
                "end_object_hex\n",
                encoding="utf-8",
            )
            partial = import_vivado_logic_segment_timing(
                missing_raw,
                identity_path,
                root / "partial.json",
                qualification="routed-board-integrated-endpoint-chain",
                allow_missing=True,
                allow_legacy_identity=True,
                allow_legacy_timing_format=True,
            )
            self.assertEqual(partial["segments"], 0)
            self.assertEqual(partial["missing_segments"], 1)
            with read_logic_segment_timing_storage(
                root / "partial.json"
            ) as partial_database:
                self.assertEqual(
                    partial_database["unmeasured_segments"][0]["id"],
                    segment["id"],
                )

    def test_vivado_import_preserves_cone_bound_measurement_trace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity_path = root / "identity.json"
            segment = {
                "id": "logic0",
                "kind": "launch",
                "system_path": "path0",
                "member_path": "member0",
                "cut_index": 0,
                "fpga": "fpga0",
                "replace_tx_endpoint": "tx0",
                "start_pin": "spurious_ff/Q",
                "end_pin": "tx_port[0]",
                "start_object_kind": "pin",
                "end_object_kind": "port",
                "cone_anchor_object_kind": "pin",
                "cone_anchor_pin": "cut_driver/O",
            }
            write_json(
                identity_path,
                {
                    "schema": "emuflow.logic-segment-identity/v1",
                    "system_routes_sha256": "a" * 64,
                    "tdm_schedule_sha256": "b" * 64,
                    "status": "pass",
                    "design": "dut",
                    "platform": "board",
                    "fpga": "fpga0",
                    "provider": "test",
                    "coverage": {
                        "segments": 1,
                        "system_paths": 1,
                        "member_paths": 1,
                        "unsupported_member_paths": 0,
                    },
                    "unsupported_member_paths": [],
                    "segments": [segment],
                },
            )
            fields = (
                segment["id"].encode().hex(),
                segment["kind"],
                "3.125",
                segment["start_pin"].encode().hex(),
                segment["end_pin"].encode().hex(),
                "cut-net-cone-upper-bound",
                "real_ff/C".encode().hex(),
                "pcs_fifo/D".encode().hex(),
            )
            raw = root / "timing.tsv"
            raw.write_text(
                "endpoint_hex\tkind\tdelay_ns\tstart_object_hex\t"
                "end_object_hex\tmeasurement\tactual_start_object_hex\t"
                "actual_end_object_hex\n"
                + "\t".join(fields)
                + "\n",
                encoding="utf-8",
            )
            output = root / "timing.json"
            report = import_vivado_logic_segment_timing(
                raw, identity_path, output, allow_legacy_identity=True
            )
            self.assertEqual(report["cone_bound_segments"], 1)
            with read_logic_segment_timing_storage(output) as database:
                self.assertEqual(
                    database["segments"][0]["measurement"],
                    "cut-net-cone-upper-bound",
                )
                self.assertEqual(
                    database["segments"][0]["actual_start_object"],
                    "real_ff/C",
                )
            collection_fields = list(fields)
            collection_fields[5] = "optimized-endpoint-collection-upper-bound"
            raw.write_text(raw.read_text().splitlines()[0]+"\n"+"\t".join(collection_fields)+"\n")
            collection_output = root / "collection.json"
            collection = import_vivado_logic_segment_timing(raw, identity_path, collection_output, allow_legacy_identity=True)
            self.assertEqual(collection["endpoint_collection_bound_segments"], 1)
            self.assertEqual(collection["endpoint_exact_segments"], 0)
            self.assertEqual(collection["cone_bound_segments"], 0)
            with read_logic_segment_timing_storage(collection_output) as database:
                self.assertEqual(database.validate_timing()["endpoint_collection_bound_segments"], 1)
                self.assertEqual(database["segments"][0]["measurement"], collection_fields[5])

    @staticmethod
    def _typed_identity(root, count=3):
        path = root / "identity.json"
        header = {
            "schema": "emuflow.logic-segment-identity/v1",
            "status": "pass",
            "design": "dut",
            "platform": "board",
            "fpga": "fpga0",
            "provider": "test",
            "coverage": {},
            "system_routes_sha256": "a" * 64,
            "tdm_schedule_sha256": "b" * 64,
            "semantic_contract_sha256": "c" * 64,
        }

        def records():
            for index in range(count):
                yield {
                    "id": f"logic{index:08d}",
                    "kind": "launch",
                    "system_path": f"path{index:08d}",
                    "member_path": f"member{index:08d}",
                    "cut_index": 0,
                    "fpga": "fpga0",
                    "replace_tx_endpoint": f"tx{index:08d}",
                    "start_pin": f"start{index:08d}",
                    "end_pin": f"end{index:08d}",
                    "static_exact_segment_id": f"segment{index:08d}",
                }

        view = write_logic_segment_identity_storage(
            path, header=header, segments=records()
        )
        view.close()
        return path, header

    def test_typed_identity_import_matches_explicit_legacy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            typed, _header = self._typed_identity(root)
            raw = root / "timing.tsv"
            with raw.open("w", encoding="utf-8") as stream:
                stream.write("endpoint\tkind\tdelay_ns\tstart_pin\tend_pin\n")
                for index in range(3):
                    stream.write(
                        f"logic{index:08d}\tlaunch\t{index + 0.25}\t"
                        f"start{index:08d}\tend{index:08d}\n"
                    )
            typed_output = root / "typed-timing.json"
            _import_fixture_logic_measurements(raw, typed, typed_output)
            with read_logic_segment_identity_storage(typed) as view:
                legacy_value = {
                    key: view[key] for key in view
                    if key not in {"segments", "unsupported_member_paths"}
                }
                legacy_value["segments"] = list(view["segments"])
                legacy_value["unsupported_member_paths"] = []
            legacy = root / "legacy.json"
            write_json(legacy, legacy_value)
            legacy_output = root / "legacy-timing.json"
            _import_fixture_logic_measurements(
                raw, legacy, legacy_output, allow_legacy_identity=True
            )
            typed_selector = read_json(typed_output)
            legacy_selector = read_json(legacy_output)
            self.assertEqual(
                typed_selector["logical_sha256"],
                legacy_selector["logical_sha256"],
            )
            with (
                read_logic_segment_timing_storage(typed_output) as left,
                read_logic_segment_timing_storage(legacy_output) as right,
            ):
                self.assertEqual(list(left["segments"]), list(right["segments"]))

    def test_typed_identity_import_rejects_duplicate_missing_and_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _header = self._typed_identity(root)
            header = "endpoint\tkind\tdelay_ns\tstart_pin\tend_pin\n"

            def row(index):
                return (
                    f"logic{index:08d}\tlaunch\t1.0\t"
                    f"start{index:08d}\tend{index:08d}\n"
                )

            for name, body in (
                ("duplicate", header + row(0) + row(0) + row(2)),
                ("missing", header + row(0) + row(2)),
                ("order", header + row(1) + row(0) + row(2)),
                ("truncated", header + row(0) + "logic00000001\tlaunch\t"),
            ):
                raw = root / f"{name}.tsv"
                raw.write_text(body, encoding="utf-8")
                with self.assertRaises(ValidationError, msg=name):
                    _import_fixture_logic_measurements(
                        raw, identity, root / f"{name}-out.json"
                    )

    def test_typed_identity_is_source_sealed_and_legacy_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, header = self._typed_identity(root, 1)
            with self.assertRaisesRegex(ValidationError, "source seal"):
                read_logic_segment_identity_storage(
                    identity, expected_system_routes_sha256="d" * 64
                )
            with read_logic_segment_identity_storage(
                identity,
                expected_system_routes_sha256=header["system_routes_sha256"],
                expected_tdm_schedule_sha256=header["tdm_schedule_sha256"],
                expected_semantic_contract_sha256=header[
                    "semantic_contract_sha256"
                ],
            ) as view:
                self.assertEqual(len(view["segments"]), 1)
            legacy = root / "legacy.json"
            write_json(legacy, {
                **header,
                "coverage": {
                    "segments": 0, "system_paths": 0, "member_paths": 0,
                    "unsupported_member_paths": 0,
                },
                "segments": [], "unsupported_member_paths": [],
            })
            with self.assertRaisesRegex(ValidationError, "explicit compatibility"):
                read_logic_segment_identity_storage(legacy)

    def test_typed_identity_rejects_truncated_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _header = self._typed_identity(root, 4)
            selector = read_json(identity)
            store = identity.parent / selector["artifact"]
            store.write_bytes(store.read_bytes()[:-64])
            with self.assertRaisesRegex(ValidationError, "seal disagrees"):
                read_logic_segment_identity_storage(identity)

    def test_typed_identity_large_import_has_bounded_python_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            count = 100_000
            identity, _header = self._typed_identity(root, count)
            raw = root / "timing.tsv"
            with raw.open("w", encoding="utf-8") as stream:
                stream.write("endpoint\tkind\tdelay_ns\tstart_pin\tend_pin\n")
                for index in range(count):
                    stream.write(
                        f"logic{index:08d}\tlaunch\t1.25\t"
                        f"start{index:08d}\tend{index:08d}\n"
                    )
            tracemalloc.start()
            report = _import_fixture_logic_measurements(
                raw, identity, root / "timing.json"
            )
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertEqual(report["segments"], count)
            self.assertLess(peak, 16 * 1024 * 1024)

    def test_typed_identity_rejects_lock_symlink_and_uri_metacharacters_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad = root / "bad.json"
            lock = root / ".bad.json.lock"
            lock.symlink_to(root / "attacker-lock")
            with self.assertRaisesRegex(ValidationError, "symbolic link"):
                read_logic_segment_identity_storage(bad)
            special = root / "hash?# directory"
            identity, _header = self._typed_identity(special, 2)
            with read_logic_segment_identity_storage(identity) as view:
                self.assertEqual(len(view["segments"]), 2)

    def test_typed_identity_selector_rejects_bool_versions_and_bad_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _header = self._typed_identity(root, 2)
            original = read_json(identity)
            attacks = []
            for field in ("application_id", "user_version"):
                value = {**original, field: True}
                attacks.append(value)
            value = {**original, "counts": {**original["counts"], "segments": True}}
            attacks.append(value)
            value = {
                **original,
                "header": {
                    **original["header"],
                    "coverage": {**original["header"]["coverage"], "extra": 0},
                },
            }
            attacks.append(value)
            value = {
                **original,
                "header": {
                    **original["header"],
                    "coverage": {
                        **original["header"]["coverage"], "segments": 999,
                    },
                },
            }
            attacks.append(value)
            for attack in attacks:
                write_json(identity, attack, compact=True)
                with self.assertRaises(ValidationError):
                    read_logic_segment_identity_storage(identity)
            write_json(identity, original, compact=True)

    def test_typed_identity_rejects_resealed_sqlite_schema_attack(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _header = self._typed_identity(root, 2)
            selector = read_json(identity)
            old_store = root / selector["artifact"]
            with closing(sqlite3.connect(old_store)) as connection, connection:
                connection.execute("CREATE TABLE injected(value TEXT)")
            digest = hashlib.sha256(old_store.read_bytes()).hexdigest()
            new_store = root / f"{identity.name}.store.{digest}.sqlite"
            old_store.replace(new_store)
            selector.update({
                "artifact": new_store.name,
                "store_sha256": digest,
                "store_bytes": new_store.stat().st_size,
            })
            write_json(identity, selector, compact=True)
            with self.assertRaisesRegex(ValidationError, "objects are invalid"):
                read_logic_segment_identity_storage(identity)

    def test_query_spool_is_removed_on_every_early_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / f"p{index}" for index in range(10)]
            with mock.patch.object(
                logic_segment_timing,
                "_write_logic_segment_query_impl",
                side_effect=ValidationError("injected early failure"),
            ):
                with self.assertRaisesRegex(ValidationError, "injected"):
                    logic_segment_timing._write_logic_segment_query(
                        *paths[:5], None, paths[5], paths[6], "fpga0", paths[7],
                        paths[8], object_provider="xilinx",
                    )
            self.assertEqual(
                list(root.glob(".logic-segment-identity.*.spool.sqlite")), []
            )

    @staticmethod
    def _empty_query_inputs():
        return LogicSegmentQueryInputs(
            original_ir=None,
            assignment={"design": "dut", "instance_assignment": {}},
            path_database={},
            routes={},
            schedule={},
            object_index=None,
            path_endpoints={},
            database_paths={},
            route_timing={},
            route_by_net={},
            timing_records=[],
            original_nets={},
            original_instances={},
            incoming_nets_by_instance={},
            exact_contract=None,
            exact_contract_sha256=None,
            exact_segment_by_key={},
            exact_captures={},
            exact_launches={},
            exact_index_path=None,
        )

    def test_indexed_query_reader_closes_on_merged_design_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            boundary = root / "boundary.json"
            write_json(boundary, {
                "schema": "emuflow.boundary-identity/v1",
                "endpoints": [],
            })

            class FakeIndexedPhysicalIR:
                def __init__(self, _path):
                    self.value = {"design": {"name": "wrong"}}
                    self.closed = False
                    FakeIndexedPhysicalIR.last = self

                def close(self):
                    self.closed = True

            with (
                mock.patch.object(
                    logic_segment_timing, "IndexedPhysicalIR",
                    FakeIndexedPhysicalIR,
                ),
                mock.patch.object(
                    logic_segment_timing, "is_emuir_sqlite", return_value=True,
                ),
                mock.patch.object(
                    logic_segment_timing, "is_compact_emuir", return_value=True,
                ),
                self.assertRaisesRegex(ValidationError, "merged IR target"),
            ):
                logic_segment_timing._write_logic_segment_query(
                    root / "original.sqlite",
                    root / "assignment.json",
                    root / "paths.json",
                    root / "routes.json",
                    root / "schedule.json",
                    SimpleNamespace(name="board"),
                    root / "merged.sqlite",
                    boundary,
                    "fpga0",
                    root / "query.tsv",
                    root / "identity.json",
                    object_provider="xilinx",
                    prepared_inputs=self._empty_query_inputs(),
                )
            self.assertTrue(FakeIndexedPhysicalIR.last.closed)
            self.assertEqual(
                list(root.glob(".logic-segment-identity.*.spool.sqlite")), []
            )
            self.assertEqual(list(root.glob(".query.tsv.*.publish.tmp")), [])

    def test_indexed_query_reader_closes_on_publication_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            boundary = root / "boundary.json"
            write_json(boundary, {
                "schema": "emuflow.boundary-identity/v1",
                "endpoints": [],
            })

            class FakeIndexedPhysicalIR:
                def __init__(self, _path):
                    self.value = {"design": {"name": "dut__fpga0"}}
                    self.instance_indices = {}
                    self.instances = {}
                    self.net_indices = {}
                    self.pins = {}
                    self.closed = False
                    FakeIndexedPhysicalIR.last = self

                def close(self):
                    self.closed = True

            with (
                mock.patch.object(
                    logic_segment_timing, "IndexedPhysicalIR",
                    FakeIndexedPhysicalIR,
                ),
                mock.patch.object(
                    logic_segment_timing, "is_emuir_sqlite", return_value=True,
                ),
                mock.patch.object(
                    logic_segment_timing, "is_compact_emuir", return_value=True,
                ),
                mock.patch.object(
                    logic_segment_timing,
                    "pretty_system_routes_sha256",
                    return_value="a" * 64,
                ),
                mock.patch.object(
                    logic_segment_timing,
                    "canonical_tdm_schedule_sha256",
                    return_value="b" * 64,
                ),
                mock.patch.object(
                    identity_storage,
                    "write_logic_segment_identity_storage",
                    side_effect=OSError("injected publication failure"),
                ),
                self.assertRaisesRegex(OSError, "publication failure"),
            ):
                logic_segment_timing._write_logic_segment_query(
                    root / "original.sqlite",
                    root / "assignment.json",
                    root / "paths.json",
                    root / "routes.json",
                    root / "schedule.json",
                    SimpleNamespace(name="board"),
                    root / "merged.sqlite",
                    boundary,
                    "fpga0",
                    root / "query.tsv",
                    root / "identity.json",
                    object_provider="xilinx",
                    prepared_inputs=self._empty_query_inputs(),
                )
            self.assertTrue(FakeIndexedPhysicalIR.last.closed)
            self.assertEqual(
                list(root.glob(".logic-segment-identity.*.spool.sqlite")), []
            )
            self.assertEqual(list(root.glob(".query.tsv.*.publish.tmp")), [])
            self.assertFalse((root / "query.tsv").exists())
            self.assertFalse((root / "identity.json").exists())

    def test_identity_and_query_publication_roll_back_together(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, header = self._typed_identity(root, 1)
            query = root / "query.tsv"
            query.write_text("old-query\n", encoding="utf-8")
            old_selector = identity.read_bytes()
            descriptor, name = tempfile.mkstemp(
                prefix=".query.tsv.", suffix=".publish.tmp", dir=root
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write("new-query\n")
                stream.flush()
                os.fsync(stream.fileno())
            sidecar = Path(name)
            real_replace = os.replace

            def fail_selector(source, target):
                if Path(target) == identity and str(source).endswith(".tmp"):
                    raise OSError("injected selector failure")
                return real_replace(source, target)

            with mock.patch.object(identity_storage.os, "replace", side_effect=fail_selector):
                with self.assertRaisesRegex(OSError, "injected selector"):
                    view = write_logic_segment_identity_storage(
                        identity,
                        header=header,
                        segments=({
                            "id": "logic00000000", "kind": "launch",
                            "system_path": "path00000000",
                            "member_path": "member00000000", "cut_index": 0,
                            "fpga": "fpga0", "replace_tx_endpoint": "tx00000000",
                            "start_pin": "start00000000", "end_pin": "end00000000",
                            "static_exact_segment_id": "segment00000000",
                        },),
                        sidecar_publication=(sidecar, query),
                    )
                    view.close()
            self.assertEqual(identity.read_bytes(), old_selector)
            self.assertEqual(query.read_text(encoding="utf-8"), "old-query\n")
            self.assertFalse(sidecar.exists())
            self.assertEqual(list(root.glob(".*.rollback.tmp")), [])

    def test_measurement_import_uses_set_join_without_per_row_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _header = self._typed_identity(root, 256)
            raw = root / "timing.tsv"
            with raw.open("w", encoding="utf-8") as stream:
                stream.write("endpoint\tkind\tdelay_ns\tstart_pin\tend_pin\n")
                for index in range(256):
                    stream.write(
                        f"logic{index:08d}\tlaunch\t1.0\t"
                        f"start{index:08d}\tend{index:08d}\n"
                    )
            statements = []
            real_connect = sqlite3.connect

            def traced_connect(*args, **kwargs):
                connection = real_connect(*args, **kwargs)
                connection.set_trace_callback(statements.append)
                return connection

            with mock.patch.object(
                logic_segment_timing.sqlite3, "connect", side_effect=traced_connect
            ):
                report = _import_fixture_logic_measurements(
                    raw, identity, root / "timing-output.json"
                )
            self.assertEqual(report["segments"], 256)
            normalized = [statement.lower().replace(" ", "") for statement in statements]
            self.assertFalse(any(
                "fromidentity.segmentswheresegment_id=" in statement
                for statement in normalized
            ))
            self.assertTrue(any("leftjoinidentity.segments" in statement for statement in normalized))

    def test_spool_indexes_schedule_and_exact_candidates_without_graph_maps(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace = []
            with LogicSegmentIdentitySpool(root) as spool:
                spool.connection.set_trace_callback(trace.append)
                count = spool.index_schedule_entries(
                    {
                        "id": f"entry{index:08d}", "net": f"net{index // 2:08d}",
                        "from": "fpga0", "to": "fpga1",
                    }
                    for index in range(100_000)
                )
                self.assertEqual(count, 100_000)
                self.assertEqual(
                    [item["id"] for item in spool.schedule_from("net00000000", "fpga0")],
                    ["entry00000000", "entry00000001"],
                )
                record = {
                    "id": "exact0", "kind": "launch", "system_path": "p",
                    "member_path": "m", "cut_index": 0, "fpga": "fpga0",
                    "replace_tx_endpoint": "tx0", "start_pin": "s",
                    "end_pin": "e", "static_exact_segment_id": "seg0",
                }
                self.assertTrue(spool.append_if_new_exact(record))
                self.assertFalse(spool.append_if_new_exact({**record, "id": "exact1"}))
                spool.index_exact_contract(
                    ({
                        "id": "capture0", "cut_net": "net0", "fpga": "fpga0",
                        "kind": "architectural-state", "endpoint": "reg0",
                        "port": "D", "bit": 0,
                    },),
                    ({
                        "id": "segment0", "kind": "rx_to_capture",
                        "fpga": "fpga0", "source_cut_net": "net0",
                        "capture_requirement": "capture0",
                    },),
                )
                self.assertEqual(
                    spool.exact_segment_id(
                        ("capture", "net0", None, "fpga0", ("reg0", "D", 0))
                    ),
                    "segment0",
                )
                self.assertEqual(spool.exact_capture("capture0")["bit"], 0)
            self.assertFalse(any(
                statement.lstrip().upper().startswith("SELECT 1 FROM SEGMENTS")
                for statement in trace
            ))

    def test_vivado_five_column_timing_requires_explicit_compatibility(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _header = self._typed_identity(root, 1)
            raw = root / "legacy.tsv"
            raw.write_text(
                "endpoint_hex\tkind\tdelay_ns\tstart_object_hex\tend_object_hex\n"
                + "\t".join((
                    "logic00000000".encode().hex(), "launch", "1.0",
                    "start00000000".encode().hex(), "end00000000".encode().hex(),
                )) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "explicit compatibility"):
                import_vivado_logic_segment_timing(
                    raw, identity, root / "strict.json"
                )
            report = import_vivado_logic_segment_timing(
                raw, identity, root / "compatible.json",
                allow_legacy_timing_format=True,
            )
            self.assertEqual(report["segments"], 1)


if __name__ == "__main__":
    unittest.main()
