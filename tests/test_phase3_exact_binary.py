import shutil
import tempfile
import unittest
from pathlib import Path

from emuflow.combinational_cut import semantic_contract_sha256
from emuflow.errors import ValidationError
from emuflow.phase3_exact_binary import (
    read_phase3_exact_binary_contract,
    validate_phase3_exact_binary,
    write_phase3_exact_binary,
)


def _contract(design="binary_fixture"):
    nodes = [
        {
            "net": "net0",
            "cut_class": "combinational",
            "source_fpgas": ["fpga0"],
            "sink_fpgas": ["fpga1"],
            "dependency_level": 0,
            "combinational_dependency_depth": 0,
            "predecessor_cut_nets": [],
            "source_segment_ids": ["segment000000"],
            "capture_segment_ids": [],
        },
        {
            "net": "net1",
            "cut_class": "combinational",
            "source_fpgas": ["fpga1"],
            "sink_fpgas": ["fpga0"],
            "dependency_level": 1,
            "combinational_dependency_depth": 1,
            "predecessor_cut_nets": ["net0"],
            "source_segment_ids": ["segment000001"],
            "capture_segment_ids": ["segment000002"],
        },
    ]
    return {
        "schema": "emuflow.static-exact-combinational-cut/v3",
        "mode": "static-exact-combinational",
        "design": design,
        "platform": "two",
        "source_identity": {"qualification": "fixture"},
        "qualification": {"status": "pass"},
        "metrics": {
            "transported_cut_nets": 2,
            "logic_segments": 3,
            "capture_requirements": 1,
            "dependency_edges": 1,
        },
        "cut_nodes": nodes,
        "dependency_edges": [
            {"from": "net0", "to": "net1", "segment": "segment000001"}
        ],
        "logic_segments": [
            {
                "id": "segment000000",
                "kind": "launch_to_tx",
                "fpga": "fpga0",
                "sink_cut_net": "net0",
            },
            {
                "id": "segment000001",
                "kind": "rx_to_tx",
                "fpga": "fpga1",
                "source_cut_net": "net0",
                "sink_cut_net": "net1",
            },
            {
                "id": "segment000002",
                "kind": "rx_to_capture",
                "fpga": "fpga1",
                "source_cut_net": "net1",
                "capture_requirement": "capture000000",
            },
        ],
        "capture_requirements": [
            {
                "id": "capture000000",
                "cut_net": "net1",
                "fpga": "fpga1",
                "kind": "architectural-state",
                "endpoint": "state/D",
                "port": "D",
                "bit": 0,
            }
        ],
    }


class Phase3ExactBinaryTest(unittest.TestCase):
    def test_roundtrip_and_relocation_preserve_canonical_contract(self):
        contract = _contract()
        assignment = {
            "semantic_contract_sha256": semantic_contract_sha256(contract),
            "cut_nets": [
                {
                    key: value
                    for key, value in node.items()
                    if key not in {"source_segment_ids", "capture_segment_ids"}
                }
                for node in contract["cut_nodes"]
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.bin"
            descriptor = write_phase3_exact_binary(
                source, contract, ["fpga0", "fpga1"]
            )
            relocated = root / "relocated" / "assignment.exact.bin"
            relocated.parent.mkdir()
            shutil.copyfile(source, relocated)
            self.assertEqual(
                read_phase3_exact_binary_contract(
                    relocated, descriptor, assignment
                ),
                contract,
            )

    def test_corruption_truncation_and_mixed_run_are_rejected(self):
        first = _contract()
        second = _contract("other_design")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_path = root / "first.bin"
            second_path = root / "second.bin"
            first_descriptor = write_phase3_exact_binary(
                first_path, first, ["fpga0", "fpga1"]
            )
            second_descriptor = write_phase3_exact_binary(
                second_path, second, ["fpga0", "fpga1"]
            )
            with self.assertRaisesRegex(ValidationError, "descriptor"):
                validate_phase3_exact_binary(first_path, second_descriptor)

            truncated = root / "truncated.bin"
            truncated.write_bytes(first_path.read_bytes()[:-1])
            with self.assertRaisesRegex(ValidationError, "preamble"):
                validate_phase3_exact_binary(truncated, first_descriptor)

            corrupted = root / "corrupted.bin"
            encoded = bytearray(first_path.read_bytes())
            encoded[28] ^= 1
            corrupted.write_bytes(encoded)
            with self.assertRaisesRegex(ValidationError, "Static Exact"):
                validate_phase3_exact_binary(corrupted, first_descriptor)

    def test_repeated_population_stays_compact(self):
        node_count = 2_000
        contract = _contract("repeated")
        contract["cut_nodes"] = []
        contract["logic_segments"] = []
        contract["capture_requirements"] = []
        contract["dependency_edges"] = []
        segment_number = 0
        capture_number = 0
        for index in range(node_count):
            net = f"net{index:07d}"
            fpga = f"fpga{index % 2}"
            source_ids = []
            capture_ids = []
            for _ in range(4):
                segment_id = f"segment{segment_number:06d}"
                segment_number += 1
                source_ids.append(segment_id)
                contract["logic_segments"].append({
                    "id": segment_id,
                    "kind": "launch_to_tx",
                    "fpga": fpga,
                    "sink_cut_net": net,
                })
            for capture_index in range(4):
                capture_id = f"capture{capture_number:06d}"
                contract["capture_requirements"].append({
                    "id": capture_id,
                    "cut_net": net,
                    "fpga": fpga,
                    "kind": "architectural-state",
                    "endpoint": f"cell{index:07d}_{capture_index}/D",
                    "port": "D",
                    "bit": 0,
                })
                segment_id = f"segment{segment_number:06d}"
                segment_number += 1
                capture_ids.append(segment_id)
                contract["logic_segments"].append({
                    "id": segment_id,
                    "kind": "rx_to_capture",
                    "fpga": fpga,
                    "source_cut_net": net,
                    "capture_requirement": capture_id,
                })
                capture_number += 1
            contract["cut_nodes"].append({
                "net": net,
                "cut_class": "combinational",
                "source_fpgas": [fpga],
                "sink_fpgas": [f"fpga{1 - index % 2}"],
                "dependency_level": 0,
                "combinational_dependency_depth": 0,
                "predecessor_cut_nets": [],
                "source_segment_ids": source_ids,
                "capture_segment_ids": capture_ids,
            })
        contract["metrics"] = {
            "transported_cut_nets": node_count,
            "logic_segments": len(contract["logic_segments"]),
            "capture_requirements": len(contract["capture_requirements"]),
            "dependency_edges": 0,
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "assignment.exact.bin"
            descriptor = write_phase3_exact_binary(
                output, contract, ["fpga0", "fpga1"]
            )
            validation = validate_phase3_exact_binary(output, descriptor)
            records = (
                descriptor["counts"]["source_segments"]
                + descriptor["counts"]["logic_segments"]
                + descriptor["counts"]["capture_requirements"]
            )
            self.assertEqual(validation["status"], "pass")
            self.assertLess(output.stat().st_size, 64 * records)


if __name__ == "__main__":
    unittest.main()
