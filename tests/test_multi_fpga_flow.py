import copy
import hashlib
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.board_link_timing import build_board_link_timing_model
from emuflow.cli import _build_parser, _dispatch
from emuflow.errors import EmuFlowError, ValidationError
from emuflow.io import read_json, write_json
from emuflow.multi_fpga_flow import (
    finalize_multi_fpga_physical_checkpoint,
    run_multi_fpga_flow,
    validate_multi_fpga_flow_bundle,
    validate_multi_fpga_flow_report,
)
from emuflow.platform import Platform
from emuflow.tdm import (
    TDM_BASELINE_PROVIDER,
    reconstruct_tdm_schedule_timing_paths,
)
from emuflow.timing_routing import (
    GLOBAL_CANDIDATE_PROVIDER,
    NATIVE_TIMING_EVALUATED_PROVIDER,
)
from tests.native_build import (
    tdm_partition_feedback,
    tdm_ratio_optimizer,
    tdm_slot_optimizer,
    tdm_timing_dag_optimizer,
    tlr_router,
)


ROOT = Path(__file__).resolve().parents[1]
PLATFORM = (
    ROOT / "platforms/virtual/academic_vtr_2fpga_p2p.json"
)
STATIC_EXACT_PLATFORM = (
    ROOT / "platforms/virtual/static_exact_acceptance_2fpga.json"
)
FAKE_OPENSTA = ROOT / "tests/fixtures/fake_opensta_paths.py"


class MultiFpgaFlowTest(unittest.TestCase):
    def test_static_exact_acceptance_fixture_is_not_vacuous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(
                EmuFlowError, "cannot populate|required FPGA|capacity"
            ):
                run_multi_fpga_flow(
                    platform_path=STATIC_EXACT_PLATFORM,
                    output_dir=Path(temporary_directory) / "sequential",
                    yosys_json=(
                        ROOT / "examples/yosys/static_exact_chain.json"
                    ),
                    top="static_exact_chain",
                    clocks=["clk"],
                    partition_provider="greedy",
                    cut_mode="sequential-only",
                    timing_driven=False,
                    clock_periods={"clk": 10.0},
                    opensta=str(FAKE_OPENSTA),
                    frame_slots=32,
                    equivalence_cycles=2,
                )

    def test_one_command_static_exact_flow_reaches_phase7c_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "static-exact"
            report = run_multi_fpga_flow(
                platform_path=STATIC_EXACT_PLATFORM,
                output_dir=root,
                yosys_json=ROOT / "examples/yosys/static_exact_chain.json",
                top="static_exact_chain",
                clocks=["clk"],
                partition_provider="greedy",
                timing_driven=False,
                clock_periods={"clk": 10.0},
                opensta=str(FAKE_OPENSTA),
                router=str(tlr_router()),
                frame_slots=32,
                equivalence_cycles=2,
                cut_mode="static-exact-combinational",
                max_cross_fpga_dependency_depth=2,
                static_exact_candidate_policy=(
                    "assignment-derived-acyclic-v2"
                ),
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(
                report["stages"]["partition"]["validation"]["cut_mode"],
                "static-exact-combinational",
            )
            self.assertGreater(
                report["stages"]["partition"]["validation"][
                    "combinational_cut_nets"
                ],
                0,
            )
            self.assertEqual(
                report["stages"]["tdm"]["provider"],
                "deterministic-round-barrier-earliest-slot-v2",
            )
            self.assertEqual(
                json.loads((root / "tdm/schedule.json").read_text())[
                    "transport_semantics"
                ],
                "sampled-virtual-wire",
            )
            self.assertEqual(
                report["stages"]["split"]["equivalence"]["qualification"],
                "exhaustive-small-model-proof-plus-random-traces",
            )
            self.assertEqual(report["runtime"]["status"], "generated")
            validation = validate_multi_fpga_flow_bundle(
                root, minimum_combinational_cut_nets=1
            )
            self.assertEqual(validation["status"], "pass")
            self.assertEqual(validation["observed_combinational_cut_nets"], 2)
            with self.assertRaisesRegex(
                ValidationError, "no completed physical Phase 7"
            ):
                validate_multi_fpga_flow_bundle(root, require_physical=True)
            routes = read_json(root / "system-route/routes.json")
            routes["design"] = "tampered"
            write_json(root / "system-route/routes.json", routes)
            with self.assertRaisesRegex(ValidationError, "SHA-256 disagrees"):
                validate_multi_fpga_flow_bundle(root)

    def test_cli_enables_timing_driven_by_default(self) -> None:
        base = [
            "multi-fpga",
            "compile",
            "design.v",
            "--platform",
            "platform.json",
            "--out",
            "build",
        ]
        self.assertTrue(_build_parser().parse_args(base).timing_driven)
        self.assertFalse(
            _build_parser().parse_args(
                [*base, "--no-timing-driven"]
            ).timing_driven
        )
        validate = _build_parser().parse_args(
            [
                "multi-fpga",
                "validate",
                "--flow",
                "build",
                "--minimum-combinational-cut-nets",
                "1",
                "--require-physical",
            ]
        )
        self.assertEqual(validate.minimum_combinational_cut_nets, 1)
        self.assertTrue(validate.require_physical)
        phase3 = _build_parser().parse_args(
            [
                "phase3",
                "--ir",
                "design.emuir.json",
                "--platform",
                "platform.json",
                "--out",
                "phase3",
            ]
        )
        self.assertEqual(phase3.provider, "patron")
        self.assertEqual(
            phase3.cut_mode, "static-exact-combinational"
        )
        checkpoint = _build_parser().parse_args(
            [
                "experiment-stage",
                "partition-run",
                "--frontend",
                "frontend",
                "--timing",
                "timing",
                "--platform",
                "platform.json",
                "--out",
                "partition",
            ]
        )
        self.assertEqual(checkpoint.provider, "patron")
        self.assertEqual(
            checkpoint.cut_mode, "static-exact-combinational"
        )

    def test_cli_exact_mode_inherits_unified_slot_refinement_default(self):
        base = [
            "multi-fpga",
            "compile",
            "--yosys-json",
            str(ROOT / "examples/yosys/counter.json"),
            "--platform",
            str(PLATFORM),
            "--out",
            "unused",
        ]
        parser = _build_parser()
        exact = parser.parse_args(
            [*base, "--cut-mode", "static-exact-combinational"]
        )
        safe = parser.parse_args(base)
        with (
            patch("emuflow.cli.run_multi_fpga_flow") as run,
            patch("emuflow.cli._print_json"),
        ):
            run.return_value = {"status": "pass"}
            self.assertEqual(_dispatch(exact), 0)
            self.assertEqual(
                run.call_args.kwargs["slot_refinement_iterations"], 200
            )
            self.assertEqual(
                run.call_args.kwargs["static_exact_candidate_policy"],
                "assignment-derived-acyclic-v2",
            )
            self.assertEqual(
                run.call_args.kwargs["max_cross_fpga_dependency_depth"], 8
            )
            self.assertEqual(
                run.call_args.kwargs[
                    "mfspart_post_refinement_timing_path_beta"
                ],
                0.0,
            )
            run.reset_mock()
            self.assertEqual(_dispatch(safe), 0)
            self.assertEqual(
                run.call_args.kwargs["slot_refinement_iterations"], 200
            )
            self.assertEqual(
                run.call_args.kwargs["partition_provider"], "patron"
            )
            self.assertEqual(
                run.call_args.kwargs["cut_mode"],
                "static-exact-combinational",
            )
            self.assertEqual(
                run.call_args.kwargs["static_exact_candidate_policy"],
                "assignment-derived-acyclic-v2",
            )
            self.assertEqual(
                run.call_args.kwargs["max_cross_fpga_dependency_depth"], 8
            )

        explicit = parser.parse_args(
            [
                *base,
                "--cut-mode",
                "static-exact-combinational",
                "--slot-refinement-iterations",
                "7",
            ]
        )
        with (
            patch("emuflow.cli.run_multi_fpga_flow") as run,
            patch("emuflow.cli._print_json"),
        ):
            run.return_value = {"status": "pass"}
            self.assertEqual(_dispatch(explicit), 0)
            self.assertEqual(
                run.call_args.kwargs["slot_refinement_iterations"], 7
            )

    def test_exact_mode_allows_unified_slot_refinement(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(
                EmuFlowError, "requires at least one --clock-period"
            ):
                run_multi_fpga_flow(
                    platform_path=PLATFORM,
                    output_dir=Path(temporary_directory) / "exact",
                    yosys_json=ROOT / "examples/yosys/counter.json",
                    top="counter",
                    clocks=["clk"],
                    cut_mode="static-exact-combinational",
                    slot_refinement_iterations=7,
                )

    def test_physical_baseline_still_materializes_timing_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_sta = root / "sta"
            fake_sta.write_text(
                """#!/usr/bin/env python3
import os
from pathlib import Path

rows = Path(os.environ["EMUFLOW_STA_NET_MAP"]).read_text().splitlines()[1:]
header = (
    "path_id_hex\\tclock_domain_hex\\tclock_period_ns\\t"
    "slack_ns\\tfixed_delay_ns\\tpath_nets_hex"
)
records = [header]
clock = "clk".encode().hex()
for index, row in enumerate(rows):
    _, emuir_hex = row.split("\\t")
    records.append(
        f"{'path-' + str(index):s}".encode().hex()
        + f"\\t{clock}\\t10\\t9.5\\t0.5\\t{emuir_hex}"
    )
Path(os.environ["EMUFLOW_STA_OUTPUT"]).write_text(
    "\\n".join(records) + "\\n"
)
if os.environ.get("EMUFLOW_STA_THROUGH_NETS"):
    requested = Path(os.environ["EMUFLOW_STA_THROUGH_NETS"]).read_text().splitlines()[1:]
    coverage = ["emuir_net_hex\\tdriver_count\\tqueried_paths\\temitted_paths"]
    for row in requested:
        _, emuir_hex = row.split("\\t")
        coverage.append(f"{emuir_hex}\\t1\\t1\\t1")
    Path(os.environ["EMUFLOW_STA_THROUGH_COVERAGE"]).write_text(
        "\\n".join(coverage) + "\\n"
    )
""",
                encoding="utf-8",
            )
            fake_sta.chmod(fake_sta.stat().st_mode | stat.S_IXUSR)
            platform_name = Platform.load(PLATFORM).name

            def fake_physical(*args, **kwargs):
                self.assertIsNotNone(kwargs["original_ir_path"])
                self.assertIsNotNone(kwargs["assignment_path"])
                self.assertIsNotNone(kwargs["routes_path"])
                self.assertIsNotNone(kwargs["path_database_path"])
                output = args[3]
                output.mkdir(parents=True)
                report = {
                    "status": "pass",
                    "design": "counter",
                    "platform": platform_name,
                }
                write_json(
                    output / "multi-fpga-physical-flow-report.json", report
                )
                write_json(output / "physical-summary.json", {"status": "pass"})
                return report

            def fake_phase7c(*args, **kwargs):
                routes = read_json(kwargs["routes_path"])
                self.assertEqual(
                    routes["provider"], NATIVE_TIMING_EVALUATED_PROVIDER
                )
                self.assertTrue(routes["timing"]["paths"])
                phase5 = read_json(args[4])
                self.assertEqual(phase5["provider"], TDM_BASELINE_PROVIDER)
                self.assertIn("timing_validation", phase5)
                output = args[6]
                output.mkdir(parents=True)
                write_json(output / "runtime_contract.json", {})
                write_json(output / "qor_report.json", {})
                return {
                    "status": "pass",
                    "design": "counter",
                    "platform": platform_name,
                    "validation": {"status": "pass"},
                    "physical": {"status": "pass"},
                    "system_timing": {"status": "pass"},
                }

            with (
                patch(
                    "emuflow.multi_fpga_flow.run_multi_fpga_physical_flow",
                    side_effect=fake_physical,
                ),
                patch(
                    "emuflow.multi_fpga_flow.run_phase7c",
                    side_effect=fake_phase7c,
                ),
                patch(
                    "emuflow.multi_fpga_flow.validate_multi_fpga_flow_report",
                    return_value={"status": "pass"},
                ),
            ):
                report = run_multi_fpga_flow(
                    platform_path=PLATFORM,
                    output_dir=root / "flow",
                    yosys_json=ROOT / "examples/yosys/counter.json",
                    top="counter",
                    clocks=["clk"],
                    partition_provider="greedy",
                    cut_mode="sequential-only",
                    timing_driven=False,
                    clock_periods={"clk": 10.0},
                    opensta=str(fake_sta),
                    router=str(tlr_router()),
                    frame_slots=32,
                    phase6_provider="baseline",
                    physical=True,
                    equivalence_cycles=2,
                )
            self.assertFalse(report["timing"]["optimization_enabled"])
            self.assertTrue((root / "flow/timing/path-database.json").is_file())
            self.assertTrue((root / "flow/timing/cut-timing-paths.json").is_file())
            self.assertFalse(
                (root / "flow/timing/partition-net-weights.json").exists()
            )

    def test_finalizes_checked_independent_physical_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "multi"
            report = run_multi_fpga_flow(
                platform_path=PLATFORM,
                output_dir=root,
                yosys_json=ROOT / "examples/yosys/counter.json",
                top="counter",
                clocks=["clk"],
                partition_provider="greedy",
                cut_mode="sequential-only",
                timing_driven=False,
                clock_periods={"clk": 10.0},
                opensta=str(FAKE_OPENSTA),
                router=str(tlr_router()),
                frame_slots=32,
                equivalence_cycles=2,
            )
            physical_root = root / "physical-resumed"
            physical_root.mkdir()
            physical_report = {
                "status": "pass",
                "design": report["stages"]["frontend"]["design"],
                "platform": report["stages"]["frontend"]["platform"],
            }
            write_json(
                physical_root / "multi-fpga-physical-flow-report.json",
                physical_report,
            )
            write_json(
                physical_root / "physical-summary.json",
                {"status": "pass"},
            )

            def fake_phase7c(*args, **kwargs):
                runtime = args[6]
                runtime.mkdir(parents=True)
                write_json(runtime / "runtime_contract.json", {})
                write_json(runtime / "qor_report.json", {})
                return {"status": "pass"}

            with (
                patch(
                    "emuflow.multi_fpga_flow.validate_multi_fpga_physical_report",
                    return_value={"status": "pass"},
                ),
                patch(
                    "emuflow.multi_fpga_flow.validate_multi_fpga_flow_report",
                    return_value={"status": "pass"},
                ),
                patch(
                    "emuflow.multi_fpga_flow.run_phase7c",
                    side_effect=fake_phase7c,
                ),
            ):
                finalized = finalize_multi_fpga_physical_checkpoint(
                    root, physical_root
                )
            self.assertEqual(finalized["summary"]["status"], "pass")
            self.assertEqual(
                finalized["artifacts"]["physical_flow_report"]["path"],
                "physical-resumed/multi-fpga-physical-flow-report.json",
            )
            with self.assertRaisesRegex(ValidationError, "runtime directory"):
                finalize_multi_fpga_physical_checkpoint(
                    root, physical_root, runtime_directory="../escape"
                )

    def test_complete_flow_selects_parallel_global_route_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "global-route"
            timing_paths = root / "paths.json"
            write_json(
                timing_paths,
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": "counter",
                    "paths": [
                        {
                            "id": "counter-cut",
                            "clock_domain": "clk",
                            "clock_period_ns": 20.0,
                            "slack_ns": 1.0,
                            "fixed_delay_ns": 0.0,
                            "cut_nets": ["q[0]", "q[2]"],
                        }
                    ],
                },
            )
            report = run_multi_fpga_flow(
                platform_path=PLATFORM,
                output_dir=output,
                yosys_json=ROOT / "examples/yosys/counter.json",
                top="counter",
                clocks=["clk"],
                partition_provider="greedy",
                cut_mode="sequential-only",
                timing_driven=False,
                router=str(tlr_router()),
                route_provider=GLOBAL_CANDIDATE_PROVIDER,
                route_candidate_workers=2,
                timing_paths=timing_paths,
                frame_slots=32,
                tdm_provider=(
                    "deterministic-round-barrier-earliest-slot-v2"
                ),
                equivalence_cycles=2,
            )
            phase4 = report["stages"]["system_route"]
            self.assertEqual(phase4["provider"], GLOBAL_CANDIDATE_PROVIDER)
            self.assertEqual(
                phase4["candidate_generation"]["requested_workers"], 2
            )
            self.assertEqual(
                phase4["candidate_generation"]["ordering"],
                "demand-index-then-generator-index",
            )

    def test_checked_board_independent_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "multi"
            link_timing_path = root / "board-link-timing.json"
            write_json(
                link_timing_path,
                build_board_link_timing_model(Platform.load(PLATFORM)),
            )
            report = run_multi_fpga_flow(
                platform_path=PLATFORM,
                output_dir=output,
                yosys_json=ROOT / "examples/yosys/counter.json",
                top="counter",
                clocks=["clk"],
                partition_provider="greedy",
                cut_mode="sequential-only",
                timing_driven=False,
                board_link_timing_db=link_timing_path,
                clock_periods={"clk": 10.0},
                opensta=str(FAKE_OPENSTA),
                router=str(tlr_router()),
                frame_slots=32,
                equivalence_cycles=8,
            )
            self.assertEqual(report["summary"]["used_fpgas"], 2)
            self.assertEqual(report["summary"]["instances"], 8)
            self.assertEqual(report["summary"]["equivalence_mismatches"], 0)
            self.assertEqual(
                report["runtime"]["validation"]["status"], "pass"
            )
            self.assertFalse(report["timing"]["optimization_enabled"])
            self.assertEqual(
                report["summary"]["timing_optimization"],
                "disabled-baseline",
            )
            self.assertEqual(
                report["stages"]["system_route"]["provider"],
                NATIVE_TIMING_EVALUATED_PROVIDER,
            )
            self.assertEqual(
                report["stages"]["tdm"]["provider"],
                TDM_BASELINE_PROVIDER,
            )
            self.assertIn(
                "timing_validation", report["stages"]["tdm"]
            )
            self.assertFalse(
                report["board_link_timing"]["optimization_enabled"]
            )
            self.assertEqual(
                report["board_link_timing"]["applied_to"],
                [
                    "phase4-post-route-timing-evaluation",
                    "phase5-baseline-schedule-timing-evaluation",
                    "phase7c-system-timing-when-physical",
                ],
            )
            self.assertEqual(report["summary"]["frame_slots"], 32)
            self.assertEqual(
                report["stages"]["frontend"]["synthesis"]["mode"],
                "provided-yosys-json",
            )
            persisted = json.loads(
                (output / "multi-fpga-flow-report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                validate_multi_fpga_flow_report(persisted)["status"],
                "pass",
            )
            for relative in (
                "multi-fpga-flow-report.json",
                "frontend/phase1/design.emuir.json",
                "partition/assignment.json",
                "system-route/routes.json",
                "tdm/schedule.json",
                "split/manifest.json",
                "split/fpga0/netlist.json",
                "split/fpga1/netlist.json",
                "runtime/runtime_contract.json",
                "runtime/qor_report.json",
                "timing/path-database.json",
                "timing/cut-timing-paths.json",
            ):
                self.assertTrue((output / relative).is_file(), relative)

    def test_report_rejects_cross_stage_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            report = run_multi_fpga_flow(
                platform_path=PLATFORM,
                output_dir=Path(temporary_directory) / "multi",
                yosys_json=ROOT / "examples/yosys/counter.json",
                top="counter",
                clocks=["clk"],
                partition_provider="greedy",
                cut_mode="sequential-only",
                timing_driven=False,
                clock_periods={"clk": 10.0},
                opensta=str(FAKE_OPENSTA),
                router=str(tlr_router()),
                frame_slots=32,
                equivalence_cycles=2,
            )
            broken = copy.deepcopy(report)
            broken["stages"]["tdm"]["platform"] = "different"
            with self.assertRaisesRegex(
                ValidationError, "platform identity disagrees"
            ):
                validate_multi_fpga_flow_report(broken)
            broken = copy.deepcopy(report)
            broken["timing"]["optimization_enabled"] = True
            with self.assertRaisesRegex(
                ValidationError, "missing partition weights"
            ):
                validate_multi_fpga_flow_report(broken)
            broken = copy.deepcopy(report)
            del broken["stages"]["tdm"]["timing_validation"]
            with self.assertRaisesRegex(
                ValidationError, "timing-evaluated Phase 3--5 baseline"
            ):
                validate_multi_fpga_flow_report(broken)

    def test_timing_driven_pipeline_connects_sta_through_tdm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_sta = root / "sta"
            fake_sta.write_text(
                """#!/usr/bin/env python3
import os
from pathlib import Path

rows = Path(os.environ["EMUFLOW_STA_NET_MAP"]).read_text().splitlines()[1:]
header = (
    "path_id_hex\\tclock_domain_hex\\tclock_period_ns\\t"
    "slack_ns\\tfixed_delay_ns\\tpath_nets_hex"
)
records = [header]
clock = "clk".encode().hex()
for index, row in enumerate(rows):
    _, emuir_hex = row.split("\\t")
    path_id = f"path-{index}".encode().hex()
    records.append(
        f"{path_id}\\t{clock}\\t10\\t9.5\\t0.5\\t{emuir_hex}"
    )
Path(os.environ["EMUFLOW_STA_OUTPUT"]).write_text(
    "\\n".join(records) + "\\n"
)
if os.environ.get("EMUFLOW_STA_THROUGH_NETS"):
    requested = Path(os.environ["EMUFLOW_STA_THROUGH_NETS"]).read_text().splitlines()[1:]
    coverage = ["emuir_net_hex\\tdriver_count\\tqueried_paths\\temitted_paths"]
    for row in requested:
        _, emuir_hex = row.split("\\t")
        coverage.append(f"{emuir_hex}\\t1\\t1\\t1")
    Path(os.environ["EMUFLOW_STA_THROUGH_COVERAGE"]).write_text(
        "\\n".join(coverage) + "\\n"
    )
""",
                encoding="utf-8",
            )
            fake_sta.chmod(fake_sta.stat().st_mode | stat.S_IXUSR)
            platform = Platform.load(PLATFORM)
            link_timing = build_board_link_timing_model(platform)
            link_timing["links"][0]["delay_bound_ns"] = 12.0
            link_timing_path = root / "board-link-timing.json"
            write_json(link_timing_path, link_timing)
            output = root / "multi"
            report = run_multi_fpga_flow(
                platform_path=PLATFORM,
                output_dir=output,
                yosys_json=ROOT / "examples/yosys/counter.json",
                top="counter",
                clocks=["clk"],
                partition_provider="greedy",
                cut_mode="sequential-only",
                timing_driven=True,
                board_link_timing_db=link_timing_path,
                clock_periods={"clk": 10.0},
                opensta=str(fake_sta),
                router=str(tlr_router()),
                ratio_optimizer=str(tdm_ratio_optimizer()),
                timing_dag_optimizer=str(tdm_timing_dag_optimizer()),
                slot_optimizer=str(tdm_slot_optimizer()),
                frame_slots=32,
                optimize_frame_slots=True,
                cross_stage_iterations=1,
                cross_stage_feedback_optimizer=str(
                    tdm_partition_feedback()
                ),
                partition_seed_attempts=2,
                partition_repair_min_used_fpgas=True,
                partition_repair_balance=True,
                equivalence_cycles=2,
            )
            self.assertEqual(report["timing"]["status"], "pass")
            self.assertEqual(
                report["board_link_timing"]["routing_projection"][
                    "maximum_route_link_delay_ns"
                ],
                12.0,
            )
            routes = read_json(output / "system-route/routes.json")
            self.assertEqual(
                routes["constraints"]["directed_link_delay_ns"]
                [platform.links[0].id][link_timing["links"][0]["from"]]
                [link_timing["links"][0]["to"]],
                12.0,
            )
            schedule = read_json(output / "tdm/schedule.json")
            ratio_plan = read_json(output / "tdm/ratio_plan.json")
            delay_by_arc = {
                (item["link"], item["from"], item["to"]): item[
                    "delay_bound_ns"
                ]
                for item in link_timing["links"]
            }
            for hop in ratio_plan["hops"]:
                self.assertEqual(
                    hop["base_delay_ns"],
                    delay_by_arc[(hop["link"], hop["from"], hop["to"])],
                )
            reconstructed = reconstruct_tdm_schedule_timing_paths(
                routes, platform, schedule
            )
            self.assertTrue(reconstructed)
            for path in reconstructed:
                for hop in path["scheduled_hops"]:
                    self.assertEqual(
                        hop["base_link_delay_ns"],
                        delay_by_arc[(hop["link"], hop["from"], hop["to"])],
                    )
            self.assertFalse(
                report["timing"]["partition_weights_applied"]
            )
            self.assertFalse(
                report["timing"]["partition_provider_weights_applied"]
            )
            self.assertFalse(
                report["timing"]["hop_refinement_weights_applied"]
            )
            self.assertEqual(
                report["timing"]["partition_weight_consumers"],
                [],
            )
            self.assertGreater(
                report["timing"]["cut_path_projection"][
                    "projected_paths"
                ],
                0,
            )
            self.assertEqual(
                Path(
                    report["timing"]["cut_path_projection"]["output"]
                ).read_text(encoding="utf-8"),
                (output / "timing/cut-timing-paths.json").read_text(
                    encoding="utf-8"
                ),
            )
            projected = read_json(output / "timing/cut-timing-paths.json")
            self.assertEqual(
                projected["source"]["input_sha256"],
                hashlib.sha256(
                    (output / "timing/path-database.json").read_bytes()
                ).hexdigest(),
            )
            self.assertIn(
                "timing_validation", report["stages"]["tdm"]
            )
            self.assertLess(
                report["frame_search"]["selected_frame_slots"], 32
            )
            self.assertEqual(
                report["summary"]["frame_slots"],
                report["frame_search"]["selected_frame_slots"],
            )
            selected_iteration = report["cross_stage"][
                "selected_iteration"
            ]
            selected = report["cross_stage"]["candidates"][
                selected_iteration
            ]
            self.assertEqual(
                report["summary"]["cross_stage_iteration"],
                selected_iteration,
            )
            self.assertEqual(
                selected["phase3_validation"],
                report["stages"]["partition"]["validation"],
            )
            self.assertEqual(
                selected["phase4_validation"],
                report["stages"]["system_route"]["validation"],
            )
            self.assertEqual(
                selected["phase5_validation"],
                report["stages"]["tdm"]["validation"],
            )
            self.assertEqual(
                report["runtime"]["validation"]["status"], "pass"
            )
            self.assertEqual(
                report["cross_stage"]["configuration"][
                    "partition_seed_attempts"
                ],
                2,
            )
            self.assertTrue(
                report["cross_stage"]["configuration"][
                    "partition_repair_min_used_fpgas"
                ]
            )
            self.assertTrue(
                report["cross_stage"]["configuration"][
                    "partition_repair_balance"
                ]
            )
            broken_cross_stage = copy.deepcopy(report)
            broken_cross_stage["cross_stage"]["selected_candidate_id"] = (
                "tampered"
            )
            with self.assertRaisesRegex(
                ValidationError, "selected cross-stage candidate"
            ):
                validate_multi_fpga_flow_report(broken_cross_stage)
            broken = copy.deepcopy(report)
            broken["frame_search"]["selected_frame_slots"] = 31
            with self.assertRaisesRegex(
                ValidationError, "selected frame-search candidate"
            ):
                validate_multi_fpga_flow_report(broken)
            for relative in (
                "timing/path-database.json",
                "timing/partition-net-weights.json",
                "timing/cut-timing-paths.json",
                "timing/board-link-timing.json",
                "timing/board-link-route-constraints.json",
                "frame-search/frame-search-report.json",
                "cross-stage/cross_stage_report.json",
                "runtime/runtime_contract.json",
            ):
                self.assertTrue((output / relative).is_file(), relative)

            direct_output = root / "multi-direct"
            direct_report = run_multi_fpga_flow(
                platform_path=PLATFORM,
                output_dir=direct_output,
                yosys_json=ROOT / "examples/yosys/counter.json",
                top="counter",
                clocks=["clk"],
                partition_provider="greedy",
                cut_mode="sequential-only",
                timing_driven=True,
                board_link_timing_db=link_timing_path,
                clock_periods={"clk": 10.0},
                opensta=str(fake_sta),
                router=str(tlr_router()),
                ratio_optimizer=str(tdm_ratio_optimizer()),
                timing_dag_optimizer=str(tdm_timing_dag_optimizer()),
                slot_optimizer=str(tdm_slot_optimizer()),
                cross_stage_iterations=0,
                equivalence_cycles=2,
            )
            direct_projection = direct_report["timing"][
                "cut_path_projection"
            ]
            self.assertEqual(
                Path(direct_projection["output"]).read_text(
                    encoding="utf-8"
                ),
                (direct_output / "timing/cut-timing-paths.json").read_text(
                    encoding="utf-8"
                ),
            )
            direct_projected = read_json(
                direct_output / "timing/cut-timing-paths.json"
            )
            self.assertEqual(
                direct_projected["source"]["input_sha256"],
                hashlib.sha256(
                    (
                        direct_output / "timing/path-database.json"
                    ).read_bytes()
                ).hexdigest(),
            )

    def test_nonempty_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            (output / "keep").write_text("user data", encoding="utf-8")
            with self.assertRaisesRegex(EmuFlowError, "empty directory"):
                run_multi_fpga_flow(
                    platform_path=PLATFORM,
                    output_dir=output,
                    yosys_json=ROOT / "examples/yosys/counter.json",
                    top="counter",
                    timing_driven=False,
                    clock_periods={"clk": 10.0},
                    opensta=str(FAKE_OPENSTA),
                )

    def test_compile_can_continue_into_checked_serial_bsp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "multi"
            platform_name = Platform.load(PLATFORM).name
            bsp_report = {
                "schema": "emuflow.multi-fpga-bsp-flow/v1",
                "status": "pass",
                "design": "counter",
                "platform": platform_name,
                "qualification": "source_bound_bsp_structure_validation",
                "hardware_release_status": "blocked_on_board_proof",
                "source_flow_report_sha256": "0" * 64,
                "stages": {
                    "phase6b": {
                        "status": "pass",
                        "design": "counter",
                        "platform": platform_name,
                    },
                    "runtime_sync": {"status": "pass"},
                    "phase6c": {
                        "status": "pass",
                        "design": "counter",
                        "platform": platform_name,
                    },
                    "phy_elaboration": {
                        "status": "pass",
                        "design": "counter",
                        "platform": platform_name,
                        "tool": {"name": "yosys"},
                    },
                },
                "validation": {
                    "fpgas": 2,
                    "elaboration_failures": 0,
                    "hardware_release_authorized": False,
                    "gt_site_map_status": "not-provided",
                },
                "artifacts": {},
            }

            def fake_bsp(**kwargs):
                destination = kwargs["output_dir"]
                destination.mkdir(parents=True)
                source_report = (
                    kwargs["flow_root"]
                    / "board-independent-flow-report.json"
                )
                bsp_report["source_flow_report_sha256"] = hashlib.sha256(
                    source_report.read_bytes()
                ).hexdigest()
                write_json(
                    destination / "multi-fpga-bsp-flow-report.json",
                    bsp_report,
                )
                return bsp_report

            with patch(
                "emuflow.multi_fpga_flow.run_multi_fpga_bsp_flow",
                side_effect=fake_bsp,
            ):
                report = run_multi_fpga_flow(
                    platform_path=PLATFORM,
                    output_dir=output,
                    yosys_json=ROOT / "examples/yosys/counter.json",
                    top="counter",
                    clocks=["clk"],
                    partition_provider="greedy",
                    cut_mode="sequential-only",
                    timing_driven=False,
                    clock_periods={"clk": 10.0},
                    opensta=str(FAKE_OPENSTA),
                    router=str(tlr_router()),
                    frame_slots=32,
                    equivalence_cycles=2,
                    serial_bsp_phy_provider=Path("provider.json"),
                    serial_bsp_runtime_sync_provider=Path("runtime.json"),
                    serial_bsp_yosys=Path("yosys"),
                )
            self.assertEqual(report["summary"]["hardware_bsp_status"], "pass")
            self.assertTrue(
                (output / "board-independent-flow-report.json").is_file()
            )
            self.assertEqual(report["hardware_bsp"], bsp_report)


if __name__ == "__main__":
    unittest.main()
