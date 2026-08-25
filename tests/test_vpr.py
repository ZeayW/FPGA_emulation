import hashlib
import subprocess
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.errors import EmuFlowError, ValidationError
from emuflow.vpr import (
    build_vtr_yosys_script,
    run_vpr_pack_place,
    run_vpr_route_packed,
    validate_vpr_pack_place_checkpoint,
    validate_vpr_timing_summary,
    validate_vpr_outputs,
)


class VprTest(unittest.TestCase):
    def test_vtr_rr_edge_ids_cover_graphs_larger_than_uint32(self) -> None:
        compiler = (
            shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
        )
        if compiler is None:
            self.skipTest("a C++17 compiler is required")
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "rr_edge_id_width.cpp"
            executable = root / "rr_edge_id_width"
            source.write_text(
                """
#include <cstddef>
#include <cstdint>
#include <limits>
#include "rr_graph_fwd.h"

static_assert(sizeof(RREdgeId) >= sizeof(std::uint64_t));

int main() {
    constexpr std::uint64_t edge =
        static_cast<std::uint64_t>(std::numeric_limits<std::uint32_t>::max()) + 17;
    const RREdgeId edge_id(edge);
    return static_cast<std::size_t>(edge_id) == edge ? 0 : 1;
}
""".strip()
                + "\n",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    compiler,
                    "-std=c++17",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I",
                    str(repository / "engines/vtr/libs/librrgraph/src/base"),
                    "-I",
                    str(repository / "engines/vtr/libs/libvtrutil/src"),
                    str(source),
                    "-o",
                    str(executable),
                ],
                check=True,
            )
            subprocess.run([str(executable)], check=True)

    def test_pack_place_checkpoint_is_source_bound_and_tamper_evident(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            architecture = root / "arch.xml"
            circuit = root / "partition.eblif"
            architecture.write_text("<architecture/>", encoding="utf-8")
            circuit.write_text(".model partition\n.end\n", encoding="utf-8")
            output = root / "pack-place"
            output.mkdir()
            netlist = output / "partition.net"
            placement = output / "partition.place"
            netlist.write_text("packed", encoding="utf-8")
            placement.write_text("placement", encoding="utf-8")
            log = output / "vpr.console.log"
            log.write_text(
                "Netlist num_nets: 4\nNetlist num_blocks: 3\nVPR succeeded\n",
                encoding="utf-8",
            )
            sha256 = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
            report = {
                "status": "pass",
                "provider": "vpr-root-build",
                "stages": ["pack", "place"],
                "metrics": {"packed_nets": 4, "packed_blocks": 3},
                "artifacts": {
                    "packed_netlist": {
                        "path": str(netlist.resolve()),
                        "bytes": netlist.stat().st_size,
                        "sha256": sha256(netlist),
                    },
                    "placement": {
                        "path": str(placement.resolve()),
                        "bytes": placement.stat().st_size,
                        "sha256": sha256(placement),
                    },
                },
                "architecture": {
                    "path": str(architecture.resolve()),
                    "sha256": sha256(architecture),
                },
                "circuit": {
                    "path": str(circuit.resolve()),
                    "sha256": sha256(circuit),
                },
                "configuration": {"seed": 1},
                "command": ["vpr"],
                "log": str(log.resolve()),
            }
            (output / "vpr-pack-place-report.json").write_text(
                json.dumps(report), encoding="utf-8"
            )
            checked = validate_vpr_pack_place_checkpoint(
                architecture, circuit, output, seed=1
            )
            self.assertEqual(checked["metrics"]["packed_blocks"], 3)
            placement.write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "placement seal"):
                validate_vpr_pack_place_checkpoint(
                    architecture, circuit, output, seed=1
                )

    def test_pack_place_resume_rejects_partial_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            architecture = root / "arch.xml"
            circuit = root / "partition.eblif"
            architecture.write_text("<architecture/>", encoding="utf-8")
            circuit.write_text(".model partition\n.end\n", encoding="utf-8")
            output = root / "pack-place"
            output.mkdir()
            (output / "partition.net").write_text("partial", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "partial checkpoint"):
                run_vpr_pack_place(
                    architecture,
                    circuit,
                    output,
                    resume=True,
                )

    def test_pack_place_checkpoint_allows_sealed_root_relocation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_physical = root / "staging/output/physical"
            old_output = old_physical / "FPGA0/vpr-pack-place"
            old_output.mkdir(parents=True)
            architecture = old_physical / "architecture/arch.xml"
            architecture.parent.mkdir()
            circuit = old_physical / "FPGA0/partition.eblif"
            architecture.write_text("<architecture/>", encoding="utf-8")
            circuit.write_text(".model partition\n.end\n", encoding="utf-8")
            netlist = old_output / "partition.net"
            placement = old_output / "partition.place"
            log = old_output / "vpr.console.log"
            netlist.write_text("packed", encoding="utf-8")
            placement.write_text("placement", encoding="utf-8")
            log.write_text(
                "Netlist num_nets: 4\nNetlist num_blocks: 3\nVPR succeeded\n",
                encoding="utf-8",
            )
            sha256 = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
            report = {
                "status": "pass",
                "provider": "vpr-root-build",
                "stages": ["pack", "place"],
                "metrics": {"packed_nets": 4, "packed_blocks": 3},
                "artifacts": {
                    "packed_netlist": {
                        "path": str(netlist),
                        "bytes": netlist.stat().st_size,
                        "sha256": sha256(netlist),
                    },
                    "placement": {
                        "path": str(placement),
                        "bytes": placement.stat().st_size,
                        "sha256": sha256(placement),
                    },
                },
                "architecture": {
                    "path": str(architecture),
                    "sha256": sha256(architecture),
                },
                "circuit": {"path": str(circuit), "sha256": sha256(circuit)},
                "configuration": {"seed": 1},
                "command": ["vpr"],
                "log": str(log),
            }
            (old_output / "vpr-pack-place-report.json").write_text(
                json.dumps(report), encoding="utf-8"
            )
            new_physical = root / "failures/output/physical"
            new_physical.parent.mkdir(parents=True)
            old_physical.rename(new_physical)
            checked = validate_vpr_pack_place_checkpoint(
                new_physical / "architecture/arch.xml",
                new_physical / "FPGA0/partition.eblif",
                new_physical / "FPGA0/vpr-pack-place",
                seed=1,
            )
            self.assertEqual(checked["metrics"]["packed_blocks"], 3)
            self.assertEqual(
                checked["architecture"]["path"],
                str((new_physical / "architecture/arch.xml").resolve()),
            )
            self.assertEqual(
                checked["circuit"]["path"],
                str((new_physical / "FPGA0/partition.eblif").resolve()),
            )
            self.assertEqual(
                checked["artifacts"]["packed_netlist"]["path"],
                str(
                    (
                        new_physical
                        / "FPGA0/vpr-pack-place/partition.net"
                    ).resolve()
                ),
            )
            self.assertEqual(
                checked["artifacts"]["placement"]["path"],
                str(
                    (
                        new_physical
                        / "FPGA0/vpr-pack-place/partition.place"
                    ).resolve()
                ),
            )
            self.assertEqual(
                checked["log"],
                str(
                    (
                        new_physical
                        / "FPGA0/vpr-pack-place/vpr.console.log"
                    ).resolve()
                ),
            )

    def test_logic_only_script_lowers_ff_variants_to_latches(self) -> None:
        script = build_vtr_yosys_script(
            [Path("rtl/cpu.v")],
            "cpu",
            Path("build/cpu.eblif"),
        )
        self.assertIn("synth -top cpu -noabc", script)
        self.assertEqual(script.count("dffunmap"), 2)
        self.assertIn("abc -lut 6", script)
        self.assertIn('write_blif -attr -cname "build/cpu.eblif"', script)

    def test_hard_block_script_uses_the_pinned_vtr_profile(self) -> None:
        script = build_vtr_yosys_script(
            [Path("examples/rtl/vtr_hard_blocks.v")],
            "vtr_hard_blocks",
            Path("build/vtr_hard_blocks.eblif"),
            hard_blocks=True,
        )
        self.assertIn("synth -top vtr_hard_blocks -run begin:fine", script)
        self.assertIn("-noalumacc -flatten", script)
        self.assertIn("vtr_multiply_map.v", script)
        self.assertIn("memory_libmap -lib", script)
        self.assertIn("vtr_memories.txt", script)
        self.assertIn("vtr_memory_map.v", script)
        self.assertIn("chtype -set multiply", script)
        self.assertIn("chtype -set single_port_ram", script)
        self.assertEqual(script.count("dffunmap"), 2)

    def test_script_can_emit_json_and_eblif_from_same_mapping(self) -> None:
        script = build_vtr_yosys_script(
            [Path("rtl/cpu.v")],
            "cpu",
            Path("build/cpu.eblif"),
            hard_blocks=True,
            json_output=Path("build/cpu.json"),
        )
        self.assertIn('write_json "build/cpu.json"', script)
        self.assertLess(script.index("write_json"), script.index("write_blif"))

    def test_empty_source_list_is_rejected(self) -> None:
        with self.assertRaisesRegex(EmuFlowError, "at least one RTL source"):
            build_vtr_yosys_script([], "cpu", Path("cpu.eblif"))

    def test_route_report_requires_success_and_all_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            netlist = root / "cpu.net"
            placement = root / "cpu.place"
            route = root / "cpu.route"
            for path in (netlist, placement, route):
                path.write_text(path.name, encoding="utf-8")
            report = validate_vpr_outputs(
                """
                Netlist num_nets: 2330
                Netlist num_blocks: 605
                Netlist io blocks: 342.
                Netlist clb blocks: 263.
                Netlist mult_36 blocks: 0.
                Netlist memory blocks: 0.
                Device Utilization: 0.69 (target 1.00)
                Total wirelength: 29761, average net length: 12.7894
                Final critical path delay (least slack): 8.08208 ns,
                Fmax: 123.731 MHz
                Final setup Worst Negative Slack (sWNS): -0.25 ns
                Final setup Worst Slack: -0.25 ns
                Final setup Total Negative Slack (sTNS): -0.5 ns
                Final setup Failing Endpoint Constraints (sFEC): 3
                Final setup Failing Endpoints: 2
                intra-domain critical path delays (CPDs):
                  n10 to n10 CPD: 2.5 ns (400 MHz)
                  n20 to n20 CPD: 8 ns (125 MHz)
                inter-domain critical path delays (CPDs):
                  n10 to n20 CPD: 3.25 ns (307.69 MHz)
                VPR succeeded
                """,
                packed_netlist=netlist,
                placement=placement,
                route=route,
            )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["metrics"]["packed_blocks"], 605)
        self.assertEqual(report["metrics"]["clb_blocks"], 263)
        self.assertEqual(report["metrics"]["wirelength"], 29761)
        self.assertEqual(report["metrics"]["fmax_mhz"], 123.731)
        self.assertEqual(report["metrics"]["setup_tns_ns"], -0.5)
        self.assertEqual(report["metrics"]["setup_failing_endpoints"], 2)
        self.assertEqual(
            report["metrics"]["clock_domain_cpd_ns"],
            {"n10->n10": 2.5, "n10->n20": 3.25, "n20->n20": 8.0},
        )
        self.assertEqual(
            report["stages"], ["pack", "place", "route", "analysis"]
        )

    def test_machine_timing_summary_is_independently_bound_to_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "timing-summary.json"
            path.write_text(
                json.dumps(
                    {
                        "cpd": 8.0,
                        "fmax": 125.0,
                        "swns": -0.25,
                        "worst_slack": -0.25,
                        "stns": -0.5,
                        "sfec": 3,
                        "failing_endpoints": 2,
                    }
                ),
                encoding="utf-8",
            )
            metrics = {
                "critical_path_ns": 8.0,
                "fmax_mhz": 125.0,
                "setup_wns_ns": -0.25,
                "setup_worst_slack_ns": -0.25,
                "setup_tns_ns": -0.5,
                "setup_failing_endpoint_constraints": 3,
                "setup_failing_endpoints": 2,
            }
            report = validate_vpr_timing_summary(path, metrics)
            self.assertEqual(report["metrics"]["setup_tns_ns"], -0.5)
            del metrics["fmax_mhz"]
            report = validate_vpr_timing_summary(path, metrics)
            self.assertEqual(report["metrics"]["fmax_mhz"], 125.0)
            path.write_text(
                json.dumps(
                    {
                        "cpd": 0.906,
                        "fmax": 1103.75,
                        "swns": 0.0,
                        "worst_slack": 3.094,
                        "stns": 0.0,
                        "sfec": 0,
                        "failing_endpoints": 0,
                    }
                ),
                encoding="utf-8",
            )
            rounded_report = validate_vpr_timing_summary(
                path,
                {
                    "critical_path_ns": 0.906,
                    "setup_wns_ns": 0.0,
                    "setup_worst_slack_ns": 3.094,
                    "setup_tns_ns": 0.0,
                    "setup_failing_endpoint_constraints": 0,
                    "setup_failing_endpoints": 0,
                },
            )
            self.assertEqual(rounded_report["metrics"]["fmax_mhz"], 1103.75)
            path.write_text(
                path.read_text(encoding="utf-8").replace("1103.75", "1103.74"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "disagrees"):
                validate_vpr_timing_summary(
                    path,
                    {
                        "critical_path_ns": 0.906,
                        "setup_wns_ns": 0.0,
                        "setup_worst_slack_ns": 3.094,
                        "setup_tns_ns": 0.0,
                        "setup_failing_endpoint_constraints": 0,
                        "setup_failing_endpoints": 0,
                    },
                )
            path.write_text(
                json.dumps(
                    {
                        "cpd": 8.0,
                        "fmax": 125.0,
                        "swns": -0.25,
                        "worst_slack": -0.25,
                        "stns": -0.5,
                        "sfec": 3,
                        "failing_endpoints": 2,
                    }
                ),
                encoding="utf-8",
            )
            metrics["setup_tns_ns"] = -0.25
            with self.assertRaisesRegex(ValidationError, "disagrees"):
                validate_vpr_timing_summary(path, metrics)
    def test_route_report_rejects_missing_success_marker(self) -> None:
        with self.assertRaisesRegex(ValidationError, "success marker"):
            validate_vpr_outputs(
                "Netlist num_nets: 1",
                packed_netlist=Path("missing.net"),
                placement=Path("missing.place"),
                route=Path("missing.route"),
            )

    def test_route_packed_uses_existing_netlist_and_placement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            architecture = root / "arch.xml"
            circuit = root / "cpu.eblif"
            netlist = root / "cpu.net"
            packed_contract = root / "packed.json"
            placement = root / "cpu.place"
            for path in (
                architecture,
                circuit,
                netlist,
                packed_contract,
                placement,
            ):
                path.write_text(path.name, encoding="utf-8")
            boundary_query = root / "boundary-query.tsv"
            boundary_query.write_text(
                "endpoint\tkind\tstart_pin\tend_pin\n", encoding="utf-8"
            )
            boundary_output = root / "route" / "boundary-timing.tsv"
            logic_query = root / "logic-query.tsv"
            logic_query.write_text(
                "endpoint\tkind\tstart_pin\tend_pin\n", encoding="utf-8"
            )
            logic_output = root / "route" / "logic-timing.tsv"
            local_query = root / "local-query.tsv"
            local_query.write_text(
                "endpoint\tkind\tstart_pin\tend_pin\n", encoding="utf-8"
            )
            local_output = root / "route" / "local-timing.tsv"

            def fake_run(arguments, **kwargs):
                self.assertEqual(
                    kwargs["env"]["EMUFLOW_VPR_BOUNDARY_QUERY"],
                    str(boundary_query.resolve()),
                )
                self.assertEqual(
                    kwargs["env"]["EMUFLOW_VPR_BOUNDARY_OUTPUT"],
                    str(boundary_output.resolve()),
                )
                self.assertEqual(
                    kwargs["env"]["EMUFLOW_VPR_LOGIC_QUERY"],
                    str(logic_query.resolve()),
                )
                self.assertEqual(
                    kwargs["env"]["EMUFLOW_VPR_LOGIC_OUTPUT"],
                    str(logic_output.resolve()),
                )
                self.assertEqual(
                    kwargs["env"]["EMUFLOW_VPR_LOCAL_PATH_QUERY"],
                    str(local_query.resolve()),
                )
                self.assertEqual(
                    kwargs["env"]["EMUFLOW_VPR_LOCAL_PATH_OUTPUT"],
                    str(local_output.resolve()),
                )
                route_index = arguments.index("--route_file") + 1
                Path(arguments[route_index]).write_text(
                    "route", encoding="utf-8"
                )
                boundary_output.write_text(
                    "endpoint\tkind\tdelay_ns\tstart_pin\tend_pin\n",
                    encoding="utf-8",
                )
                logic_output.write_text(
                    "endpoint\tkind\tdelay_ns\tstart_pin\tend_pin\n",
                    encoding="utf-8",
                )
                local_output.write_text(
                    "endpoint\tkind\tdelay_ns\tstart_pin\tend_pin\n",
                    encoding="utf-8",
                )
                summary_index = arguments.index("--write_timing_summary") + 1
                Path(arguments[summary_index]).write_text(
                    json.dumps(
                        {
                            "cpd": 1.0,
                            "fmax": 1000.0,
                            "swns": 0.0,
                            "worst_slack": 3.0,
                            "stns": 0.0,
                            "sfec": 0,
                            "failing_endpoints": 0,
                        }
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    """
                    Netlist num_nets: 2
                    Netlist num_blocks: 3
                    Total wirelength: 12
                    Final critical path delay (least slack): 1 ns, Fmax: 1000 MHz
                    Final setup Worst Negative Slack (sWNS): 0 ns
                    Final setup Worst Slack: 3 ns
                    Final setup Total Negative Slack (sTNS): 0 ns
                    Final setup Failing Endpoint Constraints (sFEC): 0
                    Final setup Failing Endpoints: 0
                    VPR succeeded
                    """,
                )

            with (
                patch("emuflow.vpr.subprocess.run", side_effect=fake_run),
                patch(
                    "emuflow.vpr.validate_vpr_route_artifacts",
                    return_value={"status": "pass"},
                ),
            ):
                report = run_vpr_route_packed(
                    architecture,
                    circuit,
                    netlist,
                    packed_contract,
                    placement,
                    root / "route",
                    executable="/source-built/vpr",
                    boundary_query=boundary_query,
                    boundary_output=boundary_output,
                    logic_query=logic_query,
                    logic_output=logic_output,
                    local_path_query=local_query,
                    local_path_output=local_output,
                )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["stages"], ["route", "analysis"])
        self.assertIn("--net_file", report["command"])
        self.assertIn("--place_file", report["command"])
        self.assertIn("--write_rr_graph", report["command"])
        self.assertIn("--write_timing_summary", report["command"])
        self.assertEqual(report["route_check"]["status"], "pass")
        self.assertFalse(report["configuration"]["retain_rr_graph"])
        self.assertIn("boundary_timing", report)
        self.assertIn("logic_segment_timing", report)
        self.assertIn("local_path_timing", report)


if __name__ == "__main__":
    unittest.main()
