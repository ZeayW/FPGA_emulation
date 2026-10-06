from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.ppro_blackbox_constraints import (
    parse_logical_targets,
    render_ppro_prepartition_constraints,
    render_ppro_timing_sdc,
)
from emuflow.ppro_blackbox_communication import generate_communication_probe_bundle
from emuflow.ppro_blackbox_microbench import generate_capacity_probe_bundle
from emuflow.ppro_blackbox_topology import generate_topology_probe_bundle


class PProBlackboxConstraintsTest(unittest.TestCase):
    def _common(self):
        return {
            "campaign_id": "real-contract",
            "public_prior_id": "lx2-public-prior-v1",
            "configuration_id": "lx2-m2",
            "tool_release": "2026.1",
            "runner_revision": "a" * 64,
            "adapter_profile": "ppro-2026-ordinary-reports-v1",
        }

    def test_fixed_assignments_render_only_documented_user_syntax(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "constraints.json"
            output = root / "prepartition.cfg"
            source.write_text(
                json.dumps(
                    {
                        "assignments": [
                            {"partition": "P0", "target": "F0"},
                            {"partition": "P1", "target": "F1"},
                        ],
                        "control_mode": "fixed_assignment",
                        "documented_actions": ["partition_constraint"],
                        "seed": 1,
                    }
                ),
                encoding="utf-8",
            )
            render_ppro_prepartition_constraints(
                source, {"F0": "MB1.F1", "F1": "MB1.F3"}, output
            )
            self.assertEqual(
                output.read_text(encoding="utf-8").splitlines()[1:],
                ["assign_inst {P0} {MB1.F1}", "assign_inst {P1} {MB1.F3}"],
            )

    def test_route_tdm_and_seed_syntax_are_not_guessed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "constraints.json"
            source.write_text(
                json.dumps(
                    {
                        "control_mode": "fixed_communication",
                        "documented_actions": ["net_route_constraint"],
                        "seed": 1,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValidationError, "supports only|does not guess"
            ):
                render_ppro_prepartition_constraints(
                    source, {"F0": "MB1.F1"}, root / "out.cfg"
                )

    def test_logical_target_cli_parser_is_strict(self):
        self.assertEqual(parse_logical_targets([]), {})
        self.assertEqual(
            parse_logical_targets(["F0=MB1.F1", "F1=MB1.F3"]),
            {"F0": "MB1.F1", "F1": "MB1.F3"},
        )
        with self.assertRaises(ValidationError):
            parse_logical_targets(["F0=MB1.F1", "F1=MB1.F1"])
        with self.assertRaises(ValidationError):
            parse_logical_targets(["bad"])

    def test_uncontrolled_run_needs_no_physical_target_names(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "constraints.json"
            output = root / "prepartition.cfg"
            source.write_text(
                json.dumps(
                    {
                        "control_mode": "none",
                        "documented_actions": [],
                        "seed": 1,
                    }
                ),
                encoding="utf-8",
            )
            render_ppro_prepartition_constraints(source, {}, output)
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "# Generated from provider-neutral documented user constraints.\n",
            )

    def test_timing_clocks_render_as_standard_sdc(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "constraints.json"
            output = root / "timing.sdc"
            source.write_text(
                json.dumps(
                    {
                        "control_mode": "none",
                        "documented_actions": [],
                        "seed": 1,
                        "timing_clocks": [
                            {"port": "clk", "period_ns": 10.0},
                            {"port": "aux_clk", "period_ns": 20.0},
                        ],
                        "timing_io": {
                            "input_groups": [
                                {
                                    "clock": "clk",
                                    "delay_ns": 0.0,
                                    "ports": ["request", "payload"],
                                }
                            ],
                            "output_groups": [
                                {
                                    "clock": "aux_clk",
                                    "delay_ns": 1.25,
                                    "ports": ["response"],
                                }
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )
            render_ppro_timing_sdc(source, output)
            self.assertEqual(
                output.read_text(encoding="utf-8").splitlines()[1:],
                [
                    "create_clock -name {clk} -period 10.000000000 [get_ports {clk}]",
                    "create_clock -name {aux_clk} -period 20.000000000 [get_ports {aux_clk}]",
                    "set_input_delay 0.000000000 -clock [get_clocks {clk}] "
                    "[get_ports {request payload}]",
                    "set_output_delay 1.250000000 -clock [get_clocks {aux_clk}] "
                    "[get_ports {response}]",
                ],
            )

    def test_timing_io_rejects_unknown_clock_and_duplicate_port(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "constraints.json"
            base = {
                "control_mode": "none",
                "documented_actions": [],
                "seed": 1,
                "timing_clocks": [{"port": "clk", "period_ns": 10.0}],
                "timing_io": {
                    "input_groups": [
                        {"clock": "other", "delay_ns": 0.0, "ports": ["d"]}
                    ],
                    "output_groups": [],
                },
            }
            source.write_text(json.dumps(base), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "undeclared timing clock"):
                render_ppro_timing_sdc(source, root / "timing.sdc")
            base["timing_io"]["input_groups"] = [
                {"clock": "clk", "delay_ns": 0.0, "ports": ["d"]},
                {"clock": "clk", "delay_ns": 1.0, "ports": ["d"]},
            ]
            source.write_text(json.dumps(base), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "repeat a port"):
                render_ppro_timing_sdc(source, root / "timing.sdc")

    def test_real_probe_constraints_all_render_without_private_database_input(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            common = self._common()
            bundles = [
                generate_capacity_probe_bundle(
                    root / "capacity",
                    axis="lut",
                    units=8,
                    repeat=0,
                    role="fit",
                    seed=1,
                    **common,
                ),
                generate_topology_probe_bundle(
                    root / "topology",
                    fpga_count=4,
                    source_index=0,
                    sink_index=3,
                    width=8,
                    pipeline_stages=4,
                    repeat=0,
                    role="fit",
                    seed=1,
                    **common,
                ),
                generate_communication_probe_bundle(
                    root / "communication",
                    kind="latency",
                    fpga_count=4,
                    source_index=0,
                    sink_indices=[1],
                    width=64,
                    flow_count=1,
                    bidirectional=False,
                    local_baseline=False,
                    forced_tdm_ratio=0,
                    repeat=0,
                    role="fit",
                    seed=1,
                    **common,
                ),
            ]
            targets = {f"F{index}": f"MB1.F{index + 1}" for index in range(4)}
            for index, bundle in enumerate(bundles):
                output = root / f"rendered-{index}.cfg"
                render_ppro_prepartition_constraints(
                    bundle.constraints_path, targets, output
                )
                text = output.read_text(encoding="utf-8")
                self.assertIn("assign_inst {P0} {MB1.F1}", text)
                self.assertNotIn("route", text.lower())
                self.assertNotIn("tdm", text.lower())


if __name__ == "__main__":
    unittest.main()
