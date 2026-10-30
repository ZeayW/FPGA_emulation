import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.common.errors import EmuFlowError, ValidationError
from emuflow.common.json_io import write_json
from emuflow.ir.ir import EmuIR
from tests.yosys_reference import import_yosys_json
from tests.vivado_query_host import write_vivado_boundary_query as write_vivado_boundary_timing_query
from tests.vivado_input_host import prepare_vivado_inputs
from emuflow_tools.ir.emuir_fixture import write_compact_emuir
from tests.vivado_measurement_host import import_indexed_vivado_boundary
from emuflow_tools.physical.vivado.vivado_netlist import emit_vivado_mapped_verilog


def _ir():
    return EmuIR(
        {
            "schema": "emuflow.emuir/v1",
            "design": {
                "name": "partition",
                "top": "partition",
                "source_format": "test",
            },
            "ports": [
                {
                    "id": "clk",
                    "name": "clk",
                    "direction": "input",
                    "width": 1,
                    "clock": True,
                },
                {
                    "id": "fabric_clk",
                    "name": "fabric_clk",
                    "direction": "input",
                    "width": 1,
                    "clock": True,
                },
            ],
            "instances": [
                {
                    "id": "dut",
                    "name": "dut",
                    "type": "LUT1",
                    "resources": {"lut": 1},
                    "parameters": {"INIT": "10"},
                    "attributes": {},
                    "constant_connections": [],
                },
                {
                    "id": "transport",
                    "name": "transport",
                    "type": "LUT1",
                    "resources": {"lut": 1},
                    "parameters": {"INIT": "10"},
                    "attributes": {},
                    "constant_connections": [],
                },
            ],
            "nets": [
                {
                    "id": "n0",
                    "name": "n0",
                    "aliases": [],
                    "bus_index": 0,
                    "cut_class": "clock",
                    "drivers": [
                        {"instance": None, "port": "clk", "bit": 0}
                    ],
                    "sinks": [
                        {"instance": "dut", "port": "I0", "bit": 0}
                    ],
                    "fanout": 1,
                }
            ],
            "clocks": [],
            "warnings": [],
        }
    )


RUNTIME = {
    "fabric_clock": {"period_ns": 4.0},
    "virtual_dut_clocks": {"clk": {"nominal_period_ns": 100.0}},
    "virtual_dut_clock": {"nominal_period_ns": 100.0},
    "timing_model": {
        "dut_clock_ports": ["clk"],
        "fabric_clock_port": "fabric_clk",
        "fabric_to_dut_max_delay_ns": 8.0,
    },
}


class VivadoBackendTest(unittest.TestCase):
    def test_whole_emuir_export_and_physical_execution_are_rejected(self):
        from emuflow_tools.physical.vivado.vivado_physical import run_indexed_vivado_partition

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / "ir.json", root / "physical"
            write_json(source, _ir().to_dict())
            with patch("emuflow.ir.ir.EmuIR.load", side_effect=AssertionError("whole graph")), \
                 patch("emuflow_tools.physical.vivado.vivado_physical.shutil.which", side_effect=AssertionError("tool lookup")):
                with self.assertRaisesRegex(ValidationError, "current indexed EmuIR"):
                    emit_vivado_mapped_verilog(source, root / "partition.v")
                with self.assertRaisesRegex(ValidationError, "current indexed EmuIR"):
                    run_indexed_vivado_partition(
                        fpga="fpga0", part="xcvu19p-fsva3824-2-e", ir_path=source,
                        mapped_verilog_path=root / "partition.v", mapped_report={},
                        runtime=RUNTIME, original_cells=2, transport_cells=0,
                        output_dir=output,
                    )
            self.assertFalse((root / "partition.v").exists())
            self.assertFalse(output.exists())

    def test_retired_timing_and_sampled_controls_are_not_public_interfaces(self):
        from contextlib import redirect_stderr
        from io import StringIO
        from emuflow.flow.cli import _build_parser
        from emuflow.flow.native_compile import run_native_multi_fpga_flow as run_multi_fpga_flow
        from emuflow_tools.stage_clients.physical import run_multi_fpga_physical_flow
        from emuflow.flow.native_compile import _request
        from tests.preflight_reference import _initial_context

        parser = _build_parser()
        compile_args = ["multi-fpga", "compile", "design.v", "--out", "out"]
        physical_args = ["multi-fpga", "physical", "--split", "split", "--platform", "board",
                         "--schedule", "schedule", "--out", "out"]
        cases = [(compile_args, "--timing-backend", "vivado"),
                 (compile_args, "--timing-vivado", "vivado"),
                 (compile_args, "--physical-vivado-max-timing-paths", "1"),
                 (physical_args, "--vivado-max-timing-paths", "1")]
        for args, option, value in cases:
            with self.subTest(option=option), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                parser.parse_args([*args, option, value])
        for command in ("emit-vivado-cut-map", "import-vivado-tsv",
                        "emit-vivado-net-map", "import-vivado-path-database"):
            error = StringIO()
            with self.subTest(command=command), redirect_stderr(error), self.assertRaises(SystemExit):
                parser.parse_args(["sta", command])
            self.assertIn("invalid choice", error.getvalue())
        for option in ("timing_backend", "timing_vivado", "physical_vivado_max_timing_paths"):
            with self.subTest(option=option), self.assertRaises(TypeError):
                run_multi_fpga_flow(**{option: "retired"})
            with self.subTest(native_option=option), self.assertRaises(TypeError):
                _request((), {"output_dir": Path("out"), option: "retired"})
            with self.subTest(adapter_option=option), self.assertRaises(TypeError):
                _initial_context({"options": {"output_dir": str(Path("out").absolute()),
                                              option: "retired"}})
        with self.assertRaises(TypeError):
            run_multi_fpga_physical_flow(Path("split"), Path("board"), Path("schedule"),
                                         Path("out"), vivado_max_timing_paths=1)

    def test_vivado_boundary_timing_uses_stable_endpoint_identities(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "ir.sqlite"
            identity_path = root / "identity.json"
            query_path = root / "query.tsv"
            timing_path = root / "timing.tsv"
            output_path = root / "timing.json"
            value = _ir().to_dict()
            value["design"]["name"] = "partition__fpga0"
            value["ports"].append(
                {
                    "id": "tx_link_fpga1",
                    "name": "tx_link_fpga1",
                    "direction": "output",
                    "width": 1,
                    "clock": False,
                }
            )
            value["nets"][0]["sinks"].append(
                {
                    "instance": None,
                    "port": "tx_link_fpga1",
                    "bit": 0,
                }
            )
            value["nets"][0]["fanout"] = 2
            from emuflow_tools.ir.emuir_fixture import write_compact_emuir
            write_compact_emuir(ir_path, value)
            identity = {
                "schema": "emuflow.boundary-identity/v1",
                "status": "pass",
                "design": "partition",
                "platform": "board",
                "fpga": "fpga0",
                "provider": "test",
                "coverage": {
                    "endpoints": 1,
                    "tx": 1,
                    "rx": 0,
                    "external_port_nets": 1,
                },
                "endpoints": [
                    {
                        "id": "tx0",
                        "kind": "tx",
                        "schedule_entry": "s0",
                        "merged_ir": {
                            "external_port": "tx_link_fpga1",
                            "external_port_bit": 0,
                            "external_net": "n0",
                            "logical_net": "n0",
                            "boundary_register_instances": [],
                        },
                    }
                ],
            }
            from emuflow_tools.timing.boundary_identity_storage import write_boundary_identity_storage
            identity["endpoints"][0].update(demand="d0",logical_net="n0",link="link",peer="fpga1",
                lane=0,logical_lane=0,internal_signal="net:n0",source_class="dut-net")
            with write_boundary_identity_storage(identity_path,
                    {key:value for key,value in identity.items() if key!="endpoints"},identity["endpoints"]):
                pass
            query = write_vivado_boundary_timing_query(
                ir_path, identity_path, query_path
            )
            query_text = Path(query["output"]).read_text(encoding="utf-8")
            timing_path.write_text(
                "endpoint_hex\tkind\tdelay_ns\tstart_object_hex\t"
                "end_object_hex\n"
                + "\t".join(
                    (
                        "tx0".encode().hex(),
                        "tx",
                        "1.75",
                        "__emuflow_net_0".encode().hex(),
                        "tx_link_fpga1[0]".encode().hex(),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            imported = import_indexed_vivado_boundary(
                timing_path, identity_path, output_path
            )

        self.assertEqual(query["endpoints"], 1)
        self.assertIn("__emuflow_net_0".encode().hex(), query_text)
        self.assertEqual(imported["maximum_delay_ns"], 1.75)


    def test_native_boundary_query_validates_typed_owner_and_replays_rows(self):
        import copy
        import csv
        import json
        import sqlite3
        from contextlib import closing
        from emuflow_tools.ir.emuir_fixture import write_compact_emuir
        from emuflow_tools.timing.boundary_identity_storage import write_boundary_identity_storage

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path, selector, requested = root/"merged.sqlite", root/"boundary.json", root/"query.tsv"
            value = _ir().to_dict()
            value["design"]["name"] = "partition__fpga0"
            value["ports"].append(dict(id="rx", name="rx", direction="input", width=1, clock=False))
            register = "capture\\路径"
            value["instances"].append(dict(id=register, name=register, type="FDRE", resources={"ff": 1},
                parameters={}, attributes={}, constant_connections=[]))
            value["nets"].append(dict(id="rxnet", name="rxnet", aliases=[], bus_index=0,
                cut_class="combinational", drivers=[dict(instance=None,port="rx",bit=0)],
                sinks=[dict(instance=register,port="D",bit=0)], fanout=1))
            write_compact_emuir(ir_path, value)
            # Row count is distinct from the compact owner's actual net key.
            with closing(sqlite3.connect(ir_path)) as db, db:
                original = db.execute("SELECT seq FROM net_records WHERE id='n0'").fetchone()[0]
                db.execute("UPDATE endpoint_records SET net_seq=1048576 WHERE net_seq=?", (original,))
                db.execute("UPDATE alias_records SET net_seq=1048576 WHERE net_seq=?", (original,))
                db.execute("UPDATE net_records SET seq=1048576 WHERE id='n0'")
            header = dict(schema="emuflow.boundary-identity/v1",status="pass",design="partition",
                platform="board",fpga="fpga0",provider="test",coverage={})
            record = dict(id="receive",kind="rx",schedule_entry="s0",demand="d0",logical_net="n0",
                link="link",peer="fpga1",lane=0,logical_lane=0,internal_signal="net:n0",source_class="dut-net",
                merged_ir=dict(external_port="rx",external_port_bit=0,external_net="rxnet",
                    logical_net="n0",boundary_register_instances=[register]))
            def publish(item, current_header=header):
                with write_boundary_identity_storage(selector, current_header, [item]):
                    pass
            publish(record)
            requested.write_text("prior query\n")
            with patch("emuflow_tools.timing.boundary_identity_storage._iter_records", side_effect=AssertionError("Python endpoint expansion")), patch(
                    "emuflow_tools.ir.indexed_physical_ir.IndexedPhysicalIR", side_effect=AssertionError("Python graph view")):
                result = write_vivado_boundary_timing_query(ir_path, selector, requested)
            self.assertEqual(requested.read_text(), "prior query\n")
            with Path(result["output"]).open() as stream:
                rows = list(csv.reader(stream, delimiter="\t"))
            self.assertEqual(result["endpoints"], 1)
            self.assertEqual(bytes.fromhex(rows[1][5]).decode(), register.replace("\\", "\\\\"))
            self.assertEqual(bytes.fromhex(rows[1][4]).decode(), "__emuflow_net_1048576")
            for mutate in (
                lambda item: item["merged_ir"].update(boundary_register_instances=[]),
                lambda item: item["merged_ir"].update(boundary_register_instances=["missing"]),
                lambda item: item["merged_ir"].update(external_port_bit=1),
                lambda item: item["merged_ir"].update(external_net="missing"),
                lambda item: item.update(kind="tx",merged_ir={**item["merged_ir"],"logical_net":"missing", "boundary_register_instances": []}),
            ):
                broken = copy.deepcopy(record)
                mutate(broken)
                publish(broken)
                with self.assertRaises(ValidationError):
                    write_vivado_boundary_timing_query(ir_path, selector, requested)
            publish(record, {**header, "design":"other"})
            with self.assertRaisesRegex(ValidationError,"namespace"):
                write_vivado_boundary_timing_query(ir_path, selector, requested)
            publish(record)
            raw = json.loads(selector.read_text())
            raw["store_sha256"] = "0"*64
            selector.write_text(json.dumps(raw))
            with self.assertRaises(ValidationError):
                write_vivado_boundary_timing_query(ir_path, selector, requested)
            self.assertEqual(requested.read_text(), "prior query\n")
            self.assertEqual(len(list(root.glob("query.tsv.generation.*"))), 1)



    def test_current_indexed_export_retains_generic_cell_semantics(self):
        from emuflow_tools.ir.emuir_fixture import write_compact_emuir
        from emuflow_tools.physical.xilinx_netlist import open_xilinx_mapped_index
        value = _ir().to_dict()
        value['instances'][0].update(type='$lut', parameters={'WIDTH': '1', 'LUT': '10'})
        value['nets'][0]['sinks'][0]['port'] = 'A'
        value['instances'][1].update(type='$_DFF_P_', parameters={})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root/'input.sqlite'
            write_compact_emuir(source, value)
            report = emit_vivado_mapped_verilog(source, root/'dut.v')
            with open_xilinx_mapped_index(Path(report['mapped_index']['path'])) as mapped:
                cells = mapped['modules'][value['design']['top']]['cells']
                lut = cells[value['instances'][0]['id']]
                flop = cells[value['instances'][1]['id']]
                self.assertEqual(lut['type'], 'LUT1')
                self.assertEqual(lut['parameters'], {'INIT': '10'})
                self.assertIn('I0', lut['connections'])
                self.assertNotIn('A', lut['connections'])
                self.assertEqual(flop['type'], 'FDRE')
                self.assertEqual(flop['connections']['CE'], ['1'])
                self.assertEqual(flop['connections']['R'], ['0'])

    def test_runtime_xdc_uses_the_common_clock_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ir.sqlite"
            write_compact_emuir(path, _ir().to_dict())
            report = prepare_vivado_inputs(path, Path(temporary), RUNTIME)
            xdc = Path(report["runtime_xdc"]).read_text()
        self.assertIn("emuflow_dut_clk_0 -period 100.000000000", xdc)
        self.assertIn("emuflow_fabric_clk -period 4.000000000", xdc)
        self.assertIn("set_max_delay -datapath_only 8.000000000", xdc)
        self.assertIn("100.000000000 [get_ports {clk}]", xdc)
        self.assertIn("4.000000000 [get_ports {fabric_clk}]", xdc)
        self.assertNotIn("\n  [get_ports", xdc)
        self.assertNotIn("\n  -from", xdc)

    def test_runtime_xdc_allows_a_combinational_dut_partition(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ir.sqlite"
            value = _ir().to_dict()
            value["ports"] = [
                port for port in value["ports"] if port["id"] != "clk"
            ]
            value["nets"] = []
            write_compact_emuir(path, value)
            report = prepare_vivado_inputs(path, Path(temporary), RUNTIME)
            xdc = Path(report["runtime_xdc"]).read_text()
        self.assertNotIn("emuflow_dut_clk", xdc)
        self.assertIn("emuflow_fabric_clk", xdc)


if __name__ == "__main__":
    unittest.main()
