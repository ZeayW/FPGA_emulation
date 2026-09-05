import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.phase7c import PHASE7C_REPORT_SCHEMA, system_timing_summary
from emuflow.platform import Platform
from emuflow.release import build_release_manifest, run_phase7d
from emuflow.runtime import validate_physical_summary


class Phase7DTest(unittest.TestCase):
    def _inputs(self, root: Path):
        source_root = root / "source"
        source_root.mkdir()
        source = source_root / "dut.v"
        source.write_text("module dut; endmodule\n", encoding="utf-8")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        platform = Platform.from_dict(
            {
                "schema": "emuflow.boarddb/v1",
                "platform": {
                    "name": "release_test",
                    "kind": "virtual",
                    "description": "release test",
                },
                "fpgas": [
                    {
                        "id": fpga,
                        "part": "xcvu3p-ffvc1517-2-e",
                        "utilization_limit": 1.0,
                        "capacity": {"lut": 100, "ff": 100},
                    }
                    for fpga in ("fpga0", "fpga1")
                ],
                "links": [
                    {
                        "id": "link",
                        "endpoints": ["fpga0", "fpga1"],
                        "direction": "full_duplex",
                        "mode": "abstract",
                        "data_lanes_per_direction": 4,
                        "fabric_clock_mhz": 250.0,
                        "latency_cycles": 2,
                    }
                ],
            }
        )
        benchmark = {
            "schema": "emuflow.benchmark-report/v1",
            "benchmark": "release",
            "design_id": "dut",
            "status": "pass",
            "source": {
                "root": str(source_root),
                "files": [{"path": "dut.v", "sha256": source_hash}],
            },
            "gates": {
                f"G{index}_{name}": "pass"
                for index, name in enumerate(
                    ("source", "elaboration", "synthesis", "emuir")
                )
            },
            "phase1": {
                "design": "dut",
                "platform": platform.name,
            },
        }
        phase3 = {
            "schema": "emuflow.phase3-report/v1",
            "status": "pass",
            "design": "dut",
            "platform": platform.name,
            "validation": {
                "instances": 10,
                "used_fpgas": 2,
                "cut_nets": 2,
                "cut_sink_endpoints": 2,
                "illegal_cuts": 0,
            },
        }
        phase4 = {
            "schema": "emuflow.phase4-report/v1",
            "status": "pass",
            "design": "dut",
            "platform": platform.name,
            "validation": {
                "demands": 2,
                "routed_sinks": 2,
                "total_link_bit_hops": 2,
                "overloaded_links": 0,
            },
        }
        phase5 = {
            "schema": "emuflow.phase5-report/v1",
            "status": "pass",
            "design": "dut",
            "platform": platform.name,
            "validation": {
                "demands": 2,
                "routed_sinks": 2,
                "scheduled_bit_hops": 2,
                "frame_slots": 8,
                "completion_slot": 2,
                "collisions": 0,
            },
        }
        phase6 = {
            "schema": "emuflow.phase6-report/v1",
            "status": "pass",
            "design": "dut",
            "platform": platform.name,
            "validation": {
                "instances": 10,
                "cut_sink_endpoints": 2,
                "scheduled_hops": 2,
                "instance_coverage_errors": 0,
                "endpoint_agreement_errors": 0,
                "unbound_package_pins": 4,
                "virtual_anchors": 4,
                "lane_map_entries": 2,
            },
            "equivalence": {
                "cycles": 8,
                "mismatches": 0,
            },
        }
        runtime = {
            "schema": "emuflow.virtual-runtime/v1",
            "design": "dut",
            "platform": platform.name,
            "semantic_envelope": {"virtual_dut_clocks": 1},
            "frame": {"slots": 8, "completion_slot": 2},
            "virtual_dut_clock": {"nominal_frequency_mhz": 31.25},
            "fabric_clock": {"period_ns": 4.0},
            "board_binding": {"status": "virtual"},
        }
        physical_summary = {
            "schema": "emuflow.phase7b-physical-summary/v1",
            "status": "pass",
            "design": "dut",
            "platform": platform.name,
            "fpgas": [
                {
                    "fpga": "fpga0",
                    "original_cells": 6,
                    "transport_cells": 2,
                    "routed_cells": 8,
                    "physical_cells": 8,
                    "infrastructure_cells": 0,
                    "unrouted_nets": 0,
                    "drc_violations": 0,
                    "wns_ns": 1.5,
                    "clocks": {
                        "fabric_period_ns": 4.0,
                        "dut_period_ns": 32.0,
                    },
                    "timing": {
                        "dut_wns_ns": 30.0,
                        "fabric_wns_ns": 1.5,
                        "fabric_to_dut_wns_ns": 20.0,
                    },
                },
                {
                    "fpga": "fpga1",
                    "original_cells": 4,
                    "transport_cells": 1,
                    "routed_cells": 5,
                    "physical_cells": 5,
                    "infrastructure_cells": 0,
                    "unrouted_nets": 0,
                    "drc_violations": 0,
                    "wns_ns": 1.0,
                    "clocks": {
                        "fabric_period_ns": 4.0,
                        "dut_period_ns": 32.0,
                    },
                    "timing": {
                        "dut_wns_ns": 29.0,
                        "fabric_wns_ns": 1.0,
                        "fabric_to_dut_wns_ns": 21.0,
                    },
                },
            ],
        }
        runtime["virtual_dut_clock"]["nominal_period_ns"] = 32.0
        physical = validate_physical_summary(
            physical_summary, runtime, platform
        )
        system_timing = {
            "schema": "emuflow.system-timing/v2",
            "status": "pass",
        }
        phase7c = {
            "schema": PHASE7C_REPORT_SCHEMA,
            "status": "pass",
            "design": "dut",
            "platform": platform.name,
            "physical": physical,
            "system_timing_summary": system_timing_summary(system_timing),
            "system_timing_ref": {
                "artifact": "qor_report",
                "json_pointer": "/timing",
            },
        }
        qor = {
            "schema": "emuflow.qor-report/v4",
            "status": "pass",
            "physical": physical,
            "timing": system_timing,
        }
        lowering = {
            "fpga0": {
                "schema": "emuflow.placement-ir-report/v1",
                "status": "pass",
                "instances": 8,
                "transport_instances": 2,
            },
            "fpga1": {
                "schema": "emuflow.placement-ir-report/v1",
                "status": "pass",
                "instances": 5,
                "transport_instances": 1,
            },
        }
        placement = {
            fpga: {
                "schema": "emuflow.phase2-report/v1",
                "status": "pass",
                "provider": "openparf-root-build",
                "placement": {
                    "status": "legal",
                    "cells": 8 if fpga == "fpga0" else 5,
                },
            }
            for fpga in ("fpga0", "fpga1")
        }
        emission = {
            fpga: {
                "schema": "emuflow.mapped-verilog-report/v1",
                "status": "pass",
                "instances": 8 if fpga == "fpga0" else 5,
            }
            for fpga in ("fpga0", "fpga1")
        }
        artifact = root / "routed.dcp"
        artifact.write_bytes(b"routed")
        return {
            "benchmark": benchmark,
            "phase3": phase3,
            "phase4": phase4,
            "phase5": phase5,
            "phase6": phase6,
            "phase7c": phase7c,
            "runtime": runtime,
            "qor": qor,
            "physical": physical_summary,
            "platform": platform,
            "lowering": lowering,
            "placement": placement,
            "emission": emission,
            "artifacts": {"fpga0.routed_dcp": artifact},
        }

    def test_complete_release_is_cross_checked_and_hashed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            values = self._inputs(Path(temporary_directory))
            manifest = build_release_manifest(
                values["benchmark"],
                values["phase3"],
                values["phase4"],
                values["phase5"],
                values["phase6"],
                values["phase7c"],
                values["runtime"],
                values["qor"],
                values["physical"],
                values["platform"],
                values["lowering"],
                values["placement"],
                values["emission"],
                values["artifacts"],
                "abc123",
            )
            self.assertEqual(manifest["status"], "pass")
            self.assertEqual(
                {gate["status"] for gate in manifest["gates"].values()},
                {"pass"},
            )
            self.assertEqual(manifest["metrics"]["routed_cells"], 13)
            self.assertEqual(manifest["metrics"]["physical_cells"], 13)
            self.assertEqual(
                manifest["metrics"]["infrastructure_cells"], 0
            )
            self.assertEqual(len(manifest["artifacts"]), 1)

    def test_external_openparf_result_is_rejected_by_release_gate(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            values = self._inputs(Path(temporary_directory))
            values["placement"]["fpga0"]["provider"] = (
                "openparf-comparison-import"
            )
            with self.assertRaisesRegex(
                ValidationError, "root-built OpenPARF"
            ):
                build_release_manifest(
                    values["benchmark"],
                    values["phase3"],
                    values["phase4"],
                    values["phase5"],
                    values["phase6"],
                    values["phase7c"],
                    values["runtime"],
                    values["qor"],
                    values["physical"],
                    values["platform"],
                    values["lowering"],
                    values["placement"],
                    values["emission"],
                    values["artifacts"],
                    "abc123",
                )

    def test_cross_phase_count_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            values = self._inputs(Path(temporary_directory))
            values["phase5"]["validation"]["scheduled_bit_hops"] = 3
            with self.assertRaisesRegex(
                ValidationError, "bit-hop counts do not agree"
            ):
                build_release_manifest(
                    values["benchmark"],
                    values["phase3"],
                    values["phase4"],
                    values["phase5"],
                    values["phase6"],
                    values["phase7c"],
                    values["runtime"],
                    values["qor"],
                    values["physical"],
                    values["platform"],
                    values["lowering"],
                    values["placement"],
                    values["emission"],
                    values["artifacts"],
                    "abc123",
                )

    def test_multicast_logical_and_remote_sink_counts_are_distinct(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            values = self._inputs(Path(temporary_directory))
            values["phase3"]["validation"]["cut_sink_endpoints"] = 9
            values["phase6"]["validation"]["cut_sink_endpoints"] = 9
            values["phase4"]["validation"]["routed_sinks"] = 3
            values["phase5"]["validation"]["routed_sinks"] = 3
            manifest = build_release_manifest(
                values["benchmark"],
                values["phase3"],
                values["phase4"],
                values["phase5"],
                values["phase6"],
                values["phase7c"],
                values["runtime"],
                values["qor"],
                values["physical"],
                values["platform"],
                values["lowering"],
                values["placement"],
                values["emission"],
                values["artifacts"],
                "abc123",
            )
            self.assertEqual(manifest["status"], "pass")

    def test_logical_sink_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            values = self._inputs(Path(temporary_directory))
            values["phase6"]["validation"]["cut_sink_endpoints"] = 3
            with self.assertRaisesRegex(
                ValidationError, "logical sink endpoint counts"
            ):
                build_release_manifest(
                    values["benchmark"],
                    values["phase3"],
                    values["phase4"],
                    values["phase5"],
                    values["phase6"],
                    values["phase7c"],
                    values["runtime"],
                    values["qor"],
                    values["physical"],
                    values["platform"],
                    values["lowering"],
                    values["placement"],
                    values["emission"],
                    values["artifacts"],
                    "abc123",
                )

    def test_remote_sink_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            values = self._inputs(Path(temporary_directory))
            values["phase5"]["validation"]["routed_sinks"] = 3
            with self.assertRaisesRegex(
                ValidationError, "remote sink counts"
            ):
                build_release_manifest(
                    values["benchmark"],
                    values["phase3"],
                    values["phase4"],
                    values["phase5"],
                    values["phase6"],
                    values["phase7c"],
                    values["runtime"],
                    values["qor"],
                    values["physical"],
                    values["platform"],
                    values["lowering"],
                    values["placement"],
                    values["emission"],
                    values["artifacts"],
                    "abc123",
                )

    def test_run_phase7d_is_byte_reproducible(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            values = self._inputs(root)
            paths = {}
            for name in (
                "benchmark",
                "phase3",
                "phase4",
                "phase5",
                "phase6",
                "phase7c",
                "runtime",
                "qor",
                "physical",
            ):
                paths[name] = root / f"{name}.json"
                paths[name].write_text(
                    json.dumps(values[name]), encoding="utf-8"
                )
            platform_path = root / "platform.json"
            platform_path.write_text(
                json.dumps(values["platform"].to_dict()), encoding="utf-8"
            )
            report_paths = {}
            for kind in ("lowering", "placement", "emission"):
                report_paths[kind] = {}
                for fpga, report in values[kind].items():
                    path = root / f"{kind}-{fpga}.json"
                    path.write_text(json.dumps(report), encoding="utf-8")
                    report_paths[kind][fpga] = path
            outputs = []
            for run in ("first", "second"):
                output = root / run
                report = run_phase7d(
                    paths["benchmark"],
                    paths["phase3"],
                    paths["phase4"],
                    paths["phase5"],
                    paths["phase6"],
                    paths["phase7c"],
                    paths["runtime"],
                    paths["qor"],
                    paths["physical"],
                    platform_path,
                    report_paths["lowering"],
                    report_paths["placement"],
                    report_paths["emission"],
                    values["artifacts"],
                    "abc123",
                    output,
                )
                self.assertEqual(report["status"], "pass")
                outputs.append((output / "release_manifest.json").read_bytes())
            self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()
