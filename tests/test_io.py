import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.architecture import ArchitectureDB
from emuflow.io import file_sha256, json_write_policy, read_json, write_json
from emuflow.managed_json_storage import pack_managed_json


class JsonIoTest(unittest.TestCase):
    def test_gzip_json_round_trip_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "document.json.gz"
            value = {"schema": "example/v1", "items": list(range(128))}
            write_json(path, value, compact=True)
            first = path.read_bytes()
            self.assertEqual(read_json(path), value)
            write_json(path, value, compact=True)
            self.assertEqual(path.read_bytes(), first)

    def test_hash_cache_reuses_only_unchanged_file_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "artifact.bin"
            output.write_bytes(b"first")
            with patch(
                "emuflow.io.hashlib.sha256", wraps=hashlib.sha256
            ) as sha256:
                first = file_sha256(output)
                self.assertEqual(file_sha256(output), first)
                self.assertEqual(sha256.call_count, 1)
                output.write_bytes(b"second-generation")
                self.assertNotEqual(file_sha256(output), first)
                self.assertEqual(sha256.call_count, 2)

    def test_architecture_load_reuses_unchanged_parsed_object(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "architecture.json"
            value = {
                "schema": "emuflow.archdb/v1",
                "part": "test-part",
                "source": {"format": "test/v1"},
                "policy": {"name": "test"},
                "sites": [{
                    "name": "SLICE_X0Y0", "type": "SLICEL", "x": 0, "y": 0,
                    "bels": [{
                        "name": "A6LUT", "type": "LUT6", "z": 0,
                        "compatible_cells": ["LUT6"],
                    }],
                }],
            }
            write_json(path, value, compact=True)
            first = ArchitectureDB.load(path)
            self.assertIs(ArchitectureDB.load(path), first)
            value["part"] = "changed-part"
            write_json(path, value, compact=True)
            changed = ArchitectureDB.load(path)
            self.assertIsNot(changed, first)
            self.assertEqual(changed.part, "changed-part")

    def test_managed_staging_skips_per_artifact_fsync(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "emuflow.io.os.fsync"
        ) as fsync:
            output = Path(temporary) / "managed.json"
            with json_write_policy(durable=False):
                write_json(output, {"schema": "example/v1"})
            fsync.assert_not_called()
            self.assertEqual(read_json(output), {"schema": "example/v1"})

    def test_managed_json_storage_is_transparent_and_compact(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "managed.json"
            value = {
                "schema": "example.large/v1",
                "records": [{"id": index, "text": "repeated" * 20} for index in range(500)],
            }
            write_json(output, pack_managed_json(value), compact=True)
            self.assertEqual(read_json(output), value)
            self.assertLess(output.stat().st_size, len(str(value)) // 5)

            packed = output.read_text(encoding="utf-8")
            output.write_text(packed.replace('"data":"', '"data":"!'), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "corrupt"):
                read_json(output)

    def test_compact_json_is_deterministic_and_round_trips(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "compact.json"
            value = {"z": [3, 2, 1], "a": {"spaced": "value"}}
            write_json(output, value, compact=True)
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                '{"a":{"spaced":"value"},"z":[3,2,1]}\n',
            )
            self.assertEqual(read_json(output), value)

    def test_write_json_replaces_only_after_complete_serialization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "artifact.json"
            write_json(output, {"generation": 1})

            with patch(
                "emuflow.io.json.dump",
                side_effect=RuntimeError("injected serialization failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    write_json(output, {"generation": 2})

            self.assertEqual(read_json(output), {"generation": 1})
            self.assertEqual(list(root.glob(".*.tmp")), [])

            write_json(output, {"generation": 3})
            self.assertEqual(read_json(output), {"generation": 3})


if __name__ == "__main__":
    unittest.main()
