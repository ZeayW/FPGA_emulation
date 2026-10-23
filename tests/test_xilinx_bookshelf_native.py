import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from emuflow.errors import ValidationError
from emuflow.xilinx_netlist import open_xilinx_mapped_index
from emuflow import xilinx_openparf_atomic as adapter
from tests.native_build import xilinx_bookshelf_exporter


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
                adapter, "resolve_native_executable", return_value=str(self.executable)
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
                        adapter, "resolve_native_executable", return_value=str(self.executable)
                    ), self.assertRaises(ValidationError):
                        adapter._render_library_and_nets(
                            mapped.cells, atoms, adapter._OrdinalSafeNames(atoms), nets_path=output
                        )
                    self.assertEqual(output.read_text(), "previous publication\n")
                    self.assertEqual(list(root.glob(".emuflow-bookshelf-*")), [])
                finally:
                    atoms.close()
