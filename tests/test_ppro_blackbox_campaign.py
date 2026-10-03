from __future__ import annotations

import tempfile
import json
import unittest
from pathlib import Path
from unittest import mock

from emuflow.errors import ValidationError
from emuflow.ppro_blackbox_campaign import (
    PProCampaignRuntime,
    discover_generated_bundles,
    execute_generated_campaign,
)
from emuflow.ppro_blackbox_smoke import generate_connected_smoke_bundle
from emuflow.ppro_blackbox_application import generate_application_holdout_bundle
from emuflow.ppro_blackbox_provenance import runner_source_bundle
from test_ppro_blackbox_ppro_adapter import PARTITION_REPORT, ROUTE_REPORT, TIMING_REPORT


RUNNER_REVISION = runner_source_bundle()["runner_revision"]


class PProBlackboxCampaignTest(unittest.TestCase):
    def _runtime(self, root: Path) -> PProCampaignRuntime:
        install = root / "install"
        (install / "bin").mkdir(parents=True)
        (install / "setting_rtl.sh").write_text("true\n", encoding="utf-8")
        executable = install / "bin" / "rtlpart_linux"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            "out = Path.cwd() / 'project' / 'rtlpart' / 'report'\n"
            "out.mkdir(parents=True, exist_ok=True)\n"
            f"(out / 'pa0.rpt').write_text({PARTITION_REPORT!r})\n"
            f"(out / 'sr0.rpt').write_text({ROUTE_REPORT!r})\n"
            f"(out / 'sr0_time.rpt').write_text({TIMING_REPORT!r})\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
        platform = root / "example-platform.ref"
        platform.write_text("opaque", encoding="utf-8")
        return PProCampaignRuntime(
            result_root=root / "results",
            install_root=install,
            platform_reference=platform,
            fpga_aliases={"F11": "F0", "F33": "F1"},
            logical_targets={"F0": "MB1.F1", "F1": "MB1.F3"},
            authorized_writable_root=root,
            timeout_seconds=30,
        )

    def _bundles(self, root: Path):
        common = {
            "campaign_id": "campaign-test",
            "public_prior_id": "lx2-public-prior-v1",
            "configuration_id": "lx2-m1",
            "tool_release": "2026.1",
            "runner_revision": RUNNER_REVISION,
            "adapter_profile": "ppro-2026-ordinary-reports-v1",
        }
        generate_connected_smoke_bundle(
            root / "case-a", case_id="smoke-a", seed=1, **common
        )
        generate_connected_smoke_bundle(
            root / "case-b", case_id="smoke-b", seed=1, **common
        )

    def test_discovery_is_bounded_and_campaign_outputs_are_compact(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle_root = root / "bundles"
            self._bundles(bundle_root)
            bundles = discover_generated_bundles(bundle_root, maximum_cases=2)
            self.assertEqual(len(bundles), 2)
            with self.assertRaisesRegex(ValidationError, "maximum case count"):
                discover_generated_bundles(bundle_root, maximum_cases=1)

            runtime = self._runtime(root)
            results = execute_generated_campaign(
                bundles, runtime=runtime, max_workers=2
            )
            self.assertEqual(len(results), 2)
            self.assertTrue(
                all(result["execution"]["outcome"] == "pass" for result in results)
            )
            identities = [result["identity"]["id"] for result in results]
            self.assertEqual(identities, sorted(identities))
            for identity in identities:
                case = runtime.result_root / identity
                observation = case / "observation.json"
                self.assertTrue(observation.is_file())
                self.assertLess(observation.stat().st_size, 16384)
                self.assertFalse((case / "project").exists())
                self.assertFalse((case / ".run-ppro.tcl").exists())
            self.assertFalse(any(bundle_root.rglob("run-spec.json")))
            self.assertFalse(any(bundle_root.rglob("connected_smoke_context.vh")))

    def test_campaign_rejects_duplicate_case_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle_root = root / "bundles"
            self._bundles(bundle_root)
            bundles = discover_generated_bundles(bundle_root, maximum_cases=2)
            with self.assertRaisesRegex(ValidationError, "duplicate case identities"):
                execute_generated_campaign(
                    [bundles[0], bundles[0]], runtime=self._runtime(root)
                )
            self.assertFalse(any((root / "results").rglob(".run-ppro.tcl")))
            self.assertEqual(len(list(bundle_root.rglob("run-spec.json"))), 2)

    def test_campaign_rejects_unknown_bundle_entry_before_execution(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle_root = root / "bundles"
            self._bundles(bundle_root)
            bundles = discover_generated_bundles(bundle_root, maximum_cases=2)
            (bundles[0] / "unexpected.txt").write_text("do not delete", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "unknown or unsafe"):
                execute_generated_campaign(bundles, runtime=self._runtime(root))
            self.assertTrue((bundles[0] / "unexpected.txt").is_file())

    def test_campaign_rejects_unknown_nested_context_entry(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle_root = root / "bundles"
            self._bundles(bundle_root)
            bundles = discover_generated_bundles(bundle_root, maximum_cases=2)
            unexpected = bundles[0] / "context" / "unexpected.vh"
            unexpected.write_text("do not delete", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "unknown context"):
                execute_generated_campaign(bundles, runtime=self._runtime(root))
            self.assertTrue(unexpected.is_file())

    def test_queue_exception_cleans_every_prerendered_runtime(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle_root = root / "bundles"
            self._bundles(bundle_root)
            bundles = discover_generated_bundles(bundle_root, maximum_cases=2)
            runtime = self._runtime(root)
            with mock.patch(
                "emuflow.ppro_blackbox_campaign.execute_blackbox_queue",
                side_effect=OSError("simulated queue failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated queue failure"):
                    execute_generated_campaign(bundles, runtime=runtime, max_workers=1)
            self.assertEqual(len(list(bundle_root.rglob("run-spec.json"))), 2)
            self.assertFalse(any(runtime.result_root.rglob(".run-ppro.tcl")))
            self.assertFalse(any(runtime.result_root.rglob("project")))

    def test_license_failure_preserves_sealed_bundles_for_retry(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle_root = root / "bundles"
            self._bundles(bundle_root)
            bundles = discover_generated_bundles(bundle_root, maximum_cases=2)
            runtime = self._runtime(root)
            executable = runtime.install_root / "bin" / "rtlpart_linux"
            executable.write_text(
                "#!/bin/sh\necho 'license checkout failed' >&2\nexit 1\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)

            results = execute_generated_campaign(
                bundles, runtime=runtime, max_workers=2
            )
            self.assertEqual(
                {item["execution"]["outcome"] for item in results},
                {"license_failure"},
            )
            self.assertEqual(len(list(bundle_root.rglob("run-spec.json"))), 2)
            self.assertFalse(any(bundle_root.rglob(".run-ppro.tcl")))

    def test_application_campaign_consumes_strict_compilation_context(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            include = source / "include"
            include.mkdir(parents=True)
            (include / "config.vh").write_text("`define WIDTH 8\n", encoding="utf-8")
            (source / "top.v").write_text(
                '`include "config.vh"\nmodule top(input clk); endmodule\n',
                encoding="utf-8",
            )
            benchmark = root / "benchmark.json"
            benchmark.write_text(
                json.dumps(
                    {
                        "schema": "emuflow.benchmark-run/v1",
                        "id": "application-context",
                        "design_id": "application-context",
                        "calibration_holdout_class": "open_cpu",
                        "top": "top",
                        "sources": ["top.v"],
                        "clocks": ["clk"],
                        "platform": "unused.json",
                        "synthesis": {
                            "family": "xcup",
                            "policy": "logic-only",
                            "include_dirs": ["include"],
                            "defines": ["SYNTHESIS"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            bundle = generate_application_holdout_bundle(
                root / "bundle",
                benchmark_run_path=benchmark,
                source_root=source,
                campaign_id="application-campaign",
                public_prior_id="lx2-public-prior-v1",
                configuration_id="lx2-m1",
                tool_release="2026.1",
                runner_revision=RUNNER_REVISION,
            )
            results = execute_generated_campaign(
                [bundle.root], runtime=self._runtime(root), max_workers=1
            )
            self.assertEqual(results[0]["execution"]["outcome"], "pass")
            self.assertFalse(bundle.root.exists())


if __name__ == "__main__":
    unittest.main()
