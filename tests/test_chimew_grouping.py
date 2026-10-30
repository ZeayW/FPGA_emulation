import copy
import gc
import hashlib
import io
import random
import shutil
import struct
import subprocess
import tempfile
import unittest
import warnings
import zlib
from pathlib import Path
from unittest import mock

from tests import chimew_grouping_fixture as grouping_module
from tests.chimew_grouping_fixture import (
    CHIMEW_CROSSING_PROVIDER,
    CHIMEW_CROSSING_SCHEMA,
    CHIMEW_GROUPING_PROVIDER,
    CHIMEW_SCHEDULE_RATIO_PROVIDER,
    CHIMEW_TIMING_GUARD_PROVIDER,
    _oracle_groups,
    build_chimew_initial_groups,
    materialize_chimew_schedule_ratios,
    validate_chimew_crossings,
)
from emuflow.common.errors import EmuFlowError, ValidationError
from tests.native_build import emuflow_native


ROOT = Path(__file__).resolve().parents[1]


class ChimewGroupingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.executable = emuflow_native()
        cls.command = [str(cls.executable), "phase6", "group-signals"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def setUp(self) -> None:
        self.schedule = {
            "design": "chimew_fixture",
            "platform": "two_fpga",
            "entries": [
                {
                    "id": f"s{index}",
                    "link": "link0",
                    "from": "fpga0",
                    "to": "fpga1",
                    "tdm_ratio": 3,
                }
                for index in range(5)
            ],
        }
        crossings = [
            ([0], [1], 9),
            ([0], [], 1),
            ([], [1], 8),
            ([1], [0], 6),
            ([1], [], 2),
        ]
        self.crossings = {
            "schema": CHIMEW_CROSSING_SCHEMA,
            "design": "chimew_fixture",
            "platform": "two_fpga",
            "provider": CHIMEW_CROSSING_PROVIDER,
            "slls_per_fpga": 2,
            "provenance": {
                "producer": "fixture-physical-router",
                "producer_version": "1",
                "routing_sha256": "a" * 64,
            },
            "metrics": {"signals": 5, "physical_sll_crossings": 7},
            "entries": [
                {
                    "schedule_entry": f"s{index}",
                    "source_slls": source,
                    "sink_slls": sink,
                    "encoding": encoding,
                }
                for index, (source, sink, encoding) in enumerate(crossings)
            ],
        }

    def test_algorithm1_native_and_independent_replay_agree(self) -> None:
        result = build_chimew_initial_groups(
            self.schedule, self.crossings, executable=str(self.executable)
        )
        self.assertEqual(result["provider"], CHIMEW_GROUPING_PROVIDER)
        self.assertEqual(result["status"], "standalone_paper_kernel")
        self.assertEqual(result["integration_status"], "not-a-phase6-pin-plan")
        self.assertEqual(result["metrics"]["groups"], 2)
        self.assertEqual(result["metrics"]["group_physical_sll_crossings"], 4)
        groups = {
            entry["schedule_entry"]: entry["group"]
            for entry in result["entries"]
        }
        self.assertEqual(groups["s0"], groups["s1"])
        self.assertEqual(groups["s0"], groups["s2"])
        self.assertEqual(groups["s3"], groups["s4"])
        self.assertNotEqual(groups["s0"], groups["s3"])

    def test_timing_guard_preserves_complete_frozen_lane(self) -> None:
        schedule = copy.deepcopy(self.schedule)
        for index, entry in enumerate(schedule["entries"]):
            entry["lane"] = index // 2
            entry["slot"] = index % 2
        result = build_chimew_initial_groups(
            schedule,
            self.crossings,
            executable=str(self.executable),
            protected_entries={"s0"},
        )
        groups = {item["schedule_entry"]: item for item in result["entries"]}
        self.assertEqual(groups["s0"]["group"], groups["s1"]["group"])
        self.assertEqual(groups["s0"]["timing_guard_lane"], groups["s1"]["timing_guard_lane"])
        self.assertNotIn("timing_guard_lane", groups["s2"])
        self.assertEqual(result["timing_guard"]["protected_lane_groups"], 1)
        self.assertEqual(result["timing_guard"]["protected_entries"], 2)

    def test_empty_timing_guard_keeps_legacy_assignment(self) -> None:
        legacy = build_chimew_initial_groups(
            self.schedule, self.crossings, executable=str(self.executable)
        )
        guarded_api = build_chimew_initial_groups(
            self.schedule,
            self.crossings,
            executable=str(self.executable),
            protected_entries=set(),
        )
        self.assertEqual(legacy, guarded_api)

    def test_guarded_mixed_ratio_domain_order_matches_native(self) -> None:
        schedule = copy.deepcopy(self.schedule)
        schedule["entries"][0].update({"lane": 0, "slot": 0, "tdm_ratio": 2})
        schedule["entries"][1].update({"lane": 0, "slot": 1, "tdm_ratio": 2})
        for index, entry in enumerate(schedule["entries"][2:], start=2):
            entry.update({"lane": index, "slot": 0})
        result = build_chimew_initial_groups(
            schedule,
            self.crossings,
            executable=str(self.executable),
            protected_entries={"s0"},
        )
        groups = {item["schedule_entry"]: item["group"] for item in result["entries"]}
        self.assertEqual(groups["s0"], groups["s1"])
        self.assertEqual(result["metrics"]["oracle_disagreements"], 0)

    def test_crossings_seal_timing_guard_for_pipeline_replay(self) -> None:
        schedule = copy.deepcopy(self.schedule)
        for index, entry in enumerate(schedule["entries"]):
            entry.update({"lane": index // 2, "slot": index % 2})
        crossings = copy.deepcopy(self.crossings)
        crossings["timing_guard"] = {
            "provider": CHIMEW_TIMING_GUARD_PROVIDER,
            "scope": "EmuFlow extension, not a Chimew paper claim",
            "source_sha256": "a" * 64,
            "maximum_weight": 10.0,
            "protected_entries": ["s0"],
        }
        implicit = build_chimew_initial_groups(
            schedule, crossings, executable=str(self.executable)
        )
        explicit = build_chimew_initial_groups(
            schedule,
            crossings,
            executable=str(self.executable),
            protected_entries={"s0"},
        )
        self.assertEqual(implicit, explicit)
        invalid = copy.deepcopy(crossings)
        invalid["timing_guard"]["protected_entries"] = ["s0", "s0"]
        with self.assertRaisesRegex(ValidationError, "entries are invalid"):
            build_chimew_initial_groups(
                schedule, invalid, executable=str(self.executable)
            )

        schedule["chimew_timing_guard"] = crossings["timing_guard"]
        with self.assertRaisesRegex(ValidationError, "belong to the crossing input"):
            build_chimew_initial_groups(schedule,crossings,executable=str(self.executable))

    def test_normalized_region_substitute_is_rejected(self) -> None:
        invalid = copy.deepcopy(self.crossings)
        invalid["provider"] = "openparf-lookahead-centroid-v1"
        with self.assertRaisesRegex(ValidationError, "physical SLL"):
            validate_chimew_crossings(self.schedule, invalid)

    def test_encoding_and_provenance_are_independently_checked(self) -> None:
        invalid_encoding = copy.deepcopy(self.crossings)
        invalid_encoding["entries"][0]["encoding"] ^= 1
        with self.assertRaisesRegex(ValidationError, "independently derived"):
            validate_chimew_crossings(self.schedule, invalid_encoding)
        invalid_digest = copy.deepcopy(self.crossings)
        invalid_digest["provenance"]["routing_sha256"] = "opaque"
        with self.assertRaisesRegex(ValidationError, "SHA-256"):
            validate_chimew_crossings(self.schedule, invalid_digest)

    def test_domains_and_ratios_never_share_a_group(self) -> None:
        schedule = copy.deepcopy(self.schedule)
        schedule["entries"][4]["from"] = "fpga1"
        schedule["entries"][4]["to"] = "fpga0"
        result = build_chimew_initial_groups(
            schedule, self.crossings, executable=str(self.executable)
        )
        groups = {item["schedule_entry"]: item["group"] for item in result["entries"]}
        self.assertNotIn(groups["s4"], {groups[f"s{index}"] for index in range(4)})

    def test_frozen_slots_never_collide_inside_a_group(self) -> None:
        schedule = copy.deepcopy(self.schedule)
        for index, entry in enumerate(schedule["entries"]):
            entry["slot"] = index % 2
        result = build_chimew_initial_groups(
            schedule, self.crossings, executable=str(self.executable)
        )
        slots_by_group = {}
        source = {entry["id"]: entry for entry in schedule["entries"]}
        for item in result["entries"]:
            slots = slots_by_group.setdefault(item["group"], set())
            slot = source[item["schedule_entry"]]["slot"]
            self.assertNotIn(slot, slots)
            slots.add(slot)
        self.assertEqual(result["metrics"]["groups"], 3)

    def test_random_small_inputs_preserve_capacity_and_coverage(self) -> None:
        randomizer = random.Random(17)
        count = 80
        schedule = {
            "design": "random",
            "platform": "two_fpga",
            "entries": [
                {
                    "id": f"r{index:03d}",
                    "link": "link0",
                    "from": "fpga0",
                    "to": "fpga1",
                    "tdm_ratio": 4,
                }
                for index in range(count)
            ],
        }
        raw = []
        crossings = 0
        for index in range(count):
            source = [bit for bit in range(4) if randomizer.randrange(3) == 0]
            sink = [bit for bit in range(4) if randomizer.randrange(3) == 0]
            encoding = sum(1 << bit for bit in source) | sum(
                1 << (4 + bit) for bit in sink
            )
            crossings += bin(encoding).count("1")
            raw.append(
                {
                    "schedule_entry": f"r{index:03d}",
                    "source_slls": source,
                    "sink_slls": sink,
                    "encoding": encoding,
                }
            )
        document = {
            "schema": CHIMEW_CROSSING_SCHEMA,
            "design": "random",
            "platform": "two_fpga",
            "provider": CHIMEW_CROSSING_PROVIDER,
            "slls_per_fpga": 4,
            "provenance": {
                "producer": "fixture-physical-router",
                "producer_version": "1",
                "routing_sha256": "b" * 64,
            },
            "metrics": {"signals": count, "physical_sll_crossings": crossings},
            "entries": raw,
        }
        result = build_chimew_initial_groups(
            schedule, document, executable=str(self.executable)
        )
        self.assertEqual(len(result["entries"]), count)
        sizes = {}
        for entry in result["entries"]:
            sizes[entry["group"]] = sizes.get(entry["group"], 0) + 1
        self.assertTrue(all(size <= 4 for size in sizes.values()))
        self.assertEqual(result["metrics"]["oracle_disagreements"], 0)

    def test_encoding_buckets_match_exhaustive_selection_trace(self) -> None:
        randomizer = random.Random(918)
        for count in range(1, 80):
            entries = [
                {
                    "id": f"e{index:03d}",
                    "link": f"l{index % 2}",
                    "from": "a",
                    "to": "b",
                    "tdm_ratio": 1 + index % 7,
                }
                for index in range(count)
            ]
            encodings = {
                entry["id"]: randomizer.randrange(64) for entry in entries
            }

            def exhaustive():
                from collections import Counter, defaultdict

                buckets = defaultdict(list)
                for index, entry in enumerate(entries):
                    buckets[
                        ((entry["link"], entry["from"], entry["to"]), entry["tdm_ratio"])
                    ].append(index)
                assignment = {}
                group_count = crossing_bits = 0
                for (_, ratio), indices in sorted(buckets.items()):
                    multiplicity = Counter(encodings[entries[index]["id"]] for index in indices)
                    remaining = sorted(
                        indices,
                        key=lambda index: (
                            -bin(encodings[entries[index]["id"]]).count("1"),
                            -encodings[entries[index]["id"]],
                            index,
                        ),
                    )
                    while remaining:
                        target = encodings[entries[remaining[0]]["id"]]
                        members = []
                        while remaining and len(members) < ratio:
                            def key(index):
                                encoding = encodings[entries[index]["id"]]
                                category = 0 if encoding == target else 1 if encoding | target == target else 2
                                return (
                                    category,
                                    bin(encoding ^ target).count("1") if category == 2 else 0,
                                    -bin(encoding).count("1"),
                                    multiplicity[encoding],
                                    -encoding,
                                    index,
                                )

                            selected = min(remaining, key=key)
                            encoding = encodings[entries[selected]["id"]]
                            target |= encoding
                            members.append(selected)
                            multiplicity[encoding] -= 1
                            remaining.remove(selected)
                        for index in members:
                            assignment[entries[index]["id"]] = group_count
                        crossing_bits += bin(target).count("1")
                        group_count += 1
                return assignment, group_count, crossing_bits

            self.assertEqual(_oracle_groups(entries, encodings), exhaustive())

    def test_large_repeated_encoding_bucket_is_scalable(self) -> None:
        count = 20_000
        schedule = {
            "design": "large-repeated-encoding",
            "platform": "two_fpga",
            "entries": [
                {
                    "id": f"s{index:05d}",
                    "link": "link0",
                    "from": "fpga0",
                    "to": "fpga1",
                    "tdm_ratio": 64,
                }
                for index in range(count)
            ],
        }
        crossings = {
            "schema": CHIMEW_CROSSING_SCHEMA,
            "design": schedule["design"],
            "platform": schedule["platform"],
            "provider": CHIMEW_CROSSING_PROVIDER,
            "slls_per_fpga": 2,
            "provenance": {
                "producer": "scale-fixture",
                "producer_version": "1",
                "routing_sha256": "c" * 64,
            },
            "metrics": {
                "signals": count,
                "physical_sll_crossings": count,
            },
            "entries": [
                {
                    "schedule_entry": entry["id"],
                    "source_slls": [index % 2],
                    "sink_slls": [],
                    "encoding": 1 << (index % 2),
                }
                for index, entry in enumerate(schedule["entries"])
            ],
        }
        result = build_chimew_initial_groups(
            schedule, crossings, executable=str(self.executable)
        )
        self.assertEqual(result["metrics"]["signals"], count)
        self.assertEqual(result["metrics"]["groups"], (count + 63) // 64)
        self.assertEqual(result["metrics"]["oracle_disagreements"], 0)




    def test_production_prefers_unified_grouping_entry(self) -> None:
        with mock.patch.object(
            grouping_module,
            "resolve_native_stage_command",
            return_value=[*self.command],
        ) as dispatch:
            result = build_chimew_initial_groups(
                self.schedule, self.crossings
            )
        self.assertEqual(result["metrics"]["oracle_disagreements"], 0)
        dispatch.assert_called_once_with(
            "phase6",
            "group-signals",
            explicit=None,
        )

    def test_typed_input_rejects_truncation_corruption_and_mixed_seals(self) -> None:
        entries = sorted(self.schedule["entries"], key=lambda entry: entry["id"])
        encodings = grouping_module.validate_chimew_crossings(
            self.schedule, self.crossings
        )
        guarded = grouping_module._guarded_lane_domains(entries, set())
        stream = io.BytesIO()
        typed_metadata = grouping_module._serialize_typed(
            entries, encodings, guarded, stream
        )
        valid = stream.getvalue()
        variants = {
            "truncated": valid[:-1],
            "bad-record-tag": valid[:56] + b"X" + valid[57:],
            # First record encoding begins after the 56-byte header, tag and
            # four int32 fields.  Keep the record parseable so CRC validation
            # is what rejects this mutation.
            "mutated-encoding": valid[:73] + bytes([valid[73] ^ 1]) + valid[74:],
            "mixed-source-seal": valid[:-32] + bytes.fromhex("ab" * 32),
            "trailing-byte": valid + b"X",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, payload in variants.items():
                with self.subTest(name=name):
                    input_path = root / f"{name}.bin"
                    input_path.write_bytes(payload)
                    completed = subprocess.run(
                        [
                            *self.command,
                            "--typed-input",
                            str(input_path),
                            "-",
                            typed_metadata["logical_sha256"],
                        ],
                        capture_output=True,
                        text=True,
                    )
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertIn("typed input", completed.stderr)

    def test_typed_input_rejects_oversize_count_and_protocol_mixing(self) -> None:
        entries = sorted(self.schedule["entries"], key=lambda entry: entry["id"])
        encodings = grouping_module.validate_chimew_crossings(
            self.schedule, self.crossings
        )
        guarded = grouping_module._guarded_lane_domains(entries, set())
        stream = io.BytesIO()
        typed_metadata = grouping_module._serialize_typed(
            entries, encodings, guarded, stream
        )
        valid = stream.getvalue()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            typed = root / "typed.bin"
            oversized = root / "oversized.bin"
            legacy = root / "legacy.txt"
            typed.write_bytes(valid)
            oversized.write_bytes(
                valid[:16] + struct.pack("<Q", (1 << 64) - 1) + valid[24:]
            )
            legacy.write_text("EMUFLOW_CHIMEW_GROUPER_INPUT_V2\nSIGNAL 0 0 1 0 1\n")
            commands = (
                [
                    *self.command,
                    "--typed-input",
                    str(oversized),
                    "-",
                    typed_metadata["logical_sha256"],
                ],
                [
                    *self.command,
                    "--typed-input",
                    str(legacy),
                    "-",
                    typed_metadata["logical_sha256"],
                ],
                [*self.command, "--legacy-text", str(typed), "-"],
                [*self.command, str(legacy), "-"],
            )
            for command in commands:
                completed = subprocess.run(
                    command, capture_output=True, text=True
                )
                self.assertNotEqual(completed.returncode, 0)
            self.assertIn(
                "typed input signal count",
                subprocess.run(
                    commands[0], capture_output=True, text=True
                ).stderr,
            )

    def test_typed_stream_rejects_source_mutation_between_passes(self) -> None:
        entries = sorted(self.schedule["entries"], key=lambda entry: entry["id"])
        encodings = grouping_module.validate_chimew_crossings(
            self.schedule, self.crossings
        )
        guarded = grouping_module._guarded_lane_domains(entries, set())
        logical_sha256 = grouping_module._typed_logical_sha256(
            entries, encodings, guarded
        )
        encodings[entries[0]["id"]] ^= 1
        stream = io.BytesIO()
        with self.assertRaisesRegex(ValidationError, "changed while streaming"):
            grouping_module._serialize_typed(
                entries,
                encodings,
                guarded,
                stream,
                logical_sha256=logical_sha256,
            )
        self.assertEqual(len(stream.getvalue()), 56 + len(entries) * 25)

    def test_typed_input_rejects_fully_resealed_payload_and_digest_relabel(self) -> None:
        entries = sorted(self.schedule["entries"], key=lambda entry: entry["id"])
        encodings = grouping_module.validate_chimew_crossings(
            self.schedule, self.crossings
        )
        guarded = grouping_module._guarded_lane_domains(entries, set())
        stream = io.BytesIO()
        metadata = grouping_module._serialize_typed(
            entries, encodings, guarded, stream
        )
        valid = stream.getvalue()
        record_end = 56 + len(entries) * 25
        records = bytearray(valid[56:record_end])
        records[17] ^= 1

        def reseal(record_bytes: bytes, digest: bytes) -> bytes:
            header = valid[:24] + digest
            checksum = zlib.crc32(header)
            checksum = zlib.crc32(record_bytes, checksum)
            return (
                header
                + record_bytes
                + grouping_module._TYPED_INPUT_END_MAGIC
                + struct.pack("<Q", checksum)
                + digest
            )

        payload_digest = hashlib.sha256(records).digest()
        relabel_digest = bytes.fromhex("ab" * 32)
        variants = {
            "payload-crc-resealed": (
                reseal(records, bytes.fromhex(metadata["logical_sha256"])),
                metadata["logical_sha256"],
                "seal disagrees",
            ),
            "payload-digest-crc-resealed": (
                reseal(records, payload_digest),
                metadata["logical_sha256"],
                "logical digest disagrees",
            ),
            # Even if the caller repeats the arbitrary relabel out of band,
            # the C++ SHA-256 replay over canonical record bytes disagrees.
            "arbitrary-digest-relabel": (
                reseal(valid[56:record_end], relabel_digest),
                relabel_digest.hex(),
                "seal disagrees",
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, (payload, expected_digest, expected_error) in variants.items():
                with self.subTest(name=name):
                    path = root / f"{name}.bin"
                    path.write_bytes(payload)
                    completed = subprocess.run(
                        [
                            *self.command,
                            "--typed-input",
                            str(path),
                            "-",
                            expected_digest,
                        ],
                        capture_output=True,
                        text=True,
                    )
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertIn(expected_error, completed.stderr)

    def test_native_failure_status_precedes_malformed_stdout_without_leaks(self) -> None:
        entries = sorted(self.schedule["entries"], key=lambda entry: entry["id"])
        encodings = grouping_module.validate_chimew_crossings(
            self.schedule, self.crossings
        )
        guarded = grouping_module._guarded_lane_domains(entries, set())
        scripts = {
            "immediate": (
                "import sys\n"
                "sys.stderr.write('intentional immediate rejection\\n')\n"
                "raise SystemExit(23)\n",
                "exit status 23: intentional immediate rejection",
            ),
            "consume-malformed": (
                "import sys\n"
                "sys.stdin.buffer.read()\n"
                "sys.stdout.write('MALFORMED\\n')\n"
                "sys.stdout.flush()\n"
                "sys.stderr.write('intentional malformed rejection\\n')\n"
                "raise SystemExit(24)\n",
                "exit status 24: intentional malformed rejection",
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, (source, expected) in scripts.items():
                with self.subTest(name=name):
                    executable = root / name
                    executable.write_text(
                        "#!/usr/bin/env python3\n" + source, encoding="utf-8"
                    )
                    executable.chmod(0o755)
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", ResourceWarning)
                        with self.assertRaisesRegex(EmuFlowError, expected):
                            grouping_module._run_native(
                                entries, encodings, guarded, str(executable)
                            )
                        gc.collect()

    def test_zero_exit_preserves_native_output_grammar_error(self) -> None:
        entries = sorted(self.schedule["entries"], key=lambda entry: entry["id"])
        encodings = grouping_module.validate_chimew_crossings(
            self.schedule, self.crossings
        )
        guarded = grouping_module._guarded_lane_domains(entries, set())
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "malformed-success"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "sys.stdin.buffer.read()\n"
                "sys.stdout.write('MALFORMED\\n')\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            with self.assertRaisesRegex(EmuFlowError, "output header is invalid"):
                grouping_module._run_native(
                    entries, encodings, guarded, str(executable)
                )

    def test_native_i32_limits_fail_before_struct_packing(self) -> None:
        entries = [
            {
                "id": "overflow",
                "link": "link0",
                "from": "fpga0",
                "to": "fpga1",
                "tdm_ratio": 1 << 31,
            }
        ]
        guarded = grouping_module._guarded_lane_domains(entries, set())
        with self.assertRaisesRegex(ValidationError, "32-bit limit"):
            grouping_module._serialize_typed(
                entries, {"overflow": 1}, guarded, io.BytesIO()
            )

    def test_lane_occupancy_materializes_explicit_adapter_ratios(self) -> None:
        schedule = {
            "schema": "emuflow.tdm-schedule/v1",
            "design": "implicit-ratios",
            "platform": "two_fpga",
            "entries": [
                {"id": "a0", "link": "l", "from": "A", "to": "B", "lane": 0, "slot": 0},
                {"id": "a1", "link": "l", "from": "A", "to": "B", "lane": 0, "slot": 2},
                {"id": "a2", "link": "l", "from": "A", "to": "B", "lane": 1, "slot": 0},
                {"id": "b0", "link": "l", "from": "B", "to": "A", "lane": 0, "slot": 0},
            ],
        }
        result = materialize_chimew_schedule_ratios(schedule)
        ratios = {entry["id"]: entry["tdm_ratio"] for entry in result["entries"]}
        self.assertEqual(ratios, {"a0": 2, "a1": 2, "a2": 1, "b0": 1})
        self.assertEqual(
            result["chimew_ratio_materialization"]["provider"],
            CHIMEW_SCHEDULE_RATIO_PROVIDER,
        )
        self.assertEqual(
            result["chimew_ratio_materialization"]["direction_lane_groups"], 3
        )
        self.assertNotIn("tdm_ratio", schedule["entries"][0])
        self.assertEqual(result, materialize_chimew_schedule_ratios(schedule))

    def test_ratio_materialization_rejects_ambiguous_inputs(self) -> None:
        schedule = {
            "entries": [
                {"id": "a0", "link": "l", "from": "A", "to": "B", "lane": 0, "slot": 0},
                {"id": "a1", "link": "l", "from": "A", "to": "B", "lane": 0, "slot": 0},
            ]
        }
        with self.assertRaisesRegex(ValidationError, "collision"):
            materialize_chimew_schedule_ratios(schedule)
        schedule["entries"][1]["slot"] = 1
        schedule["entries"][0]["tdm_ratio"] = 2
        with self.assertRaisesRegex(ValidationError, "mixes"):
            materialize_chimew_schedule_ratios(schedule)


if __name__ == "__main__":
    unittest.main()
