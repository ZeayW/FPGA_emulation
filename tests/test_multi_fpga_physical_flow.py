import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.errors import ValidationError
from emuflow.io import read_json, write_json
from emuflow.ir import EmuIR
from emuflow.multi_fpga_physical_flow import (
    _partition_declares_dut_clock,
    _physical_clock_delays,
    _record_chimew_fixed_io_target,
    _write_vpr_runtime_sdc,
    run_multi_fpga_physical_flow,
    validate_multi_fpga_physical_report,
)


ROOT = Path(__file__).resolve().parents[1]
PLATFORM = ROOT / "platforms/virtual/academic_vtr_2fpga_p2p.json"


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _merged_ir(fpga):
    instances = [
        {
            "id": f"{fpga}_original",
            "name": f"{fpga}_original",
            "type": "LUT1",
            "resources": {"lut": 1},
            "parameters": {"INIT": "10"},
            "attributes": {},
            "constant_connections": [
                {"port": "I0", "bit": 0, "value": "0"}
            ],
        },
        {
            "id": f"{fpga}_transport",
            "name": f"{fpga}_transport",
            "type": "LUT1",
            "resources": {"lut": 1},
            "parameters": {"INIT": "10"},
            "attributes": {},
            "constant_connections": [
                {"port": "I0", "bit": 0, "value": "1"}
            ],
        },
    ]
    return EmuIR(
        {
            "schema": "emuflow.emuir/v1",
            "design": {
                "name": f"design__{fpga}",
                "top": f"design__{fpga}",
                "source_format": "test",
            },
            "ports": [],
            "instances": instances,
            "nets": [],
            "clocks": [],
            "warnings": [],
        }
    )


class MultiFpgaPhysicalFlowTest(unittest.TestCase):
    def test_partition_dut_clock_requirement_comes_from_split_ports(self):
        self.assertTrue(
            _partition_declares_dut_clock(
                {"ports": [{"id": "clk", "clock": True}]}
            )
        )
        self.assertFalse(
            _partition_declares_dut_clock(
                {"ports": [{"id": "data", "clock": False}]}
            )
        )
        with self.assertRaisesRegex(ValidationError, "ports are invalid"):
            _partition_declares_dut_clock({"ports": None})

    def test_runtime_sdc_allows_clockless_combinational_dut_partition(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime.sdc"
            report = _write_vpr_runtime_sdc(
                path,
                {
                    "clock_nets": {
                        "fabric_clk": "fabric_clock_net",
                        "virtual_clock_enable": "enable_net",
                    }
                },
                fabric_period_ns=20.0,
                dut_period_ns=200.0,
                cross_period_ns=180.0,
                dut_clock_required=False,
            )

            self.assertFalse(report["dut_clock_present"])
            self.assertFalse(report["dut_clock_required"])
            self.assertEqual(report["dut_clocks"], [])
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "# EmuFlow endpoint-complete Phase 7 timing contract.\n"
                "create_clock -name emuflow_fabric_clk -period 20.000000000 "
                "[get_ports {fabric_clock_net}]\n",
            )

    def test_clockless_partition_does_not_treat_enable_as_dut_clock(self):
        delays, presence = _physical_clock_delays(
            {
                "metrics": {
                    "critical_path_ns": 7.0,
                    "clock_domain_cpd_ns": {"fabric_net->fabric_net": 3.0},
                }
            },
            {
                "clock_nets": {
                    "fabric_clk": "fabric_net",
                    "virtual_clock_enable": "enable_net",
                }
            },
        )

        self.assertEqual(delays["fabric"], 3.0)
        self.assertEqual(delays["dut"], 0.0)
        self.assertEqual(delays["cross"], 0.0)
        self.assertEqual(
            presence, {"fabric": True, "dut": False, "cross": False}
        )

    def test_runtime_sdc_caps_long_virtual_clock_for_vpr_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime.sdc"
            report = _write_vpr_runtime_sdc(
                path,
                {
                    "clock_nets": {
                        "fabric_clk": "fabric_clock_net",
                        "dut_clk": "dut_clock_net",
                    }
                },
                fabric_period_ns=20.0,
                dut_period_ns=2_238_160.0,
                cross_period_ns=2_237_980.0,
            )

            self.assertEqual(report["requested_periods_ns"]["dut"], 2_238_160.0)
            self.assertEqual(report["requested_periods_ns"]["cross"], 2_237_980.0)
            self.assertEqual(report["effective_vpr_periods_ns"]["dut"], 2_000_000.0)
            self.assertEqual(report["effective_vpr_periods_ns"]["cross"], 1_999_820.0)
            self.assertTrue(report["vpr_sdc_time_capped"])
            self.assertEqual(report["sha256"], _sha256(path))
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "# EmuFlow endpoint-complete Phase 7 timing contract.\n"
                "create_clock -name emuflow_fabric_clk -period 20.000000000 "
                "[get_ports {fabric_clock_net}]\n"
                "create_clock -name emuflow_dut_clk_0 -period 2000000.000000000 "
                "[get_ports {dut_clock_net}]\n"
                "set_max_delay 1999820.000000000 -from "
                "[get_clocks {emuflow_fabric_clk}] -to "
                "[get_clocks {emuflow_dut_clk_0}]\n"
                "set_max_delay 1999820.000000000 -from "
                "[get_clocks {emuflow_dut_clk_0}] -to "
                "[get_clocks {emuflow_fabric_clk}]\n",
            )

    def test_chimew_fixed_io_targets_allow_only_intragroup_tdm_sharing(self):
        targets = {}
        packed_groups = {}
        group_packed_blocks = {}
        for _slot in range(2):
            _record_chimew_fixed_io_target(
                targets,
                packed_groups,
                group_packed_blocks,
                packed_name="packed_io0",
                group="group0",
                target_y=0.25,
            )
        self.assertEqual(targets, {"packed_io0": 0.25})

        with self.assertRaisesRegex(ValidationError, "different signal groups"):
            _record_chimew_fixed_io_target(
                targets,
                packed_groups,
                group_packed_blocks,
                packed_name="packed_io0",
                group="group1",
                target_y=0.25,
            )
        with self.assertRaisesRegex(ValidationError, "split across"):
            _record_chimew_fixed_io_target(
                targets,
                packed_groups,
                group_packed_blocks,
                packed_name="packed_io1",
                group="group0",
                target_y=0.25,
            )

    def test_every_partition_is_bound_through_checked_route(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            split = root / "split"
            split.mkdir()
            manifest = {
                "schema": "emuflow.split-manifest/v1",
                "design": "design",
                "platform": "academic_vtr_2fpga_p2p",
                "provider": "test",
                "board_binding": {"status": "virtual"},
                "fpgas": [],
                "lane_map": "lane_map.json",
                "runtime_controller_rtl": "virtual_runtime_controller.sv",
            }
            (split / "virtual_runtime_controller.sv").write_text(
                "module emuflow_virtual_runtime_controller; endmodule\n",
                encoding="utf-8",
            )
            for fpga in ("fpga0", "fpga1"):
                fpga_root = split / fpga
                fpga_root.mkdir()
                write_json(
                    fpga_root / "netlist.json",
                    {
                        "instances": [{"id": f"{fpga}_original"}],
                        "ports": [{"id": "clk", "clock": True}],
                    },
                )
                write_json(
                    fpga_root / "transport.json",
                    {
                        "schema": "emuflow.transport-endpoints/v1",
                        "design": "design",
                        "platform": "academic_vtr_2fpga_p2p",
                        "fpga": fpga,
                        "endpoints": [],
                    },
                )
                (fpga_root / "transport_schedule.sv").write_text(
                    f"module emuflow_transport_{fpga}; endmodule\n",
                    encoding="utf-8",
                )
                manifest["fpgas"].append(
                    {
                        "fpga": fpga,
                        "netlist": f"{fpga}/netlist.json",
                        "transport": f"{fpga}/transport.json",
                        "transport_rtl": f"{fpga}/transport_schedule.sv",
                        "virtual_anchors": f"{fpga}/virtual_anchors.json",
                    }
                )
            # Deliberately reverse the producer order; the physical report is
            # required to follow the BoardDB order even under concurrency.
            manifest["fpgas"].reverse()
            write_json(split / "manifest.json", manifest)
            write_json(root / "schedule.json", {})
            architecture = root / "architecture.xml"
            architecture.write_text("<architecture/>\n", encoding="utf-8")

            runtime = {
                "design": "design",
                "fabric_clock": {"period_ns": 4.0},
                "virtual_dut_clock": {"nominal_period_ns": 128.0},
                "timing_model": {"fabric_to_dut_max_delay_ns": 8.0},
            }

            worker_barrier = threading.Barrier(2, timeout=5.0)

            def fake_yosys(_sources, _top, output, **_kwargs):
                worker_barrier.wait()
                write_json(output, {"modules": {}})

            def fake_lower(netlist_path, _transport, _transport_ir, output, report):
                fpga = netlist_path.parent.name
                value = _merged_ir(fpga)
                write_json(output, value.to_dict())
                boundary_path = output.with_name("boundary-identities.json")
                boundary = {
                    "schema": "emuflow.boundary-identity/v1",
                    "status": "pass",
                    "design": "design",
                    "platform": "academic_vtr_2fpga_p2p",
                    "fpga": fpga,
                    "provider": "test",
                    "coverage": {
                        "endpoints": 0,
                        "tx": 0,
                        "rx": 0,
                        "external_port_nets": 0,
                    },
                    "endpoints": [],
                }
                write_json(boundary_path, boundary)
                result = {
                    "schema": "emuflow.placement-ir-report/v1",
                    "status": "pass",
                    "design": f"design__{fpga}",
                    "instances": 2,
                    "nets": 0,
                    "resource_totals": {"lut": 2},
                    "transport_instances": 1,
                    "output": str(output),
                    "boundary_identity": {
                        "schema": "emuflow.boundary-identity/v1",
                        "output": str(boundary_path),
                        "validation": {
                            "status": "pass",
                            "fpga": fpga,
                            "endpoints": 0,
                            "tx": 0,
                            "rx": 0,
                        },
                    },
                }
                write_json(report, result)
                return result

            def fake_pack(arch, circuit, output_dir, **_kwargs):
                output_dir.mkdir(parents=True, exist_ok=True)
                netlist = output_dir / "partition.net"
                placement = output_dir / "partition.place"
                netlist.write_text("<block/>\n", encoding="utf-8")
                placement.write_text(
                    "Array size: 8 x 9 logic blocks\n", encoding="utf-8"
                )
                return {
                    "status": "pass",
                    "architecture": {"sha256": _sha256(arch)},
                    "circuit": {"sha256": _sha256(circuit)},
                    "artifacts": {
                        "packed_netlist": {"path": str(netlist)},
                        "placement": {"path": str(placement)},
                    },
                }

            def fake_eblif(_ir, output, _report):
                output.write_text(".model partition\n.end\n", encoding="utf-8")
                return {
                    "status": "pass",
                    "output": str(output),
                    "output_sha256": _sha256(output),
                    "emitted_atoms": 2,
                    "source_instances": 2,
                    "top_ports": [],
                    "clock_nets": {
                        "fabric_clk": "fabric_clk",
                        "clk": "clk",
                        "virtual_clock_enable": "virtual_clock_enable",
                    },
                }

            def fake_architecture(**kwargs):
                write_json(kwargs["architecture_output_path"], {})
                write_json(kwargs["timing_output_path"], {})
                return {"status": "pass"}

            def fake_packed(netlist, output, **_kwargs):
                write_json(output, {})
                return {"status": "pass", "design": "partition"}

            def fake_placement(_packed, _arch, output_dir, **_kwargs):
                output_dir.mkdir(parents=True, exist_ok=True)
                placement = output_dir / "partition.place"
                placement.write_text(
                    "Array size: 8 x 9 logic blocks\n", encoding="utf-8"
                )
                return {
                    "status": "pass",
                    "artifacts": {"vpr_placement": str(placement)},
                }

            def fake_route(arch, circuit, *_args, **_kwargs):
                _kwargs["boundary_output"].parent.mkdir(
                    parents=True, exist_ok=True
                )
                _kwargs["boundary_output"].write_text(
                    "endpoint\tkind\tdelay_ns\tstart_pin\tend_pin\n",
                    encoding="utf-8",
                )
                metrics = {
                    "critical_path_ns": 1.0,
                    "setup_wns_ns": 0.0,
                    "setup_worst_slack_ns": 3.0,
                    "setup_tns_ns": 0.0,
                    "setup_failing_endpoint_constraints": 0,
                    "setup_failing_endpoints": 0,
                }
                return {
                    "status": "pass",
                    "architecture": {"sha256": _sha256(arch)},
                    "circuit": {"sha256": _sha256(circuit)},
                    "sdc_file": {
                        "sha256": _sha256(_kwargs["sdc_file"]),
                    },
                    "metrics": metrics,
                    "route_check": {"status": "pass"},
                    "timing_summary": {
                        "status": "pass",
                        "metrics": metrics,
                    },
                }

            with (
                patch(
                    "emuflow.multi_fpga_physical_flow.build_virtual_runtime",
                    return_value=runtime,
                ),
                patch(
                    "emuflow.multi_fpga_physical_flow.run_generic_yosys",
                    side_effect=fake_yosys,
                ),
                patch(
                    "emuflow.multi_fpga_physical_flow.import_yosys_json",
                    return_value=_merged_ir("transport"),
                ),
                patch(
                    "emuflow.multi_fpga_physical_flow.run_placement_ir_lowering",
                    side_effect=fake_lower,
                ),
                patch(
                    "emuflow.multi_fpga_physical_flow.run_vpr_pack_place",
                    side_effect=fake_pack,
                ),
                patch(
                    "emuflow.multi_fpga_physical_flow.emit_vtr_eblif",
                    side_effect=fake_eblif,
                ),
                patch(
                    "emuflow.multi_fpga_physical_flow.run_vtr_architecture_import",
                    side_effect=fake_architecture,
                ),
                patch(
                    "emuflow.multi_fpga_physical_flow.run_packed_netlist_import",
                    side_effect=fake_packed,
                ),
                patch(
                    "emuflow.multi_fpga_physical_flow.run_packed_openparf_placement",
                    side_effect=fake_placement,
                ),
                patch(
                    "emuflow.multi_fpga_physical_flow.validate_openparf_runtime",
                    return_value={"status": "pass"},
                ),
                patch(
                    "emuflow.multi_fpga_physical_flow.run_vpr_route_packed",
                    side_effect=fake_route,
                ),
            ):
                report = run_multi_fpga_physical_flow(
                    split,
                    PLATFORM,
                    root / "schedule.json",
                    root / "physical",
                    architecture=architecture,
                    workers=2,
                )
                managed_report = run_multi_fpga_physical_flow(
                    split,
                    PLATFORM,
                    root / "schedule.json",
                    root / "physical-managed",
                    architecture=architecture,
                    workers=2,
                    managed_storage=True,
                )
                managed_summary = read_json(
                    root / "physical-managed/physical-summary.json"
                )
                self.assertEqual(
                    (root / "physical/architecture/vtr-flagship.xml").read_bytes(),
                    architecture.read_bytes(),
                )
                self.assertEqual(
                    Path(report["architecture"]["path"]).resolve(),
                    (root / "physical/architecture/vtr-flagship.xml").resolve(),
                )
                for item in report["fpgas"]:
                    runtime_sdc = Path(item["stages"]["runtime_sdc"]["path"])
                    self.assertNotIn(
                        "virtual_clock_enable",
                        runtime_sdc.read_text(encoding="utf-8"),
                    )

        self.assertEqual(report["summary"]["fpgas"], 2)
        self.assertEqual(report["summary"]["original_cells"], 2)
        self.assertEqual(report["summary"]["transport_cells"], 2)
        self.assertEqual(report["physical_summary"]["validation"]["status"], "pass")
        self.assertEqual(
            [item["fpga"] for item in report["fpgas"]],
            ["fpga0", "fpga1"],
        )
        self.assertEqual(report["execution"]["requested_workers"], 2)
        self.assertEqual(report["execution"]["effective_workers"], 2)
        self.assertFalse(report["execution"]["pack_place_resume"])
        self.assertFalse(report["execution"]["route_resume"])
        self.assertNotIn("physical_summary", managed_report)
        self.assertEqual(
            managed_report["physical_summary_ref"], "physical-summary.json"
        )
        self.assertEqual(
            validate_multi_fpga_physical_report(
                managed_report,
                managed_summary,
            )["status"],
            "pass",
        )

    def test_rejects_non_positive_worker_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValidationError, "workers"):
                run_multi_fpga_physical_flow(
                    Path(temporary),
                    PLATFORM,
                    Path(temporary) / "schedule.json",
                    Path(temporary) / "physical",
                    workers=0,
                )

    def test_rejects_partial_chimew_physical_anchor_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            split = root / "split"
            split.mkdir()
            manifest = {
                "schema": "emuflow.split-manifest/v1",
                "design": "design",
                "platform": "academic_vtr_2fpga_p2p",
                "fpgas": [],
                "runtime_controller_rtl": "runtime.sv",
                "electrical_binding": "electrical_binding.json",
            }
            write_json(split / "manifest.json", manifest)
            write_json(split / "electrical_binding.json", {"bindings": []})
            write_json(root / "schedule.json", {})
            with self.assertRaisesRegex(ValidationError, "position hints"):
                run_multi_fpga_physical_flow(
                    split,
                    PLATFORM,
                    root / "schedule.json",
                    root / "physical",
                )

    def test_nonempty_output_requires_explicit_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            split = root / "split"
            split.mkdir()
            write_json(
                split / "manifest.json",
                {
                    "schema": "emuflow.split-manifest/v1",
                    "fpgas": [{"fpga": "fpga0"}, {"fpga": "fpga1"}],
                },
            )
            output = root / "physical"
            output.mkdir()
            (output / "partial").write_text("partial\n", encoding="utf-8")
            write_json(root / "schedule.json", {})
            runtime = {
                "fabric_clock": {"period_ns": 4.0},
                "virtual_dut_clock": {"nominal_period_ns": 128.0},
                "timing_model": {"fabric_to_dut_max_delay_ns": 8.0},
            }
            with (
                patch(
                    "emuflow.multi_fpga_physical_flow.build_virtual_runtime",
                    return_value=runtime,
                ),
                self.assertRaisesRegex(
                    Exception, "physical output must be an empty directory"
                ),
            ):
                run_multi_fpga_physical_flow(
                    split,
                    PLATFORM,
                    root / "schedule.json",
                    output,
                )

    def test_rejects_logic_database_without_complete_original_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(
                ValidationError, "complete original STA path database"
            ):
                run_multi_fpga_physical_flow(
                    root,
                    PLATFORM,
                    root / "schedule.json",
                    root / "physical",
                    logic_path_database_path=root / "cut-paths.json",
                )


if __name__ == "__main__":
    unittest.main()
