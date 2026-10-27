import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from emuflow.errors import ValidationError
from emuflow.xilinx_netlist import open_xilinx_mapped_index
from emuflow import xilinx_openparf_atomic as adapter
from tests.native_build import emuflow_native


GOLDEN_NETS = """net n0 2
  a0 O
  a3 D
endnet
net n1 2
  a3 Q
  a1 A[1]
endnet
net n2 2
  a0 I0
  a2 CI
endnet
net n3 2
  a1 A[0]
  a3 C
endnet
"""
GOLDEN_LIBRARY = """CELL CARRY8
  PIN CI INPUT CAS
  PIN CO[0] OUTPUT
  PIN CO[1] OUTPUT
  PIN CO[2] OUTPUT
  PIN CO[3] OUTPUT
  PIN CO[4] OUTPUT
  PIN CO[5] OUTPUT
  PIN CO[6] OUTPUT
  PIN CO[7] OUTPUT CAS
END CELL

CELL DSP48E2
  PIN A[0] INPUT
  PIN A[1] INPUT
  PIN A[2] INPUT
  PIN P[0] OUTPUT
  PIN P[1] OUTPUT
END CELL

CELL FDRE
  PIN C INPUT CLOCK
  PIN CE INPUT CTRL_CE
  PIN D INPUT
  PIN Q OUTPUT
  PIN R INPUT CTRL_SR
END CELL

CELL LUT2
  PIN I0 INPUT
  PIN I1 INPUT
  PIN O OUTPUT
END CELL
"""
GOLDEN_MUX_LIBRARY = """
CELL MUXF7
  PIN I0 INPUT
  PIN I1 INPUT
  PIN O OUTPUT
  PIN S INPUT
END CELL

CELL MUXF8
  PIN I0 INPUT
  PIN I1 INPUT
  PIN O OUTPUT
  PIN S INPUT
END CELL

CELL MUXF9
  PIN I0 INPUT
  PIN I1 INPUT
  PIN O OUTPUT
  PIN S INPUT
END CELL
"""


def fixture(root):
    def cell(kind, connections, outputs):
        return {
            "type": kind, "hide_name": 0, "parameters": {}, "attributes": {},
            "connections": connections,
            "port_directions": {p: "output" if p in outputs else "input"
                                for p in connections},
        }
    # Insertion order deliberately differs from lexical atom order. Include
    # bus pins, constants, a no-driver net, singleton nets, and an omitted GND.
    cells = {
        "z_ff": cell("FDRE", {"C": [10], "CE": ["1"], "D": [2], "Q": [5],
                               "R": ["0"]}, {"Q"}),
        "c_carry": cell("CARRY8", {"CI": [8], "CO": list(range(20, 28))}, {"CO"}),
        "a_lut": cell("LUT2", {"I0": [8], "I1": ["x"], "O": [2]}, {"O"}),
        "gnd": cell("GND", {"G": [40]}, {"G"}),
        "b_dsp": cell("DSP48E2", {"A": [10, 5, "0"], "P": [27, 30]}, {"P"}),
    }
    # Carry CO[7] and DSP P[0] must not share a driven net.
    cells["b_dsp"]["connections"]["P"][0] = 31
    path = root / "mapped.sqlite"
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
          CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE cells(id TEXT PRIMARY KEY,seq INTEGER UNIQUE,type TEXT,record_json TEXT);
          CREATE TABLE cell_pins(cell_seq INTEGER,cell_id TEXT,port TEXT,port_bit INTEGER,
            port_width INTEGER,direction INTEGER,value_kind INTEGER,net_bit INTEGER,
            constant TEXT,PRIMARY KEY(cell_id,port,port_bit)) WITHOUT ROWID;
          CREATE TABLE netnames(id TEXT PRIMARY KEY,seq INTEGER,record_json TEXT);
        """)
        db.executemany("INSERT INTO metadata VALUES (?,?)", [
            ("schema", "emuflow.xilinx-mapped-index/v2"),
            ("top_json", json.dumps("top")), ("creator_json", json.dumps("fixture")),
            ("ports_json", "{}"),
        ])
        for seq, (name, value) in enumerate(cells.items()):
            db.execute("INSERT INTO cells VALUES (?,?,?,?)", (
                name, seq, value["type"], json.dumps({
                    k: v for k, v in value.items()
                    if k not in {"connections", "port_directions"}
                }),
            ))
            for port, values in value["connections"].items():
                for bit, net in enumerate(values):
                    is_net = isinstance(net, int)
                    db.execute("INSERT INTO cell_pins VALUES (?,?,?,?,?,?,?,?,?)", (
                        seq, name, port, bit, len(values),
                        int(value["port_directions"][port] == "output"),
                        0 if is_net else 1, net if is_net else None,
                        None if is_net else net,
                    ))
    atoms = adapter._IndexedAtomSequence()
    for name, value in cells.items():
        if value["type"] != "GND":
            atoms.append({"instance": name, "cell_type": value["type"],
                          "source_cluster": name, "resource": "fixture"})
    atoms.finalize()
    return cells, path, atoms


class NativeBookshelfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.command = [str(emuflow_native()), "phase7", "export-bookshelf"]

    def test_native_matches_complete_golden_and_does_not_reconstruct_cells(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cells, path, atoms = fixture(root)
            self.addCleanup(atoms.close)
            names = adapter._OrdinalSafeNames(atoms)
            expected = (GOLDEN_LIBRARY + GOLDEN_MUX_LIBRARY, None, 4, 10)
            with open_xilinx_mapped_index(path) as mapped, mock.patch.object(
                adapter, "resolve_native_stage_command", return_value=self.command
            ), mock.patch.object(
                adapter, "_ordered_cell_items", side_effect=AssertionError("Python cell traversal")
            ), mock.patch.object(
                adapter, "_expanded_pins", side_effect=AssertionError("Python pin traversal")
            ):
                output = root / "published" / "design.nets"
                actual = adapter._render_library_and_nets(
                    mapped.cells, atoms, names,
                    platform_mux_types=["MUXF7", "MUXF8", "MUXF9"], nets_path=output,
                )
            self.assertEqual(actual, expected)
            self.assertEqual(output.read_text(), GOLDEN_NETS)
            self.assertIn("PIN C INPUT CLOCK", actual[0])
            self.assertIn("PIN CO[7] OUTPUT CAS", actual[0])

    def test_invalid_relations_do_not_replace_published_nets(self):
        mutations = {
            "incomplete": "DELETE FROM cell_pins WHERE cell_id='b_dsp' AND port='A' AND port_bit=1",
            "inout": "UPDATE cell_pins SET direction=2 WHERE cell_id='z_ff' AND port='D'",
            "two_drivers": "UPDATE cell_pins SET net_bit=2 WHERE cell_id='z_ff' AND port='Q'",
            "ownership": "UPDATE cells SET type='LUT1' WHERE id='a_lut'",
            "orphan_pin": "UPDATE cell_pins SET cell_id='missing' WHERE cell_id='z_ff'",
            "constant_conflict": "UPDATE cell_pins SET constant='0' WHERE cell_id='z_ff' AND port='D'",
            "integer_type": "UPDATE cell_pins SET port_width='bad' WHERE cell_id='z_ff' AND port='D'",
            "null_net": "UPDATE cell_pins SET net_bit=NULL WHERE cell_id='z_ff' AND port='D'",
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _cells, path, atoms = fixture(root)
                try:
                    with closing(sqlite3.connect(path)) as db, db:
                        db.execute(mutation)
                    output = root / "design.nets"
                    output.write_text("previous publication\n")
                    with open_xilinx_mapped_index(path) as mapped, mock.patch.object(
                        adapter, "resolve_native_stage_command", return_value=self.command
                    ), self.assertRaises(ValidationError):
                        adapter._render_library_and_nets(
                            mapped.cells, atoms, adapter._OrdinalSafeNames(atoms), nets_path=output
                        )
                    self.assertEqual(output.read_text(), "previous publication\n")
                    self.assertEqual(list(root.glob(".emuflow-bookshelf-*")), [])
                finally:
                    atoms.close()

    def test_unified_publishes_report_and_preserves_source_with_special_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cells, path, atoms = fixture(root)
            self.addCleanup(atoms.close)
            expected = GOLDEN_NETS
            path = path.rename(root / "mapped ?#%中文.sqlite")
            snapshots = {path: path.read_bytes(), atoms.path: atoms.path.read_bytes()}
            nets, summary, scratch = [root / suffix for suffix in ("nets", "summary", "scratch")]
            report = root / "operation.json"
            result = subprocess.run([str(emuflow_native()), "--report", str(report), "phase7", "export-bookshelf",
                str(path), str(atoms.path), str(nets), str(summary), str(scratch)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(nets.read_text(), expected)
            self.assertFalse(scratch.exists())
            self.assertFalse(list(root.glob("*.tmp.*")))
            value = json.loads(report.read_text())
            self.assertEqual(value["stage"], "phase7.export-bookshelf")
            self.assertEqual(value["status"], "pass")
            self.assertEqual([item["path"] for item in value["inputs"]],
                             [str(path.resolve()), str(atoms.path.resolve())])
            self.assertEqual(len(value["outputs"]), 2)
            self.assertGreater(value["metrics"]["wall_seconds"], 0)
            for source, data in snapshots.items():
                self.assertEqual(source.read_bytes(), data)

    def test_production_selects_unified_only_install_without_python_pin_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cells, mapped_path, atoms = fixture(root)
            self.addCleanup(atoms.close)
            names = adapter._OrdinalSafeNames(atoms)
            expected = (GOLDEN_LIBRARY, None, 4, 10)
            runtime = root / "runtime"
            (runtime / "bin").mkdir(parents=True)
            (runtime / "bin/emuflow-native").symlink_to(emuflow_native())
            output = root / "design.nets"
            with open_xilinx_mapped_index(mapped_path) as mapped, mock.patch.dict(
                os.environ, {"EMUFLOW_NATIVE_ROOT": str(runtime)}
            ), mock.patch.object(adapter, "_ordered_cell_items", side_effect=AssertionError("Python cells")), \
                    mock.patch.object(adapter, "_expanded_pins", side_effect=AssertionError("Python pins")):
                actual = adapter._render_library_and_nets(mapped.cells, atoms, names, nets_path=output)
            self.assertEqual(actual, expected)
            self.assertEqual(output.read_text(), GOLDEN_NETS)

    def test_unified_rejects_aliases_and_owned_scratch_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _cells, mapped, atoms = fixture(root)
            self.addCleanup(atoms.close)
            nets, summary, scratch = [root / suffix for suffix in ("design.nets", "summary.tsv", "scratch.sqlite")]
            nets.write_text("previous nets")
            summary.write_text("previous summary")
            scratch.write_text("another invocation")
            snapshots = {p: p.read_bytes() for p in (mapped, atoms.path, nets, summary, scratch)}
            cases = [
                ([], [mapped, summary, root / "fresh.sqlite"]),
                ([], [nets, atoms.path, root / "fresh.sqlite"]),
                ([], [nets, summary, mapped]),
                ([], [nets, nets, root / "fresh.sqlite"]),
                ([], [nets, summary, scratch]),
                (["--report", str(mapped)], [nets, summary, root / "fresh.sqlite"]),
                (["--report", str(summary)], [nets, summary, root / "fresh.sqlite"]),
            ]
            alias = root / "parent-alias"
            alias.symlink_to(root, target_is_directory=True)
            cases.append(([], [nets, alias / nets.name, root / "fresh.sqlite"]))
            dangling = root / "dangling.sqlite"
            dangling.symlink_to(root / "unowned.sqlite")
            cases.append(([], [nets, summary, dangling]))
            hardlink = root / "mapped-alias.sqlite"
            os.link(mapped, hardlink)
            cases.append(([], [nets, hardlink, root / "fresh.sqlite"]))
            for options, destinations in cases:
                with self.subTest(destinations=destinations, options=options):
                    result = subprocess.run([str(emuflow_native()), *options, "phase7", "export-bookshelf",
                                             str(mapped), str(atoms.path), *map(str,destinations)],
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    for path, data in snapshots.items():
                        self.assertEqual(path.read_bytes(), data)
                    self.assertFalse((root / "fresh.sqlite").exists())
                    self.assertTrue(dangling.is_symlink())
                    self.assertFalse((root / "unowned.sqlite").exists())
                    self.assertFalse(list(root.glob("*.tmp.*")))

    def test_unified_rejects_unsupported_storage_before_publishing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _cells, mapped, atoms = fixture(root)
            self.addCleanup(atoms.close)
            with closing(sqlite3.connect(mapped)) as db, db:
                db.execute("PRAGMA user_version=99")
            result = subprocess.run([str(emuflow_native()), "phase7", "export-bookshelf",
                                     str(mapped), str(atoms.path), str(root / "nets"),
                                     str(root / "summary"), str(root / "scratch")],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('"invalid-artifact"', result.stderr)
            self.assertFalse(any((root / name).exists() for name in ("nets", "summary", "scratch")))
