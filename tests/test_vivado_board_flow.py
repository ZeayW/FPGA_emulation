import copy
import hashlib
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.errors import ValidationError
from emuflow.io import read_json, write_json
from emuflow.ir import EMUIR_SCHEMA, EmuIR
from emuflow.platform import Platform
from emuflow.vivado_board_flow import (
    build_vivado_board_top,
    run_vivado_board_flow,
    validate_vivado_board_flow_bundle,
    validate_vivado_board_flow_report,
)


ROOT = Path(__file__).resolve().parents[1]
PLATFORM = ROOT / "platforms/virtual/xcvu3p_2fpga_p2p.json"


def _transport_names(platform: Platform, fpga: str) -> tuple[str, str, str]:
    link = platform.links[0]
    peer = link.endpoints[1] if link.endpoints[0] == fpga else link.endpoints[0]
    suffix = f"{link.id}_{peer}".replace("-", "_")
    return f"tx_{suffix}", f"rx_{suffix}", peer


def _placement_ir(platform: Platform, fpga: str) -> EmuIR:
    tx, rx, _peer = _transport_names(platform, fpga)
    width = platform.links[0].transport_bits_per_cycle_per_direction
    return EmuIR(
        {
            "schema": EMUIR_SCHEMA,
            "design": {
                "name": f"counter__{fpga}",
                "top": f"counter__{fpga}",
                "source_format": "test",
            },
            "ports": [
                {
                    "id": "fabric_clk",
                    "name": "fabric_clk",
                    "direction": "input",
                    "width": 1,
                    "clock": True,
                },
                {
                    "id": "reset",
                    "name": "reset",
                    "direction": "input",
                    "width": 1,
                    "clock": False,
                },
                {
                    "id": "links_ready",
                    "name": "links_ready",
                    "direction": "input",
                    "width": 1,
                    "clock": False,
                },
                {
                    "id": tx,
                    "name": tx,
                    "direction": "output",
                    "width": width,
                    "clock": False,
                },
                {
                    "id": rx,
                    "name": rx,
                    "direction": "input",
                    "width": width,
                    "clock": False,
                },
            ],
            "instances": [],
            "nets": [],
            "clocks": [],
            "warnings": [],
        }
    )


def _phase6c_record(platform: Platform, fpga: str) -> dict:
    tx, rx, peer = _transport_names(platform, fpga)
    link = platform.links[0]
    return {
        "fpga": fpga,
        "part": next(item.part for item in platform.fpgas if item.id == fpga),
        "module": f"emuflow_serial_wrapper_{fpga}",
        "rtl": f"{fpga}.serial_wrapper.sv",
        "transport_connections": [
            {
                "link": link.id,
                "peer": peer,
                "width": link.transport_bits_per_cycle_per_direction,
                "transport_tx_port": tx,
                "transport_rx_port": rx,
                "wrapper_tx_port": tx,
                "wrapper_rx_port": rx,
            }
        ],
        "board_services": {
            "reference_clocks": [],
            "resets": [],
            "clock_reset_domains": [],
        },
        "sites": [],
        "transceiver_quads": [],
    }


class VivadoBoardFlowTest(unittest.TestCase):
    def test_static_board_controls_reach_actual_physical_top(self):
        platform = Platform.load(PLATFORM)
        fpga = platform.fpgas[0].id
        record = _phase6c_record(platform, fpga)
        record["board_services"] = {"static_outputs": [
            {"id": "qsfp_resetl", "value": 1}]}
        result = build_vivado_board_top(_placement_ir(platform, fpga), record)
        self.assertIn("output wire board_static_qsfp_resetl", result)
        self.assertIn("assign board_static_qsfp_resetl = 1'b1;", result)
        self.assertNotIn(".board_static_qsfp_resetl(", result)

    def test_board_top_connects_mapped_partition_not_duplicate_transport(self):
        platform = Platform.load(PLATFORM)
        fpga = platform.fpgas[0].id
        rtl = build_vivado_board_top(
            _placement_ir(platform, fpga),
            _phase6c_record(platform, fpga),
        )
        self.assertIn(f"\\counter__{fpga}  mapped_partition", rtl)
        self.assertIn(f"emuflow_serial_wrapper_{fpga} serial_wrapper", rtl)
        self.assertNotIn(f"emuflow_transport_{fpga} transport", rtl)
        self.assertIn(".links_ready(board_links_ready)", rtl)

    def test_board_flow_runs_all_fpgas_and_blocks_bitstream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            flow = root / "flow"
            bsp = root / "bsp"
            provider_root = root / "provider"
            output = root / "output"
            platform = Platform.load(PLATFORM)
            flow.mkdir()
            bsp.mkdir()
            provider_root.mkdir()
            write_json(flow / "multi-fpga-flow-report.json", {"status": "pass"})
            write_json(bsp / "multi-fpga-bsp-flow-report.json", {"status": "pass"})
            phase6c = bsp / "phase6c"
            phase6c.mkdir()
            records = []
            physical_records = []
            gt_tcl = {}
            for fpga in platform.fpgas:
                record = _phase6c_record(platform, fpga.id)
                records.append(record)
                physical_records.append(
                    {
                        "fpga": fpga.id,
                        "original_cells": 0,
                        "transport_cells": 0,
                    }
                )
                fpga_root = flow / "physical" / fpga.id
                (fpga_root / "vivado").mkdir(parents=True)
                write_json(
                    fpga_root / "placement.emuir.json",
                    _placement_ir(platform, fpga.id).to_dict(),
                )
                (phase6c / record["rtl"]).write_text(
                    f"module {record['module']}; endmodule\n",
                    encoding="utf-8",
                )
                name = f"{fpga.id}.gt_sites.tcl"
                (phase6c / name).write_text("# GT sites\n", encoding="utf-8")
                gt_tcl[fpga.id] = name
            write_json(
                phase6c / "phase6c_report.json",
                {
                    "artifacts": {
                        "runtime_sync_rtl": [],
                        "open_pcs_rtl": [],
                        "gt_site_tcl": gt_tcl,
                    }
                },
            )
            write_json(
                phase6c / "serial_wrapper_manifest.json",
                {"fpgas": records},
            )
            provider_hdl = provider_root / "provider.sv"
            provider_hdl.write_text("module provider; endmodule\n", encoding="utf-8")
            xci = provider_root / "provider.xci"
            xci.write_text("xci\n", encoding="utf-8")
            provider_path = provider_root / "provider.json"
            write_json(provider_path, {})
            vivado = root / "vivado"
            vivado.write_text(
                """#!/usr/bin/env python3
import pathlib, re, sys
if '-version' in sys.argv:
    print('Vivado test')
    raise SystemExit(0)
script = pathlib.Path(sys.argv[sys.argv.index('-source') + 1]).read_text()
out = pathlib.Path.cwd()
mapped = re.search(r'mapped cell coverage disagrees', script)
for name in ('synthesized.dcp', 'placed.dcp', 'routed.dcp',
             'route_status.rpt', 'drc.rpt', 'timing_summary.rpt',
             'utilization.rpt', 'congestion.rpt', 'congestion.csv',
             'slr_utilization.rpt', 'slr_crossing.rpt'):
    (out / name).write_text(name + '\\n')
(out / 'board_metrics.tsv').write_text(
    'metric\\tvalue\\nvivado_version\\ttest\\npart\\t' +
    re.search(r'puts \\$metrics "part\\\\t([^"\\n]+)', script).group(1) +
    '\\nmapped_cells\\t0\\nblack_boxes\\t0\\n' +
    'channel_primitives\\t0\\ncommon_primitives\\t0\\n' +
    'unrouted_nets\\t0\\ndrc_errors\\t2\\ndrc_warnings\\t1\\n' +
    'wns_ns\\tNA\\ncritical_path_ns\\tNA\\n' +
    'slr_count\\t1\\n' +
    'slr_crossing_status\\tsingle-slr-not-applicable\\n' +
    'channel_locs\\t\\ncommon_locs\\t\\n')
""",
                encoding="utf-8",
            )
            vivado.chmod(vivado.stat().st_mode | stat.S_IXUSR)
            normalized_provider = {
                "schema": "emuflow.serial-phy-provider/v3",
                "id": "test-provider",
                "qualification": "vendor_generated_hardware",
                "source_root": ".",
                "sources": [
                    {"path": provider_hdl.name, "language": "systemverilog"}
                ],
                "vendor_products": {"xci": [{"path": xci.name}]},
            }
            physical = {
                "backend": {"id": "open"},
                "fpgas": physical_records,
            }
            write_json(
                flow / "multi-fpga-flow-report.json",
                {"status": "pass", "physical": physical},
            )
            write_json(
                flow / "runtime/runtime_contract.json",
                {
                    "timing_model": {
                        "dut_clock_port": "dut_clk",
                        "fabric_clock_port": "fabric_clk",
                        "fabric_to_dut_max_delay_ns": 20.0,
                    },
                    "virtual_dut_clock": {"nominal_period_ns": 128.0},
                    "fabric_clock": {"period_ns": 20.0},
                },
            )
            write_json(
                bsp / "multi-fpga-bsp-flow-report.json",
                {
                    "status": "pass",
                    "source_flow_report_sha256": hashlib.sha256(
                        (flow / "multi-fpga-flow-report.json").read_bytes()
                    ).hexdigest(),
                },
            )
            with (
                patch(
                    "emuflow.vivado_board_flow.validate_multi_fpga_flow_report",
                    return_value={
                        "design": "counter",
                        "platform": platform.name,
                    },
                ),
                patch(
                    "emuflow.vivado_board_flow.validate_multi_fpga_bsp_flow_report",
                    return_value={
                        "design": "counter",
                        "platform": platform.name,
                    },
                ),
                patch(
                    "emuflow.vivado_board_flow.validate_multi_fpga_physical_report"
                ),
                patch(
                    "emuflow.vivado_board_flow.validate_serial_phy_provider",
                    return_value={"normalized": normalized_provider},
                ),
            ):
                report = run_vivado_board_flow(
                    flow_root=flow,
                    bsp_root=bsp,
                    platform_path=PLATFORM,
                    phy_provider_path=provider_path,
                    vivado_executable=vivado,
                    output_dir=output,
                )
            self.assertEqual(report["summary"]["fpgas"], 2)
            self.assertEqual(report["summary"]["drc_errors"], 4)
            self.assertEqual(report["summary"]["physical_evidence_fpgas"], 2)
            self.assertEqual(report["summary"]["multi_slr_fpgas"], 0)
            self.assertEqual(report["source_physical_backend"], "open")
            self.assertEqual(
                report["fpgas"][0]["vivado_relowering"]["status"], "pass"
            )
            self.assertFalse(report["release"]["hardware_release_authorized"])
            self.assertEqual(
                report["fpgas"][0]["physical_evidence"],
                {
                    "scope": "authoritative-vivado-post-route-reports",
                    "slr_count": 1,
                    "slr_crossing_status": "single-slr-not-applicable",
                    "artifacts": {
                        "congestion": "congestion.rpt",
                        "congestion_csv": "congestion.csv",
                        "slr_crossing": "slr_crossing.rpt",
                        "slr_utilization": "slr_utilization.rpt",
                    },
                },
            )
            generated_tcl = (
                output
                / report["fpgas"][0]["artifacts"]["generated_tcl"]["path"]
            ).read_text(encoding="utf-8")
            self.assertIn("report_design_analysis -congestion", generated_tcl)
            self.assertIn("-min_congestion_level 3", generated_tcl)
            self.assertIn("-csv", generated_tcl)
            self.assertIn("report_utilization -slr", generated_tcl)
            self.assertIn("report_slr_crossing -file", generated_tcl)
            validate_vivado_board_flow_report(report)
            relocated = root / "relocated-board-bundle"
            shutil.copytree(output, relocated)
            bundle_validation = validate_vivado_board_flow_bundle(relocated)
            self.assertTrue(bundle_validation["bundle_relocatable"])
            self.assertEqual(bundle_validation["artifacts_verified"], 28)
            v2_bundle = root / "relocated-board-bundle-v2"
            shutil.copytree(output, v2_bundle)
            v2_report_path = v2_bundle / "vivado-board-flow-report.json"
            v2_report = read_json(v2_report_path)
            v2_report["schema"] = "emuflow.vivado-board-flow/v2"
            for fpga_record in v2_report["fpgas"]:
                fpga_record["physical_evidence"]["artifacts"].pop(
                    "congestion_csv"
                )
                fpga_record["artifacts"].pop("congestion.csv")
            write_json(v2_report_path, v2_report)
            self.assertEqual(
                validate_vivado_board_flow_bundle(v2_bundle)[
                    "artifacts_verified"
                ],
                26,
            )
            congestion = relocated / platform.fpgas[0].id / "congestion.rpt"
            congestion.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "hash differs"):
                validate_vivado_board_flow_bundle(relocated)
            legacy_report = copy.deepcopy(report)
            legacy_report["schema"] = "emuflow.vivado-board-flow/v1"
            for fpga_record in legacy_report["fpgas"]:
                fpga_record.pop("physical_evidence")
            self.assertEqual(
                validate_vivado_board_flow_report(legacy_report)[
                    "physical_evidence_fpgas"
                ],
                0,
            )
            report["fpgas"][0]["physical_evidence"][
                "slr_crossing_status"
            ] = "measured"
            with self.assertRaisesRegex(ValidationError, "SLR evidence"):
                validate_vivado_board_flow_report(report)
            with self.assertRaisesRegex(ValidationError, "bitstream"):
                run_vivado_board_flow(
                    flow_root=flow,
                    bsp_root=bsp,
                    platform_path=PLATFORM,
                    phy_provider_path=provider_path,
                    vivado_executable=vivado,
                    output_dir=root / "bitstream",
                    write_bitstream=True,
                )


if __name__ == "__main__":
    unittest.main()
