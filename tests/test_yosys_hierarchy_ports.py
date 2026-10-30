import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from emuflow.common.errors import ImportError
from emuflow.frontend.yosys_hierarchy_native import stage_hierarchy
from unittest.mock import patch
from tests.native_build import emuflow_native


def expand(value):
    # Exercise the production native producer. The former implementation lives
    # separately in the differential oracle, not behind a production import.
    with tempfile.TemporaryDirectory() as directory:
        store = Path(directory)/"hierarchy.sqlite"
        native = str(emuflow_native())
        with patch("emuflow.frontend.yosys_hierarchy_native.resolve_native_executable",return_value=native), patch("emuflow.frontend.yosys_hierarchy_native.require_native_schema"):
            _,ports,_,_ = stage_hierarchy(Path("<test-stream>"),store,"top",source_stream=io.StringIO(json.dumps(value)))
        with closing(sqlite3.connect(store)) as connection:
            cells = {row[0]:json.loads(row[1]) for row in connection.execute("SELECT id,record_json FROM staging_instances")}
            names = {}
            for bit,name,index,width in connection.execute("SELECT yosys_bit,name,bus_index,bus_width FROM staging_names"):
                names.setdefault(name,{"bits":[None]*width})["bits"][index] = bit
        return ports,cells,names,{}


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
    def test_constant_child_outputs_propagate_through_nested_parent_aliases(self):
        from tests.yosys_reference import import_yosys_json_indexed
        from emuflow.ir.indexed_physical_ir import IndexedPhysicalIR
        from emuflow.physical.vivado.vivado_source import validate_vivado_source

        for constant in ("0", "1", "x", "z"):
            with self.subTest(constant=constant), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                value = {"modules": {
                    "top": {"attributes": {"top": "1"},
                        "ports": {"q": {"direction": "output", "bits": [3]}},
                        "cells": {"a_sink": {"type": "LUT1", "parameters": {"INIT": "10"},
                            "port_directions": {"I0": "input", "O": "output"},
                            "connections": {"I0": [2], "O": [3]}},
                            "z_source": {"type": "mid", "connections": {"y": [2]}}}},
                    "mid": {"ports": {"y": {"direction": "output", "bits": [2]}},
                        "cells": {"source": {"type": "const", "connections": {"y": [2]}}}},
                    "const": {"ports": {"y": {"direction": "output", "bits": [constant]}}},
                }}
                source, output = root / "source.json", root / "design.emuir.sqlite"
                source.write_text(json.dumps(value))
                import_yosys_json_indexed(source, output, top="top", _expand_hierarchy=True)
                with IndexedPhysicalIR(output) as ir:
                    self.assertEqual(ir.instances["a_sink"]["constant_connections"],
                                     [{"port": "I0", "bit": 0, "value": constant}])
                self.assertEqual(validate_vivado_source(output)["used_undriven_nets"], 0)

    def test_open_output_and_inout_do_not_poison_connected_alias(self):
        for direction in ("output", "inout"):
            for binding in (None, []):
                with self.subTest(direction=direction, binding=binding):
                    ports, _, names, aliases = expand(fixture(direction, binding))
                    self.assertIsInstance(ports["a"]["bits"][0], int)
                    self.assertEqual(ports["a"]["bits"], ports["y"]["bits"])
                    internal = names["u.internal"]["bits"][0]
                    self.assertEqual(aliases.get(internal, internal), ports["a"]["bits"][0])

    def test_conflicting_child_constant_driver_is_rejected(self):
        value = {"modules": {
            "top": {"ports": {}, "cells": {
                "u": {"type": "constant", "connections": {"y": ["1"]}}}},
            "constant": {"ports": {"y": {"direction": "output", "bits": ["0"]}}},
        }}
        with self.assertRaisesRegex(ImportError, "incompatible constant bindings"):
            expand(value)

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
        with self.assertRaisesRegex(ImportError, "connection width disagrees"):
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
            for name in ("zero", "pattern"):
                self.assertEqual(actual[name]["bits"], expected[name]["bits"])
            self.assertEqual(actual["zero"]["bits"], ["0"])
            self.assertEqual(actual["pattern"]["bits"], ["0", "1"])


if __name__ == "__main__":
    unittest.main()
