import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from emuflow.emuir_storage import is_emuir_sqlite
from emuflow.ir import EmuIR
from emuflow.yosys import import_yosys_json


ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EmuIRStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ir = import_yosys_json(
            ROOT / "examples/yosys/counter.json",
            top="counter",
            clocks=["clk"],
        )

    def test_round_trip_is_exact_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = root / "first.emuir.sqlite"
            second = root / "second.emuir.sqlite"
            self.ir.save(first)
            self.ir.save(second)
            self.assertTrue(is_emuir_sqlite(first))
            self.assertEqual(EmuIR.load(first).to_dict(), self.ir.to_dict())
            self.assertEqual(_sha256(first), _sha256(second))

    def test_truncation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = Path(temporary_directory) / "design.emuir.sqlite"
            self.ir.save(artifact)
            raw = artifact.read_bytes()
            artifact.write_bytes(raw[: len(raw) // 2])
            with self.assertRaises((ValueError, sqlite3.DatabaseError)):
                EmuIR.load(artifact)

    def test_new_writer_rejects_monolithic_json_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact = Path(temporary_directory) / "design.emuir.json"
            with self.assertRaisesRegex(ValueError, "indexed"):
                self.ir.save(artifact)


if __name__ == "__main__":
    unittest.main()
