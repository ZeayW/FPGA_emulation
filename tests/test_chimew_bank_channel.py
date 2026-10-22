import copy
import hashlib
import io
import json
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from emuflow import chimew_bank_channel as bank_channel_module
from emuflow import chimew_bank_channel_storage as storage_module
from emuflow import chimew_bank_channel_report_storage as report_storage_module
from emuflow import chimew_qualification as qualification_module
from emuflow.chimew_bank_channel import (
    CHIMEW_BANK_CHANNEL_INPUT_PROVIDER,
    CHIMEW_BANK_CHANNEL_INPUT_SCHEMA,
    CHIMEW_BANK_CHANNEL_INPUT_SCHEMA_V1,
    CHIMEW_BANK_CHANNEL_PROVIDER,
    _verify_certificate,
    _verify_stage2_certificate,
    evaluate_chimew_bank_channel_assignment,
    validate_chimew_bank_channel_input,
)
from emuflow.chimew_bank_channel_storage import (
    BANK_CHANNEL_APPLICATION_ID,
    BANK_CHANNEL_STORAGE_SCHEMA,
    BANK_CHANNEL_USER_VERSION,
    BankChannelInputView,
    canonical_bank_channel_input_sha256,
    copy_chimew_bank_channel_storage,
    read_chimew_bank_channel_input,
    write_chimew_bank_channel_storage,
)
from emuflow.chimew_bank_channel_report_storage import (
    BankChannelReportView,
    canonical_bank_channel_report_sha256,
    read_chimew_bank_channel_report,
    write_chimew_bank_channel_report_storage,
)
from emuflow.errors import EmuFlowError, ValidationError
from emuflow.chimew_qualification import (
    validate_chimew_bank_channel_report_artifact,
)
from tests.native_build import emuflow_native


ROOT = Path(__file__).resolve().parents[1]


def member(identifier, source_y, sink_y, *, fanins=1, timing_weight=None):
    result = {
        "id": identifier,
        "fanout": {"x": 0.0, "y": source_y},
        "fanins": [
            {"x": 100.0, "y": sink_y + index} for index in range(fanins)
        ],
    }
    if timing_weight is not None:
        result["timing_weight"] = timing_weight
    return result


class ChimewBankChannelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        compiler = shutil.which("c++") or shutil.which("clang++")
        if compiler is None:
            raise RuntimeError("a C++17 compiler is required")
        cls.executable = Path(cls.temporary_directory.name) / "chimew-assignment"
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-pthread",
                str(ROOT / "src/native/chimew_bank_channel_assigner.cpp"),
                "-o",
                str(cls.executable),
            ],
            check=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def setUp(self) -> None:
        self.document = {
            "schema": CHIMEW_BANK_CHANNEL_INPUT_SCHEMA,
            "provider": CHIMEW_BANK_CHANNEL_INPUT_PROVIDER,
            "design": "two_stage_fixture",
            "platform": "two_fpga_fixture",
            "coordinate_system": "physical-site-xy",
            "cost_quantization_per_site": 1000,
            "provenance": {
                "producer": "fixture-lookahead-flow",
                "producer_version": "1",
                "grouping_sha256": "a" * 64,
                "placement_sha256": "b" * 64,
                "architecture_sha256": "c" * 64,
            },
            "domains": [{"id": "AB", "fpga_a": "A", "fpga_b": "B"}],
            "bank_pairs": [
                {
                    "id": "near",
                    "domain": "AB",
                    "bank_a": {"id": "A0", "x": 0.0, "y": 0.0},
                    "bank_b": {"id": "B0", "x": 100.0, "y": 0.0},
                    "channels": [
                        {
                            "id": "near0",
                            "order": 0,
                            "pin_a": {"x": 0.0, "y": 0.0},
                            "pin_b": {"x": 100.0, "y": 0.0},
                        },
                        {
                            "id": "near1",
                            "order": 1,
                            "pin_a": {"x": 0.0, "y": 20.0},
                            "pin_b": {"x": 100.0, "y": 20.0},
                        },
                        {
                            "id": "near2",
                            "order": 2,
                            "pin_a": {"x": 0.0, "y": 40.0},
                            "pin_b": {"x": 100.0, "y": 40.0},
                        },
                    ],
                },
                {
                    "id": "far",
                    "domain": "AB",
                    "bank_a": {"id": "A1", "x": 0.0, "y": 100.0},
                    "bank_b": {"id": "B1", "x": 100.0, "y": 100.0},
                    "channels": [
                        {
                            "id": "far0",
                            "order": 0,
                            "pin_a": {"x": 0.0, "y": 100.0},
                            "pin_b": {"x": 100.0, "y": 100.0},
                        }
                    ],
                },
            ],
            "groups": [
                {
                    "id": "tdm_ab",
                    "domain": "AB",
                    "kind": "tdm_group",
                    "direction": "a_to_b",
                    "members": [member("s0", 0.0, 0.0)],
                },
                {
                    "id": "tdm_ba",
                    "domain": "AB",
                    "kind": "tdm_group",
                    "direction": "b_to_a",
                    "members": [
                        {
                            "id": "s1",
                            "fanout": {"x": 100.0, "y": 20.0},
                            "fanins": [{"x": 0.0, "y": 20.0}],
                        }
                    ],
                },
                {
                    "id": "common_near",
                    "domain": "AB",
                    "kind": "common_signal",
                    "direction": "a_to_b",
                    "members": [member("s2", 40.0, 40.0, fanins=2)],
                },
                {
                    "id": "common_far",
                    "domain": "AB",
                    "kind": "common_signal",
                    "direction": "b_to_a",
                    "members": [
                        {
                            "id": "s3",
                            "fanout": {"x": 100.0, "y": 100.0},
                            "fanins": [{"x": 0.0, "y": 100.0}],
                        }
                    ],
                },
            ],
            "metrics": {
                "groups": 4,
                "signals": 4,
                "fanins": 5,
                "bank_pairs": 2,
                "channels": 4,
            },
        }

    def test_two_stage_assignment_and_direction_priority_are_certified(self) -> None:
        result = evaluate_chimew_bank_channel_assignment(
            self.document, executable=str(self.executable)
        )
        self.assertEqual(result["provider"], CHIMEW_BANK_CHANNEL_PROVIDER)
        self.assertEqual(result["integration_status"], "not-a-phase6-pin-plan")
        self.assertEqual(result["metrics"]["certificate_disagreements"], 0)
        self.assertEqual(result["metrics"]["certified_matchings"], 5)
        assignments = {record["group"]: record for record in result["assignments"]}
        self.assertEqual(assignments["common_far"]["bank_pair"], "far")
        self.assertEqual(assignments["tdm_ab"]["channel"], "near0")
        self.assertEqual(assignments["tdm_ba"]["channel"], "near1")
        self.assertEqual(assignments["common_near"]["channel"], "near2")
        self.assertEqual(result["direction_priority"]["near"], "a_to_b")

    def test_indexed_input_is_lazy_and_assignment_is_differential(self) -> None:
        legacy = evaluate_chimew_bank_channel_assignment(
            self.document, executable=str(self.executable)
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selector = root / "bank_channel_input.json"
            view = write_chimew_bank_channel_storage(selector, self.document)
            self.assertIsInstance(view, BankChannelInputView)
            self.assertNotIsInstance(view["groups"], list)
            self.assertEqual(view.materialize(), self.document)
            self.assertEqual(
                canonical_bank_channel_input_sha256(view),
                canonical_bank_channel_input_sha256(self.document),
            )
            indexed = evaluate_chimew_bank_channel_assignment(
                view, executable=str(self.executable)
            )
            self.assertEqual(indexed, legacy)

            copied = root / "renamed-input.json"
            copy_chimew_bank_channel_storage(selector, copied)
            copied_view = read_chimew_bank_channel_input(copied)
            self.assertIsInstance(copied_view, BankChannelInputView)
            self.assertEqual(copied_view.materialize(), self.document)
            copied_view.close()
            old_cursor = view["groups"]
            view.close()
            with self.assertRaisesRegex(ValidationError, "view is closed"):
                next(iter(old_cursor))

    def test_indexed_input_rejects_corruption_and_semantic_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selector = root / "bank_channel_input.json"
            view = write_chimew_bank_channel_storage(selector, self.document)
            original_store = view.store_path
            view.close()
            corrupted = bytearray(original_store.read_bytes())
            corrupted[-1] ^= 1
            original_store.write_bytes(corrupted)
            with self.assertRaisesRegex(ValidationError, "store seal"):
                read_chimew_bank_channel_input(selector)

            semantic_selector = root / "semantic.json"
            view = write_chimew_bank_channel_storage(
                semantic_selector, self.document
            )
            view.close()
            connection = sqlite3.connect(view.store_path)
            bad_group = copy.deepcopy(self.document["groups"][0])
            bad_group["members"] = []
            malformed_document = copy.deepcopy(self.document)
            malformed_document["groups"][0] = bad_group
            malformed_group_digest = hashlib.sha256()
            for group in malformed_document["groups"]:
                malformed_group_digest.update(
                    json.dumps(
                        group,
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                )
                malformed_group_digest.update(b"\n")
            malformed_logical_digest = hashlib.sha256(
                json.dumps(
                    malformed_document,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            connection.execute(
                "UPDATE groups SET record_json=? WHERE seq=0",
                (
                    json.dumps(
                        bad_group,
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )
            connection.execute(
                "UPDATE metadata SET value=? WHERE key='groups_sha256'",
                (json.dumps(malformed_group_digest.hexdigest()),),
            )
            connection.execute(
                "UPDATE metadata SET value=? WHERE key='logical_sha256'",
                (json.dumps(malformed_logical_digest),),
            )
            connection.commit()
            connection.close()
            digest = hashlib.sha256(view.store_path.read_bytes()).hexdigest()
            replacement = semantic_selector.with_name(
                f"{semantic_selector.stem}.{digest}.sqlite"
            )
            view.store_path.rename(replacement)
            manifest = json.loads(semantic_selector.read_text())
            manifest["artifact"] = replacement.name
            manifest["store_sha256"] = digest
            manifest["store_bytes"] = replacement.stat().st_size
            manifest["groups"]["sha256"] = malformed_group_digest.hexdigest()
            manifest["logical_sha256"] = malformed_logical_digest
            semantic_selector.write_text(json.dumps(manifest), encoding="utf-8")
            tampered = read_chimew_bank_channel_input(semantic_selector)
            with self.assertRaisesRegex(ValidationError, "has no signals"):
                validate_chimew_bank_channel_input(tampered)

            seal_selector = root / "seal.json"
            sealed = write_chimew_bank_channel_storage(
                seal_selector, self.document
            )
            sealed.close()
            connection = sqlite3.connect(sealed.store_path)
            changed_group = copy.deepcopy(self.document["groups"][0])
            changed_group["members"][0]["fanout"]["y"] += 1.0
            connection.execute(
                "UPDATE groups SET record_json=? WHERE seq=0",
                (
                    json.dumps(
                        changed_group,
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )
            connection.commit()
            connection.close()
            seal_digest = hashlib.sha256(
                sealed.store_path.read_bytes()
            ).hexdigest()
            seal_replacement = seal_selector.with_name(
                f"{seal_selector.stem}.{seal_digest}.sqlite"
            )
            sealed.store_path.rename(seal_replacement)
            seal_manifest = json.loads(seal_selector.read_text())
            seal_manifest["artifact"] = seal_replacement.name
            seal_manifest["store_sha256"] = seal_digest
            seal_manifest["store_bytes"] = seal_replacement.stat().st_size
            seal_selector.write_text(
                json.dumps(seal_manifest), encoding="utf-8"
            )
            stale_seal = read_chimew_bank_channel_input(seal_selector)
            with self.assertRaisesRegex(ValidationError, "logical record seal"):
                validate_chimew_bank_channel_input(stale_seal)

            fake = root / "fake.json"
            fake_store = root / "fake.sqlite"
            connection = sqlite3.connect(fake_store)
            connection.execute(f"PRAGMA application_id={BANK_CHANNEL_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={BANK_CHANNEL_USER_VERSION}")
            connection.commit()
            connection.close()
            fake_digest = hashlib.sha256(fake_store.read_bytes()).hexdigest()
            renamed_fake = root / f"fake.{fake_digest}.sqlite"
            fake_store.rename(renamed_fake)
            fake.write_text(
                json.dumps(
                    {
                        "schema": BANK_CHANNEL_STORAGE_SCHEMA,
                        "logical_schema": self.document["schema"],
                        "artifact": renamed_fake.name,
                        "application_id": BANK_CHANNEL_APPLICATION_ID,
                        "user_version": BANK_CHANNEL_USER_VERSION,
                        "store_bytes": renamed_fake.stat().st_size,
                        "store_sha256": fake_digest,
                        "header": {},
                        "problem_header": {},
                        "groups": {"records": 1, "sha256": "0" * 64},
                        "logical_sha256": "0" * 64,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValidationError):
                read_chimew_bank_channel_input(fake)

    def test_indexed_input_failed_replacement_restores_previous_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selector = Path(temporary) / "bank_channel_input.json"
            previous = write_chimew_bank_channel_storage(selector, self.document)
            selector_bytes = selector.read_bytes()
            store_bytes = previous.store_path.read_bytes()
            previous.close()
            changed = copy.deepcopy(self.document)
            changed["provenance"]["producer_version"] = "2"
            with mock.patch(
                "emuflow.chimew_bank_channel_storage._open_validated_view",
                side_effect=ValidationError("injected post-publish failure"),
            ):
                with self.assertRaisesRegex(ValidationError, "injected"):
                    write_chimew_bank_channel_storage(selector, changed)
            self.assertEqual(selector.read_bytes(), selector_bytes)
            restored = read_chimew_bank_channel_input(selector)
            self.assertEqual(restored.store_path.read_bytes(), store_bytes)
            self.assertEqual(restored.materialize(), self.document)
            restored_store = restored.store_path
            restored.close()
            replacement = write_chimew_bank_channel_storage(selector, changed)
            self.assertNotEqual(replacement.store_path, restored_store)
            self.assertFalse(restored_store.exists())
            self.assertEqual(
                replacement["provenance"]["producer_version"], "2"
            )

    def test_indexed_input_rejects_unbound_headers_and_schema_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            legacy = root / "legacy.json"
            legacy.write_text(json.dumps(self.document), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "allow_legacy"):
                read_chimew_bank_channel_input(legacy)
            self.assertEqual(
                read_chimew_bank_channel_input(legacy, allow_legacy=True),
                self.document,
            )

            selector = root / "unknown-selector.json"
            write_chimew_bank_channel_storage(selector, self.document).close()
            manifest = json.loads(selector.read_text())
            manifest["unknown"] = "not-permitted"
            selector.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "selector is malformed"):
                read_chimew_bank_channel_input(selector)

            selector = root / "schema-binding.json"
            write_chimew_bank_channel_storage(selector, self.document).close()
            manifest = json.loads(selector.read_text())
            manifest["logical_schema"] = CHIMEW_BANK_CHANNEL_INPUT_SCHEMA_V1
            selector.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "selector is malformed"):
                read_chimew_bank_channel_input(selector)

            selector = root / "relation-extension.json"
            write_chimew_bank_channel_storage(selector, self.document).close()
            manifest = json.loads(selector.read_text())
            manifest["groups"]["unknown"] = 1
            selector.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "group seal"):
                read_chimew_bank_channel_input(selector)

            selector = root / "problem-header.json"
            view = write_chimew_bank_channel_storage(selector, self.document)
            store = view.store_path
            view.close()
            manifest = json.loads(selector.read_text())
            forged = copy.deepcopy(manifest["problem_header"])
            forged["design"] = "forged-design"
            connection = sqlite3.connect(store)
            connection.execute(
                "UPDATE metadata SET value=? WHERE key='problem_header'",
                (
                    json.dumps(
                        forged,
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )
            connection.commit()
            connection.close()
            digest = hashlib.sha256(store.read_bytes()).hexdigest()
            replacement = selector.with_name(f"{selector.stem}.{digest}.sqlite")
            store.rename(replacement)
            manifest["problem_header"] = forged
            manifest["artifact"] = replacement.name
            manifest["store_sha256"] = digest
            manifest["store_bytes"] = replacement.stat().st_size
            selector.write_text(json.dumps(manifest), encoding="utf-8")
            attacked = read_chimew_bank_channel_input(selector)
            with self.assertRaisesRegex(ValidationError, "sealed logical header"):
                validate_chimew_bank_channel_input(attacked)
            attacked.close()

            selector = root / "bank-identity.json"
            view = write_chimew_bank_channel_storage(selector, self.document)
            store = view.store_path
            view.close()
            connection = sqlite3.connect(store)
            connection.execute(
                "UPDATE banks SET bank_id='forged-bank-id' WHERE seq=0"
            )
            connection.commit()
            connection.close()
            manifest = json.loads(selector.read_text())
            digest = hashlib.sha256(store.read_bytes()).hexdigest()
            replacement = selector.with_name(f"{selector.stem}.{digest}.sqlite")
            store.rename(replacement)
            manifest["artifact"] = replacement.name
            manifest["store_sha256"] = digest
            manifest["store_bytes"] = replacement.stat().st_size
            selector.write_text(json.dumps(manifest), encoding="utf-8")
            attacked = read_chimew_bank_channel_input(selector)
            with self.assertRaisesRegex(ValidationError, "bank identity"):
                next(iter(attacked["bank_pairs"]))
            with self.assertRaisesRegex(ValidationError, "bank identity"):
                attacked["bank_pairs"][0]
            with self.assertRaisesRegex(ValidationError, "bank identity"):
                validate_chimew_bank_channel_input(attacked)
            attacked.close()

            selector = root / "extra-table.json"
            view = write_chimew_bank_channel_storage(selector, self.document)
            store = view.store_path
            view.close()
            connection = sqlite3.connect(store)
            connection.execute("CREATE TABLE attacker(value TEXT)")
            connection.commit()
            connection.close()
            manifest = json.loads(selector.read_text())
            digest = hashlib.sha256(store.read_bytes()).hexdigest()
            replacement = selector.with_name(f"{selector.stem}.{digest}.sqlite")
            store.rename(replacement)
            manifest["artifact"] = replacement.name
            manifest["store_sha256"] = digest
            manifest["store_bytes"] = replacement.stat().st_size
            selector.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "SQLite schema"):
                read_chimew_bank_channel_input(selector)

            selector = root / "symlink.json"
            view = write_chimew_bank_channel_storage(selector, self.document)
            store = view.store_path
            view.close()
            escaped = root / "escaped.sqlite"
            store.rename(escaped)
            store.symlink_to(escaped)
            with self.assertRaisesRegex(ValidationError, "store seal"):
                read_chimew_bank_channel_input(selector)

    def test_writer_waits_for_old_view_before_sidecar_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selector = Path(temporary) / "bank_channel_input.json"
            old_view = write_chimew_bank_channel_storage(selector, self.document)
            old_store = old_view.store_path
            changed = copy.deepcopy(self.document)
            changed["provenance"]["producer_version"] = "replacement"
            entered_writer = threading.Event()
            completed = threading.Event()
            failures = []
            real_writer = storage_module._write_chimew_bank_channel_storage_locked

            def instrumented_writer(path, document):
                entered_writer.set()
                return real_writer(path, document)

            def replace() -> None:
                try:
                    replacement = write_chimew_bank_channel_storage(
                        selector, changed
                    )
                    replacement.close()
                except BaseException as error:
                    failures.append(error)
                finally:
                    completed.set()

            with mock.patch(
                "emuflow.chimew_bank_channel_storage."
                "_write_chimew_bank_channel_storage_locked",
                side_effect=instrumented_writer,
            ):
                thread = threading.Thread(target=replace)
                thread.start()
                self.assertFalse(entered_writer.wait(0.2))
                self.assertTrue(old_store.is_file())
                old_view.close()
                self.assertTrue(entered_writer.wait(2.0))
                self.assertTrue(completed.wait(5.0))
                thread.join()
            self.assertEqual(failures, [])
            self.assertFalse(old_store.exists())

    def test_fanin_cost_is_averaged_per_signal(self) -> None:
        result = evaluate_chimew_bank_channel_assignment(
            self.document, executable=str(self.executable)
        )
        common = next(
            record for record in result["assignments"] if record["group"] == "common_near"
        )
        self.assertEqual(common["channel_cost"], 0.5)

    def test_bidirectional_tdm_bundle_uses_member_specific_costs(self) -> None:
        document = copy.deepcopy(self.document)
        document["groups"] = [
            {
                "id": "shared_bundle",
                "domain": "AB",
                "kind": "tdm_group",
                "direction": "bidirectional",
                "members": [
                    {
                        **member("forward", 0.0, 0.0),
                        "direction": "a_to_b",
                    },
                    {
                        "id": "reverse",
                        "direction": "b_to_a",
                        "fanout": {"x": 100.0, "y": 20.0},
                        "fanins": [{"x": 0.0, "y": 20.0}],
                    },
                ],
            }
        ]
        document["metrics"] = {
            "groups": 1,
            "signals": 2,
            "fanins": 2,
            "bank_pairs": 2,
            "channels": 4,
            "bidirectional_bundles": 1,
        }
        report = evaluate_chimew_bank_channel_assignment(
            document, executable=str(self.executable)
        )
        self.assertEqual(report["metrics"]["bidirectional_bundles"], 1)
        self.assertEqual(len(report["assignments"]), 1)
        self.assertEqual(
            report,
            evaluate_chimew_bank_channel_assignment(
                document, executable=str(self.executable)
            ),
        )

        malformed = copy.deepcopy(document)
        malformed["groups"][0]["members"][1]["direction"] = "a_to_b"
        with self.assertRaisesRegex(ValidationError, "both directions"):
            validate_chimew_bank_channel_input(malformed)

    def test_legacy_v1_input_remains_supported(self) -> None:
        document = copy.deepcopy(self.document)
        document["schema"] = CHIMEW_BANK_CHANNEL_INPUT_SCHEMA_V1
        validated = validate_chimew_bank_channel_input(document)
        self.assertEqual(validated["metrics"]["bidirectional_bundles"], 0)
        self.assertEqual(
            evaluate_chimew_bank_channel_assignment(
                document, executable=str(self.executable)
            )["metrics"]["groups"],
            len(document["groups"]),
        )

    def test_dedicated_direction_bank_uses_its_full_lane_inventory(self) -> None:
        document = copy.deepcopy(self.document)
        document["bank_pairs"] = [document["bank_pairs"][0]]
        document["groups"] = [
            {
                "id": "high_y_ab",
                "domain": "AB",
                "kind": "tdm_group",
                "direction": "a_to_b",
                "members": [member("high_y", 40.0, 40.0)],
            }
        ]
        document["metrics"] = {
            "groups": 1,
            "signals": 1,
            "fanins": 1,
            "bank_pairs": 1,
            "channels": 3,
        }
        result = evaluate_chimew_bank_channel_assignment(
            document, executable=str(self.executable)
        )
        self.assertEqual(result["assignments"][0]["channel"], "near2")
        self.assertEqual(result["assignments"][0]["channel_cost"], 0.0)

    def test_unique_bank_candidate_fast_path_is_certified(self) -> None:
        document = copy.deepcopy(self.document)
        document["domains"].append(
            {"id": "CD", "fpga_a": "C", "fpga_b": "D"}
        )
        document["bank_pairs"][1]["domain"] = "CD"
        document["groups"][3]["domain"] = "CD"
        result = evaluate_chimew_bank_channel_assignment(
            document, executable=str(self.executable)
        )
        assignments = {
            record["group"]: record for record in result["assignments"]
        }
        self.assertEqual(assignments["tdm_ab"]["bank_pair"], "near")
        self.assertEqual(assignments["common_far"]["bank_pair"], "far")
        self.assertEqual(result["metrics"]["certificate_disagreements"], 0)

    def test_parallel_banks_are_deterministic_and_certified(self) -> None:
        document = copy.deepcopy(self.document)
        document["domains"].append(
            {"id": "CD", "fpga_a": "C", "fpga_b": "D"}
        )
        document["bank_pairs"][1]["domain"] = "CD"
        document["groups"][3]["domain"] = "CD"
        with mock.patch.dict(
            "os.environ", {"EMUFLOW_CHIMEW_BANK_WORKERS": "1"}
        ):
            serial = evaluate_chimew_bank_channel_assignment(
                document, executable=str(self.executable)
            )
        with mock.patch.dict(
            "os.environ", {"EMUFLOW_CHIMEW_BANK_WORKERS": "4"}
        ):
            parallel = evaluate_chimew_bank_channel_assignment(
                document, executable=str(self.executable)
            )
        self.assertEqual(parallel, serial)
        self.assertEqual(parallel["metrics"]["certificate_disagreements"], 0)

    def test_identical_channel_cost_rows_are_exactly_certified(self) -> None:
        document = copy.deepcopy(self.document)
        document["bank_pairs"] = [copy.deepcopy(document["bank_pairs"][0])]
        document["bank_pairs"][0]["channels"] = [
            {
                "id": f"near{channel}",
                "order": channel,
                "pin_a": {"x": 0.0, "y": float(channel)},
                "pin_b": {"x": 100.0, "y": float(channel)},
            }
            for channel in range(16)
        ]
        document["groups"] = [
            {
                "id": f"common{group}",
                "domain": "AB",
                "kind": "common_signal",
                "direction": "a_to_b",
                "members": [member(f"signal{group}", 8.0, 8.0)],
            }
            for group in range(16)
        ]
        document["metrics"] = {
            "groups": 16,
            "signals": 16,
            "fanins": 16,
            "bank_pairs": 1,
            "channels": 16,
        }
        with mock.patch.object(
            bank_channel_module,
            "_candidate_cost",
            wraps=bank_channel_module._candidate_cost,
        ) as candidate_cost:
            result = evaluate_chimew_bank_channel_assignment(
                document, executable=str(self.executable)
            )
        self.assertEqual(len(result["assignments"]), 16)
        self.assertEqual(
            len({record["channel"] for record in result["assignments"]}), 16
        )
        self.assertEqual(result["metrics"]["certificate_disagreements"], 0)
        self.assertEqual(result["metrics"]["certified_matchings"], 3)
        self.assertLess(candidate_cost.call_count, 100)

    def test_invalid_parallel_bank_worker_override_is_rejected(self) -> None:
        with mock.patch.dict(
            "os.environ", {"EMUFLOW_CHIMEW_BANK_WORKERS": "invalid"}
        ):
            with self.assertRaisesRegex(EmuFlowError, "must be an integer"):
                evaluate_chimew_bank_channel_assignment(
                    self.document, executable=str(self.executable)
                )

    def test_normalized_coordinates_and_bad_provenance_are_rejected(self) -> None:
        normalized = copy.deepcopy(self.document)
        normalized["coordinate_system"] = "normalized-xy"
        with self.assertRaisesRegex(ValidationError, "normalized"):
            validate_chimew_bank_channel_input(normalized)
        opaque = copy.deepcopy(self.document)
        opaque["provenance"]["architecture_sha256"] = "opaque"
        with self.assertRaisesRegex(ValidationError, "SHA-256"):
            validate_chimew_bank_channel_input(opaque)

    def test_capacity_and_common_signal_contracts_are_hard(self) -> None:
        insufficient = copy.deepcopy(self.document)
        insufficient["bank_pairs"][0]["channels"] = insufficient["bank_pairs"][0][
            "channels"
        ][:2]
        insufficient["metrics"]["channels"] = 3
        with self.assertRaisesRegex(ValidationError, "insufficient"):
            validate_chimew_bank_channel_input(insufficient)
        nonsingleton = copy.deepcopy(self.document)
        nonsingleton["groups"][2]["members"].append(member("extra", 40.0, 40.0))
        with self.assertRaisesRegex(ValidationError, "singleton"):
            validate_chimew_bank_channel_input(nonsingleton)

    def test_residual_dual_tampering_is_rejected(self) -> None:
        with self.assertRaisesRegex(EmuFlowError, "negative reduced cost"):
            _verify_certificate(
                1,
                [1],
                1,
                [(0, 0, 5)],
                {0: (0, 5)},
                [0, 0, 0, 0],
                5,
            )

    def test_compact_repeated_row_certificate_rejects_tampering(self) -> None:
        bank = {
            "channels": [
                {"pin_a": (0.0, 0.0), "pin_b": (100.0, 0.0)},
                {"pin_a": (0.0, 20.0), "pin_b": (100.0, 20.0)},
            ]
        }
        group = {
            "kind": 1,
            "direction": 0,
            "members": [
                {
                    "fanout": (0.0, 0.0),
                    "fanins": [(100.0, 0.0)],
                }
            ],
        }
        with self.assertRaisesRegex(EmuFlowError, "negative reduced cost"):
            _verify_stage2_certificate(
                bank,
                [group],
                [0],
                0,
                {0: (0, 0)},
                [0, 0, 0, 100_000, 100_000],
                0,
                1000,
            )

    def test_200_group_sparse_certificate_regression(self) -> None:
        document = copy.deepcopy(self.document)
        document["bank_pairs"] = []
        document["groups"] = []
        for bank_index in range(4):
            y = float(bank_index * 100)
            document["bank_pairs"].append(
                {
                    "id": f"bank{bank_index}",
                    "domain": "AB",
                    "bank_a": {"id": f"A{bank_index}", "x": 0.0, "y": y},
                    "bank_b": {"id": f"B{bank_index}", "x": 100.0, "y": y},
                    "channels": [
                        {
                            "id": f"bank{bank_index}_channel{channel}",
                            "order": channel,
                            "pin_a": {"x": 0.0, "y": y + channel / 100.0},
                            "pin_b": {"x": 100.0, "y": y + channel / 100.0},
                        }
                        for channel in range(50)
                    ],
                }
            )
            for signal in range(50):
                document["groups"].append(
                    {
                        "id": f"g{bank_index}_{signal}",
                        "domain": "AB",
                        "kind": "common_signal",
                        "direction": "a_to_b" if signal % 2 == 0 else "b_to_a",
                        "members": [
                            member(
                                f"s{bank_index}_{signal}",
                                y + signal / 100.0,
                                y + signal / 100.0,
                            )
                        ],
                    }
                )
        document["metrics"] = {
            "groups": 200,
            "signals": 200,
            "fanins": 200,
            "bank_pairs": 4,
            "channels": 200,
        }
        result = evaluate_chimew_bank_channel_assignment(
            document, executable=str(self.executable)
        )
        self.assertEqual(len(result["assignments"]), 200)
        self.assertEqual(len({row["channel"] for row in result["assignments"]}), 200)
        self.assertEqual(result["metrics"]["certified_matchings"], 9)

    def test_timing_weight_prioritizes_the_critical_signal(self) -> None:
        document = copy.deepcopy(self.document)
        document["bank_pairs"] = [document["bank_pairs"][0]]
        document["bank_pairs"][0]["channels"] = [
            {
                "id": "low",
                "order": 0,
                "pin_a": {"x": 0.0, "y": 0.0},
                "pin_b": {"x": 100.0, "y": 0.0},
            },
            {
                "id": "high",
                "order": 1,
                "pin_a": {"x": 0.0, "y": 100.0},
                "pin_b": {"x": 100.0, "y": 100.0},
            },
        ]
        document["groups"] = [
            {
                "id": "noncritical",
                "domain": "AB",
                "kind": "tdm_group",
                "direction": "a_to_b",
                "members": [member("n", 0.0, 0.0)],
            },
            {
                "id": "critical",
                "domain": "AB",
                "kind": "tdm_group",
                "direction": "a_to_b",
                "members": [member("c", 10.0, 10.0, timing_weight=10.0)],
            },
        ]
        document["metrics"] = {
            "groups": 2,
            "signals": 2,
            "fanins": 2,
            "bank_pairs": 1,
            "channels": 2,
        }
        result = evaluate_chimew_bank_channel_assignment(
            document, executable=str(self.executable)
        )
        assignment = {
            record["group"]: record["channel"]
            for record in result["assignments"]
        }
        self.assertEqual(assignment["critical"], "low")
        self.assertEqual(assignment["noncritical"], "high")

    def test_indexed_report_preserves_the_logical_assignment_contract(self) -> None:
        legacy = evaluate_chimew_bank_channel_assignment(
            self.document, executable=str(self.executable)
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            indexed = evaluate_chimew_bank_channel_assignment(
                self.document,
                executable=str(self.executable),
                output_path=path,
            )
            self.assertIsInstance(indexed, BankChannelReportView)
            self.assertEqual(indexed.materialize(), legacy)
            self.assertEqual(
                canonical_bank_channel_report_sha256(indexed),
                canonical_bank_channel_report_sha256(legacy),
            )
            self.assertLess(path.stat().st_size, 2048)
            indexed.close()
            with self.assertRaisesRegex(ValidationError, "allow_legacy"):
                legacy_path = Path(temporary) / "legacy.json"
                legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
                read_chimew_bank_channel_report(legacy_path)
            self.assertEqual(
                read_chimew_bank_channel_report(
                    legacy_path, allow_legacy=True
                ),
                legacy,
            )

    def test_native_stdout_certificate_matches_file_protocol(self) -> None:
        problem = validate_chimew_bank_channel_input(self.document)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.txt"
            output_path = root / "output.txt"
            bank_channel_module._serialize(problem, input_path)
            subprocess.run(
                [
                    str(self.executable),
                    "--legacy-text",
                    str(input_path),
                    str(output_path),
                ],
                check=True,
            )
            streamed = subprocess.run(
                [str(self.executable), "--legacy-text", str(input_path), "-"],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertEqual(streamed.stderr, "")
            self.assertEqual(streamed.stdout, output_path.read_text())

    def test_typed_native_stream_matches_explicit_legacy_protocol(self) -> None:
        problem = validate_chimew_bank_channel_input(self.document)
        logical_sha256 = canonical_bank_channel_input_sha256(self.document)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy_input = root / "legacy.txt"
            typed_input = root / "typed.bin"
            legacy_output = root / "legacy.out"
            typed_output = root / "typed.out"
            bank_channel_module._serialize(problem, legacy_input)
            with typed_input.open("wb") as stream:
                bank_channel_module._serialize_typed(
                    problem, stream, logical_sha256
                )
            subprocess.run(
                [
                    str(self.executable),
                    "--legacy-text",
                    str(legacy_input),
                    str(legacy_output),
                ],
                check=True,
            )
            subprocess.run(
                [
                    str(self.executable),
                    "--typed-input",
                    str(typed_input),
                    str(typed_output),
                ],
                check=True,
            )
            self.assertEqual(typed_output.read_bytes(), legacy_output.read_bytes())
            self.assertGreater(typed_input.stat().st_size, 0)

    def test_unified_native_entry_matches_phase6_assigner(self) -> None:
        problem = validate_chimew_bank_channel_input(self.document)
        logical_sha256 = canonical_bank_channel_input_sha256(self.document)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            typed_input = root / "typed.bin"
            legacy_output = root / "legacy.out"
            unified_output = root / "unified.out"
            with typed_input.open("wb") as stream:
                bank_channel_module._serialize_typed(
                    problem, stream, logical_sha256
                )
            subprocess.run(
                [
                    str(self.executable),
                    "--typed-input",
                    str(typed_input),
                    str(legacy_output),
                ],
                check=True,
            )
            subprocess.run(
                [
                    str(emuflow_native()),
                    "phase6",
                    "assign-banks",
                    "--typed-input",
                    str(typed_input),
                    str(unified_output),
                ],
                check=True,
            )
            self.assertEqual(
                unified_output.read_bytes(), legacy_output.read_bytes()
            )

    def test_production_native_invocation_does_not_write_legacy_input(self) -> None:
        with mock.patch.object(
            bank_channel_module,
            "_serialize",
            side_effect=AssertionError("legacy text producer was called"),
        ):
            result = evaluate_chimew_bank_channel_assignment(
                self.document, executable=str(self.executable)
            )
        self.assertEqual(result["metrics"]["certificate_disagreements"], 0)

    def test_production_prefers_unified_phase6_entry(self) -> None:
        problem = validate_chimew_bank_channel_input(self.document)
        digest = canonical_bank_channel_input_sha256(self.document)
        with mock.patch.object(
            bank_channel_module,
            "resolve_native_stage_command",
            return_value=[str(self.executable)],
        ) as dispatch:
            with bank_channel_module._run_native(
                problem, None, logical_sha256=digest
            ):
                pass
        dispatch.assert_called_once_with(
            "emuflow_chimew_bank_channel_assigner",
            "phase6",
            "assign-banks",
            explicit=None,
        )

    def test_typed_input_rejects_truncation_corruption_and_mixed_seals(self) -> None:
        problem = validate_chimew_bank_channel_input(self.document)
        digest = canonical_bank_channel_input_sha256(self.document)
        stream = io.BytesIO()
        bank_channel_module._serialize_typed(problem, stream, digest)
        valid = stream.getvalue()
        changed = copy.deepcopy(self.document)
        changed["design"] = "different-generation"
        other_digest = canonical_bank_channel_input_sha256(changed)
        variants = {
            "truncated": valid[:-1],
            # The cost scale is a semantically active header field at bytes
            # 56..63.  Keep it finite/positive so only the stream seal rejects
            # the mutation.
            "mutated-cost-scale": (
                valid[:56] + struct.pack("<d", 2000.0) + valid[64:]
            ),
            "bad-record-tag": valid[:96] + b"X" + valid[97:],
            "mixed-source-seal": valid[:-32] + bytes.fromhex(other_digest),
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
                            str(self.executable),
                            "--typed-input",
                            str(input_path),
                            "-",
                        ],
                        capture_output=True,
                        text=True,
                    )
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertIn("typed input", completed.stderr)
                    if name == "mutated-cost-scale":
                        self.assertIn("seal disagrees", completed.stderr)

    def test_typed_input_rejects_oversize_header_counts(self) -> None:
        problem = validate_chimew_bank_channel_input(self.document)
        digest = canonical_bank_channel_input_sha256(self.document)
        stream = io.BytesIO()
        bank_channel_module._serialize_typed(problem, stream, digest)
        valid = stream.getvalue()
        # bank_count begins at byte 16.  UINT64_MAX is rejected before any
        # allocation or record parsing, regardless of the now-stale seal.
        oversized = valid[:16] + struct.pack("<Q", (1 << 64) - 1) + valid[24:]
        with tempfile.TemporaryDirectory() as temporary:
            input_path = Path(temporary) / "oversize.bin"
            input_path.write_bytes(oversized)
            completed = subprocess.run(
                [str(self.executable), "--typed-input", str(input_path), "-"],
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("typed input bank count", completed.stderr)

    def test_early_native_exit_preserves_status_and_stderr(self) -> None:
        document = copy.deepcopy(self.document)
        document["groups"][0]["members"][0]["fanins"] = [
            {"x": 100.0, "y": float(index)} for index in range(100_000)
        ]
        document["metrics"]["fanins"] = 100_004
        problem = validate_chimew_bank_channel_input(document)
        digest = canonical_bank_channel_input_sha256(document)
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "reject-input"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "sys.stderr.write('intentional typed-input rejection\\n')\n"
                "raise SystemExit(23)\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            with self.assertRaisesRegex(
                EmuFlowError,
                "exit status 23: intentional typed-input rejection",
            ):
                bank_channel_module._run_native(
                    problem, str(executable), logical_sha256=digest
                )

    def test_typed_input_mode_rejects_legacy_text_and_vice_versa(self) -> None:
        problem = validate_chimew_bank_channel_input(self.document)
        digest = canonical_bank_channel_input_sha256(self.document)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "legacy.txt"
            typed = root / "typed.bin"
            bank_channel_module._serialize(problem, legacy)
            with typed.open("wb") as stream:
                bank_channel_module._serialize_typed(problem, stream, digest)
            for command in (
                [str(self.executable), "--typed-input", str(legacy), "-"],
                [str(self.executable), "--legacy-text", str(typed), "-"],
                [str(self.executable), str(legacy), "-"],
            ):
                completed = subprocess.run(
                    command, capture_output=True, text=True
                )
                self.assertNotEqual(completed.returncode, 0)

    def test_streamed_certificate_protocol_rejects_out_of_order_sections(self) -> None:
        problem = validate_chimew_bank_channel_input(self.document)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.txt"
            output_path = root / "output.txt"
            bank_channel_module._serialize(problem, input_path)
            subprocess.run(
                [
                    str(self.executable),
                    "--legacy-text",
                    str(input_path),
                    str(output_path),
                ],
                check=True,
            )
            lines = output_path.read_text(encoding="utf-8").splitlines()
            metric_at_end = [lines[0], *lines[2:], lines[1]]
            first_certificate = next(
                index for index, line in enumerate(lines) if line.startswith("CERT ")
            )
            bank_after_certificate = list(lines)
            bank_record = bank_after_certificate.pop(2)
            bank_after_certificate.insert(first_certificate, bank_record)
            interleaved_potential = list(lines)
            interleaved_potential.insert(first_certificate + 2, lines[1])
            for name, malformed in (
                ("metric-at-end", metric_at_end),
                ("bank-after-certificate", bank_after_certificate),
                ("stage1-potential-interleaved", interleaved_potential),
            ):
                with self.subTest(name=name):
                    executable = root / name
                    executable.write_text(
                        "#!/usr/bin/env python3\n"
                        "import sys\n"
                        f"sys.stdout.write({repr(chr(10).join(malformed) + chr(10))})\n",
                        encoding="utf-8",
                    )
                    executable.chmod(0o755)
                    with self.assertRaisesRegex(
                        EmuFlowError, "output is malformed|assignment is incomplete"
                    ):
                        evaluate_chimew_bank_channel_assignment(
                            self.document, executable=str(executable)
                        )

    def test_streamed_certificate_rejects_wrong_sizes_at_each_header(self) -> None:
        problem = validate_chimew_bank_channel_input(self.document)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.txt"
            output_path = root / "output.txt"
            bank_channel_module._serialize(problem, input_path)
            subprocess.run(
                [
                    str(self.executable),
                    "--legacy-text",
                    str(input_path),
                    str(output_path),
                ],
                check=True,
            )
            lines = output_path.read_text(encoding="utf-8").splitlines()
            labels = ("STAGE1", next(
                line.split()[1]
                for line in lines
                if line.startswith("CERT BANK")
            ))
            for label in labels:
                certificate_index = next(
                    index
                    for index, line in enumerate(lines)
                    if line.startswith(f"CERT {label} ")
                )
                expected = int(lines[certificate_index].split()[2])
                for suffix, declared in (
                    ("truncated", expected - 1),
                    ("huge", 1_000_000_000),
                ):
                    with self.subTest(label=label, suffix=suffix):
                        malformed = list(lines)
                        malformed[certificate_index] = (
                            f"CERT {label} {declared}"
                        )
                        executable = root / f"{label}-{suffix}"
                        executable.write_text(
                            "#!/usr/bin/env python3\n"
                            "import sys\n"
                            f"sys.stdout.write({repr(chr(10).join(malformed) + chr(10))})\n",
                            encoding="utf-8",
                        )
                        executable.chmod(0o755)
                        with self.assertRaisesRegex(
                            EmuFlowError, "certificate dimensions are invalid"
                        ):
                            evaluate_chimew_bank_channel_assignment(
                                self.document, executable=str(executable)
                            )

    def test_semantic_channel_index_batches_one_large_bank(self) -> None:
        document = copy.deepcopy(self.document)
        document["bank_pairs"] = [copy.deepcopy(document["bank_pairs"][0])]
        document["bank_pairs"][0]["channels"] = [
            {
                "id": f"large{index}",
                "order": index,
                "pin_a": {"x": 0.0, "y": float(index)},
                "pin_b": {"x": 100.0, "y": float(index)},
            }
            for index in range(5001)
        ]
        document["groups"] = [
            {
                "id": "only",
                "domain": "AB",
                "kind": "tdm_group",
                "direction": "a_to_b",
                "members": [member("only", 0.0, 0.0)],
            }
        ]
        document["metrics"] = {
            "groups": 1,
            "signals": 1,
            "fanins": 1,
            "bank_pairs": 1,
            "channels": 5001,
        }
        report = evaluate_chimew_bank_channel_assignment(
            document, executable=str(self.executable)
        )
        real_connect = sqlite3.connect
        channel_batches = []

        class ConnectionSpy:
            def __init__(self, connection):
                self.connection = connection

            def executemany(self, sql, rows):
                if "INSERT INTO channels" in sql:
                    channel_batches.append(len(rows))
                return self.connection.executemany(sql, rows)

            def __getattr__(self, name):
                return getattr(self.connection, name)

        with mock.patch.object(
            qualification_module.sqlite3,
            "connect",
            side_effect=lambda *args, **kwargs: ConnectionSpy(
                real_connect(*args, **kwargs)
            ),
        ):
            validate_chimew_bank_channel_report_artifact(document, report)
        self.assertEqual(channel_batches, [4096, 905])

    def test_indexed_report_rejects_resealed_malformed_store_and_symlink(self) -> None:
        report = evaluate_chimew_bank_channel_assignment(
            self.document, executable=str(self.executable)
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "report.json"
            view = write_chimew_bank_channel_report_storage(path, report)
            store = view.store_path
            view.close()
            with sqlite3.connect(store) as connection:
                connection.execute("PRAGMA application_id=1")
            digest = hashlib.sha256(store.read_bytes()).hexdigest()
            replacement = root / f"report.{digest}.sqlite"
            store.replace(replacement)
            selector = json.loads(path.read_text(encoding="utf-8"))
            selector["artifact"] = replacement.name
            selector["store_bytes"] = replacement.stat().st_size
            selector["store_sha256"] = digest
            path.write_text(json.dumps(selector), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "SQLite metadata disagrees"
            ):
                read_chimew_bank_channel_report(path)

            valid_path = root / "valid.json"
            write_chimew_bank_channel_report_storage(valid_path, report).close()
            symlink = root / "alias.json"
            symlink.symlink_to(valid_path.name)
            with self.assertRaisesRegex(ValidationError, "symlink"):
                read_chimew_bank_channel_report(symlink)

    def test_indexed_report_rejects_resealed_typed_row_corruption(self) -> None:
        report = evaluate_chimew_bank_channel_assignment(
            self.document, executable=str(self.executable)
        )
        for column, value, message in (
            ("bank_cost", "nan", "finite number"),
            ("channel_cost", "inf", "finite number"),
            ("bank_cost_rank", "not-an-integer", "non-negative integer"),
            ("bank_pair", sqlite3.Binary(b"wrong-type"), "non-empty string"),
        ):
            with self.subTest(column=column), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                path = root / "report.json"
                view = write_chimew_bank_channel_report_storage(path, report)
                store = view.store_path
                view.close()
                with sqlite3.connect(store) as connection:
                    connection.execute(
                        f"UPDATE assignments SET {column}=? WHERE seq=0", (value,)
                    )
                digest = hashlib.sha256(store.read_bytes()).hexdigest()
                replacement = root / f"report.{digest}.sqlite"
                store.replace(replacement)
                selector = json.loads(path.read_text(encoding="utf-8"))
                selector["artifact"] = replacement.name
                selector["store_bytes"] = replacement.stat().st_size
                selector["store_sha256"] = digest
                path.write_text(json.dumps(selector), encoding="utf-8")
                with self.assertRaisesRegex(ValidationError, message):
                    read_chimew_bank_channel_report(path)

    def test_invalid_old_selector_cannot_authorize_artifact_deletion(self) -> None:
        report = evaluate_chimew_bank_channel_assignment(
            self.document, executable=str(self.executable)
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "report.json"
            victim = root / "victim.sqlite"
            victim.write_bytes(b"must survive")
            path.write_text(
                json.dumps(
                    {
                        "schema": report_storage_module.REPORT_STORAGE_SCHEMA,
                        "artifact": victim.name,
                    }
                ),
                encoding="utf-8",
            )
            current = write_chimew_bank_channel_report_storage(path, report)
            current.close()
            self.assertEqual(victim.read_bytes(), b"must survive")

    def test_semantic_validator_rejects_header_metric_and_priority_relabelling(self) -> None:
        report = evaluate_chimew_bank_channel_assignment(
            self.document, executable=str(self.executable)
        )
        mutations = []
        changed = copy.deepcopy(report)
        changed["status"] = "pass"
        mutations.append(changed)
        changed = copy.deepcopy(report)
        changed["paper_scope"] = "unknown"
        mutations.append(changed)
        changed = copy.deepcopy(report)
        changed["metrics"]["unknown"] = 1
        mutations.append(changed)
        changed = copy.deepcopy(report)
        changed["metrics"]["certified_matchings"] = float(
            changed["metrics"]["certified_matchings"]
        )
        mutations.append(changed)
        changed = copy.deepcopy(report)
        changed["direction_priority"]["near"] = "b_to_a"
        mutations.append(changed)
        changed = copy.deepcopy(report)
        changed["direction_priority"]["renamed"] = changed[
            "direction_priority"
        ].pop("near")
        mutations.append(changed)
        for changed in mutations:
            with self.subTest(changed=changed):
                with self.assertRaises(ValidationError):
                    validate_chimew_bank_channel_report_artifact(
                        self.document, changed
                    )

        changed = copy.deepcopy(report)
        changed["assignments"][0]["group"] = "resealed-group-identity"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            typed = write_chimew_bank_channel_report_storage(path, changed)
            with typed:
                with self.assertRaisesRegex(ValidationError, "coverage"):
                    validate_chimew_bank_channel_report_artifact(
                        self.document, typed
                    )

    def test_report_reader_lease_blocks_replacement_and_old_store_gc(self) -> None:
        first = evaluate_chimew_bank_channel_assignment(
            self.document, executable=str(self.executable)
        )
        second = copy.deepcopy(first)
        second["status"] = "replacement"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            old = write_chimew_bank_channel_report_storage(path, first)
            old_store = old.store_path
            started = threading.Event()
            completed = threading.Event()
            failures = []

            def replace() -> None:
                started.set()
                try:
                    write_chimew_bank_channel_report_storage(path, second).close()
                except BaseException as error:
                    failures.append(error)
                finally:
                    completed.set()

            thread = threading.Thread(target=replace)
            thread.start()
            self.assertTrue(started.wait(1.0))
            self.assertFalse(completed.wait(0.1))
            self.assertTrue(old_store.is_file())
            self.assertEqual(old["status"], first["status"])
            old.close()
            self.assertTrue(completed.wait(2.0))
            thread.join()
            self.assertEqual(failures, [])
            self.assertFalse(old_store.exists())
            current = read_chimew_bank_channel_report(path)
            self.assertEqual(current["status"], "replacement")
            current.close()

    def test_report_publication_restores_previous_selector_on_validation_failure(self) -> None:
        first = evaluate_chimew_bank_channel_assignment(
            self.document, executable=str(self.executable)
        )
        second = copy.deepcopy(first)
        second["status"] = "replacement"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            write_chimew_bank_channel_report_storage(path, first).close()
            original_selector = path.read_bytes()
            original_files = {item.name for item in path.parent.iterdir()}
            with mock.patch.object(
                report_storage_module,
                "BankChannelReportView",
                side_effect=ValidationError("injected report validation failure"),
            ):
                with self.assertRaisesRegex(ValidationError, "injected"):
                    write_chimew_bank_channel_report_storage(path, second)
            self.assertEqual(path.read_bytes(), original_selector)
            self.assertEqual(
                {item.name for item in path.parent.iterdir()}, original_files
            )


if __name__ == "__main__":
    unittest.main()
