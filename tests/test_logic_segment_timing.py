import tempfile
import unittest
from pathlib import Path

from emuflow.io import read_json, write_json
from emuflow.ir import EmuIR
from emuflow.errors import ValidationError
from emuflow.logic_segment_timing import (
    _architectural_launch_endpoints,
    _boundary_tx_port,
    _incoming_transported_cut_nets,
    _vivado_object,
    _vpr_atom_pin,
    import_vivado_logic_segment_timing,
    import_vpr_logic_segment_timing,
)
from emuflow.local_path_timing import (
    _explicit_vpr_path_pins,
    import_vpr_local_path_timing,
    path_id_set_sha256,
    validate_local_path_identity,
    validate_local_path_timing,
)


class LogicSegmentTimingTest(unittest.TestCase):
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

    def test_vpr_boundary_alias_and_explicit_local_path_chain_are_checked(self):
        ir = EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "dut__fpga0",
                    "top": "dut__fpga0",
                    "source_format": "test",
                },
                "ports": [],
                "instances": [
                    {
                        "id": "launch",
                        "name": "launch",
                        "type": "$_DFF_P_",
                        "resources": {"ff": 1},
                        "parameters": {},
                        "attributes": {},
                        "constant_connections": [],
                    },
                    {
                        "id": "logic",
                        "name": "logic",
                        "type": "$lut",
                        "resources": {"lut": 1},
                        "parameters": {"WIDTH": 1, "LUT": "2'b10"},
                        "attributes": {},
                        "constant_connections": [],
                    },
                    {
                        "id": "capture",
                        "name": "capture",
                        "type": "$_DFF_P_",
                        "resources": {"ff": 1},
                        "parameters": {},
                        "attributes": {},
                        "constant_connections": [],
                    },
                ],
                "nets": [
                    {
                        "id": "n0",
                        "name": "n0",
                        "drivers": [
                            {"instance": "launch", "port": "Q", "bit": 0}
                        ],
                        "sinks": [
                            {"instance": "logic", "port": "A", "bit": 0}
                        ],
                        "fanout": 1,
                        "cut_class": "combinational",
                    },
                    {
                        "id": "n1",
                        "name": "n1",
                        "drivers": [
                            {"instance": "logic", "port": "Y", "bit": 0}
                        ],
                        "sinks": [
                            {"instance": "capture", "port": "D", "bit": 0}
                        ],
                        "fanout": 1,
                        "cut_class": "combinational",
                    },
                    {
                        "id": "external",
                        "name": "external",
                        "drivers": [],
                        "sinks": [],
                        "cut_class": "undriven",
                    },
                ],
                "clocks": [],
                "warnings": [],
            }
        )
        endpoints = {
            "tx0": {
                "kind": "tx",
                "merged_ir": {
                    "external_net": "external",
                    "external_port": "tx_link",
                    "external_port_bit": 3,
                },
            }
        }
        self.assertEqual(
            _boundary_tx_port(
                ir,
                {},
                endpoints,
                "tx0",
                {"external": 0},
                {
                    ("tx_link", 3): {
                        "direction": "output",
                        "source_net": "n0",
                        "packed_block": "out:emuflow_top_output_000007",
                    }
                },
            ),
            "out:emuflow_top_output_000007.outpad[0]",
        )

        instances = {item["id"]: item for item in ir.value["instances"]}
        index = {item["id"]: offset for offset, item in enumerate(ir.value["instances"])}
        start = {"instance": "launch", "port": "Q", "bit": 0}
        end = {"instance": "capture", "port": "D", "bit": 0}
        pins = _explicit_vpr_path_pins(
            {"path_nets": ["n0", "n1"]},
            {item["id"]: item for item in ir.value["nets"]},
            ir,
            index,
            instances,
            start,
            end,
        )
        self.assertEqual(
            pins,
            ["i0.Q[0]", "i1.in[0]", "i1.out[0]", "i2.D[0]"],
        )
        identity = {
            "schema": "emuflow.local-path-identity/v2",
            "status": "pass",
            "design": "dut",
            "fpga": "fpga0",
            "provider": "test",
            "source": {
                "path_database_sha256": "a" * 64,
                "original_ir_sha256": "b" * 64,
                "assignment_sha256": "c" * 64,
                "routes_sha256": "d" * 64,
                "original_paths": 1,
                "original_path_ids_sha256": path_id_set_sha256(["p0"]),
            },
            "coverage": {"local_paths": 1},
            "paths": [{
                "id": "p0",
                "kind": "local",
                "fpga": "fpga0",
                "clock_domain": "clk",
                "clock_period_ns": 4.0,
                "required_time_ns": 3.5,
                "start_pin": pins[0],
                "end_pin": pins[-1],
                "measurement": "explicit-routed-path-chain",
                "path_pins": pins,
            }],
        }
        self.assertEqual(validate_local_path_identity(identity)["status"], "pass")
        identity["paths"][0]["path_pins"][-1] = "wrong.D[0]"
        with self.assertRaisesRegex(ValidationError, "path chain"):
            validate_local_path_identity(identity)

    def test_explicit_local_path_chain_falls_back_on_ambiguous_cell_arc(self):
        ir = EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "dut__fpga0",
                    "top": "dut__fpga0",
                    "source_format": "test",
                },
                "ports": [],
                "instances": [
                    {
                        "id": item,
                        "name": item,
                        "type": "$_DFF_P_" if item != "logic" else "$lut",
                        "resources": (
                            {"ff": 1} if item != "logic" else {"lut": 1}
                        ),
                        "parameters": (
                            {} if item != "logic" else {
                                "WIDTH": 2,
                                "LUT": "4'b1000",
                            }
                        ),
                        "attributes": {},
                        "constant_connections": [],
                    }
                    for item in ("launch", "logic", "capture")
                ],
                "nets": [
                    {
                        "id": "n0",
                        "name": "n0",
                        "drivers": [
                            {"instance": "launch", "port": "Q", "bit": 0}
                        ],
                        "sinks": [
                            {"instance": "logic", "port": "A", "bit": 0},
                            {"instance": "logic", "port": "A", "bit": 1},
                        ],
                        "fanout": 2,
                        "cut_class": "combinational",
                    },
                    {
                        "id": "n1",
                        "name": "n1",
                        "drivers": [
                            {"instance": "logic", "port": "Y", "bit": 0}
                        ],
                        "sinks": [
                            {"instance": "capture", "port": "D", "bit": 0}
                        ],
                        "fanout": 1,
                        "cut_class": "combinational",
                    },
                ],
                "clocks": [],
                "warnings": [],
            }
        )
        instances = {item["id"]: item for item in ir.value["instances"]}
        index = {
            item["id"]: offset
            for offset, item in enumerate(ir.value["instances"])
        }
        self.assertEqual(
            _explicit_vpr_path_pins(
                {"path_nets": ["n0", "n1"]},
                {item["id"]: item for item in ir.value["nets"]},
                ir,
                index,
                instances,
                {"instance": "launch", "port": "Q", "bit": 0},
                {"instance": "capture", "port": "D", "bit": 0},
            ),
            [],
        )

    def test_local_path_v2_import_preserves_selected_chain_certificate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity_path = root / "local-identity.json"
            record = {
                "id": "local-a",
                "kind": "local",
                "fpga": "fpga0",
                "clock_domain": "clk",
                "clock_period_ns": 10.0,
                "required_time_ns": 9.0,
                "start_pin": "i0.Q[0]",
                "end_pin": "i1.D[0]",
                "measurement": "explicit-routed-path-chain",
                "path_pins": ["i0.Q[0]", "i1.D[0]"],
            }
            write_json(
                identity_path,
                {
                    "schema": "emuflow.local-path-identity/v2",
                    "status": "pass",
                    "design": "dut",
                    "fpga": "fpga0",
                    "provider": "test",
                    "source": {
                        "path_database_sha256": "a" * 64,
                        "original_ir_sha256": "b" * 64,
                        "assignment_sha256": "c" * 64,
                        "routes_sha256": "d" * 64,
                        "original_paths": 1,
                        "original_path_ids_sha256": path_id_set_sha256(
                            ["local-a"]
                        ),
                    },
                    "coverage": {"local_paths": 1},
                    "paths": [record],
                },
            )
            raw = root / "local.tsv"
            raw.write_text(
                "endpoint\tkind\tdelay_ns\tstart_pin\tend_pin\n"
                "local-a\tlocal\t2.5\ti0.Q[0]\ti1.D[0]\n",
                encoding="utf-8",
            )
            output = root / "local.json"
            import_vpr_local_path_timing(raw, identity_path, output)
            database = read_json(output)
            self.assertEqual(
                database["identity_schema"],
                "emuflow.local-path-identity/v2",
            )
            self.assertEqual(database["paths"][0]["path_pins"], record["path_pins"])
            self.assertEqual(
                validate_local_path_timing(database)["measurement_counts"],
                {
                    "explicit-routed-path-chain": 1,
                    "endpoint-longest-path-fallback": 0,
                },
            )

    def test_local_path_import_is_source_bound_and_coverage_exact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity_path = root / "local-identity.json"
            source_ids = ["local-a", "cross-b"]
            record = {
                "id": "local-a",
                "kind": "local",
                "fpga": "fpga0",
                "clock_domain": "clk",
                "clock_period_ns": 10.0,
                "start_pin": "i0.Q[0]",
                "end_pin": "i1.D[0]",
            }
            write_json(
                identity_path,
                {
                    "schema": "emuflow.local-path-identity/v1",
                    "status": "pass",
                    "design": "dut",
                    "fpga": "fpga0",
                    "provider": "test",
                    "source": {
                        "path_database_sha256": "a" * 64,
                        "original_ir_sha256": "b" * 64,
                        "assignment_sha256": "c" * 64,
                        "routes_sha256": "d" * 64,
                        "original_paths": 2,
                        "original_path_ids_sha256": path_id_set_sha256(
                            source_ids
                        ),
                    },
                    "coverage": {"local_paths": 1},
                    "paths": [record],
                },
            )
            raw = root / "local.tsv"
            raw.write_text(
                "endpoint\tkind\tdelay_ns\tstart_pin\tend_pin\n"
                "local-a\tlocal\t3.25\ti0.Q[0]\ti1.D[0]\n",
                encoding="utf-8",
            )
            output = root / "local.json"
            report = import_vpr_local_path_timing(
                raw, identity_path, output
            )
            self.assertEqual(report["local_paths"], 1)
            self.assertEqual(report["maximum_delay_ns"], 3.25)
            database = read_json(output)
            self.assertEqual(
                validate_local_path_timing(database)["status"], "pass"
            )
            raw.write_text(
                "endpoint\tkind\tdelay_ns\tstart_pin\tend_pin\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValidationError):
                import_vpr_local_path_timing(raw, identity_path, output)

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
                        "type": "VTR_DP_RAM",
                        "resources": {"bram": 1},
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
                *(("addr1", bit) for bit in range(4)),
                ("out2", 3),
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
                {"instance": "ram", "port": "addr1", "bit": 3},
                pins,
                instances,
            ),
            ("pin", "ram/addr1[3]"),
        )
        self.assertEqual(
            _vivado_object(
                ir,
                {
                    "object": "ram/memory_reg_bram_0/CLKBWRCLK",
                    "instance": "ram",
                    "port": "out2",
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

    def test_vpr_pin_mapping_covers_logic_ff_and_memory_endpoints(self):
        ir = EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "dut__fpga0",
                    "top": "dut__fpga0",
                    "source_format": "test",
                },
                "ports": [],
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
                        "type": "VTR_DP_RAM",
                        "resources": {"bram": 1},
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
        index = {"lut": 0, "ff": 1, "ram": 2}
        self.assertEqual(
            _vpr_atom_pin(
                ir, index, {"instance": "lut", "port": "A", "bit": 1}
            ),
            "i0.in[1]",
        )
        self.assertEqual(
            _vpr_atom_pin(
                ir, index, {"instance": "ff", "port": "D", "bit": 0}
            ),
            "i1.D[0]",
        )
        self.assertEqual(
            _vpr_atom_pin(
                ir,
                index,
                {"instance": "ram", "port": "addr1", "bit": 3},
            ),
            "i2__bit0.addr1[3]",
        )
        self.assertEqual(
            _vpr_atom_pin(
                ir,
                index,
                {"instance": "ram", "port": "data1", "bit": 3},
            ),
            "i2__bit3.data1[0]",
        )
        with self.assertRaisesRegex(ValidationError, "DP-RAM pin ram.D"):
            _vpr_atom_pin(
                ir, index, {"instance": "ram", "port": "D", "bit": 0}
            )

    def test_vpr_top_output_uses_eblif_packed_alias(self):
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
                        "id": "result",
                        "name": "result",
                        "direction": "output",
                        "width": 1,
                        "clock": False,
                    }
                ],
                "instances": [],
                "nets": [
                    {
                        "id": "result_net",
                        "name": "result_net",
                        "drivers": [],
                        "sinks": [
                            {"instance": None, "port": "result", "bit": 0}
                        ],
                        "cut_class": "undriven",
                    }
                ],
                "clocks": [],
                "warnings": [],
            }
        )
        self.assertEqual(
            _vpr_atom_pin(
                ir,
                {},
                {"instance": None, "port": "result", "bit": 0},
                {},
                {
                    ("result", 0): {
                        "direction": "output",
                        "source_net": "n0",
                        "packed_block": "out:emuflow_top_output_000003",
                    }
                },
            ),
            "out:emuflow_top_output_000003.outpad[0]",
        )

    def test_import_is_identity_and_coverage_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity_path = root / "identity.json"
            write_json(
                identity_path,
                {
                    "schema": "emuflow.logic-segment-identity/v1",
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
            report = import_vpr_logic_segment_timing(
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
            write_json(
                identity_path,
                {
                    "schema": "emuflow.logic-segment-identity/v1",
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
                raw, identity_path, output
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
            )
            self.assertEqual(partial["segments"], 0)
            self.assertEqual(partial["missing_segments"], 1)
            partial_database = read_json(root / "partial.json")
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
            database = read_json(output)
            self.assertEqual(
                database["segments"][0]["measurement"],
                "cut-net-cone-upper-bound",
            )
            self.assertEqual(
                database["segments"][0]["actual_start_object"],
                "real_ff/C",
            )


if __name__ == "__main__":
    unittest.main()
