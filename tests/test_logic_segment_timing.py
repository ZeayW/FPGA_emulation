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
    import_vivado_logic_segment_timing as _native_import_vivado_logic_segment_timing,
    prepare_logic_segment_query_inputs,
)
from tests.physical_query_reference import (
    _architectural_launch_endpoints, _index_exact_logic_segments,
    _incoming_transported_cut_nets, _normalize_xilinx_logical_endpoint,
    _route_timing_paths, _vivado_object,
)
from emuflow.logic_segment_timing_storage import (
    read_logic_segment_timing_storage,
)
from emuflow.logic_segment_identity_storage import (
    read_logic_segment_identity_storage,
)
from tests.logic_identity_fixture import identity_facts
import emuflow.logic_segment_identity_storage as identity_storage
import emuflow.logic_segment_timing as logic_segment_timing
from emuflow.local_path_timing import (
    LOCAL_PATH_IDENTITY_STORAGE_QUALIFICATION,
    local_path_timing_header,
    path_id_set_sha256,
    prepare_local_path_query_inputs,
    read_local_path_identity,
    validate_local_path_identity,
    validate_local_path_timing,
    write_xilinx_local_path_query,
)
from emuflow.local_path_timing_storage import (LOCAL_PATH_TIMING_STORAGE_SCHEMA, read_local_path_timing_storage)
from tests.timing_facts import local_timing_facts
from emuflow.routing import SYSTEM_ROUTES_SCHEMA, system_route_timing_paths
from emuflow.platform import Platform


import_vivado_logic_segment_timing = _native_import_vivado_logic_segment_timing


def _identity_document_facts(path, document):
    """Encode explicit small identity facts in the current source-bound owner."""
    with identity_facts(path, header={k:v for k,v in document.items()
            if k not in {"segments", "unsupported_member_paths"}},
            segments=document["segments"],
            unsupported_member_paths=document.get("unsupported_member_paths", ())):
        pass


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

    def _native_local_case(self, function):
        from tests.native_build import emuflow_native
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
                os.environ, {'EMUFLOW_NATIVE_ROOT': str(emuflow_native().parent.parent)}):
            function(Path(temporary))

    def test_compact_local_path_preparation_never_loads_whole_graph(self):
        from tests.test_local_path_query_native import test_prepare_and_query_do_not_construct_python_source_graph
        self._native_local_case(test_prepare_and_query_do_not_construct_python_source_graph)

    def test_local_path_owner_uses_selected_drivers_for_top_level_ports(self):
        from tests.test_local_path_query_native import test_selected_driver_ownership_ignores_unrelated_remote_fanout
        self._native_local_case(test_selected_driver_ownership_ignores_unrelated_remote_fanout)

    def test_local_path_owner_rejects_unreported_cross_partition_path(self):
        from tests.test_local_path_query_native import test_unreported_cross_partition_path_fails_in_full_owner
        self._native_local_case(test_unreported_cross_partition_path_fails_in_full_owner)

    def test_local_path_owner_keeps_pure_port_passthrough_unresolved(self):
        from tests.test_local_path_query_native import test_pure_primary_port_passthrough_does_not_invent_an_fpga
        self._native_local_case(test_pure_primary_port_passthrough_does_not_invent_an_fpga)

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
        with tempfile.TemporaryDirectory() as temporary:
            header = {k:v for k,v in database.items() if k != "paths"}
            with local_timing_facts(Path(temporary) / "local.json",
                    header, database["paths"]) as view:
                self.assertEqual(list(view["paths"]), list(view["paths"]))
                checked = validate_local_path_timing(view)
                self.assertEqual(checked["local_paths"], 1)
                self.assertEqual(checked["maximum_delay_ns"], 2.5)
                self.assertEqual(checked["measurement_counts"]["endpoint-longest-path-fallback"], 1)

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
            _identity_document_facts(
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
                raw, identity_path, output
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
            _identity_document_facts(
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
                raw, identity_path, output,
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
            _identity_document_facts(
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
                raw, identity_path, output
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
            collection = import_vivado_logic_segment_timing(raw, identity_path, collection_output)
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

        view = identity_facts(
            path, header=header, segments=records()
        )
        view.close()
        return path, header

    def test_typed_identity_import_preserves_explicit_measurement_facts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            typed, _header = self._typed_identity(root)
            raw = root / "timing.tsv"
            raw.write_text("endpoint\tkind\tdelay_ns\tstart_pin\tend_pin\n" + "".join(
                f"logic{index:08d}\tlaunch\t{index + 0.25}\tstart{index:08d}\tend{index:08d}\n"
                for index in range(3)))
            output = root / "timing.json"
            report = _import_fixture_logic_measurements(raw, typed, output)
            self.assertEqual(report["segments"], 3)
            with read_logic_segment_timing_storage(output) as view:
                self.assertEqual([(v["id"], v["delay_ns"], v["start_pin"], v["end_pin"])
                    for v in view["segments"]], [(f"logic{i:08d}", i+0.25,
                    f"start{i:08d}", f"end{i:08d}") for i in range(3)])

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
            with self.assertRaisesRegex(ValidationError, "selector"):
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
            with self.assertRaisesRegex(ValidationError, "schema objects"):
                read_logic_segment_identity_storage(identity)

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
                identity_storage.sqlite3, "connect", side_effect=traced_connect
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
            self.assertFalse(any("insertintomeasurements" in statement for statement in normalized))
            # The native importer owns its measurement spool and set joins.
            self.assertFalse(any("fromidentity.segments" in statement for statement in normalized))

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
