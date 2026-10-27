from tests.phase3_exact_fixture import (write_exact_fixture, exact_contract_facts, exact_partition_facts)
import copy
import hashlib
import json
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

from tests.native_build import emuflow_native

from emuflow.partition.combinational_cut import semantic_contract_sha256
from emuflow.common.errors import ValidationError
from emuflow.partition.phase3_exact_binary import (iter_phase3_exact_captures, validate_phase3_exact_binary)
from tests.phase3_exact_fixture import validate_static_exact_capture_bit
from tests.native_semantic_facts import (
    read_nodes as read_native_static_exact_cut_node_stream,
    read_captures as read_native_static_exact_capture_stream,
    semantic_arrays, native_hash,
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
    def test_v2_capture_bits_preserve_bounded_python_integers(self):
        for bit in (0, 2**32, 2**64, 1 << 130, 1 << 8190, 1 << 8191):
            with self.subTest(bit=bit), tempfile.TemporaryDirectory() as temporary:
                contract = _contract(f"wide_bit_{bit.bit_length()}")
                contract["capture_requirements"][0]["bit"] = bit
                digest = semantic_contract_sha256(contract)
                assignment = {
                    "semantic_contract_sha256": digest,
                    "cut_nets": [
                        {
                            key: value
                            for key, value in node.items()
                            if key
                            not in {"source_segment_ids", "capture_segment_ids"}
                        }
                        for node in contract["cut_nodes"]
                    ],
                }
                path = Path(temporary) / "assignment.exact.bin"
                descriptor = write_exact_fixture(
                    path, contract, ["fpga0", "fpga1"]
                )
                self.assertEqual(descriptor["capture_bit_max_bits"], 8192)

                self.assertEqual(
                    list(iter_phase3_exact_captures(path, descriptor))[0]["bit"],
                    bit,
                )
                self.assertEqual(
                    exact_partition_facts(
                        path, descriptor, "fpga1"
                    )["capture_requirements"][0]["bit"],
                    bit,
                )
                self.assertEqual(
                    exact_contract_facts(
                        path, descriptor, assignment
                    ),
                    contract,
                )
                validation = validate_phase3_exact_binary(path, descriptor)
                self.assertEqual(validation["status"], "pass")
                self.assertEqual(validation["capture_bit_max_bits"], 8192)

    def test_capture_bits_over_8192_bits_fail_before_encoding_or_digest(self):
        for bit in (1 << 8192, (1 << 8193) - 1):
            with self.subTest(bit_length=bit.bit_length()):
                with self.assertRaisesRegex(
                    ValidationError, "at most 8192 bits"
                ):
                    validate_static_exact_capture_bit(bit)
                contract = _contract(f"oversized_{bit.bit_length()}")
                contract["capture_requirements"][0]["bit"] = bit
                with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
                    ValidationError, "at most 8192 bits"
                ):
                    write_exact_fixture(
                        Path(temporary) / "assignment.exact.bin",
                        contract,
                        ["fpga0", "fpga1"],
                        semantic_digest="0" * 64,
                    )

    def test_v1_capture_bits_remain_readable(self):
        contract = _contract("legacy_v1")
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
            path = Path(temporary) / "assignment.exact.bin"
            descriptor = write_exact_fixture(
                path,
                contract,
                ["fpga0", "fpga1"],
                _format_version=1,
            )
            self.assertEqual(
                descriptor["schema"],
                "emuflow.phase3-static-exact-binary/v1",
            )
            self.assertEqual(
                exact_contract_facts(path, descriptor, assignment),
                contract,
            )
            self.assertEqual(
                validate_phase3_exact_binary(path, descriptor)["schema"],
                "emuflow.phase3-static-exact-binary/v1",
            )

    def test_v2_capture_bit_rejects_noncanonical_and_truncated_encoding(self):
        contract = _contract("malformed_wide_bit")
        contract["capture_requirements"][0]["bit"] = 256
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.bin"
            descriptor = write_exact_fixture(
                source, contract, ["fpga0", "fpga1"]
            )
            encoded = bytearray(source.read_bytes())
            _magic, _version, manifest_offset, manifest_length = struct.unpack_from(
                "<8sIQQ", encoded
            )
            manifest = json.loads(
                encoded[
                    manifest_offset : manifest_offset + manifest_length
                ].decode("utf-8")
            )
            section = manifest["sections"]["captures:1"]
            section_offset = section["offset"]
            cursor = section_offset + struct.calcsize("<IIB")
            for _ in range(2):
                field_length = struct.unpack_from("<I", encoded, cursor)[0]
                cursor += 4 + field_length
            bit_length = struct.unpack_from("<I", encoded, cursor)[0]
            self.assertEqual(bit_length, 2)
            bit_offset = cursor + 4

            def publish_mutation(name, mutate):
                damaged = bytearray(encoded)
                mutate(damaged)
                capture_bytes = bytes(
                    damaged[
                        section_offset : section_offset + section["bytes"]
                    ]
                )
                changed_manifest = copy.deepcopy(manifest)
                changed_manifest["sections"]["captures:1"]["sha256"] = (
                    hashlib.sha256(capture_bytes).hexdigest()
                )
                manifest_bytes = json.dumps(
                    changed_manifest,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                self.assertEqual(len(manifest_bytes), manifest_length)
                damaged[
                    manifest_offset : manifest_offset + manifest_length
                ] = manifest_bytes
                path = root / name
                path.write_bytes(damaged)
                changed_descriptor = {
                    **descriptor,
                    "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                }
                return path, changed_descriptor

            noncanonical, noncanonical_descriptor = publish_mutation(
                "noncanonical.bin",
                lambda damaged: damaged.__setitem__(bit_offset, 0),
            )
            with self.assertRaisesRegex(ValidationError, "not canonical"):
                list(
                    iter_phase3_exact_captures(
                        noncanonical, noncanonical_descriptor
                    )
                )

            truncated, truncated_descriptor = publish_mutation(
                "truncated-bit.bin",
                lambda damaged: struct.pack_into("<I", damaged, cursor, 3),
            )
            with self.assertRaisesRegex(ValidationError, "bit is invalid"):
                validate_phase3_exact_binary(truncated, truncated_descriptor)

            oversized, oversized_descriptor = publish_mutation(
                "oversized-bit.bin",
                lambda damaged: struct.pack_into("<I", damaged, cursor, 1025),
            )
            with self.assertRaisesRegex(ValidationError, "at most 8192 bits"):
                validate_phase3_exact_binary(oversized, oversized_descriptor)

    def test_resealed_huge_population_counts_fail_before_allocation(self):
        contract = _contract("huge_manifest_counts")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.bin"
            descriptor = write_exact_fixture(
                source, contract, ["fpga0", "fpga1"]
            )
            encoded = source.read_bytes()
            magic, version, manifest_offset, manifest_length = struct.unpack_from(
                "<8sIQQ", encoded
            )
            manifest = json.loads(
                encoded[
                    manifest_offset : manifest_offset + manifest_length
                ].decode("utf-8")
            )
            huge = 2**63

            def reseal(name, mutate):
                changed = copy.deepcopy(manifest)
                mutate(changed)
                manifest_bytes = json.dumps(
                    changed,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                payload = bytearray(encoded[:manifest_offset])
                payload.extend(manifest_bytes)
                struct.pack_into(
                    "<8sIQQ",
                    payload,
                    0,
                    magic,
                    version,
                    manifest_offset,
                    len(manifest_bytes),
                )
                path = root / name
                path.write_bytes(payload)
                changed_descriptor = {
                    **descriptor,
                    "bytes": len(payload),
                    "manifest_sha256": hashlib.sha256(
                        manifest_bytes
                    ).hexdigest(),
                    "counts": changed["counts"],
                }
                return path, changed_descriptor

            mutations = {
                "cut-count": lambda value: value["counts"].__setitem__(
                    "cut_nodes", huge
                ),
                "source-count": lambda value: value["counts"].__setitem__(
                    "source_segments", huge
                ),
                "segment-count": lambda value: value["counts"].__setitem__(
                    "logic_segments", huge
                ),
                "consumer-count": lambda value: value["counts"].__setitem__(
                    "consumer_segments", huge
                ),
                "capture-count": lambda value: value["counts"].__setitem__(
                    "capture_requirements", huge
                ),
                "capture-section-records": lambda value: (
                    value["counts"].__setitem__("capture_requirements", huge),
                    value["sections"]["captures:1"].__setitem__(
                        "records", huge
                    ),
                ),
            }
            for label, mutate in mutations.items():
                with self.subTest(label=label):
                    path, changed_descriptor = reseal(
                        f"{label}.bin", mutate
                    )
                    with self.assertRaisesRegex(
                        ValidationError, "count|population"
                    ):
                        validate_phase3_exact_binary(
                            path, changed_descriptor
                        )


    def test_native_canonical_hash_matches_python_for_unicode_flags_and_wide_bits(self):
        executable = emuflow_native()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def string(value):
                raw = value.encode("utf-8")
                return struct.pack("<I", len(raw)) + raw
            def artifact(path, magic, version, records):
                offsets = [0]
                for record in records:
                    offsets.append(offsets[-1] + len(record))
                path.write_bytes(struct.pack("<QIIQ", magic, version, len(records), offsets[-1]) +
                    b"".join(struct.pack("<Q", offset) for offset in offsets) + b"".join(records))
            for flag, bit in ((1, 0), (2, 255), (1, 256), (2, 1 << 130), (1, 1 << 8191)):
                with self.subTest(flag=flag, bits=bit.bit_length()):
                    contract = _contract("unicode\x00\b\f\n\r\t\"\\\x7fα😀")
                    contract["extra_header"] = {"values": [1, True, False, None, {}, [], {"z": "α😀", "float": -0.0}], "n": 1.25e-30}
                    contract["capture_requirements"][0].update(endpoint="state\x00\"\\\nα😀", port="D\tα😀", bit=bit)
                    contract["cut_nodes"][1]["capture_segment_ids"].append("segment000003")
                    contract["capture_requirements"].append({
                        "id": "capture000001", "cut_net": "net1", "fpga": "fpga1",
                        "kind": "top-output", "endpoint": "top:outα😀[0]",
                    })
                    contract["logic_segments"].append({
                        "id": "segment000003", "kind": "rx_to_capture", "fpga": "fpga1",
                        "source_cut_net": "net1", "capture_requirement": "capture000001",
                    })
                    contract["metrics"].update(logic_segments=4, capture_requirements=2)
                    if flag == 2:
                        contract["logic_segments"][0]["source_semantics"] = "configuration-stable-constant"
                    expected = semantic_contract_sha256(contract)
                    nodes_path, captures_path = root / "nodes.bin", root / "captures.bin"
                    node_records = []
                    for index, node in enumerate(contract["cut_nodes"]):
                        lists = [node[key] for key in ("sink_fpgas", "predecessor_cut_nets", "source_segment_ids", "capture_segment_ids")]
                        row = string(node["net"]) + string(node["cut_class"]) + string(node["source_fpgas"][0])
                        row += struct.pack("<6I", node["dependency_level"], node["combinational_dependency_depth"], *(len(values) for values in lists))
                        row += b"".join(string(value) for values in lists for value in values)
                        coordinates = [next(i for i, candidate in enumerate(contract["cut_nodes"]) if candidate["net"] == net) for net in node["predecessor_cut_nets"]]
                        node_records.append(row + b"".join(struct.pack("<I", value) for value in coordinates) + bytes([flag if index == 0 else 0]))
                    capture_records = []
                    for row in contract['capture_requirements']:
                        kind = int(row['kind'] == 'architectural-state')
                        bit_value = row.get('bit', 0)
                        fields = [row['cut_net'].encode(), row['fpga'].encode(), row['endpoint'].encode(),
                                  row.get('port', '').encode(),
                                  bit_value.to_bytes(max(1, (bit_value.bit_length()+7)//8), 'big') if kind else b'']
                        capture_records.append(struct.pack('<B5I', kind, *(len(value) for value in fields)) + b''.join(fields))
                    artifact(nodes_path, 0x314E4553464D5545, 2, node_records)
                    artifact(captures_path, 0x31434553464D5545, 1, capture_records)
                    with (
                        closing(
                            read_native_static_exact_cut_node_stream(nodes_path)
                        ) as nodes,
                        closing(
                            read_native_static_exact_capture_stream(captures_path)
                        ) as native_captures,
                    ):
                        streamed = {
                            **contract,
                            "cut_nodes": nodes,
                            "capture_requirements": native_captures,
                        }
                        streamed.update(semantic_arrays(nodes, native_captures, contract['metrics'], 1))
                        self.assertEqual(native_hash(nodes_path, captures_path, streamed), expected)
                        streamed["invalid_header"] = {1: "not-a-string-key"}
                        with self.assertRaises(ValidationError):
                            native_hash(nodes_path, captures_path, streamed)
                        del streamed["invalid_header"]
                        expected_path, actual_path = (
                            root / "expected.bin",
                            root / "native.bin",
                        )
                        expected_descriptor = write_exact_fixture(
                            expected_path,
                            contract,
                            ["fpga0", "fpga1"],
                            semantic_digest=expected,
                        )

                        def native_command(phase, operation):
                            return [str(executable), phase, operation]

                        with patch('emuflow.common.native_tools.resolve_native_stage_command', side_effect=native_command):
                            actual_descriptor = write_exact_fixture(
                                actual_path,
                                streamed,
                                ["fpga0", "fpga1"],
                                semantic_digest=expected,
                            )
                        self.assertEqual(actual_path.read_bytes(), expected_path.read_bytes())
                        self.assertEqual(actual_descriptor, expected_descriptor)
                        if bit < 2**32 - 1:
                            write_exact_fixture(
                                expected_path,
                                contract,
                                ["fpga0", "fpga1"],
                                semantic_digest=expected,
                                _format_version=1,
                            )
                            with patch(
                                "emuflow.common.native_tools.resolve_native_stage_command",
                                side_effect=native_command,
                            ):
                                write_exact_fixture(
                                    actual_path,
                                    streamed,
                                    ["fpga0", "fpga1"],
                                    semantic_digest=expected,
                                    _format_version=1,
                                )
                            self.assertEqual(
                                actual_path.read_bytes(), expected_path.read_bytes()
                            )
                        else:
                            actual_path.write_bytes(b"previous-generation")
                            with (
                                patch(
                                    "emuflow.common.native_tools.resolve_native_stage_command",
                                    side_effect=native_command,
                                ),
                                self.assertRaises(ValidationError),
                            ):
                                write_exact_fixture(
                                    actual_path,
                                    streamed,
                                    ["fpga0", "fpga1"],
                                    semantic_digest=expected,
                                    _format_version=1,
                                )
                            self.assertEqual(
                                actual_path.read_bytes(), b"previous-generation"
                            )
                        broken = root / "broken-nodes.bin"
                        broken.write_bytes(nodes_path.read_bytes()[:-1])
                        nodes.native_semantic_artifact_path = broken
                        with self.assertRaises(ValidationError):
                            native_hash(broken, captures_path, streamed)
            # Independent command-envelope rejection must preserve the previous
            # output and cannot silently fall back to the Python hasher.
            tags = [("capture_requirements", 1), ("cut_nodes", 2), ("dependency_edges", 3), ("logic_segments", 4)]
            good_request = struct.pack("<QII", 0x31484553464D5545, 1, 4) + b"".join(string(key) + bytes([tag]) for key, tag in tags)
            request, output = root / "request.bin", root / "digest.txt"
            for payload in (good_request[:-1], good_request + b"extra", struct.pack("<QII", 0x31484553464D5545, 1, 0)):
                request.write_bytes(payload)
                output.write_bytes(b"previous-generation")
                result = subprocess.run([str(executable), "phase3", "hash-semantics", str(nodes_path), str(captures_path), str(request), str(output)], capture_output=True, text=True, timeout=15)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.read_bytes(), b"previous-generation")

            section_request = struct.pack("<QIII", 0x31574553464D5545, 1, 2, 2)
            section_request += string("fpga0") + string("fpga1") + string(str(root / "staged-exact.bin"))
            bad_node_path = root / "bad-coordinate-node.bin"
            bad_node = bytearray(nodes_path.read_bytes())
            struct.pack_into("<I", bad_node, len(bad_node) - 5, 1)
            bad_node_path.write_bytes(bad_node)
            for payload, node_path in ((section_request[:-1], nodes_path),
                                       (section_request + b"extra", nodes_path),
                                       (section_request, bad_node_path)):
                request.write_bytes(payload)
                output.write_bytes(b"previous-generation")
                result = subprocess.run([str(executable), "phase3", "write-exact-sections", str(node_path), str(captures_path), str(request), str(output)], capture_output=True, text=True, timeout=15)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.read_bytes(), b"previous-generation")


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
            descriptor = write_exact_fixture(
                source, contract, ["fpga0", "fpga1"]
            )
            relocated = root / "relocated" / "assignment.exact.bin"
            relocated.parent.mkdir()
            shutil.copyfile(source, relocated)
            self.assertEqual(
                exact_contract_facts(
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
            first_descriptor = write_exact_fixture(
                first_path, first, ["fpga0", "fpga1"]
            )
            second_descriptor = write_exact_fixture(
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
            descriptor = write_exact_fixture(
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
