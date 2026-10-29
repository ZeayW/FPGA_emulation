from tests.compile_adapter_fixture import preflight as run_preflight
from contextlib import contextmanager
from tests.partition_fixture import run_multi_fpga_flow_fixture
from tests.xilinx_timing_fixture import write_mock_xilinx_timing_contract
import copy
import hashlib
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from emuflow.platform.board_link_timing import build_board_link_timing_model
from emuflow.flow.cli import _build_parser, _dispatch
from emuflow.common.errors import EmuFlowError, ValidationError
from emuflow.common.io import read_json, write_json
from emuflow.flow.multi_fpga_flow import (
    _resolve_partition_openroad,
    _schedule_has_entries,
    _validate_mapping_timing_contract,
    finalize_multi_fpga_physical_checkpoint,
    run_multi_fpga_flow,
    validate_multi_fpga_flow_bundle,
    validate_multi_fpga_flow_report,
)
from emuflow.scheduling.tdm_schedule_storage import TypedScheduleEntrySequence
from emuflow.transport.transport_cost import (
    BASE_FEATURE_NAMES,
    FRAME_SLOTS,
    OPEN_TRANSPORT_MODEL,
    OPEN_TRANSPORT_PROVENANCE,
)
from emuflow.platform.platform import Platform
from emuflow.scheduling.tdm import TDM_EXACT_DEPENDENCY_PROVIDER
from tests.tdm_timing_oracle import timing_paths as reconstruct_tdm_schedule_timing_paths
from emuflow.scheduling.timing_routing import (
    GLOBAL_CANDIDATE_PROVIDER,
)
from tests.native_build import tlr_router


ROOT = Path(__file__).resolve().parents[1]
PLATFORM = (
    ROOT / "platforms/virtual/academic_vtr_2fpga_p2p.json"
)
STATIC_EXACT_PLATFORM = (
    ROOT / "platforms/virtual/static_exact_acceptance_2fpga.json"
)
FAKE_OPENSTA = ROOT / "tests/fixtures/fake_opensta_paths.py"


@contextmanager
def _catalog_source_fixture():
    """Only path resolution is tested here; the compiler is always mocked."""
    with tempfile.TemporaryDirectory(prefix="catalog-path-fixture-") as temporary:
        root = Path(temporary)
        (root / "picorv32.v").write_text("// Directory-resolution fixture; no synthesis.\n")
        yield root


class MultiFpgaFlowTest(unittest.TestCase):
    def test_phase6_dispatch_does_not_iterate_typed_schedule_entries(self):
        entries = object.__new__(TypedScheduleEntrySequence)
        entries._count = 3
        with (
            patch(
                "emuflow.flow.multi_fpga_flow.read_tdm_schedule",
                return_value={"entries": entries},
            ),
            patch.object(
                TypedScheduleEntrySequence,
                "__iter__",
                side_effect=AssertionError("entries were materialized"),
            ),
        ):
            self.assertTrue(_schedule_has_entries(Path("schedule.json")))

    def test_native_frontend_contract_is_checked_before_yosys_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            timing = root / "timing.json"
            write_json(timing, {"schema": "emuflow.xilinx-preplacement-timing-db/v1"})
            def require(schema):
                if schema == "yosys-emuir-import-request-v1":
                    raise EmuFlowError("missing native frontend contract")
            with (
                patch("emuflow.flow.multi_fpga_flow.resolve_native_executable", return_value=str(FAKE_OPENSTA)),
                patch("emuflow.flow.multi_fpga_flow.check_opensta_engine_version"),
                patch("emuflow.common.native_tools.require_native_schema", side_effect=require),
                patch("emuflow.frontend.frontend_native.prepare_frontend") as mapper,
            ):
                with self.assertRaisesRegex(EmuFlowError, "missing native frontend contract"):
                    run_preflight(
                        platform_path=PLATFORM, output_dir=root / "flow",
                        sources=[ROOT / "examples/rtl/counter.v"], top="counter",
                        clocks=["clk"], clock_periods={"clk": 10},
                        architecture_timing_db=timing,
                        mapping_profile="xilinx-ultrascaleplus-open-v1",
                        cut_mode="static-exact-combinational", partition_provider="tritonpart",
                    )
                mapper.assert_not_called()

    def test_multiple_static_exact_clocks_reach_streaming_frontend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            timing = root / "timing.json"
            write_json(
                timing,
                {"schema": "emuflow.xilinx-preplacement-timing-db/v1"},
            )
            with (
                patch(
                    "emuflow.flow.multi_fpga_flow.resolve_native_executable",
                    return_value=str(FAKE_OPENSTA),
                ),
                patch("emuflow.flow.multi_fpga_flow.check_opensta_engine_version"),
                patch(
                    "emuflow.frontend.frontend_native.prepare_frontend",
                    side_effect=RuntimeError("reached multi-clock mapper"),
                ) as mapper,
            ):
                with self.assertRaisesRegex(RuntimeError, "reached multi-clock mapper"):
                    run_preflight(
                        platform_path=PLATFORM, output_dir=root / "flow",
                        sources=[ROOT / "examples/rtl/counter.v"], top="counter",
                        clocks=["dla_core_clk", "dla_csb_clk"],
                        clock_periods={"dla_core_clk": 10, "dla_csb_clk": 20},
                        architecture_timing_db=timing,
                        mapping_profile="xilinx-ultrascaleplus-open-v1",
                        cut_mode="static-exact-combinational", partition_provider="tritonpart",
                    )
                mapper.assert_called_once()

    def test_native_xilinx_mapper_selection_reaches_streaming_frontend(self):
        for strategy in (None, "hierarchical-classic-v1", "hierarchical-abc9-v1"):
            with self.subTest(strategy=strategy), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                timing = root / "timing.json"
                write_json(timing, {"schema": "emuflow.xilinx-preplacement-timing-db/v1"})
                with (
                    patch("emuflow.flow.multi_fpga_flow.resolve_native_executable", return_value=str(FAKE_OPENSTA)),
                    patch("emuflow.flow.multi_fpga_flow.check_opensta_engine_version"),
                    patch("emuflow.frontend.frontend_native.prepare_frontend",
                          side_effect=RuntimeError("reached mapper")) as frontend,
                ):
                    with self.assertRaisesRegex(RuntimeError, "reached mapper"):
                        run_preflight(
                            cut_mode="static-exact-combinational",
                            platform_path=PLATFORM, output_dir=root / "flow",
                            sources=[ROOT / "examples/rtl/counter.v"], top="counter",
                            mapping_profile="xilinx-ultrascaleplus-open-v1",
                            xilinx_mapping_strategy=strategy,
                            architecture_timing_db=timing, clock_periods={"clk": 10},
                            partition_provider="tritonpart",
                        )
                    self.assertEqual(frontend.call_args.args[0].xilinx_mapping_strategy or "hierarchical-abc9-v1",
                                     strategy or "hierarchical-abc9-v1")

    def test_mapper_override_rejects_invalid_context_before_frontend(self):
        for profile, supplied, strategy, message in (
            ("vtr-hard-blocks", None, "hierarchical-classic-v1", "unsupported multi-FPGA mapping profile"),
            ("xilinx-ultrascaleplus-open-v1", ROOT / "examples/yosys/counter.json",
             "hierarchical-abc9-v1", "requires native Xilinx"),
            ("xilinx-ultrascaleplus-open-v1", None, "unknown", "unsupported Xilinx"),
        ):
            with self.subTest(profile=profile, supplied=supplied, strategy=strategy):
                with tempfile.TemporaryDirectory() as tmp:
                    out = Path(tmp) / "flow"
                    with self.assertRaisesRegex(EmuFlowError, message):
                        run_preflight(
                            platform_path=PLATFORM, output_dir=out,
                            mapping_profile=profile, yosys_json=supplied,
                            xilinx_mapping_strategy=strategy,
                        )
                    self.assertFalse(out.exists())

    def test_checked_benchmark_can_select_mapper_without_overriding_rtl(self):
        with _catalog_source_fixture() as source_root:
            for strategy in ("hierarchical-classic-v1", "hierarchical-abc9-v1"):
                args = _build_parser().parse_args([
                    "multi-fpga", "compile", "--benchmark-run",
                    str(ROOT / "benchmarks/runs/picorv32_l2.json"), "--source-root",
                    str(source_root), "--platform", "platform.json",
                    "--xilinx-mapping-strategy", strategy, "--out", "unused",
                ])
                with patch("emuflow.flow.cli.run_multi_fpga_flow") as run, patch("emuflow.flow.cli._print_json"):
                    run.return_value = {"status": "pass"}
                    self.assertEqual(_dispatch(args), 0)
                    self.assertEqual(run.call_args.kwargs["xilinx_mapping_strategy"], strategy)
                    self.assertEqual(run.call_args.kwargs["top"], "picorv32")
                    self.assertEqual(run.call_args.kwargs["clock_periods"], {"clk": 10.0})

    def test_partition_engine_is_resolved_before_flow_execution(self) -> None:
        with patch(
            "emuflow.flow.multi_fpga_flow.resolve_native_executable",
            return_value="/sealed/install/bin/openroad",
        ) as resolver:
            self.assertEqual(
                _resolve_partition_openroad("patron", None),
                "/sealed/install/bin/openroad",
            )
            self.assertEqual(
                _resolve_partition_openroad("tritonpart", "/explicit/openroad"),
                "/sealed/install/bin/openroad",
            )
            self.assertIsNone(_resolve_partition_openroad("tritonpart", None, Path("initial.part")))
        self.assertEqual(
            resolver.call_args_list,
            [
                call("openroad", None),
                call("openroad", "/explicit/openroad"),
            ],
        )

    def test_mapping_profile_rejects_mismatched_timing_provider_early(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            vtr_timing = root / "vtr-timing.json"
            xilinx_timing = root / "xilinx-timing.json"
            write_json(
                vtr_timing,
                {"schema": "emuflow.architecture-timing-db/v1"},
            )
            write_json(
                xilinx_timing,
                {"schema": "emuflow.xilinx-preplacement-timing-db/v1"},
            )

            with self.assertRaisesRegex(EmuFlowError, "unsupported multi-FPGA mapping profile"):
                _validate_mapping_timing_contract("vtr-hard-blocks", vtr_timing)
            _validate_mapping_timing_contract(
                "xilinx-ultrascaleplus-open-v1", xilinx_timing
            )
            with self.assertRaisesRegex(
                EmuFlowError, "requires Architecture TimingDB schema"
            ):
                _validate_mapping_timing_contract(
                    "xilinx-ultrascaleplus-open-v1", vtr_timing
                )
            with self.assertRaisesRegex(
                EmuFlowError, "requires a source-sealed Xilinx"
            ):
                _validate_mapping_timing_contract(
                    "xilinx-ultrascaleplus-open-v1", None
                )

    def test_rapidwright_backend_rejects_vtr_mapping_before_frontend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "flow"
            with self.assertRaisesRegex(
                EmuFlowError,
                "unsupported multi-FPGA mapping profile",
            ):
                run_preflight(
                    platform_path=PLATFORM,
                    output_dir=output,
                    yosys_json=ROOT / "examples/yosys/counter.json",
                    mapping_profile="vtr-hard-blocks",
                    physical=True,
                    physical_backend="rapidwright",
                )
            self.assertFalse(output.exists())


    def test_static_exact_acceptance_fixture_is_not_vacuous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(
                EmuFlowError, "cannot populate|required FPGA|capacity"
            ):
                run_multi_fpga_flow_fixture(
                    platform_path=STATIC_EXACT_PLATFORM,
                    output_dir=Path(temporary_directory) / "sequential",
                    yosys_json=(
                        ROOT / "examples/yosys/static_exact_chain.json"
                    ),
                    top="static_exact_chain",
                    clocks=["clk"],
                    partition_provider="tritonpart",
                    cut_mode="static-exact-combinational",
                    timing_driven=False,
                    clock_periods={"clk": 10.0},
                    opensta=str(FAKE_OPENSTA),
                    frame_slots=32,
                    equivalence_cycles=2,
                    mapping_profile="xilinx-ultrascaleplus-open-v1",
                )

    def test_one_command_static_exact_flow_reaches_phase7c_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "static-exact"
            report = run_multi_fpga_flow_fixture(
                platform_path=STATIC_EXACT_PLATFORM,
                output_dir=root,
                yosys_json=ROOT / "examples/yosys/static_exact_chain.json",
                top="static_exact_chain",
                clocks=["clk"],
                partition_provider="tritonpart",
                # This transport fixture has one indivisible FF across two parts.
                balance_tolerance=1.0,
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
                mapping_profile="xilinx-ultrascaleplus-open-v1",
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
                TDM_EXACT_DEPENDENCY_PROVIDER,
            )
            self.assertEqual(
                read_json(root / "tdm/schedule.json")[
                    "transport_semantics"
                ],
                "sampled-virtual-wire",
            )
            schedule_manifest = json.loads(
                (root / "tdm/schedule.json").read_text(encoding="utf-8")
            )
            self.assertRegex(
                schedule_manifest["artifact"],
                r"^schedule\.[0-9a-f]{64}\.sqlite$",
            )
            self.assertEqual(
                report["artifacts"]["schedule_store"]["path"],
                (Path("tdm") / schedule_manifest["artifact"]).as_posix(),
            )
            route_manifest = json.loads(
                (root / "system-route/routes.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertRegex(
                route_manifest["artifact"],
                r"^routes\.[0-9a-f]{64}\.sqlite$",
            )
            self.assertEqual(
                report["artifacts"]["route_store"]["path"],
                (
                    Path("system-route") / route_manifest["artifact"]
                ).as_posix(),
            )
            self.assertEqual(
                report["stages"]["split"]["equivalence"]["qualification"],
                "exhaustive-small-model-proof-plus-random-traces",
            )
            self.assertEqual(report["runtime"]["status"], "generated")
            self.assertNotIn("system_timing", report["runtime"])
            self.assertEqual(
                report["runtime"]["system_timing_ref"],
                {"artifact": "qor_report", "json_pointer": "/timing"},
            )
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
        defaults = _build_parser().parse_args(base)
        self.assertTrue(defaults.timing_driven)
        self.assertFalse(hasattr(defaults, "ratio_quantum"))
        self.assertFalse(hasattr(defaults, "physical_rapidwright_placer"))
        with self.assertRaises(SystemExit):
            _build_parser().parse_args(
                [*base, "--physical-rapidwright-placer", "legacy"]
            )
        self.assertFalse(
            _build_parser().parse_args(
                [*base, "--no-timing-driven"]
            ).timing_driven
        )
        resumed = _build_parser().parse_args(
            [*base, "--tritonpart-solution", "saved.part.4"]
        )
        self.assertEqual(
            resumed.tritonpart_solution, Path("saved.part.4")
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
        self.assertEqual(phase3.cut_mode, "static-exact-combinational")
        with self.assertRaises(SystemExit):
            _build_parser().parse_args(["cross-stage", "optimize"])

    def test_cli_has_one_tdm_provider_and_rejects_retired_options(self):
        parser = _build_parser()
        base = ["multi-fpga", "compile", "--out", "unused"]
        with patch("emuflow.flow.cli.run_multi_fpga_flow", return_value={"status": "pass"}) as run, patch("emuflow.flow.cli._print_json"):
            self.assertEqual(_dispatch(parser.parse_args(base)), 0)
            self.assertEqual(run.call_args.kwargs["partition_provider"], "patron")
            self.assertEqual(run.call_args.kwargs["patron_algorithm_version"], 14)
            self.assertEqual(run.call_args.kwargs["cut_mode"], "static-exact-combinational")
            self.assertEqual(run.call_args.kwargs["max_cross_fpga_dependency_depth"], 0)
            self.assertNotIn("slot_refinement_iterations", run.call_args.kwargs)
            self.assertNotIn("ratio_optimizer", run.call_args.kwargs)
        for option in ("--slot-refinement-iterations", "--ratio-optimizer", "--timing-dag-optimizer", "--ratio-quantum"):
            with self.subTest(option=option), self.assertRaises(SystemExit):
                parser.parse_args([*base, option, "7"])
        with self.assertRaises(SystemExit):
            parser.parse_args([*base, "--tdm-provider", "native-timing-dag-ratio-v1"])

    def test_cli_checked_benchmark_owns_native_frontend_contract(self):
        with _catalog_source_fixture() as source_root:
            root = Path(__file__).resolve().parents[1]
            benchmark = root / "benchmarks/runs/picorv32_l2.json"
            args = _build_parser().parse_args(
                [
                    "multi-fpga",
                    "compile",
                    "--benchmark-run",
                    str(benchmark),
                    "--source-root",
                    str(source_root),
                    "--platform",
                    "platform.json",
                    "--out",
                    "unused",
                ]
            )
            with (
                patch("emuflow.flow.cli.run_multi_fpga_flow") as run,
                patch("emuflow.flow.cli._print_json"),
            ):
                run.return_value = {"status": "pass"}
                self.assertEqual(_dispatch(args), 0)
            kwargs = run.call_args.kwargs
            self.assertEqual(kwargs["top"], "picorv32")
            self.assertEqual(kwargs["clocks"], ["clk"])
            self.assertEqual(kwargs["clock_periods"], {"clk": 10.0})
            self.assertEqual(
                kwargs["mapping_profile"], "xilinx-ultrascaleplus-open-v1"
            )
            self.assertEqual(
                kwargs["timing_io"],
                json.loads(benchmark.read_text(encoding="utf-8"))["timing_io"],
            )
            self.assertEqual([path.name for path in kwargs["sources"]], ["picorv32.v"])

    def test_cli_checked_benchmark_rejects_manual_frontend_override(self):
        root = Path(__file__).resolve().parents[1]
        args = _build_parser().parse_args(
            [
                "multi-fpga",
                "compile",
                "rtl/other.v",
                "--benchmark-run",
                str(root / "benchmarks/runs/picorv32_l2.json"),
                "--source-root",
                str(root / "third_party/rtl/picorv32"),
                "--platform",
                "platform.json",
                "--out",
                "unused",
            ]
        )
        with self.assertRaisesRegex(EmuFlowError, "owns its frontend contract"):
            _dispatch(args)

    def test_exact_mode_requires_clock_periods(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(
                EmuFlowError, "requires at least one --clock-period"
            ):
                run_preflight(
                    platform_path=PLATFORM,
                    output_dir=Path(temporary_directory) / "exact",
                    yosys_json=ROOT / "examples/yosys/counter.json",
                    top="counter",
                    clocks=["clk"],
                    cut_mode="static-exact-combinational",

                    mapping_profile="xilinx-ultrascaleplus-open-v1",
                    architecture_timing_db=write_mock_xilinx_timing_contract(Path(temporary_directory) / "mock-timing.json"),
                )

    def test_compile_rejects_retired_cut_mode_before_frontend(self):
        with patch("emuflow.frontend.frontend_native.prepare_frontend", side_effect=AssertionError("frontend started")):
            with self.assertRaisesRegex(EmuFlowError, "requires static-exact-combinational"):
                run_preflight(output_dir=Path("unused"), cut_mode="sequential-only")



    def test_rapidwright_runtime_preflight_precedes_frontend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "multi"
            with patch(
                "emuflow.flow.multi_fpga_flow.validate_openparf_runtime",
                side_effect=EmuFlowError("missing PyTorch runtime"),
            ):
                with self.assertRaisesRegex(
                    EmuFlowError, "missing PyTorch runtime"
                ):
                    run_preflight(
                        platform_path=PLATFORM,
                        output_dir=root,
                        yosys_json=ROOT / "examples/yosys/counter.json",
                        top="counter",
                        clocks=["clk"],
                        partition_provider="tritonpart",
                        cut_mode="static-exact-combinational",
                        timing_driven=False,
                        clock_periods={"clk": 10.0},
                        opensta=str(FAKE_OPENSTA),
                        router=str(tlr_router()),
                        frame_slots=32,
                        physical=True,
                        mapping_profile="xilinx-ultrascaleplus-open-v1",
                        physical_backend="rapidwright",
                    )
            self.assertFalse(root.exists())

    def test_rapidwright_native_runtime_preflight_precedes_frontend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            root = temporary / "multi"
            timing = temporary / "xilinx-timing.json"
            write_json(
                timing,
                {"schema": "emuflow.xilinx-preplacement-timing-db/v1"},
            )
            with patch(
                "emuflow.flow.multi_fpga_flow.validate_openparf_runtime",
                side_effect=EmuFlowError("missing PyTorch runtime"),
            ) as preflight:
                with self.assertRaisesRegex(
                    EmuFlowError, "missing PyTorch runtime"
                ):
                    run_preflight(
                        platform_path=PLATFORM,
                        output_dir=root,
                        yosys_json=ROOT / "examples/yosys/counter.json",
                        top="counter",
                        clocks=["clk"],
                        partition_provider="tritonpart",
                        cut_mode="static-exact-combinational",
                        timing_driven=False,
                        clock_periods={"clk": 10.0},
                        architecture_timing_db=timing,
                        opensta=str(FAKE_OPENSTA),
                        router=str(tlr_router()),
                        frame_slots=32,
                        physical=True,
                        mapping_profile="xilinx-ultrascaleplus-open-v1",
                        physical_backend="rapidwright",
                    )
            preflight.assert_called_once_with(
                install_root=None,
                python_executable=None,
            )
            self.assertFalse(root.exists())

    def test_board_link_preflight_rejects_mismatches_before_frontend(self) -> None:
        platform = Platform.load(PLATFORM)
        for mismatch in ("platform", "coverage", "clock"):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                database = build_board_link_timing_model(platform)
                if mismatch == "platform":
                    database["platform"] += "-stress"
                elif mismatch == "coverage":
                    database["links"].pop()
                else:
                    database["links"][0]["fabric_clock_mhz"] += 1.0
                path = root / "board-link-timing.json"
                write_json(path, database)
                output = root / "flow"
                with patch("emuflow.frontend.frontend_native.prepare_frontend") as synthesis, patch(
                    "emuflow.flow.multi_fpga_flow.check_opensta_engine_version"
                ) as engine_check:
                    with self.assertRaisesRegex(ValidationError, "board link timing"):
                        run_preflight(
                            platform_path=PLATFORM,
                            output_dir=output,
                            sources=[ROOT / "examples/rtl/counter.v"],
                            top="counter",
                            mapping_profile="xilinx-ultrascaleplus-open-v1",
                            partition_provider="tritonpart",
                            cut_mode="static-exact-combinational",
                            timing_driven=False,
                            clock_periods={"clk": 10.0},
                            board_link_timing_db=path,
                        )
                synthesis.assert_not_called()
                engine_check.assert_not_called()
                self.assertFalse(output.exists())

    def test_transport_cost_preflight_rejects_wrong_platform_before_frontend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            features = list(BASE_FEATURE_NAMES) + [
                f"frame_slots_{slots}" for slots in FRAME_SLOTS[1:]
            ]
            path = root / "transport-cost.json"
            write_json(path, {
                "schema": "emuflow.transport-cost/v2",
                "platform": Platform.load(PLATFORM).name + "-stress",
                "profile": "nominal",
                "model": OPEN_TRANSPORT_MODEL,
                "feature_names": features,
                "resources": {"lut": {name: 1.0 for name in features}},
                "provenance": {"class": OPEN_TRANSPORT_PROVENANCE, "fit_sha256": "1" * 64},
            })
            output = root / "flow"
            with patch("emuflow.frontend.frontend_native.prepare_frontend") as synthesis, patch(
                "emuflow.flow.multi_fpga_flow.check_opensta_engine_version"
            ) as engine_check:
                with self.assertRaisesRegex(ValidationError, "transport cost platform disagrees"):
                    run_preflight(
                        platform_path=PLATFORM,
                        output_dir=output,
                        sources=[ROOT / "examples/rtl/counter.v"],
                        top="counter",
                        mapping_profile="xilinx-ultrascaleplus-open-v1",
                        partition_provider="tritonpart",
                        cut_mode="static-exact-combinational",
                        timing_driven=False,
                        clock_periods={"clk": 10.0},
                        transport_cost_db=path,
                    )
            synthesis.assert_not_called()
            engine_check.assert_not_called()
            self.assertFalse(output.exists())

    def test_opensta_version_preflight_precedes_frontend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "multi"
            legacy_sta = Path(temporary_directory) / "sta"
            legacy_sta.write_text("#!/bin/sh\necho 2.6.0\n", encoding="utf-8")
            legacy_sta.chmod(legacy_sta.stat().st_mode | stat.S_IXUSR)
            with self.assertRaisesRegex(
                ValidationError, "requires 3.1.0 or newer"
            ):
                run_preflight(
                    platform_path=PLATFORM, mapping_profile="xilinx-ultrascaleplus-open-v1",
                    output_dir=root,
                    yosys_json=ROOT / "examples/yosys/counter.json",
                    top="counter",
                    clocks=["clk"],
                    partition_provider="tritonpart",
                    cut_mode="static-exact-combinational",
                    timing_driven=False,
                    clock_periods={"clk": 10.0},
                    opensta=str(legacy_sta),
                    router=str(tlr_router()),
                    frame_slots=32,
                )
            self.assertFalse(root.exists())

    def test_finalizes_checked_independent_physical_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "multi"
            report = run_multi_fpga_flow_fixture(
                platform_path=PLATFORM,
                output_dir=root,
                yosys_json=ROOT / "examples/yosys/counter.json",
                top="counter",
                clocks=["clk"],
                partition_provider="tritonpart",
                cut_mode="static-exact-combinational",
                timing_driven=False,
                clock_periods={"clk": 10.0},
                opensta=str(FAKE_OPENSTA),
                router=str(tlr_router()),
                frame_slots=32,
                equivalence_cycles=2,
                mapping_profile="xilinx-ultrascaleplus-open-v1",
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
                runtime = args[3]
                runtime.mkdir(parents=True)
                write_json(runtime / "runtime_contract.json", {})
                write_json(runtime / "qor_report.json", {})
                return {"status": "pass"}

            with (
                patch(
                    "emuflow.flow.multi_fpga_flow.validate_multi_fpga_physical_report",
                    return_value={"status": "pass"},
                ),
                patch(
                    "emuflow.flow.multi_fpga_flow.validate_multi_fpga_flow_report",
                    return_value={"status": "pass"},
                ),
                patch(
                    "emuflow.flow.multi_fpga_flow.run_native_physical_signoff",
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
            report = run_multi_fpga_flow_fixture(
                platform_path=PLATFORM,
                output_dir=output,
                yosys_json=ROOT / "examples/yosys/counter.json",
                top="counter",
                clocks=["clk"],
                partition_provider="tritonpart",
                cut_mode="static-exact-combinational",
                timing_driven=False,
                router=str(tlr_router()),
                route_provider=GLOBAL_CANDIDATE_PROVIDER,
                route_candidate_workers=2,
                clock_periods={"clk": 20.0},
                opensta=str(FAKE_OPENSTA),
                frame_slots=32,
                tdm_provider=TDM_EXACT_DEPENDENCY_PROVIDER,
                equivalence_cycles=2,
                mapping_profile="xilinx-ultrascaleplus-open-v1",
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
            transport_cost_path = root / "transport-cost.json"
            platform = Platform.load(PLATFORM)
            write_json(
                link_timing_path,
                build_board_link_timing_model(platform),
            )
            feature_names = list(BASE_FEATURE_NAMES) + [
                f"frame_slots_{slots}" for slots in FRAME_SLOTS[1:]
            ]
            write_json(
                transport_cost_path,
                {
                    "schema": "emuflow.transport-cost/v2",
                    "platform": platform.name,
                    "profile": "nominal",
                    "model": OPEN_TRANSPORT_MODEL,
                    "feature_names": feature_names,
                    "resources": {
                        "lut": {name: 1.0 for name in feature_names},
                        "ff": {name: 2.0 for name in feature_names},
                    },
                    "provenance": {
                        "class": OPEN_TRANSPORT_PROVENANCE,
                        "fit_sha256": "1" * 64,
                    },
                },
            )
            report = run_multi_fpga_flow_fixture(
                platform_path=PLATFORM,
                output_dir=output,
                yosys_json=ROOT / "examples/yosys/counter.json",
                top="counter",
                clocks=["clk"],
                partition_provider="tritonpart",
                cut_mode="static-exact-combinational",
                timing_driven=False,
                board_link_timing_db=link_timing_path,
                transport_cost_db=transport_cost_path,
                clock_periods={"clk": 10.0},
                opensta=str(FAKE_OPENSTA),
                router=str(tlr_router()),
                frame_slots=16,
                equivalence_cycles=8,
                mapping_profile="xilinx-ultrascaleplus-open-v1",
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
                GLOBAL_CANDIDATE_PROVIDER,
            )
            self.assertEqual(
                report["stages"]["tdm"]["provider"],
                TDM_EXACT_DEPENDENCY_PROVIDER,
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
            self.assertEqual(report["summary"]["frame_slots"], 16)
            prediction = report["stages"]["split"]["validation"][
                "transport_cost_prediction"
            ]
            self.assertEqual(prediction["status"], "pass")
            self.assertEqual(len(prediction["fpgas"]), 2)
            self.assertIn("transport_cost", report["artifacts"])
            self.assertEqual(
                read_json(output / "split/transport-cost.json"),
                read_json(transport_cost_path),
            )
            self.assertEqual(
                report["stages"]["frontend"]["synthesis"]["mode"],
                "provided-yosys-json",
            )
            self.assertEqual(
                report["stages"]["frontend"]["synthesis"][
                    "interchange_retention"
                ],
                "source-owned-stream; no graph staging copy",
            )
            self.assertFalse((output / "frontend/synthesized.json").exists())
            self.assertFalse((output / "frontend/synthesized.json.gz").exists())
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
                "frontend/phase1/design.emuir.sqlite",
                "partition/assignment.json",
                "system-route/routes.json",
                "tdm/schedule.json",
                "split/manifest.json",
                "split/split-netlists.sqlite",
                "runtime/runtime_contract.json",
                "runtime/qor_report.json",
                "timing/path-database.json",
                "timing/cut-timing-paths.json",
            ):
                self.assertTrue((output / relative).is_file(), relative)

    def test_report_rejects_cross_stage_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            report = run_multi_fpga_flow_fixture(
                platform_path=PLATFORM,
                output_dir=Path(temporary_directory) / "multi",
                yosys_json=ROOT / "examples/yosys/counter.json",
                top="counter",
                clocks=["clk"],
                partition_provider="tritonpart",
                cut_mode="static-exact-combinational",
                timing_driven=False,
                clock_periods={"clk": 10.0},
                opensta=str(FAKE_OPENSTA),
                router=str(tlr_router()),
                frame_slots=32,
                equivalence_cycles=2,
                mapping_profile="xilinx-ultrascaleplus-open-v1",
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
                    mapping_profile="xilinx-ultrascaleplus-open-v1",
                    architecture_timing_db=write_mock_xilinx_timing_contract(Path(temporary_directory) / "mock-timing.json"),
                )

    def test_retired_serial_bsp_options_are_not_a_compile_api(self):
        import inspect
        self.assertFalse(any(name.startswith("serial_bsp_") for name in inspect.signature(run_multi_fpga_flow).parameters))
        with self.assertRaises(TypeError):
            run_multi_fpga_flow(serial_bsp_phy_provider=Path("provider.json"))

    def test_zero_cut_flow_skips_vacuous_interconnect_optimizers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            constraints = root / "constraints.json"
            write_json(
                constraints,
                {
                    "schema": "emuflow.partition-constraints/v1",
                    "fixed": [{"patterns": ["*"], "fpga": "fpga0"}],
                    "min_used_fpgas": 1,
                    "balance_tolerance": 3.0,
                },
            )
            output = root / "flow"
            report = run_multi_fpga_flow_fixture(
                platform_path=PLATFORM, mapping_profile="xilinx-ultrascaleplus-open-v1",
                output_dir=output,
                yosys_json=ROOT / "examples/yosys/counter.json",
                top="counter",
                clocks=["clk"],
                partition_constraints=constraints,
                partition_provider="tritonpart",
                cut_mode="static-exact-combinational",
                timing_driven=True,
                clock_periods={"clk": 10.0},
                opensta=str(FAKE_OPENSTA),
                router=str(tlr_router()),


                frame_slots=32,

                equivalence_cycles=2,
            )

            routes = read_json(output / "system-route/routes.json")
            schedule = read_json(output / "tdm/schedule.json")
            replay = validate_multi_fpga_flow_bundle(output)

            def bounded_control_read(path, *args, **kwargs):
                if Path(path).name in {"routes.json", "schedule.json"}:
                    raise AssertionError(
                        "terminal replay expanded a typed data-plane store"
                    )
                return read_json(path, *args, **kwargs)

            with patch(
                "emuflow.flow.multi_fpga_flow.read_json",
                side_effect=bounded_control_read,
            ):
                typed_replay = validate_multi_fpga_flow_bundle(output)
            self.assertEqual(
                report["stages"]["partition"]["validation"]["cut_nets"],
                0,
            )
            self.assertEqual(routes["provider"], GLOBAL_CANDIDATE_PROVIDER)
            self.assertEqual(routes["routes"], [])
            self.assertEqual(routes["metrics"]["estimated_max_tdm_ratio"], 0)
            self.assertEqual(schedule["provider"], TDM_EXACT_DEPENDENCY_PROVIDER)
            self.assertEqual(schedule["entries"], [])
            self.assertEqual(
                report["timing"]["cut_path_projection"]["mode"],
                "not-required-no-inter-fpga-cuts",
            )
            self.assertFalse(
                (output / "timing/cut-timing-paths.json").exists()
            )
            self.assertNotIn("frame_search", report)
            self.assertNotIn("cross_stage", report)
            self.assertEqual(replay["status"], "pass")
            self.assertEqual(typed_replay, replay)

    def test_timing_driven_pipeline_connects_sta_through_tdm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_sta = root / "sta"
            fake_sta.write_text(
                """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

if sys.argv[1:] == ["-version"]:
    print("3.1.0")
    raise SystemExit(0)

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
            report = run_multi_fpga_flow_fixture(
                platform_path=PLATFORM,
                output_dir=output,
                yosys_json=ROOT / "examples/yosys/counter.json",
                top="counter",
                clocks=["clk"],
                partition_provider="tritonpart",
                cut_mode="static-exact-combinational",
                timing_driven=True,
                board_link_timing_db=link_timing_path,
                clock_periods={"clk": 10.0},
                opensta=str(FAKE_OPENSTA),
                router=str(tlr_router()),


                frame_slots=32,


                partition_seed_attempts=1,
                partition_repair_min_used_fpgas=True,
                equivalence_cycles=2,
                mapping_profile="xilinx-ultrascaleplus-open-v1",
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
            delay_by_arc = {
                (item["link"], item["from"], item["to"]): item[
                    "delay_bound_ns"
                ]
                for item in link_timing["links"]
            }
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
            self.assertTrue(
                report["timing"]["partition_weights_applied"]
            )
            self.assertTrue(
                report["timing"]["partition_provider_weights_applied"]
            )
            self.assertFalse(
                report["timing"]["hop_refinement_weights_applied"]
            )
            self.assertEqual(
                report["timing"]["partition_weight_consumers"],
                ["phase3-provider"],
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
            self.assertEqual(report["summary"]["frame_slots"], 32)
            self.assertNotIn("cross_stage", report)
            self.assertEqual(report["runtime"]["validation"]["status"], "pass")
            for relative in (
                "timing/path-database.json",
                "timing/partition-net-weights.json",
                "timing/cut-timing-paths.json",
                "timing/board-link-timing.json",
                "timing/board-link-route-constraints.json",
                "runtime/runtime_contract.json",
            ):
                self.assertTrue((output / relative).is_file(), relative)

            direct_output = root / "multi-direct"
            direct_report = run_multi_fpga_flow_fixture(
                platform_path=PLATFORM,
                output_dir=direct_output,
                yosys_json=ROOT / "examples/yosys/counter.json",
                top="counter",
                clocks=["clk"],
                partition_provider="tritonpart",
                cut_mode="static-exact-combinational",
                timing_driven=True,
                board_link_timing_db=link_timing_path,
                clock_periods={"clk": 10.0},
                opensta=str(FAKE_OPENSTA),
                router=str(tlr_router()),


                equivalence_cycles=2,
                mapping_profile="xilinx-ultrascaleplus-open-v1",
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


if __name__ == "__main__":
    unittest.main()
