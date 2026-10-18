import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.cli import _build_parser, _dispatch
from emuflow.default_platform import (
    DEFAULT_PLATFORM_PATH,
    materialize_research_platform,
    research_platform_path,
)
from emuflow.errors import ValidationError
from emuflow.io import read_json
from emuflow.multi_fpga_flow import run_multi_fpga_flow
from emuflow.platform import Platform


ROOT = Path(__file__).resolve().parents[1]


class DefaultPlatformTest(unittest.TestCase):
    def test_bundled_topology_and_bank_contract(self):
        expected_pairs = {
            2: {("fpga0", "fpga1")},
            4: {("fpga0", "fpga2"), ("fpga0", "fpga3"),
                ("fpga1", "fpga2"), ("fpga1", "fpga3")},
        }
        for count, pairs in expected_pairs.items():
            with self.subTest(fpga_count=count):
                platform = Platform.load(research_platform_path(count))
                self.assertEqual(platform.virtual_board_model, "xcvu19p-hp-lvds-cable-v1")
                self.assertEqual(len(platform.fpgas), count)
                self.assertEqual({link.endpoints for link in platform.links}, pairs)
                for link in platform.links:
                    self.assertEqual(link.direction, "full_duplex")
                    self.assertEqual(link.capacity_sharing, "shared_bidirectional")
                    self.assertEqual((link.bank_count, link.wires_per_bank), (18, 20))
                    self.assertEqual(link.data_lanes_per_direction, 360)
                    self.assertEqual((link.cable_count, link.hp_banks_per_cable), (6, 3))
                    self.assertEqual(link.fabric_clock_mhz, 250.0)
                    self.assertEqual(link.latency_cycles, 8)
                    self.assertEqual(link.tdm_delay_model.delay_ns(8), 34.0)
                    self.assertEqual(link.tdm_delay_model.delay_ns(16), 38.0)
                for fpga in platform.fpgas:
                    self.assertEqual((fpga.hp_io_bank_count, fpga.hd_io_bank_count), (38, 4))
                    used = sum(link.bank_count for link in platform.links if fpga.id in link.endpoints)
                    self.assertEqual(used, 18 if count == 2 else 36)
                    self.assertLessEqual(used, fpga.hp_io_bank_count)
                normalized = platform.to_dict()
                self.assertEqual(Platform.from_dict(normalized).to_dict(), normalized)

    def test_packaged_presets_match_public_files_and_device_resources(self):
        provider = read_json(
            ROOT / "resources/rapidwright/xcvu19p-fsva3824-2-e.provider.json"
        )
        resources = provider["expected_physical_resources"]
        mapping = {"lut": "CLB_LUT", "ff": "CLB_FF", "bram18k": "RAMB18E2",
                   "dsp48": "DSP48E2", "uram288": "URAM288"}
        for count in (2, 4):
            path = research_platform_path(count)
            self.assertEqual(read_json(path), read_json(ROOT / "platforms/virtual" / path.name))
            for fpga in Platform.load(path).fpgas:
                self.assertEqual(fpga.part, provider["device"]["part"])
                self.assertEqual(fpga.utilization_limit, 0.25 if count == 4 else 0.75)
                for board_unit, provider_unit in mapping.items():
                    self.assertEqual(fpga.capacity[board_unit], resources[provider_unit])

    def test_cli_and_python_default_select_same_installed_resource(self):
        parser = _build_parser()
        args = parser.parse_args([
            "multi-fpga", "compile", "design.v", "--top", "top",
            "--clock", "clk", "--clock-period", "clk=10", "--out", "unused",
        ])
        self.assertEqual(args.platform, DEFAULT_PLATFORM_PATH)
        self.assertEqual(
            inspect.signature(run_multi_fpga_flow).parameters["platform_path"].default,
            DEFAULT_PLATFORM_PATH,
        )
        self.assertTrue(DEFAULT_PLATFORM_PATH.is_file())
        self.assertEqual(DEFAULT_PLATFORM_PATH.parent.parent.name, "data")
        with patch("emuflow.cli.run_multi_fpga_flow") as run, patch("emuflow.cli._print_json"):
            run.return_value = {"status": "pass"}
            self.assertEqual(_dispatch(args), 0)
            self.assertEqual(run.call_args.kwargs["platform_path"], DEFAULT_PLATFORM_PATH)

    def test_python_omitted_platform_reaches_frontend_with_bundled_model(self):
        class FrontendReached(Exception):
            pass

        with tempfile.TemporaryDirectory() as directory:
            with patch("emuflow.multi_fpga_flow.run_phase1", side_effect=FrontendReached) as frontend, patch("emuflow.multi_fpga_flow.check_opensta_engine_version"):
                with self.assertRaises(FrontendReached):
                    run_multi_fpga_flow(
                        output_dir=Path(directory) / "run",
                        yosys_json=ROOT / "examples/yosys/counter.json",
                        top="counter", clocks=["clk"], clock_periods={"clk": 10.0},
                        mapping_profile="generic-soft", partition_provider="greedy",
                        timing_driven=False, opensta="/usr/bin/true",
                    )
                self.assertEqual(frontend.call_args.args[1], DEFAULT_PLATFORM_PATH)

    def test_cli_materializes_both_models_without_source_relative_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            for count in (2, 4):
                output = Path(directory) / f"board-{count}.json"
                args = _build_parser().parse_args([
                    "platform", "research-model", "--fpga-count", str(count),
                    "--output", str(output),
                ])
                with patch("emuflow.cli._print_json") as report:
                    self.assertEqual(_dispatch(args), 0)
                    self.assertEqual(report.call_args.args[0]["qualification"], "model-only")
                self.assertEqual(
                    Platform.load(output).to_dict(),
                    Platform.load(research_platform_path(count)).to_dict(),
                )

    def test_explicit_platform_keeps_its_declared_semantics(self):
        explicit = ROOT / "platforms/virtual/academic_vtr_2fpga_p2p.json"
        args = _build_parser().parse_args([
            "multi-fpga", "compile", "--platform", str(explicit), "--out", "unused",
        ])
        self.assertEqual(args.platform, explicit)
        link = Platform.load(args.platform).links[0]
        self.assertEqual(link.capacity_sharing, "per_direction")
        self.assertIsNone(link.bank_count)
        self.assertIsNone(link.tdm_delay_model)
        self.assertIsNone(Platform.load(args.platform).virtual_board_model)

    def test_unsupported_research_platform_size_is_rejected(self):
        for count in (True, 2.0, 1, 3, "4"):
            with self.subTest(count=count), self.assertRaises(ValidationError):
                research_platform_path(count)

    def test_effective_data_width_override_updates_shared_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "custom.json"
            args = _build_parser().parse_args([
                "platform", "research-model", "--wires-per-bank", "16",
                "--output", str(output),
            ])
            with patch("emuflow.cli._print_json"):
                self.assertEqual(_dispatch(args), 0)
            for link in Platform.load(output).links:
                self.assertEqual((link.bank_count, link.wires_per_bank), (18, 16))
                self.assertEqual(link.data_lanes_per_direction, 288)
                self.assertEqual((link.cable_count, link.hp_banks_per_cable), (6, 3))

    def test_invalid_effective_data_width_is_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "invalid.json"
            for width in (True, 0, -1, 20.0, "20"):
                with self.subTest(width=width), self.assertRaises(ValidationError):
                    materialize_research_platform(output, wires_per_bank=width)
                self.assertFalse(output.exists())
