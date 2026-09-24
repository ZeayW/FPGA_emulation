import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.dreamplacefpga_interchange import (
    DREAMPLACEFPGA_LOGICAL_MODEL_SCHEMA,
    DREAMPLACEFPGA_PLACEMENT_CANDIDATE_SCHEMA,
    build_dreamplacefpga_logical_model,
    build_dreamplacefpga_placement_candidate,
    validate_dreamplacefpga_placement_candidate,
    write_dreamplacefpga_logical_netlist,
)
from emuflow.errors import ValidationError


def _cell(cell_type, input_port, output_port, input_net, output_net, **parameters):
    return {
        "type": cell_type,
        "parameters": parameters,
        "attributes": {},
        "port_directions": {input_port: "input", output_port: "output"},
        "connections": {input_port: [input_net], output_port: [output_net]},
    }


class DreamplaceFPGAInterchangeTest(unittest.TestCase):
    def _mapped(self):
        return {
            "modules": {
                "top": {
                    "attributes": {"top": "1"},
                    "ports": {
                        "din": {"direction": "input", "bits": [2]},
                        "dout": {"direction": "output", "bits": [6]},
                    },
                    "cells": {
                        "lut": _cell("LUT6", "I", "O", 2, 3, INIT="a5"),
                        "ff": _cell("FDRE", "D", "Q", 3, 4),
                        "dsp": _cell("DSP48E2", "A", "P", 4, 5),
                        "bram": _cell("RAMB36E2", "ADDR", "DO", 5, 6),
                    },
                    "netnames": {
                        "din_net": {"bits": [2]},
                        "lut_net": {"bits": [3]},
                        "ff_net": {"bits": [4]},
                        "dsp_net": {"bits": [5]},
                        "dout_net": {"bits": [6]},
                    },
                }
            }
        }

    def _architecture(self):
        def template(*bels):
            return {"bels": list(bels), "alternative_templates": []}

        def bel(name, cell_type, z=0):
            return {
                "name": name,
                "type": cell_type,
                "z": z,
                "compatible_cells": [cell_type],
            }

        return {
            "schema": "emuflow.archdb/v1",
            "part": "xcvu-test",
            "source": {"format": "unit-test/v1"},
            "policy": {"name": "unit-test"},
            "site_templates": {
                "SLICEL": template(
                    bel("A6LUT", "LUT6"), bel("AFF", "FDRE", 1)
                ),
                "DSP48E2": template(bel("DSP48E2", "DSP48E2")),
                "RAMB36E2": template(bel("RAMB36E2", "RAMB36E2")),
            },
            "sites": [
                {
                    "name": "SLICE_X0Y0", "type": "SLICEL",
                    "template": "SLICEL", "x": 0, "y": 0,
                    "physical_region": {"slr": "SLR0", "clock_region": "X0Y0"},
                },
                {
                    "name": "DSP48E2_X0Y0", "type": "DSP48E2",
                    "template": "DSP48E2", "x": 1, "y": 0,
                    "physical_region": {"slr": "SLR0", "clock_region": "X0Y0"},
                },
                {
                    "name": "RAMB36_X0Y0", "type": "RAMBFIFO36",
                    "template": "RAMB36E2", "x": 2, "y": 0,
                    "physical_region": {"slr": "SLR0", "clock_region": "X0Y0"},
                },
            ],
        }

    def _physical(self):
        return {
            "part": "xcvu-test",
            "placements": [
                {
                    "instance": "lut", "cell_type": "LUT6",
                    "site": "SLICE_X0Y0", "bel": "A6LUT",
                    "site_fixed": True, "bel_fixed": True,
                },
                {
                    "instance": "ff", "cell_type": "FDRE",
                    "site": "SLICE_X0Y0", "bel": "AFF",
                    "site_fixed": True, "bel_fixed": True,
                },
                {
                    "instance": "dsp", "cell_type": "DSP48E2",
                    "site": "DSP48E2_X0Y0", "bel": "DSP48E2",
                    "site_fixed": True, "bel_fixed": True,
                },
                {
                    "instance": "bram", "cell_type": "RAMB36E2",
                    "site": "RAMB36_X0Y0", "bel": "RAMB36E2",
                    "site_fixed": True, "bel_fixed": True,
                },
            ],
        }

    def _write_inputs(self, root):
        mapped = root / "mapped.json"
        architecture = root / "architecture.json"
        physical = root / "placed.phys"
        mapped.write_text(json.dumps(self._mapped()), encoding="utf-8")
        architecture.write_text(json.dumps(self._architecture()), encoding="utf-8")
        physical.write_bytes(b"fixture-physical-netlist")
        return mapped, architecture, physical

    def test_logical_model_preserves_supported_instances_nets_and_parameters(self):
        with tempfile.TemporaryDirectory() as temporary:
            mapped, _architecture, _physical = self._write_inputs(Path(temporary))
            first = build_dreamplacefpga_logical_model(mapped)
            second = build_dreamplacefpga_logical_model(mapped)
        self.assertEqual(first, second)
        self.assertEqual(first["schema"], DREAMPLACEFPGA_LOGICAL_MODEL_SCHEMA)
        self.assertEqual(
            [item["name"] for item in first["instances"]],
            ["bram", "dsp", "ff", "lut"],
        )
        self.assertEqual(len(first["nets"]), 5)
        lut = next(item for item in first["instances"] if item["name"] == "lut")
        self.assertEqual(lut["properties"], {"INIT": "a5"})

    def test_logical_adapter_rejects_unsupported_primitive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, _architecture, _physical = self._write_inputs(root)
            value = self._mapped()
            value["modules"]["top"]["cells"] = {
                "carry": _cell("CARRY8", "DI", "CO", 2, 3)
            }
            mapped.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "CARRY8"):
                build_dreamplacefpga_logical_model(mapped)

    def test_logical_adapter_rejects_constant_pin_without_softening(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, _architecture, _physical = self._write_inputs(root)
            value = self._mapped()
            value["modules"]["top"]["cells"]["lut"]["connections"]["I"] = ["0"]
            mapped.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "does not approximate"):
                build_dreamplacefpga_logical_model(mapped)

    def test_binary_writer_requires_real_pycapnp(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, _architecture, _physical = self._write_inputs(root)
            with mock.patch(
                "emuflow.dreamplacefpga_interchange.importlib.import_module",
                side_effect=ImportError("missing"),
            ):
                with self.assertRaisesRegex(ValidationError, "pycapnp"):
                    write_dreamplacefpga_logical_netlist(
                        mapped, root / "design.netlist"
                    )
            self.assertFalse((root / "design.netlist").exists())

    def test_physical_adapter_builds_checked_candidate_certificate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, architecture, physical = self._write_inputs(root)
            output = root / "placement.json"
            decoded = self._physical()
            with mock.patch(
                "emuflow.dreamplacefpga_interchange."
                "read_dreamplacefpga_physical_placements",
                return_value=decoded,
            ):
                result = build_dreamplacefpga_placement_candidate(
                    mapped, architecture, physical, output, decoded=decoded
                )
                checked = validate_dreamplacefpga_placement_candidate(
                    mapped, architecture, physical, output
                )
        self.assertEqual(
            result["schema"], DREAMPLACEFPGA_PLACEMENT_CANDIDATE_SCHEMA
        )
        self.assertFalse(result["production_qualified"])
        self.assertEqual(result["summary"]["cells"], 4)
        self.assertEqual(result["summary"]["sites"], 3)
        self.assertEqual(checked["status"], "pass")

    def test_physical_adapter_rejects_missing_cell(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, architecture, physical = self._write_inputs(root)
            decoded = self._physical()
            decoded["placements"].pop()
            with self.assertRaisesRegex(ValidationError, "coverage is incomplete"):
                build_dreamplacefpga_placement_candidate(
                    mapped,
                    architecture,
                    physical,
                    root / "placement.json",
                    decoded=decoded,
                )

    def test_physical_adapter_rejects_bel_overlap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, architecture, physical = self._write_inputs(root)
            decoded = self._physical()
            decoded["placements"][1].update({
                "site": "SLICE_X0Y0", "bel": "A6LUT", "cell_type": "FDRE"
            })
            with self.assertRaisesRegex(
                ValidationError, "incompatible|overlaps BEL"
            ):
                build_dreamplacefpga_placement_candidate(
                    mapped,
                    architecture,
                    physical,
                    root / "placement.json",
                    decoded=decoded,
                )

    def test_saved_candidate_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, architecture, physical = self._write_inputs(root)
            output = root / "placement.json"
            decoded = self._physical()
            with mock.patch(
                "emuflow.dreamplacefpga_interchange."
                "read_dreamplacefpga_physical_placements",
                return_value=decoded,
            ):
                build_dreamplacefpga_placement_candidate(
                    mapped, architecture, physical, output, decoded=decoded
                )
                value = json.loads(output.read_text(encoding="utf-8"))
                value["clusters"][0]["assignments"][0]["site"] = "SLICE_X9Y9"
                output.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(ValidationError, "cell assignment"):
                    validate_dreamplacefpga_placement_candidate(
                        mapped, architecture, physical, output
                    )

    def test_saved_candidate_rejects_summary_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, architecture, physical = self._write_inputs(root)
            output = root / "placement.json"
            decoded = self._physical()
            with mock.patch(
                "emuflow.dreamplacefpga_interchange."
                "read_dreamplacefpga_physical_placements",
                return_value=decoded,
            ):
                build_dreamplacefpga_placement_candidate(
                    mapped, architecture, physical, output, decoded=decoded
                )
                value = json.loads(output.read_text(encoding="utf-8"))
                value["summary"]["cells"] = 5
                output.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(ValidationError, "summary"):
                    validate_dreamplacefpga_placement_candidate(
                        mapped, architecture, physical, output
                    )


if __name__ == "__main__":
    unittest.main()
