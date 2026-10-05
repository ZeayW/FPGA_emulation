from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.ppro_blackbox_application import generate_application_holdout_bundle


class PProBlackboxApplicationTest(unittest.TestCase):
    def test_holdout_requires_a_period_for_every_clock(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_root = root / "source"
            source_root.mkdir()
            (source_root / "top.v").write_text(
                "module top(input wire clk); endmodule\n", encoding="utf-8"
            )
            benchmark = root / "benchmark.json"
            benchmark.write_text(
                json.dumps(
                    {
                        "schema": "emuflow.benchmark-run/v1",
                        "id": "missing_period",
                        "design_id": "missing_period",
                        "calibration_holdout_class": "open_cpu",
                        "top": "top",
                        "sources": ["top.v"],
                        "clocks": ["clk"],
                        "platform": "unused.json",
                        "synthesis": {"family": "xcup", "policy": "logic-only"},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "period for every clock"):
                generate_application_holdout_bundle(
                    root / "bundle",
                    benchmark_run_path=benchmark,
                    source_root=source_root,
                    campaign_id="blind",
                    public_prior_id="prior-v1",
                    configuration_id="platform-v1",
                    tool_release="2026.1",
                    runner_revision="d" * 64,
                )

    def test_holdout_class_is_required_before_bundle_generation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_root = root / "source"
            source_root.mkdir()
            (source_root / "top.v").write_text(
                "module top(input wire clk); endmodule\n", encoding="utf-8"
            )
            benchmark = root / "benchmark.json"
            benchmark.write_text(
                json.dumps(
                    {
                        "schema": "emuflow.benchmark-run/v1",
                        "id": "unclassified_holdout",
                        "design_id": "unclassified",
                        "top": "top",
                        "sources": ["top.v"],
                        "clocks": ["clk"],
                        "platform": "unused.json",
                        "synthesis": {"family": "xcup", "policy": "logic-only"},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValidationError, "lacks calibration_holdout_class"
            ):
                generate_application_holdout_bundle(
                    root / "bundle",
                    benchmark_run_path=benchmark,
                    source_root=source_root,
                    campaign_id="blind",
                    public_prior_id="prior-v1",
                    configuration_id="platform-v1",
                    tool_release="2026.1",
                    runner_revision="d" * 64,
                )

    def test_catalog_holdout_is_free_partition_and_path_redacted(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_root = root / "source"
            source_root.mkdir()
            (source_root / "top.v").write_text(
                "module top(input wire clk, output reg q); always @(posedge clk) q <= ~q; endmodule\n",
                encoding="utf-8",
            )
            benchmark = root / "benchmark.json"
            benchmark.write_text(
                json.dumps(
                    {
                        "schema": "emuflow.benchmark-run/v1",
                        "id": "natural_holdout",
                        "design_id": "natural",
                        "calibration_holdout_class": "open_cpu",
                        "top": "top",
                        "sources": ["*.v"],
                        "clocks": ["clk"],
                        "clock_periods_ns": {"clk": 10.0},
                        "platform": "unused-by-ppro.json",
                        "synthesis": {"family": "xcup", "policy": "logic-only"},
                    }
                ),
                encoding="utf-8",
            )
            bundle = generate_application_holdout_bundle(
                root / "bundle",
                benchmark_run_path=benchmark,
                source_root=source_root,
                campaign_id="blind",
                public_prior_id="prior-v1",
                configuration_id="platform-v1",
                tool_release="2026.1",
                runner_revision="d" * 64,
            )
            spec = bundle.run_spec
            self.assertEqual(spec["identity"]["role"], "holdout")
            self.assertEqual(spec["experiment"]["kind"], "application_holdout")
            self.assertEqual(spec["experiment"]["control_mode"], "none")
            self.assertEqual(spec["experiment"]["documented_actions"], [])
            self.assertEqual(
                spec["adapter"]["expected_reports"],
                ["partition_summary", "resource_summary"],
            )
            self.assertEqual(spec["workload"]["design_metrics"]["source_file_count"], 1.0)
            self.assertEqual(spec["workload"]["design_metrics"]["include_file_count"], 0.0)
            self.assertNotIn(str(root), json.dumps(spec))
            context = json.loads(
                bundle.compilation_context_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                context["schema"], "emuflow.ppro-compilation-context/v1"
            )
            self.assertEqual(context["include_dirs"], [])
            self.assertEqual(context["defines"], [])
            constraints = json.loads(bundle.constraints_path.read_text(encoding="utf-8"))
            self.assertEqual(constraints["control_mode"], "none")
            self.assertEqual(
                constraints["timing_clocks"],
                [{"period_ns": 10.0, "port": "clk"}],
            )

    def test_source_change_changes_rtl_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_root = root / "source"
            source_root.mkdir()
            rtl = source_root / "top.v"
            benchmark = root / "benchmark.json"
            benchmark.write_text(
                json.dumps(
                    {
                        "schema": "emuflow.benchmark-run/v1",
                        "id": "identity_test",
                        "design_id": "identity",
                        "calibration_holdout_class": "open_cpu",
                        "top": "top",
                        "sources": ["top.v"],
                        "clocks": ["clk"],
                        "clock_periods_ns": {"clk": 10.0},
                        "platform": "unused.json",
                        "synthesis": {"family": "xcup", "policy": "logic-only"},
                    }
                ),
                encoding="utf-8",
            )
            kwargs = dict(
                benchmark_run_path=benchmark,
                source_root=source_root,
                campaign_id="blind",
                public_prior_id="prior-v1",
                configuration_id="platform-v1",
                tool_release="2026.1",
                runner_revision="d" * 64,
            )
            rtl.write_text("module top(input clk); endmodule\n", encoding="utf-8")
            first = generate_application_holdout_bundle(root / "one", **kwargs)
            rtl.write_text("module top(input clk); wire x = clk; endmodule\n", encoding="utf-8")
            second = generate_application_holdout_bundle(root / "two", **kwargs)
            self.assertNotEqual(
                first.run_spec["workload"]["rtl_sha256"],
                second.run_spec["workload"]["rtl_sha256"],
            )

    def test_clock_period_changes_compilation_identity_not_rtl_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_root = root / "source"
            source_root.mkdir()
            (source_root / "top.v").write_text(
                "module top(input clk); endmodule\n", encoding="utf-8"
            )
            benchmark = root / "benchmark.json"
            value = {
                "schema": "emuflow.benchmark-run/v1",
                "id": "clock_identity",
                "design_id": "clock_identity",
                "calibration_holdout_class": "open_cpu",
                "top": "top",
                "sources": ["top.v"],
                "clocks": ["clk"],
                "clock_periods_ns": {"clk": 10.0},
                "platform": "unused.json",
                "synthesis": {"family": "xcup", "policy": "logic-only"},
            }
            benchmark.write_text(json.dumps(value), encoding="utf-8")
            kwargs = dict(
                benchmark_run_path=benchmark,
                source_root=source_root,
                campaign_id="blind",
                public_prior_id="prior-v1",
                configuration_id="platform-v1",
                tool_release="2026.1",
                runner_revision="d" * 64,
            )
            first = generate_application_holdout_bundle(root / "one", **kwargs)
            value["clock_periods_ns"]["clk"] = 8.0
            benchmark.write_text(json.dumps(value), encoding="utf-8")
            second = generate_application_holdout_bundle(root / "two", **kwargs)
            self.assertEqual(
                first.run_spec["workload"]["rtl_sha256"],
                second.run_spec["workload"]["rtl_sha256"],
            )
            self.assertNotEqual(
                first.run_spec["workload"]["parameters_sha256"],
                second.run_spec["workload"]["parameters_sha256"],
            )

    def test_header_or_define_change_changes_rtl_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_root = root / "source"
            include = source_root / "include"
            include.mkdir(parents=True)
            (source_root / "top.v").write_text(
                '`include "config.vh"\nmodule top(input clk); endmodule\n',
                encoding="utf-8",
            )
            header = include / "config.vh"
            header.write_text("`define WIDTH 8\n", encoding="utf-8")
            benchmark = root / "benchmark.json"
            value = {
                "schema": "emuflow.benchmark-run/v1",
                "id": "context_identity",
                "design_id": "context_identity",
                "calibration_holdout_class": "open_cpu",
                "top": "top",
                "sources": ["top.v"],
                "clocks": ["clk"],
                "clock_periods_ns": {"clk": 10.0},
                "platform": "unused.json",
                "synthesis": {
                    "family": "xcup",
                    "policy": "logic-only",
                    "include_dirs": ["include"],
                    "defines": ["SYNTHESIS"],
                },
            }
            benchmark.write_text(json.dumps(value), encoding="utf-8")
            kwargs = dict(
                benchmark_run_path=benchmark,
                source_root=source_root,
                campaign_id="blind",
                public_prior_id="prior-v1",
                configuration_id="platform-v1",
                tool_release="2026.1",
                runner_revision="d" * 64,
            )
            first = generate_application_holdout_bundle(root / "one", **kwargs)
            header.write_text("`define WIDTH 16\n", encoding="utf-8")
            second = generate_application_holdout_bundle(root / "two", **kwargs)
            value["synthesis"]["defines"] = ["SYNTHESIS", "FEATURE=1"]
            benchmark.write_text(json.dumps(value), encoding="utf-8")
            third = generate_application_holdout_bundle(root / "three", **kwargs)
            self.assertNotEqual(
                first.run_spec["workload"]["rtl_sha256"],
                second.run_spec["workload"]["rtl_sha256"],
            )
            self.assertNotEqual(
                second.run_spec["workload"]["rtl_sha256"],
                third.run_spec["workload"]["rtl_sha256"],
            )


if __name__ == "__main__":
    unittest.main()
