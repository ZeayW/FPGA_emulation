import copy
import tempfile
import unittest
from pathlib import Path

from emuflow.equivalence import _MappedModel
from emuflow.io import read_json, write_json
from emuflow.ir import EmuIR
from emuflow.multi_fpga_flow import run_multi_fpga_flow
from emuflow.partition import build_clusters, normalize_partition_constraints
from emuflow.platform import Platform
from emuflow.vtr_architecture import run_vtr_architecture_import
from emuflow.vtr_netlist import normalize_vtr_hard_block_json
from emuflow.yosys import import_yosys_json
from tests.native_build import (
    tdm_ratio_optimizer,
    tdm_timing_dag_optimizer,
    tlr_router,
    vtr_architecture_importer,
)


ROOT = Path(__file__).resolve().parents[1]
PLATFORM = ROOT / "platforms/virtual/academic_vtr_2fpga_p2p.json"
FAKE_OPENSTA = ROOT / "tests/fixtures/fake_opensta_paths.py"
VTR_ARCHITECTURE = (
    ROOT / "examples/architecture/vtr_k6_heterogeneous_fixture.xml"
)


def _raw_vtr_json() -> dict:
    ports = {
        "clk": {"direction": "input", "bits": [2]},
        "we": {"direction": "input", "bits": [3]},
        "addr": {"direction": "input", "bits": [4, 5]},
        "data": {"direction": "input", "bits": [6, 7]},
        "a": {"direction": "input", "bits": [8, 9]},
        "b": {"direction": "input", "bits": [10, 11]},
        "q": {"direction": "output", "bits": [12, 13]},
        "product": {
            "direction": "output",
            "bits": [14, 15, 16, 17],
        },
    }
    memory_directions = {
        "clk": "input",
        "addr": "input",
        "data": "input",
        "we": "input",
        "out": "output",
    }
    cells = {
        f"memory.0.0.bits[{bit}].bit_cell": {
            "type": "single_port_ram",
            "parameters": {},
            "attributes": {},
            "port_directions": memory_directions,
            "connections": {
                "clk": [2],
                "addr": [4, 5],
                "data": [18 if bit == 0 else 7],
                "we": [3],
                "out": [12 + bit],
            },
        }
        for bit in range(2)
    }
    cells["$mul$top.hard_multiply"] = {
        "type": "multiply",
        "parameters": {},
        "attributes": {},
        "connections": {
            "a": [8, 9],
            "b": [10, 11],
            "out": [14, 15, 16, 17],
        },
    }
    cells["feed_data"] = {
        "type": "$lut",
        "parameters": {"WIDTH": "10", "LUT": "1000"},
        "attributes": {},
        "port_directions": {"A": "input", "Y": "output"},
        "connections": {"A": [6, 7], "Y": [18]},
    }
    return {
        "creator": "unit-test",
        "modules": {
            "top": {
                "attributes": {"top": "1"},
                "ports": ports,
                "cells": cells,
                "netnames": {
                    name: {"hide_name": 0, "bits": value["bits"]}
                    for name, value in ports.items()
                }
                | {
                    "internal_data": {
                        "hide_name": 0,
                        "bits": [18],
                    }
                },
            }
        },
    }


class VtrNetlistTest(unittest.TestCase):
    def test_memory_atoms_are_collapsed_and_resources_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "raw.json"
            normalized = root / "normalized.json"
            write_json(source, _raw_vtr_json())
            report = normalize_vtr_hard_block_json(
                source, normalized, top="top"
            )
            value = read_json(normalized)
            cells = value["modules"]["top"]["cells"]
            self.assertEqual(report["memory_atoms_collapsed"], 2)
            self.assertEqual(report["memory_macros"], 1)
            self.assertEqual(report["multiplier_macros"], 1)
            self.assertEqual(len(cells), 3)
            self.assertEqual(
                cells["$mul$top.hard_multiply"]["port_directions"]["out"],
                "output",
            )
            memory = cells["memory.0.0.memory_macro"]
            self.assertEqual(memory["type"], "VTR_SP_RAM")
            self.assertEqual(memory["parameters"]["DATA_WIDTH"], 2)
            self.assertEqual(memory["parameters"]["DEPTH"], 4)
            self.assertEqual(memory["connections"]["data"], [18, 7])

            ir = import_yosys_json(normalized, top="top", clocks=["clk"])
            resources = ir.resource_totals().to_dict()
            self.assertEqual(resources["bram"], 1)
            self.assertEqual(resources["dsp"], 1)
            self.assertEqual(resources["lut"], 1)
            q_nets = [
                net for net in ir.value["nets"] if net["name"].startswith("q")
            ]
            self.assertTrue(q_nets)
            self.assertTrue(
                all(net["cut_class"] == "register_output" for net in q_nets)
            )
            ram_input_nets = [
                net
                for net in ir.value["nets"]
                if any(
                    sink["instance"] == "memory.0.0.memory_macro"
                    and sink["port"] in {"addr", "data", "we"}
                    for sink in net["sinks"]
                )
                and net["drivers"][0]["instance"] is not None
            ]
            self.assertTrue(
                all(
                    net["cut_class"] == "register_input"
                    for net in ram_input_nets
                )
            )

    def test_cycle_model_accepts_vtr_hard_macros(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "raw.json"
            normalized = root / "normalized.json"
            write_json(source, _raw_vtr_json())
            normalize_vtr_hard_block_json(source, normalized, top="top")
            model = _MappedModel(
                import_yosys_json(normalized, top="top", clocks=["clk"])
            )
            state = model.initial_state()
            values, next_state, outputs = model.evaluate(
                state, cycle=0, seed=7
            )
            self.assertEqual(len(model.multiply_ids), 1)
            self.assertEqual(len(model.ram_ids), 1)
            self.assertEqual(outputs["q[0]"], 0)
            self.assertEqual(outputs["q[1]"], 0)
            self.assertEqual(model.state_bit_count(), 2)
            self.assertTrue(values)
            self.assertIn("memory.0.0.memory_macro", next_state)

    def test_clock_sharing_does_not_merge_independent_hard_macros(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "raw.json"
            normalized = root / "normalized.json"
            write_json(source, _raw_vtr_json())
            normalize_vtr_hard_block_json(source, normalized, top="top")
            value = import_yosys_json(
                normalized, top="top", clocks=["clk"]
            ).to_dict()
            memory = next(
                instance
                for instance in value["instances"]
                if instance["type"] == "VTR_SP_RAM"
            )
            duplicate = copy.deepcopy(memory)
            duplicate["id"] = "second_memory_macro"
            duplicate["name"] = "second_memory_macro"
            value["instances"].append(duplicate)
            clock_net = next(
                net for net in value["nets"] if net["cut_class"] == "clock"
            )
            clock_net["sinks"].append(
                {
                    "instance": "second_memory_macro",
                    "port": "clk",
                    "bit": 0,
                }
            )
            clock_net["fanout"] += 1
            ir = EmuIR(value)
            platform = Platform.load(PLATFORM)
            constraints = normalize_partition_constraints(
                None, ir, platform
            )
            clusters = build_clusters(ir, constraints)["clusters"]
            hard_clusters = [
                cluster
                for cluster in clusters
                if any(
                    instance in {
                        "memory.0.0.memory_macro",
                        "second_memory_macro",
                    }
                    for instance in cluster["instances"]
                )
            ]
            self.assertEqual(len(hard_clusters), 2)

    def test_checked_multi_fpga_pipeline_accepts_hard_macros(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "raw.json"
            normalized = root / "normalized.json"
            constraints = root / "partition-constraints.json"
            architecture_db = root / "architecture.json"
            timing_db = root / "architecture-timing.json"
            raw = _raw_vtr_json()
            # Launch the cut path from the synchronous RAM output. The shared
            # fake OpenSTA fixture emits every queried net, so this integration
            # case must contain a structurally complete timed path as well.
            raw["modules"]["top"]["cells"]["feed_data"]["connections"][
                "A"
            ] = [12, 7]
            write_json(source, raw)
            normalize_vtr_hard_block_json(source, normalized, top="top")
            run_vtr_architecture_import(
                input_path=VTR_ARCHITECTURE,
                architecture_output_path=architecture_db,
                timing_output_path=timing_db,
                architecture_id="fixture-k6",
                width=24,
                height=24,
                executable=str(vtr_architecture_importer()),
            )
            write_json(
                constraints,
                {
                    "schema": "emuflow.partition-constraints/v1",
                    "fixed": [
                        {"instance": "feed_data", "fpga": "fpga0"},
                        {
                            "instance": "memory.0.0.memory_macro",
                            "fpga": "fpga1",
                        },
                    ],
                },
            )
            report = run_multi_fpga_flow(
                platform_path=PLATFORM,
                output_dir=root / "flow",
                yosys_json=normalized,
                top="top",
                clocks=["clk"],
                partition_constraints=constraints,
                partition_provider="greedy",
                clock_periods={"clk": 10.0},
                opensta=str(FAKE_OPENSTA),
                architecture_timing_db=timing_db,
                min_used_fpgas=2,
                router=str(tlr_router()),
                ratio_optimizer=str(tdm_ratio_optimizer()),
                timing_dag_optimizer=str(tdm_timing_dag_optimizer()),
                frame_slots=32,
                equivalence_cycles=4,
            )
            equivalence = report["stages"]["split"]["equivalence"]
            self.assertEqual(report["status"], "pass")
            self.assertEqual(equivalence["multipliers"], 1)
            self.assertEqual(equivalence["memory_macros"], 1)
            self.assertEqual(equivalence["mismatches"], 0)


if __name__ == "__main__":
    unittest.main()
