import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.errors import EmuFlowError, ValidationError
from emuflow.io import write_json
from emuflow.ir import EmuIR
from emuflow.logic_segment_identity_storage import (
    write_logic_segment_identity_storage,
)
from emuflow.yosys import import_yosys_json
from emuflow.vivado_backend import (
    vivado_runtime_xdc,
    write_vivado_boundary_timing_query,
)
from emuflow.vivado_physical import execute_vivado
from emuflow.vivado_physical_timing import import_indexed_vivado_boundary
from emuflow.vivado_netlist import (
    emit_vivado_mapped_verilog,
    lower_vivado_primitives,
)


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
        from emuflow.vivado_physical import run_indexed_vivado_partition

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / "ir.json", root / "physical"
            write_json(source, _ir().to_dict())
            with patch("emuflow.ir.EmuIR.load", side_effect=AssertionError("whole graph")), \
                 patch("emuflow.vivado_backend._resolve_vivado", side_effect=AssertionError("tool lookup")):
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
        from emuflow.cli import _build_parser
        from emuflow.multi_fpga_flow import run_multi_fpga_flow
        from emuflow.multi_fpga_physical_flow import run_multi_fpga_physical_flow
        from emuflow.native_compile import _request
        from emuflow.native_compile_adapter import _initial_context

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
            ir_path = root / "ir.json"
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
            write_json(ir_path, value)
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
            write_json(identity_path, identity)
            query = write_vivado_boundary_timing_query(
                ir_path, identity_path, query_path
            )
            query_text = query_path.read_text(encoding="utf-8")
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


    def test_vivado_critical_warning_is_a_hard_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            completed = type(
                "Completed",
                (),
                {
                    "returncode": 0,
                    "stdout": "CRITICAL WARNING: ignored constraint\n",
                },
            )()
            def run(*args, **kwargs):
                kwargs["stdout"].write(completed.stdout)
                return completed

            with patch(
                "emuflow.vivado_physical.subprocess.run",
                side_effect=run,
            ):
                with self.assertRaisesRegex(
                    EmuFlowError, "critical_warnings=1"
                ):
                    execute_vivado(
                        "vivado",
                        root / "provider.tcl",
                        [],
                        root,
                        "vivado.log",
                    )

    def test_shared_xilinx_reference_conversion_retains_generic_cell_semantics(self):
        value = _ir().to_dict()
        value["instances"][0]["type"] = "$lut"
        value["instances"][0]["parameters"] = {
            "WIDTH": "1",
            "LUT": "10",
        }
        value["nets"][0]["sinks"][0]["port"] = "A"
        value["instances"][1]["type"] = "$_DFF_P_"
        value["instances"][1]["parameters"] = {}
        lowered = lower_vivado_primitives(EmuIR(value))
        self.assertEqual(lowered.value["instances"][0]["type"], "LUT1")
        self.assertEqual(lowered.value["instances"][1]["type"], "FDRE")
        self.assertEqual(lowered.value["nets"][0]["sinks"][0]["port"], "I0")

    def test_runtime_xdc_uses_the_common_clock_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ir.json"
            write_json(path, _ir().to_dict())
            xdc = vivado_runtime_xdc(path, RUNTIME)
        self.assertIn("emuflow_dut_clk_0 -period 100.000000000", xdc)
        self.assertIn("emuflow_fabric_clk -period 4.000000000", xdc)
        self.assertIn("set_max_delay -datapath_only 8.000000000", xdc)
        self.assertIn("100.000000000 [get_ports {clk}]", xdc)
        self.assertIn("4.000000000 [get_ports {fabric_clk}]", xdc)
        self.assertNotIn("\n  [get_ports", xdc)
        self.assertNotIn("\n  -from", xdc)

    def test_runtime_xdc_allows_a_combinational_dut_partition(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ir.json"
            value = _ir().to_dict()
            value["ports"] = [
                port for port in value["ports"] if port["id"] != "clk"
            ]
            value["nets"] = []
            write_json(path, value)
            xdc = vivado_runtime_xdc(path, RUNTIME)
        self.assertNotIn("emuflow_dut_clk", xdc)
        self.assertIn("emuflow_fabric_clk", xdc)


if __name__ == "__main__":
    unittest.main()


def test_mapped_name_preserves_vivado_backslash_identity():
    from emuflow.vivado_backend import _vivado_mapped_name
    assert _vivado_mapped_name("$flatten\\cpu") == "$flatten\\\\cpu"
    assert _vivado_mapped_name("dut/路径[0]") == "dut/路径[0]"
