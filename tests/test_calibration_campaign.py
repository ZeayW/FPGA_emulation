import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from emuflow.calibration_campaign import (
    RUN_RESULT_SCHEMA,
    _capacity_rtl,
    collect_calibration_observations,
    plan_calibration_campaign,
    validate_calibration_campaign,
    validate_calibration_run_result,
)
from emuflow.errors import ValidationError
from emuflow.io import read_json, write_json


def campaign():
    return {
        "schema": "emuflow.platform-calibration-campaign/v1",
        "dataset": {
            "id": "synthetic-fit-campaign",
            "role": "fit",
            "reference_alias": "synthetic-reference",
            "source_class": "synthetic_fixture",
            "authorization_id": "unit-test-public-fixture",
            "publication_scope": "public",
        },
        "configurations": [
            {
                "id": "2fpga-p2p",
                "topology_file": "/external/synthetic-2fpga.stf",
                "targets": {"F0": "B1.F1", "F1": "B1.F2"},
                "utilization_limits_percent": {
                    "lut": 75,
                    "ff": 75,
                    "bram": 75,
                    "dsp": 75,
                },
                "routes": [
                    {
                        "id": "F0-F1",
                        "path": ["F0", "F1"],
                        "control": "topology_unique_path",
                    }
                ],
            },
            {
                "id": "3fpga-chain",
                "topology_file": "/external/synthetic-3fpga.stf",
                "targets": {
                    "F0": "B1.F1",
                    "F1": "B1.F2",
                    "F2": "B1.F3",
                },
                "utilization_limits_percent": {
                    "lut": 75,
                    "ff": 75,
                    "bram": 75,
                    "dsp": 75,
                },
                "routes": [
                    {
                        "id": "F0-F2",
                        "path": ["F0", "F1", "F2"],
                        "control": "topology_unique_path",
                    }
                ],
            },
        ],
        "cases": [
            {
                "id": "lut-100",
                "kind": "capacity_boundary",
                "configuration": "2fpga-p2p",
                "resource": "lut",
                "units": 100,
                "target_fpga": "F0",
            },
            {
                "id": "lut-provider-failure",
                "kind": "capacity_boundary",
                "configuration": "2fpga-p2p",
                "resource": "lut",
                "units": 200,
                "target_fpga": "F0",
            },
            {
                "id": "link-32",
                "kind": "link_capacity_boundary",
                "configuration": "2fpga-p2p",
                "route": "F0-F1",
                "payload_bits": 32,
                "parallel_flows": 1,
            },
            {
                "id": "delay-two-hop",
                "kind": "link_delay",
                "configuration": "3fpga-chain",
                "route": "F0-F2",
                "payload_bits": 64,
                "parallel_flows": 3,
            },
        ],
    }


class CalibrationCampaignTest(unittest.TestCase):
    def test_capacity_probes_preserve_requested_resource_structure(self):
        ff_rtl = _capacity_rtl("ff", 8)
        self.assertIn('shreg_extract = "no"', ff_rtl)
        self.assertIn("probe[(i + 1) % 8]", ff_rtl)
        bram_rtl = _capacity_rtl("bram", 2)
        self.assertIn('ram_style = "block"', bram_rtl)
        self.assertIn("read_data <= memory[address]", bram_rtl)
        self.assertEqual(bram_rtl.count("calibration_bram_cell u_cell"), 1)
        dsp_rtl = _capacity_rtl("dsp", 2)
        self.assertIn('use_dsp = "yes"', dsp_rtl)
        self.assertIn("calibration_dsp_cell u_cell", dsp_rtl)

    def test_plan_materializes_isolated_rtl_and_fixed_constraints(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = plan_calibration_campaign(campaign(), root)
            self.assertEqual(manifest["execution_policy"]["cold_start"], True)
            self.assertFalse(manifest["execution_policy"]["persistent_cache"])
            self.assertEqual(len(manifest["cases"]), 4)
            capacity_cfg = (root / "cases/lut-100/prepartition.cfg").read_text()
            self.assertEqual(
                capacity_cfg,
                "assign_inst {u_probe} {B1.F1}\n",
            )
            link_cfg = (root / "cases/link-32/prepartition.cfg").read_text()
            self.assertIn("assign_inst {u_source_f0} {B1.F1}", link_cfg)
            self.assertIn("assign_inst {u_sink_f0} {B1.F2}", link_cfg)
            self.assertNotIn("-exclusive", link_cfg)
            self.assertIn("module calibration_top", (root / "cases/link-32/design.sv").read_text())
            lut_rtl = (root / "cases/lut-100/design.sv").read_text()
            self.assertIn("calibration_lut_cell u_cell", lut_rtl)
            self.assertIn('dont_touch = "true"', lut_rtl)
            self.assertNotIn("assign result = ^probe", lut_rtl)
            contention_rtl = (root / "cases/delay-two-hop/design.sv").read_text()
            self.assertIn("calibration_source u_source_f0", contention_rtl)
            self.assertIn("calibration_source u_source_f1", contention_rtl)
            self.assertIn("calibration_source u_source_f2", contention_rtl)
            self.assertIn("calibration_sink u_sink_f2", contention_rtl)
            self.assertNotIn("WIDTH*FLOWS", contention_rtl)
            runner = (root / "cases/link-32/run_ppro.tcl").read_text()
            self.assertIn("run_compile", runner)
            self.assertIn("run_pre_partition", runner)
            self.assertIn("relative topology_file requires PPRO_CT_RCF_ROOT", runner)
            self.assertNotIn("-res_result", runner)
            self.assertIn("-lut_area 75", runner)
            self.assertIn("run_partition", runner)
            self.assertIn("run_system_route", runner)
            self.assertIn("fresh cold-start directory", runner)
            launcher = (root / "cases/link-32/run_ppro.sh").read_text()
            self.assertIn('source "$PPRO_CT_RCF_ROOT/setting_rtl.sh"', launcher)
            self.assertIn(
                'set +u\nsource "$PPRO_CT_RCF_ROOT/setting_rtl.sh"\nset -u',
                launcher,
            )
            self.assertIn('export TMPDIR="$case_dir/tmp"', launcher)
            self.assertIn('"$PPRO_CT_RCF_ROOT/bin/rtlpart_linux"', launcher)
            self.assertIn('-script_file "$case_dir/run_ppro.tcl"', launcher)
            self.assertIn('> "$case_dir/runner.exit-code.tmp"', launcher)
            self.assertTrue((root / "cases/link-32/run_ppro.sh").stat().st_mode & 0o100)
            self.assertEqual(
                manifest["cases"][2]["runner"]["command"],
                ["bash", "cases/link-32/run_ppro.sh"],
            )
            self.assertEqual(
                manifest["cases"][2]["runner"]["kind"],
                "ppro_rtlpart_script_file_v1",
            )
            self.assertEqual(
                manifest["cases"][3]["logical_targets"],
                {"F0": "B1.F1", "F1": "B1.F2", "F2": "B1.F3"},
            )
            self.assertEqual(
                manifest["cases"][3]["expected_assignment"],
                {
                    "u_source_f0": "B1.F1",
                    "u_source_f1": "B1.F1",
                    "u_source_f2": "B1.F1",
                    "u_sink_f0": "B1.F3",
                    "u_sink_f1": "B1.F3",
                    "u_sink_f2": "B1.F3",
                },
            )
            self.assertEqual(
                read_json(root / "campaign-manifest.json")["schema"],
                "emuflow.platform-calibration-campaign-manifest/v1",
            )

    def test_collector_excludes_infrastructure_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = plan_calibration_campaign(campaign(), root)
            self._result(root, "lut-100", "pass", {"actual_resource_demand_per_fpga": 98})
            self._result(root, "lut-provider-failure", "license_fail", {})
            self._result(root, "link-32", "pass", {})
            self._result(
                root,
                "delay-two-hop",
                "pass",
                {
                    "sr0_worst_cross_fpga_delay_ns": 31.5,
                    "sr0_cross_fpga_path_count": 192,
                    "sr0_max_tdm_ratio": 3,
                },
            )
            observations = collect_calibration_observations(manifest, root)
            self.assertEqual(observations["collection"]["included_cases"], 3)
            self.assertEqual(
                observations["collection"]["excluded_cases"],
                [{"id": "lut-provider-failure", "reason": "license_fail"}],
            )
            self.assertEqual(observations["capacity_boundaries"][0]["demand_per_fpga"], 98)
            self.assertEqual(observations["link_delay_measurements"][0]["hop_count"], 2)
            self.assertEqual(observations["link_delay_measurements"][0]["contention_units"], 2)
            self.assertEqual(observations["link_delay_measurements"][0]["max_tdm_ratio"], 3)

    def test_generated_launcher_uses_reference_environment_and_script_file(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan_calibration_campaign(campaign(), root)
            reference_root = root / "reference"
            bin_dir = reference_root / "bin"
            bin_dir.mkdir(parents=True)
            (reference_root / "setting_rtl.sh").write_text(
                'if [[ -z "$SYN_HOME" ]]; then export PPRO_TEST_ENV=loaded; fi\n',
                encoding="utf-8",
            )
            fake_rtlpart = bin_dir / "rtlpart_linux"
            fake_rtlpart.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "printf '%s\\n' \"$PPRO_TEST_ENV\" > \"$PPRO_CAPTURE_DIR/environment.txt\"\n"
                "printf '%s\\n' \"$TMPDIR\" > \"$PPRO_CAPTURE_DIR/tmpdir.txt\"\n"
                "printf '%s\\n' \"${s2c_LICENSE:-}\" > \"$PPRO_CAPTURE_DIR/license.txt\"\n"
                "printf '%s\\n' \"$@\" > \"$PPRO_CAPTURE_DIR/argv.txt\"\n",
                encoding="utf-8",
            )
            fake_rtlpart.chmod(0o755)
            license_file = root / "license.lic"
            license_file.write_text("fixture\n", encoding="utf-8")
            capture = root / "capture"
            capture.mkdir()
            launcher = root / "cases/link-32/run_ppro.sh"
            subprocess.run(
                ["bash", str(launcher)],
                cwd=root.parent,
                env={
                    **os.environ,
                    "PPRO_CT_RCF_ROOT": str(reference_root),
                    "PPRO_CT_RCF_LICENSE": str(license_file),
                    "PPRO_CAPTURE_DIR": str(capture),
                },
                check=True,
            )
            self.assertEqual((capture / "environment.txt").read_text(), "loaded\n")
            self.assertEqual((capture / "license.txt").read_text(), f"{license_file}\n")
            observed_tmp = Path((capture / "tmpdir.txt").read_text().strip())
            self.assertEqual(observed_tmp.resolve(), (root / "cases/link-32/tmp").resolve())
            observed_argv = (capture / "argv.txt").read_text().splitlines()
            self.assertEqual(observed_argv[0], "-script_file")
            self.assertEqual(
                Path(observed_argv[1]).resolve(),
                (root / "cases/link-32/run_ppro.tcl").resolve(),
            )
            self.assertEqual(
                (root / "cases/link-32/runner.exit-code").read_text(), "0\n"
            )

    def test_result_contract_rejects_unknown_diagnostic_fields(self):
        value = {
            "schema": RUN_RESULT_SCHEMA,
            "case_id": "case-1",
            "status": "pass",
            "controls": {
                "assignment_applied": True,
                "route_applied": False,
                "observed_assignment": {},
                "observed_route": None,
            },
            "metrics": {},
            "raw_report_path": "/private/reference/report",
        }
        with self.assertRaisesRegex(ValidationError, "unknown fields"):
            validate_calibration_run_result(value, expected_case_id="case-1")

    def test_missing_fixed_control_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = plan_calibration_campaign(campaign(), root)
            self._result(
                root,
                "lut-100",
                "pass",
                {"actual_resource_demand_per_fpga": 98},
                assignment=False,
            )
            with self.assertRaisesRegex(ValidationError, "fixed assignment"):
                collect_calibration_observations(manifest, root)

    def test_link_capacity_case_must_be_single_hop(self):
        value = campaign()
        value["cases"][2]["configuration"] = "3fpga-chain"
        value["cases"][2]["route"] = "F0-F2"
        with self.assertRaisesRegex(ValidationError, "single-hop"):
            validate_calibration_campaign(value)

    def test_utilization_limit_must_be_explicit_and_bounded(self):
        value = campaign()
        value["configurations"][0]["utilization_limits_percent"]["lut"] = 101
        with self.assertRaisesRegex(ValidationError, "<= 100"):
            validate_calibration_campaign(value)

    def test_observed_route_must_match_unique_topology_path(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = plan_calibration_campaign(campaign(), root)
            self._result(
                root,
                "link-32",
                "pass",
                {},
                observed_route=["F0", "F9", "F1"],
            )
            with self.assertRaisesRegex(ValidationError, "observed route"):
                collect_calibration_observations(manifest, root)

    def test_existing_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "owned.txt").write_text("user data")
            with self.assertRaisesRegex(ValidationError, "not empty"):
                plan_calibration_campaign(campaign(), root)

    def test_unknown_source_class_is_rejected(self):
        value = campaign()
        value["dataset"]["source_class"] = "private_tool_clone"
        with self.assertRaisesRegex(ValidationError, "source_class"):
            validate_calibration_campaign(value)

    def test_case_identifier_cannot_escape_output_root(self):
        value = campaign()
        value["cases"][0]["id"] = "../outside"
        with self.assertRaisesRegex(ValidationError, "identifier"):
            validate_calibration_campaign(value)

    def test_collector_rejects_modified_result_contract(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = plan_calibration_campaign(campaign(), root)
            manifest["cases"][0]["result_contract"] = "../foreign-result.json"
            with self.assertRaisesRegex(ValidationError, "result contract"):
                collect_calibration_observations(manifest, root)

    @staticmethod
    def _result(
        root,
        case_id,
        status,
        metrics,
        assignment=True,
        route=True,
        observed_route=None,
    ):
        assignments = {
            "lut-100": {"u_probe": "B1.F1"},
            "lut-provider-failure": {"u_probe": "B1.F1"},
            "link-32": {"u_source_f0": "B1.F1", "u_sink_f0": "B1.F2"},
            "delay-two-hop": {
                "u_source_f0": "B1.F1",
                "u_source_f1": "B1.F1",
                "u_source_f2": "B1.F1",
                "u_sink_f0": "B1.F3",
                "u_sink_f1": "B1.F3",
                "u_sink_f2": "B1.F3",
            },
        }
        routes = {
            "link-32": ["F0", "F1"],
            "delay-two-hop": ["F0", "F1", "F2"],
        }
        write_json(
            root / "cases" / case_id / "result.json",
            {
                "schema": RUN_RESULT_SCHEMA,
                "case_id": case_id,
                "status": status,
                "controls": {
                    "assignment_applied": assignment,
                    "route_applied": route,
                    "observed_assignment": assignments[case_id],
                    "observed_route": (
                        observed_route
                        if observed_route is not None
                        else routes.get(case_id)
                    ),
                },
                "metrics": metrics,
            },
        )


if __name__ == "__main__":
    unittest.main()
