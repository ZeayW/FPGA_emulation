from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.ppro_blackbox_runtime import (
    PProRuntimeConfig,
    parse_fpga_aliases,
    render_ppro_runtime_binding,
)
from emuflow.ppro_blackbox_application import benchmark_rtl_identity
from emuflow.ppro_blackbox_runner import execute_blackbox_case
from tests.test_ppro_blackbox_runner import run_spec


class PProBlackboxRuntimeTest(unittest.TestCase):
    def _fixture(self, root: Path):
        install = root / "install"
        (install / "bin").mkdir(parents=True)
        (install / "bin" / "rtlpart_linux").write_text("binary", encoding="utf-8")
        (install / "setting_rtl.sh").write_text("true\n", encoding="utf-8")
        inputs = root / "inputs"
        inputs.mkdir()
        (inputs / "probe.v").write_text("module ppro_blackbox_latency; endmodule\n")
        (inputs / "files.f").write_text("probe.v\n", encoding="utf-8")
        platform = root / "opaque-platform.ref"
        constraints = root / "documented-user-constraints.cfg"
        platform.write_text("opaque", encoding="utf-8")
        constraint_value = {
            "assignments": [{"partition": "P0", "target": "F0"}],
            "control_mode": "fixed_assignment",
            "documented_actions": ["partition_constraint"],
            "seed": 7,
        }
        constraints.write_text(json.dumps(constraint_value), encoding="utf-8")
        spec = run_spec()
        spec["adapter"]["profile"] = "ppro-2026-ordinary-reports-v1"
        spec["experiment"] = {
            "kind": "latency",
            "control_mode": "fixed_assignment",
            "documented_actions": ["partition_constraint"],
            "constraints_sha256": hashlib.sha256(
                json.dumps(
                    constraint_value, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest(),
        }
        config = PProRuntimeConfig(
            case_dir=root / "case",
            install_root=install,
            platform_reference=platform,
            documented_constraints=constraints,
            fpga_aliases={"F11": "F0", "F33": "F1"},
            logical_targets={"F0": "MB1.F1", "F1": "MB1.F3"},
            authorized_writable_root=root,
        )
        return spec, inputs / "files.f", config

    def test_renderer_rejects_stale_runner_revision_before_materialization(self):
        with tempfile.TemporaryDirectory() as raw:
            spec, filelist, config = self._fixture(Path(raw))
            spec["tool"]["runner_revision"] = "0" * 64
            with self.assertRaisesRegex(
                ValidationError,
                "runner revision does not match the active runtime source bundle",
            ):
                render_ppro_runtime_binding(
                    spec, source_filelist=filelist, config=config
                )
            self.assertFalse(config.case_dir.exists())

    def test_renderer_keeps_private_inputs_runtime_only(self):
        with tempfile.TemporaryDirectory() as raw:
            spec, filelist, config = self._fixture(Path(raw))
            binding = render_ppro_runtime_binding(
                spec, source_filelist=filelist, config=config
            )
            self.assertFalse(binding.retain_failure_diagnostics)
            self.assertTrue(binding.cleanup_raw_reports)
            script = (config.case_dir / ".run-ppro.tcl").read_text(encoding="utf-8")
            launcher = (config.case_dir / ".run-ppro.sh").read_text(encoding="utf-8")
            runtime_files = (config.case_dir / ".runtime-files.f").read_text(
                encoding="utf-8"
            )
            self.assertIn(
                "create_project -project_name {project} -project_path ", script
            )
            self.assertIn(" -force", script)
            self.assertIn("run_compile -top {ppro_blackbox_latency}", script)
            self.assertIn("run_pre_partition -stf", script)
            for resource in ("lut", "ff", "bram", "uram", "dsp"):
                self.assertIn(f"-{resource}_area 75", script)
            self.assertIn("run_partition -costmode 1 -max_process_num 4", script)
            self.assertIn("run_system_route", script)
            self.assertNotIn("run_system_route -timing_budget", script)
            self.assertNotIn("run_ssta -state sr0", script)
            self.assertIn("rtlpart_linux < ", launcher)
            ppro_constraints = (config.case_dir / ".prepartition.cfg").read_text(
                encoding="utf-8"
            )
            self.assertIn("assign_inst {P0} {MB1.F1}", ppro_constraints)
            self.assertEqual(
                runtime_files.strip(), str((filelist.parent / "probe.v").resolve())
            )
            self.assertEqual(binding.fpga_aliases, {"F11": "F0", "F33": "F1"})
            self.assertEqual(binding.output_path.name, "observation.json")
            self.assertIn((config.case_dir / "project").resolve(), binding.cleanup_paths)
            self.assertEqual(binding.environment["TMPDIR"], str((config.case_dir / ".tmp").resolve()))
            self.assertEqual(
                binding.environment["HOME"],
                str((config.case_dir / ".tmp" / "home").resolve()),
            )
            self.assertIn(
                (config.case_dir / "runtime_Flag.tcl").resolve(),
                binding.cleanup_paths,
            )
            self.assertNotIn(str(config.install_root), repr(spec))
            self.assertNotIn(str(config.platform_reference), repr(spec))
            self.assertNotIn("MB1.F1", repr(spec))

    def test_renderer_rejects_mock_profile_and_filelist_options(self):
        with tempfile.TemporaryDirectory() as raw:
            spec, filelist, config = self._fixture(Path(raw))
            mock_spec = run_spec()
            with self.assertRaisesRegex(ValidationError, "real ordinary-report"):
                render_ppro_runtime_binding(
                    mock_spec, source_filelist=filelist, config=config
                )
            filelist.write_text("+incdir+secret\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "options"):
                render_ppro_runtime_binding(
                    spec, source_filelist=filelist, config=config
                )

    def test_renderer_rejects_constraints_that_disagree_with_run_spec(self):
        with tempfile.TemporaryDirectory() as raw:
            spec, filelist, config = self._fixture(Path(raw))
            value = json.loads(
                config.documented_constraints.read_text(encoding="utf-8")
            )
            value["seed"] = 8
            config.documented_constraints.write_text(
                json.dumps(value), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValidationError, "disagree with the run spec"):
                render_ppro_runtime_binding(
                    spec, source_filelist=filelist, config=config
                )

    def test_connected_smoke_v3_cannot_skip_compilation_context(self):
        with tempfile.TemporaryDirectory() as raw:
            spec, filelist, config = self._fixture(Path(raw))
            spec["workload"]["generator_id"] = "ppro-blackbox-connected-smoke-v3"
            with self.assertRaisesRegex(ValidationError, "require a compilation context"):
                render_ppro_runtime_binding(
                    spec, source_filelist=filelist, config=config
                )

    def test_application_context_is_sealed_and_rendered_as_standard_filelist_options(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            spec, filelist, config = self._fixture(root)
            source_root = filelist.parent
            include = source_root / "include"
            include.mkdir()
            (include / "config.vh").write_text("`define WIDTH 8\n", encoding="utf-8")
            benchmark = root / "benchmark.json"
            benchmark.write_text(
                json.dumps(
                    {
                        "schema": "emuflow.benchmark-run/v1",
                        "id": "runtime-context",
                        "design_id": "runtime-context",
                        "top": "ppro_blackbox_latency",
                        "sources": ["probe.v"],
                        "clocks": ["clk"],
                        "clock_periods_ns": {"clk": 10.0},
                        "platform": "unused.json",
                        "synthesis": {
                            "family": "xcup",
                            "policy": "logic-only",
                            "include_dirs": ["include"],
                            "defines": ["SYNTHESIS", "WIDTH=8"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            identity = benchmark_rtl_identity(benchmark, source_root)
            spec["workload"]["rtl_sha256"] = identity["rtl_sha256"]
            spec["experiment"]["kind"] = "application_holdout"
            spec["experiment"]["control_mode"] = "none"
            spec["experiment"]["documented_actions"] = []
            spec["adapter"]["expected_reports"] = [
                "partition_summary",
                "resource_summary",
            ]
            application_constraints = root / "application-constraints.json"
            application_value = {
                "control_mode": "none",
                "documented_actions": [],
                "seed": 7,
                "timing_clocks": [{"port": "clk", "period_ns": 10.0}],
            }
            application_constraints.write_text(
                json.dumps(application_value), encoding="utf-8"
            )
            spec["experiment"]["constraints_sha256"] = hashlib.sha256(
                json.dumps(
                    application_value, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            config = PProRuntimeConfig(
                **{
                    **config.__dict__,
                    "documented_constraints": application_constraints,
                    "logical_targets": {},
                }
            )
            context = root / "compilation-context.json"
            context.write_text(
                json.dumps(
                    {
                        "schema": "emuflow.ppro-compilation-context/v1",
                        "source_root": str(source_root.resolve()),
                        "include_dirs": ["include"],
                        "defines": ["SYNTHESIS", "WIDTH=8"],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "require a compilation context"):
                render_ppro_runtime_binding(
                    spec,
                    source_filelist=filelist,
                    config=config,
                )
            binding = render_ppro_runtime_binding(
                spec,
                source_filelist=filelist,
                compilation_context=context,
                config=config,
            )
            script = (config.case_dir / ".run-ppro.tcl").read_text(
                encoding="utf-8"
            )
            self.assertIn("run_system_route -timing_budget", script)
            self.assertIn("run_gen_rtl -max_process_num 4", script)
            self.assertIn("run_ssta -post_partition -state gr0", script)
            self.assertNotIn("run_ssta -post_partition -state gr0 -config", script)
            self.assertEqual(
                binding.report_paths["system_timing"].name,
                "gr0_ssta_post_part.rpt",
            )
            compile_config = (config.case_dir / ".compile.cfg").read_text(
                encoding="utf-8"
            )
            self.assertEqual(
                compile_config,
                "add_file {" + str((config.case_dir / ".timing.sdc").resolve()) + "}\n",
            )
            self.assertIn(
                " -config {" + str((config.case_dir / ".compile.cfg").resolve()) + "}",
                script,
            )
            self.assertEqual(
                (config.case_dir / ".timing.sdc").read_text(encoding="utf-8"),
                "# Generated from provider-neutral benchmark timing constraints.\n"
                "create_clock -name {clk} -period 10.000000000 "
                "[get_ports {clk}]\n",
            )
            lines = (config.case_dir / ".runtime-files.f").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(lines[0], f"+incdir+{include.resolve()}")
            self.assertEqual(lines[1:3], ["+define+SYNTHESIS", "+define+WIDTH=8"])
            self.assertEqual(lines[3], str((source_root / "probe.v").resolve()))

            (include / "config.vh").write_text("`define WIDTH 9\n", encoding="utf-8")
            second_config = PProRuntimeConfig(
                **{**config.__dict__, "case_dir": root / "tampered-case"}
            )
            with self.assertRaisesRegex(ValidationError, "sealed RTL identity"):
                render_ppro_runtime_binding(
                    spec,
                    source_filelist=filelist,
                    compilation_context=context,
                    config=second_config,
                )

    def test_alias_parser_is_strict(self):
        self.assertEqual(
            parse_fpga_aliases(["F11=F0", "F33=F1"]),
            {"F11": "F0", "F33": "F1"},
        )
        with self.assertRaises(ValidationError):
            parse_fpga_aliases(["A=F0", "B=F0"])
        with self.assertRaises(ValidationError):
            parse_fpga_aliases(["missing-separator"])

    def test_renderer_refuses_to_overwrite_a_completed_case(self):
        with tempfile.TemporaryDirectory() as raw:
            spec, filelist, config = self._fixture(Path(raw))
            config.case_dir.mkdir(parents=True)
            (config.case_dir / "observation.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "already has an observation"):
                render_ppro_runtime_binding(
                    spec, source_filelist=filelist, config=config
                )

    def test_renderer_enforces_authorized_writable_root(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            spec, filelist, config = self._fixture(root)
            invalid = PProRuntimeConfig(
                case_dir=root / "case",
                install_root=config.install_root,
                platform_reference=config.platform_reference,
                documented_constraints=config.documented_constraints,
                fpga_aliases=config.fpga_aliases,
                logical_targets=config.logical_targets,
                authorized_writable_root=root / "different-root",
            )
            with self.assertRaisesRegex(ValidationError, "authorized writable root"):
                render_ppro_runtime_binding(
                    spec, source_filelist=filelist, config=invalid
                )

    def test_renderer_rejects_nonwritable_authorized_root_before_case_creation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            spec, filelist, config = self._fixture(root)
            missing_root = root / "missing-research-root"
            invalid = PProRuntimeConfig(
                case_dir=missing_root / "case",
                install_root=config.install_root,
                platform_reference=config.platform_reference,
                documented_constraints=config.documented_constraints,
                fpga_aliases=config.fpga_aliases,
                logical_targets=config.logical_targets,
                authorized_writable_root=missing_root,
            )
            with self.assertRaisesRegex(
                ValidationError, "authorized writable root does not exist"
            ):
                render_ppro_runtime_binding(
                    spec, source_filelist=filelist, config=invalid
                )
            self.assertFalse(missing_root.exists())

    def test_disposable_runtime_is_scrubbed_after_compact_observation(self):
        with tempfile.TemporaryDirectory() as raw:
            spec, filelist, config = self._fixture(Path(raw))
            executable = config.install_root / "bin" / "rtlpart_linux"
            executable.write_text(
                """#!/usr/bin/env bash
set -e
out="$PWD/project/rtlpart/report"
mkdir -p "$out"
printf 'provider runtime state\n' > "$PWD/runtime_Flag.tcl"
printf 'provider home state\n' > "$HOME/provider-state"
cat > "$out/pa0.rpt" <<'EOF'
| Resource Type | PIO | INT | LUT | FF | BRAM | LUTRAM | DSP | URAM | MSGPORT | STATICPROBE | BOUNDARY |
| F11 | 0 | 0 | 60 | 90 | 1 | 0 | 1 | 0 | 0 | 0 | 0 |
| F33 | 0 | 0 | 40 | 70 | 1 | 0 | 1 | 0 | 0 | 0 | 0 |
| Total Resource | 0 | 0 | 100 | 160 | 2 | 0 | 2 | 0 | 0 | 0 | 0 |
| Resource Type | PIO | INT | LUT | FF | BRAM | LUTRAM | DSP | URAM | MSGPORT | STATICPROBE | BOUNDARY |
| F11 | 0% | 0% | 3% | 4% | 5% | 0% | 6% | 0% | 0% | 0% | 0% |
| F33 | 0% | 0% | 4% | 5% | 6% | 0% | 7% | 0% | 0% | 0% | 0% |
| Total Util | 0% | 0% | 4% | 5% | 6% | 0% | 7% | 0% | 0% | 0% | 0% |
EOF
cat > "$out/sr0.rpt" <<'EOF'
2.1 fpga tdm connect net num
| src_fpga | dst_fpga | tdm_net_num |
| 11 | 33 | 4 |
2.2 fpga untdm connect net num
| src_fpga | dst_fpga | untdm_net_num |
2.3 tdm cable info report
| srcFPGA | dstFPGA | srcJconn | dstJconn |
| 11 | 33 | J0 | J0 |
2.4 tdm_info report
| FPGAID | targetFPGAID | BankID | targetBankID | dir | eringDevID | lineRate | channelNum | maxRatio |
| F11 | F33 | 1 | 1 | OUTPUT | 1 | 1600 | 4 | 1 |
2.5 fpga tdm detailed info file path
EOF
cat > "$out/gr0_ssta_post_part.rpt" <<'EOF'
10.25 data arrival time ( normalized delay 10.25 )
EOF
""",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            binding = render_ppro_runtime_binding(
                spec, source_filelist=filelist, config=config
            )
            result = execute_blackbox_case(spec, binding)
            self.assertEqual(result["execution"]["outcome"], "pass")
            self.assertEqual(
                result["metrics"]["timing"]["sr0_worst_cross_fpga_delay_ns"], 10.25
            )
            self.assertTrue(binding.output_path.is_file())
            self.assertFalse((config.case_dir / "project").exists())
            self.assertFalse((config.case_dir / ".run-ppro.tcl").exists())
            self.assertFalse((config.case_dir / ".run-ppro.sh").exists())
            self.assertFalse((config.case_dir / "runtime_Flag.tcl").exists())
            self.assertFalse((config.case_dir / ".tmp").exists())
            serialized = binding.output_path.read_text(encoding="utf-8")
            self.assertNotIn(str(config.install_root), serialized)
            self.assertNotIn(str(config.platform_reference), serialized)

    def test_keep_raw_project_enables_bounded_failure_diagnostics(self):
        with tempfile.TemporaryDirectory() as raw:
            spec, filelist, config = self._fixture(Path(raw))
            config = PProRuntimeConfig(
                **{**config.__dict__, "keep_raw_project": True}
            )
            binding = render_ppro_runtime_binding(
                spec, source_filelist=filelist, config=config
            )
            self.assertTrue(binding.retain_failure_diagnostics)
            self.assertFalse(binding.cleanup_raw_reports)

    def test_report_aliases_may_include_transit_fpgas(self):
        with tempfile.TemporaryDirectory() as raw:
            spec, filelist, config = self._fixture(Path(raw))
            config = PProRuntimeConfig(
                **{
                    **config.__dict__,
                    "fpga_aliases": {"F11": "F0", "F22": "F2", "F33": "F1"},
                }
            )
            binding = render_ppro_runtime_binding(
                spec, source_filelist=filelist, config=config
            )
            self.assertEqual(binding.fpga_aliases["F22"], "F2")


if __name__ == "__main__":
    unittest.main()
