import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ImportError
from emuflow.yosys import (
    _expand_staged_yosys_hierarchy,
    _stream_yosys_hierarchy_to_staging,
)


def expand(value):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    cells, names = {}, {}
    try:
        _stream_yosys_hierarchy_to_staging(
            Path("<test-stream>"), "top", connection,
            source_stream=io.StringIO(json.dumps(value)),
        )
        ports, _, aliases = _expand_staged_yosys_hierarchy(
            connection, "top",
            stage_cell=lambda name, value: cells.update({name: value}),
            stage_netname=lambda name, value: names.update({name: value}),
        )
        return ports, cells, names, aliases
    finally:
        connection.close()


def fixture(direction, binding):
    connections = {"a": [2], "y": [3]}
    if binding is not None:
        connections["dec"] = binding
    return {"modules": {
        "top": {
            "ports": {"a": {"direction": "input", "bits": [2]},
                      "y": {"direction": "output", "bits": [3]}},
            "cells": {"u": {"type": "leaf", "connections": connections}},
        },
        "leaf": {
            "ports": {"a": {"direction": "input", "bits": [2]},
                      "dec": {"direction": direction, "bits": [2] * 36},
                      "y": {"direction": "output", "bits": [2]}},
            "netnames": {"internal": {"bits": [2]}},
        },
    }}


class HierarchyPortTest(unittest.TestCase):
    def test_open_output_and_inout_do_not_poison_connected_alias(self):
        for direction in ("output", "inout"):
            for binding in (None, []):
                with self.subTest(direction=direction, binding=binding):
                    ports, _, names, aliases = expand(fixture(direction, binding))
                    self.assertIsInstance(ports["a"]["bits"][0], int)
                    self.assertEqual(ports["a"]["bits"], ports["y"]["bits"])
                    internal = names["u.internal"]["bits"][0]
                    self.assertEqual(aliases.get(internal, internal), ports["a"]["bits"][0])

    def test_open_output_preserves_internal_driver_and_consumer(self):
        value = fixture("output", [])
        leaf = value["modules"]["leaf"]
        leaf["ports"]["dec"]["bits"] = [4] * 36
        leaf["ports"]["y"]["bits"] = [5]
        leaf["cells"] = {
            "driver": {"type": "LUT1", "connections": {"I0": [2], "O": [4]}},
            "consumer": {"type": "LUT1", "connections": {"I0": [4], "O": [5]}},
        }
        _, cells, _, _ = expand(value)
        driven = cells["u.driver"]["connections"]["O"]
        self.assertIsInstance(driven[0], int)
        self.assertEqual(driven, cells["u.consumer"]["connections"]["I0"])

    def test_open_input_remains_an_undriven_wire(self):
        for binding in (None, []):
            value = fixture("input", binding)
            leaf = value["modules"]["leaf"]
            leaf["ports"]["dec"]["bits"] = list(range(4, 40))
            leaf["cells"] = {"consumer": {
                "type": "LUT1", "connections": {"I0": [4], "O": [40]}}}
            ports, cells, _, _ = expand(value)
            floating = cells["u.consumer"]["connections"]["I0"][0]
            self.assertIsInstance(floating, int)
            self.assertNotEqual(floating, ports["a"]["bits"][0])

    def test_nonempty_width_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ImportError, "expected 36 connection bits, got 1"):
            expand(fixture("output", [4]))

    @unittest.skipUnless(os.environ.get("EMUFLOW_TEST_YOSYS"), "native Yosys opt-in")
    def test_native_yosys_flatten_equivalence(self):
        source = Path(__file__).parent / "fixtures/yosys_open_hierarchy_ports.v"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hierarchy, flattened = root / "hierarchy.json", root / "flat.json"
            script = (
                f'read_verilog "{source}"; hierarchy -check -top top; proc; opt; '
                f'write_json "{hierarchy}"; flatten; write_json "{flattened}"'
            )
            subprocess.run([os.environ["EMUFLOW_TEST_YOSYS"], "-Q", "-q", "-p", script],
                           check=True, capture_output=True, text=True)
            value = json.loads(hierarchy.read_text())
            raw_connections = value["modules"]["top"]["cells"]["u"]["connections"]
            self.assertEqual(raw_connections["dec"], [])
            actual, _, _, _ = expand(value)
            expected = json.loads(flattened.read_text())["modules"]["top"]["ports"]
            # Compare alias relationships, not tool-specific wire numbering.
            self.assertEqual(actual["a"]["bits"], actual["y"]["bits"])
            self.assertEqual(expected["a"]["bits"], expected["y"]["bits"])
            self.assertIsInstance(actual["y"]["bits"][0], int)
            self.assertIsInstance(actual["z"]["bits"][0], int)
            self.assertIsInstance(expected["z"]["bits"][0], int)
            self.assertNotEqual(actual["z"]["bits"], actual["a"]["bits"])
            self.assertNotEqual(expected["z"]["bits"], expected["a"]["bits"])


if __name__ == "__main__":
    unittest.main()
