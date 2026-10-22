import tempfile
import unittest
from pathlib import Path

from emuflow.io import write_json
from emuflow.boundary_timing_storage import read_boundary_timing_storage
from emuflow.vpr_boundary_timing import (
    import_vpr_boundary_timing,
    write_vpr_boundary_timing_query,
)


class VprBoundaryTimingTest(unittest.TestCase):
    def test_native_traversal_excludes_clock_capture_constraint_edges(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "engines"
            / "vtr"
            / "vpr"
            / "src"
            / "analysis"
            / "timing_reports.cpp"
        ).read_text(encoding="utf-8")
        self.assertEqual(source.count("if (!is_data_delay_edge(edge))"), 2)
        self.assertIn("tatum::EdgeType::PRIMITIVE_COMBINATIONAL", source)
        self.assertIn("tatum::EdgeType::PRIMITIVE_CLOCK_LAUNCH", source)
        self.assertIn("tatum::EdgeType::INTERCONNECT", source)
        self.assertIn(
            "Clock-capture edges carry setup/hold constraints", source
        )
        self.assertIn("explicit path evaluations", source)
        self.assertIn(
            "endpoint\\tkind\\tstart_pin\\tend_pin\\tpath_pins", source
        )
        self.assertIn("matches != 1", source)
        self.assertIn("node_clock_launch_edge", source)
        self.assertIn("result += relax_delay(launch_edge)", source)
        self.assertIn("node_clock_capture_edge", source)
        self.assertIn("result += setup", source)
        self.assertIn('queries[index].kind == "launch"', source)
        self.assertIn('queries[index].kind == "capture"', source)
        self.assertIn("pin_by_name.reserve", source)
        self.assertIn("find_query_pin(fields[2])", source)
        self.assertNotIn("atom_netlist.find_pin(fields[2])", source)

    def test_query_maps_emuir_objects_to_vpr_atom_pins(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "placement.json"
            identity_path = root / "identities.json"
            query_path = root / "query.tsv"
            write_json(
                ir_path,
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
                            "id": "source_lut",
                            "name": "source_lut",
                            "type": "$lut",
                            "resources": {"lut": 1},
                            "parameters": {"WIDTH": 1, "LUT": "2'b10"},
                            "attributes": {},
                            "constant_connections": [],
                        },
                        {
                            "id": "__emuflow_transport__/shadow_ff",
                            "name": "__emuflow_transport__/shadow_ff",
                            "type": "$_DFF_P_",
                            "resources": {"ff": 1},
                            "parameters": {},
                            "attributes": {},
                            "constant_connections": [],
                        },
                    ],
                    "nets": [
                        {
                            "id": "logical",
                            "name": "logical",
                            "drivers": [
                                {"instance": "source_lut", "port": "Y", "bit": 0}
                            ],
                            "sinks": [],
                            "fanout": 0,
                            "cut_class": "combinational",
                        },
                        {
                            "id": "tx-external",
                            "name": "tx-external",
                            "drivers": [],
                            "sinks": [],
                            "fanout": 0,
                            "cut_class": "combinational",
                        },
                        {
                            "id": "rx-external",
                            "name": "rx-external",
                            "drivers": [],
                            "sinks": [],
                            "fanout": 0,
                            "cut_class": "combinational",
                        },
                    ],
                    "clocks": [],
                    "warnings": [],
                },
            )
            identities = {
                "schema": "emuflow.boundary-identity/v1",
                "status": "pass",
                "design": "dut",
                "platform": "board",
                "fpga": "fpga0",
                "provider": "test",
                "coverage": {
                    "endpoints": 2,
                    "tx": 1,
                    "rx": 1,
                    "external_port_nets": 2,
                },
                "endpoints": [
                    {
                        "id": "tx0",
                        "kind": "tx",
                        "schedule_entry": "s0",
                        "merged_ir": {
                            "logical_net": "logical",
                            "external_net": "tx-external",
                            "external_port": "tx_port",
                            "external_port_bit": 0,
                            "boundary_register_instances": [],
                        },
                    },
                    {
                        "id": "rx0",
                        "kind": "rx",
                        "schedule_entry": "s1",
                        "merged_ir": {
                            "logical_net": None,
                            "external_net": "rx-external",
                            "external_port": "rx_port",
                            "external_port_bit": 0,
                            "boundary_register_instances": [
                                "__emuflow_transport__/shadow_ff"
                            ],
                        },
                    },
                ],
            }
            write_json(identity_path, identities)
            report = write_vpr_boundary_timing_query(
                ir_path,
                identity_path,
                query_path,
                eblif_report={
                    "top_ports": [
                        {
                            "port": "tx_port",
                            "bit": 0,
                            "direction": "output",
                            "net": "emuflow_top_output_000000",
                            "source_net": "n1",
                            "packed_block": "out:emuflow_top_output_000000",
                        },
                        {
                            "port": "rx_port",
                            "bit": 0,
                            "direction": "input",
                            "net": "n2",
                            "source_net": "n2",
                            "packed_block": "n2",
                        },
                    ]
                },
            )
            self.assertEqual(report["endpoints"], 2)
            self.assertEqual(
                query_path.read_text(encoding="utf-8").splitlines()[1:],
                [
                    "tx0\ttx\ti0.out[0]\tout:emuflow_top_output_000000.outpad[0]",
                    "rx0\trx\tn2.inpad[0]\ti1.D[0]",
                ],
            )
            timing_tsv = root / "timing.tsv"
            timing_tsv.write_text(
                "endpoint\tkind\tdelay_ns\tstart_pin\tend_pin\n"
                "tx0\ttx\t1.25\ti0.out[0]\tout:emuflow_top_output_000000.outpad[0]\n"
                "rx0\trx\t0.75\tn2.inpad[0]\ti1.D[0]\n",
                encoding="utf-8",
            )
            output = root / "boundary-timing.json"
            imported = import_vpr_boundary_timing(
                timing_tsv, identity_path, query_path, output
            )
            self.assertEqual(imported["maximum_delay_ns"], 1.25)
            with read_boundary_timing_storage(output) as database:
                self.assertEqual(
                    database["qualification"],
                    "routed-academic-architecture-endpoint-exact",
                )


if __name__ == "__main__":
    unittest.main()
