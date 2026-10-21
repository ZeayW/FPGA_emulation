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
from emuflow.sta import VIVADO_PATH_DATABASE_TSV_HEADER
from emuflow.vtr_netlist import normalize_vtr_hard_block_json
from emuflow.yosys import import_yosys_json
from emuflow.vivado_backend import (
    _run_vivado,
    import_vivado_boundary_timing,
    run_vivado_partition_backend,
    run_vivado_timing_path_database,
    validate_vivado_cell_coverage,
    vivado_runtime_xdc,
    write_vivado_boundary_timing_query,
)
from emuflow.vivado_netlist import (
    emit_vivado_mapped_verilog,
    lower_vivado_primitives,
)
from tests.test_vtr_netlist import _raw_vtr_json


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
            imported = import_vivado_boundary_timing(
                timing_path, identity_path, output_path
            )

        self.assertEqual(query["endpoints"], 1)
        self.assertIn("__emuflow_net_0".encode().hex(), query_text)
        self.assertEqual(imported["maximum_delay_ns"], 1.75)

    def test_vtr_dsp_and_bram_macros_emit_synthesizable_models(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "raw.json"
            normalized = root / "normalized.json"
            ir_path = root / "hard-blocks.emuir.json"
            verilog = root / "hard-blocks.v"
            write_json(raw, _raw_vtr_json())
            normalize_vtr_hard_block_json(raw, normalized, top="top")
            ir = import_yosys_json(normalized, top="top", clocks=["clk"])
            write_json(ir_path, ir.to_dict())
            report = emit_vivado_mapped_verilog(ir_path, verilog)
            text = verilog.read_text(encoding="utf-8")

        self.assertEqual(
            report["hard_macro_models"],
            ["VTR_MULTIPLY", "VTR_SP_RAM"],
        )
        self.assertIn("module VTR_MULTIPLY", text)
        self.assertIn("module VTR_SP_RAM", text)
        self.assertIn("ram_style = \"block\"", text)
        dp_ram = text.split("module VTR_DP_RAM", 1)[1].split(
            "endmodule", 1
        )[0]
        self.assertEqual(dp_ram.count("always @(posedge clk)"), 2)
        self.assertNotIn(
            "if (we1)\n      memory[addr1] <= data1;\n    if (we2)",
            dp_ram,
        )
        self.assertIn(".\\ADDR_WIDTH (2)", text)
        self.assertNotIn(".\\ADDR_WIDTH (2'b10)", text)
        self.assertIn("{__emuflow_net_", text)
        self.assertIn("EMUFLOW_MAPPED = \"yes\"", text)
        self.assertIn(
            'KEEP_HIERARCHY = "yes", EMUFLOW_MAPPED = "yes"', text
        )

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
            with patch(
                "emuflow.vivado_backend.subprocess.run",
                return_value=completed,
            ):
                with self.assertRaisesRegex(
                    EmuFlowError, "1 critical warning"
                ):
                    _run_vivado(
                        "vivado",
                        root / "provider.tcl",
                        [],
                        root,
                        "vivado.log",
                    )

    def test_generic_logic_is_lowered_to_vivado_primitives(self):
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

    def test_vivado_outputs_the_same_partition_result_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "ir.json"
            mapped = root / "partition.v"
            output = root / "out"
            logic_identity = root / "logic-identity.json"
            logic_query = root / "logic-query.tsv"
            write_json(ir_path, _ir().to_dict())
            mapped.write_text("module partition; endmodule\n", encoding="utf-8")
            logic_identity_value = {
                    "schema": "emuflow.logic-segment-identity/v1",
                    "system_routes_sha256": "a" * 64,
                    "tdm_schedule_sha256": "b" * 64,
                    "status": "pass",
                    "design": "partition",
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
                            "start_pin": "dut/O",
                            "end_pin": "tx[0]",
                            "start_object_kind": "pin",
                            "end_object_kind": "port",
                        }
                    ],
                }
            identity_view = write_logic_segment_identity_storage(
                logic_identity,
                header={
                    key: value for key, value in logic_identity_value.items()
                    if key not in {"segments", "unsupported_member_paths"}
                },
                segments=logic_identity_value["segments"],
                unsupported_member_paths=logic_identity_value[
                    "unsupported_member_paths"
                ],
            )
            identity_view.close()
            logic_query.write_text("query\n", encoding="utf-8")

            def fake_run(_exe, script, _arguments, out, log_name):
                out.mkdir(parents=True, exist_ok=True)
                (out / log_name).write_text("pass\n", encoding="utf-8")
                if script.name == "implement_partition.tcl":
                    metrics = {
                        "vivado_version": "2025.2",
                        "part": "xcvu9p-flga2104-2L-e",
                        "mapped_cells": "2",
                        "physical_cells": "3",
                        "infrastructure_cells": "1",
                        "optimization_cells": "0",
                        "dsp48_cells": "0",
                        "ramb18_cells": "0",
                        "ramb36_cells": "0",
                        "nets": "10",
                        "unrouted_nets": "0",
                        "drc_violations": "0",
                        "drc_warnings": "5",
                        "dut_period_ns": "100.0",
                        "fabric_period_ns": "4.0",
                        "wns_ns": "0.25",
                        "critical_path_ns": "3.75",
                        "dut_wns_ns": "96.25",
                        "dut_delay_ns": "3.75",
                        "fabric_wns_ns": "0.25",
                        "fabric_delay_ns": "3.75",
                        "fabric_to_dut_wns_ns": "4.25",
                        "fabric_to_dut_delay_ns": "3.75",
                    }
                    (out / "implementation_metrics.tsv").write_text(
                        "metric\tvalue\n"
                        + "\n".join(
                            f"{name}\t{value}" for name, value in metrics.items()
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    inventory = (
                        "name\tref_name\n"
                        "dut\tLUT1\n"
                        "transport\tLUT1\n"
                    )
                    (out / "mapped_cells.tsv").write_text(
                        inventory, encoding="utf-8"
                    )
                    (out / "routed_mapped_cells.tsv").write_text(
                        inventory, encoding="utf-8"
                    )
                    for name in (
                        "synthesized.dcp",
                        "placed.dcp",
                        "routed.dcp",
                        "route_status.rpt",
                        "drc.rpt",
                        "timing_summary.rpt",
                        "utilization.rpt",
                    ):
                        (out / name).write_text("artifact\n", encoding="utf-8")
                elif script.name == "export_logic_segment_timing.tcl":
                    (out / "logic-segment-timing.tsv").write_text(
                        "endpoint_hex\tkind\tdelay_ns\tstart_object_hex\t"
                        "end_object_hex\tmeasurement\tactual_start_object_hex\t"
                        "actual_end_object_hex\n"
                        + "\t".join(
                            (
                                "logic0".encode().hex(),
                                "launch",
                                "2.25",
                                "dut/O".encode().hex(),
                                "tx[0]".encode().hex(),
                                "endpoint-exact",
                                "dut/O".encode().hex(),
                                "tx[0]".encode().hex(),
                            )
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                else:
                    (out / "timing-paths.tsv").write_text(
                        VIVADO_PATH_DATABASE_TSV_HEADER
                        + "\n"
                        + "\t".join(
                            (
                                "path0".encode().hex(),
                                "clk".encode().hex(),
                                "100.0",
                                "96.25",
                                "3.75",
                                "n0".encode().hex(),
                            )
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                return {
                    "status": "pass",
                    "command": ["vivado"],
                    "log": str(out / log_name),
                    "log_sha256": "0" * 64,
                }

            with (
                patch(
                    "emuflow.vivado_backend._resolve_vivado",
                    return_value="vivado",
                ),
                patch(
                    "emuflow.vivado_backend._run_vivado",
                    side_effect=fake_run,
                ),
            ):
                report = run_vivado_partition_backend(
                    fpga="fpga0",
                    part="xcvu9p-flga2104-2L-e",
                    ir_path=ir_path,
                    mapped_verilog_path=mapped,
                    runtime=RUNTIME,
                    original_cells=1,
                    transport_cells=1,
                    output_dir=output,
                    logic_identity_path=logic_identity,
                    logic_query_path=logic_query,
                )

        result = report["result"]
        self.assertEqual(result["identity"]["backend"], "vivado")
        self.assertEqual(result["cell_accounting"]["physical_cells"], 3)
        self.assertEqual(result["hard_resources"]["dsp48_cells"], 0)
        self.assertEqual(result["closure"]["drc_warnings"], 5)
        self.assertEqual(
            report["validation"]["physical_cells"], 3
        )
        self.assertEqual(result["timing"]["fabric_wns_ns"], 0.25)
        self.assertEqual(report["timing_path_validation"]["paths"], 1)
        self.assertEqual(report["cell_coverage"]["logical_cells"], 2)
        self.assertEqual(
            report["logic_segment_timing"]["import"]["maximum_delay_ns"],
            2.25,
        )

    def test_vivado_cell_coverage_uses_stable_mapped_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            synthesized = root / "mapped.tsv"
            routed = root / "routed.tsv"
            value = _ir().to_dict()
            value["instances"][0]["id"] = r"hier\dut"
            value["instances"][0]["name"] = r"hier\dut"
            value["nets"][0]["sinks"][0]["instance"] = r"hier\dut"
            inventory = (
                "name\tref_name\n"
                "hier\\\\dut\tLUT1\n"
                "transport\tLUT1\n"
            )
            synthesized.write_text(inventory, encoding="utf-8")
            routed.write_text(
                inventory.replace("hier\\\\dut\tLUT1", "hier\\\\dut\tLUT2"),
                encoding="utf-8",
            )
            report = validate_vivado_cell_coverage(
                EmuIR(value), synthesized, routed
            )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["logical_cells"], 2)
        self.assertEqual(report["reference_type_changes"], 1)

    def test_vivado_cell_coverage_rejects_a_missing_logical_cell(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            synthesized = root / "mapped.tsv"
            routed = root / "routed.tsv"
            synthesized.write_text(
                "name\tref_name\ndut\tLUT1\ntransport\tLUT1\n",
                encoding="utf-8",
            )
            routed.write_text(
                "name\tref_name\ndut\tLUT1\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValidationError, "routed logical-cell coverage disagrees"
            ):
                validate_vivado_cell_coverage(
                    _ir(), synthesized, routed
                )

    def test_vivado_timing_produces_the_common_sta_path_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "ir.json"
            output = root / "path-database.json"
            write_json(ir_path, _ir().to_dict())

            def fake_run(_exe, script, _arguments, out, log_name):
                (out / log_name).write_text("pass\n", encoding="utf-8")
                if script.name == "analyze_timing.tcl":
                    (out / "timing.dcp").write_text(
                        "checkpoint\n", encoding="utf-8"
                    )
                    (out / "timing_summary.rpt").write_text(
                        "timing\n", encoding="utf-8"
                    )
                    (out / "timing_metrics.tsv").write_text(
                        "metric\tvalue\n"
                        "vivado_version\t2025.2\n"
                        "part\txcvu9p-flga2104-2L-e\n"
                        "mapped_cells\t2\n"
                        "clocks\t1\n",
                        encoding="utf-8",
                    )
                else:
                    (out / "vivado-timing-paths.tsv").write_text(
                        VIVADO_PATH_DATABASE_TSV_HEADER
                        + "\n"
                        + "\t".join(
                            (
                                "path0".encode().hex(),
                                "clk".encode().hex(),
                                "10.0",
                                "1.0",
                                "9.0",
                                "n0".encode().hex(),
                            )
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                return {
                    "status": "pass",
                    "command": ["vivado"],
                    "log": str(out / log_name),
                    "log_sha256": "0" * 64,
                }

            with (
                patch(
                    "emuflow.vivado_backend._resolve_vivado",
                    return_value="vivado",
                ),
                patch(
                    "emuflow.vivado_backend._run_vivado",
                    side_effect=fake_run,
                ),
            ):
                report = run_vivado_timing_path_database(
                    ir_path=ir_path,
                    output_path=output,
                    clocks={"clk": 10.0},
                    part="xcvu9p-flga2104-2L-e",
                )

        self.assertEqual(report["mode"], "vivado-post-synthesis")
        self.assertEqual(report["validation"]["paths"], 1)


if __name__ == "__main__":
    unittest.main()
