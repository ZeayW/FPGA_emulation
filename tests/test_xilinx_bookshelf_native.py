import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from emuflow.errors import ValidationError
from emuflow.xilinx_netlist import open_xilinx_mapped_index
from emuflow import xilinx_openparf_atomic as adapter
from tests.native_build import xilinx_bookshelf_exporter, emuflow_native


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
    with sqlite3.connect(path) as db:
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
        cls.executable = xilinx_bookshelf_exporter()

    def test_native_matches_reference_and_does_not_reconstruct_cells(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cells, path, atoms = fixture(root)
            self.addCleanup(atoms.close)
            names = adapter._OrdinalSafeNames(atoms)
            expected = adapter._render_library_and_nets(
                cells, atoms, names, platform_mux_types=["MUXF7", "MUXF8", "MUXF9"]
            )
            with open_xilinx_mapped_index(path) as mapped, mock.patch.object(
                adapter, "resolve_native_stage_command", return_value=[str(self.executable)]
            ), mock.patch.object(
                adapter, "_ordered_cell_items", side_effect=AssertionError("Python cell traversal")
            ), mock.patch.object(
                adapter, "_expanded_pins", side_effect=AssertionError("Python pin traversal")
            ):
                actual = adapter._render_library_and_nets(
                    mapped.cells, atoms, names,
                    platform_mux_types=["MUXF7", "MUXF8", "MUXF9"],
                )
                output = root / "published" / "design.nets"
                streamed = adapter._render_library_and_nets(
                    mapped.cells, atoms, names,
                    platform_mux_types=["MUXF7", "MUXF8", "MUXF9"], nets_path=output,
                )
            self.assertEqual(actual, expected)
            self.assertEqual(streamed, (expected[0], None, expected[2], expected[3]))
            self.assertEqual(output.read_text(), expected[1])
            self.assertIn("PIN C INPUT CLOCK", actual[0])
            self.assertIn("PIN CO[7] OUTPUT CAS", actual[0])
            self.assertGreater(actual[3], 0)

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
                    with sqlite3.connect(path) as db:
                        db.execute(mutation)
                    output = root / "design.nets"
                    output.write_text("previous publication\n")
                    with open_xilinx_mapped_index(path) as mapped, mock.patch.object(
                        adapter, "resolve_native_stage_command", return_value=[str(self.executable)]
                    ), self.assertRaises(ValidationError):
                        adapter._render_library_and_nets(
                            mapped.cells, atoms, adapter._OrdinalSafeNames(atoms), nets_path=output
                        )
                    self.assertEqual(output.read_text(), "previous publication\n")
                    self.assertEqual(list(root.glob(".emuflow-bookshelf-*")), [])
                finally:
                    atoms.close()

    def test_unified_and_compatibility_commands_share_artifacts_and_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cells, path, atoms = fixture(root)
            self.addCleanup(atoms.close)
            names = adapter._OrdinalSafeNames(atoms)
            expected = adapter._render_library_and_nets(cells, atoms, names)
            path = path.rename(root / "mapped ?#%中文.sqlite")
            snapshots = {path: path.read_bytes(), atoms.path: atoms.path.read_bytes()}
            for index, command in enumerate([
                [str(self.executable)],
                [str(emuflow_native()), "phase7", "export-bookshelf"],
            ]):
                nets, summary, scratch = [root / f"{index}.{suffix}" for suffix in ("nets", "tsv", "sqlite")]
                report = root / "operation.json"
                if index:
                    command[1:1] = ["--report", str(report)]
                # Compatibility accepts SQLite's encoded URI; the production
                # command accepts plain paths and owns immutable reads itself.
                inputs = ([path.as_uri() + "?mode=ro&immutable=1",
                           atoms.path.as_uri() + "?mode=ro&immutable=1"]
                          if not index else [str(path), str(atoms.path)])
                result = subprocess.run([*command, *inputs, str(nets), str(summary), str(scratch)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(nets.read_text(), expected[1])
                self.assertFalse(scratch.exists())
                self.assertFalse(list(root.glob("*.tmp.*")))
                if index:
                    value = json.loads(report.read_text())
                    self.assertEqual(value["stage"], "phase7.export-bookshelf")
                    self.assertEqual(value["status"], "pass")
                    self.assertEqual([item["path"] for item in value["inputs"]],
                                     [str(path.resolve()), str(atoms.path.resolve())])
                    self.assertEqual(len(value["outputs"]), 2)
                    self.assertGreater(value["metrics"]["wall_seconds"], 0)
            self.assertEqual((root / "0.tsv").read_bytes(), (root / "1.tsv").read_bytes())
            for source, data in snapshots.items():
                self.assertEqual(source.read_bytes(), data)

    def test_production_selects_unified_only_install_without_python_pin_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cells, mapped_path, atoms = fixture(root)
            self.addCleanup(atoms.close)
            names = adapter._OrdinalSafeNames(atoms)
            expected = adapter._render_library_and_nets(cells, atoms, names)
            runtime = root / "runtime"
            (runtime / "bin").mkdir(parents=True)
            (runtime / "bin/emuflow-native").symlink_to(emuflow_native())
            output = root / "design.nets"
            with open_xilinx_mapped_index(mapped_path) as mapped, mock.patch.dict(
                os.environ, {"EMUFLOW_NATIVE_ROOT": str(runtime)}
            ), mock.patch.object(adapter, "_ordered_cell_items", side_effect=AssertionError("Python cells")), \
                    mock.patch.object(adapter, "_expanded_pins", side_effect=AssertionError("Python pins")):
                actual = adapter._render_library_and_nets(mapped.cells, atoms, names, nets_path=output)
            self.assertEqual(actual, (expected[0], None, expected[2], expected[3]))
            self.assertEqual(output.read_text(), expected[1])

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
            with sqlite3.connect(mapped) as db:
                db.execute("PRAGMA user_version=99")
            result = subprocess.run([str(emuflow_native()), "phase7", "export-bookshelf",
                                     str(mapped), str(atoms.path), str(root / "nets"),
                                     str(root / "summary"), str(root / "scratch")],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('"invalid-artifact"', result.stderr)
            self.assertFalse(any((root / name).exists() for name in ("nets", "summary", "scratch")))
