import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.errors import ImportError, ValidationError
from emuflow.openparf_native_capabilities import (
    probe_openparf_native_capabilities,
)
from emuflow.xilinx_placer_capability import (
    qualify_xilinx_placer_capabilities,
)
from emuflow.xilinx_openparf_atomic import (
    OPENPARF_ATOMIC_MANIFEST_SCHEMA,
    OPENPARF_ATOMIC_PLACEMENT_SCHEMA,
    export_xilinx_openparf_atomic,
    run_xilinx_openparf_atomic_qualification,
    validate_xilinx_openparf_atomic_placement,
)


def _cell(cell_type, connections):
    outputs = {"O", "Q"}
    return {
        "type": cell_type,
        "port_directions": {
            port: ("output" if port in outputs else "input")
            for port in connections
        },
        "connections": connections,
    }


def _fixture(root: Path, *, coincident=False):
    mapped = root / "mapped.json"
    packed = root / "packed.json"
    architecture = root / "architecture.json"
    mapped.write_text(json.dumps({
        "modules": {"top": {"attributes": {"top": "1"}, "cells": {
            "lut": _cell("LUT6", {"I0": [10], "O": [1]}),
            "ff": _cell(
                "FDRE", {"C": [10], "CE": ["1"], "D": [1],
                         "Q": [2], "R": ["0"]}
            ),
        }}},
    }), encoding="utf-8")
    packed.write_text(json.dumps({
        "schema": "emuflow.packed-site-netlist/v1", "top": "top",
        "clusters": [{
            "id": "ordinary", "kind": "slice",
            "site_templates": ["SLICEL", "SLICEM"],
            "control_set": "ordinary-control-set",
            "assignments": [
                {"instance": "lut", "cell_type": "LUT6", "bel": "A6LUT"},
                {"instance": "ff", "cell_type": "FDRE", "bel": "AFF"},
            ],
        }],
        "cascade_chains": [],
    }), encoding="utf-8")
    lut_bels = [
        {
            "name": f"{letter}6LUT", "type": "LUT6", "z": index,
            "compatible_cells": [f"LUT{width}" for width in range(1, 7)],
        }
        for index, letter in enumerate("ABCDEFGH")
    ]
    ff_bels = [
        {
            "name": name, "type": "FF", "z": index,
            "compatible_cells": ["FDCE", "FDPE", "FDRE", "FDSE"],
        }
        for index, name in enumerate(
            name for letter in "ABCDEFGH"
            for name in (f"{letter}FF", f"{letter}FF2")
        )
    ]
    sites = [
        {
            "name": "SLICE_X0Y0", "type": "SLICEL", "template": "SLICEL",
            "x": 0, "y": 0, "tile": {"grid_col": 4, "grid_row": 8},
        },
        {
            "name": "SLICE_X0Y1", "type": "SLICEL", "template": "SLICEL",
            "x": 0, "y": 1,
            "tile": {"grid_col": 4 if coincident else 5, "grid_row": 8},
        },
    ]
    architecture.write_text(json.dumps({
        "schema": "emuflow.archdb/v1", "part": "fixture",
        "source": {"format": "test/v1"}, "policy": {"name": "test"},
        "site_templates": {"SLICEL": {
            "bels": [*lut_bels, *ff_bels], "alternative_templates": [],
        }},
        "sites": sites,
    }), encoding="utf-8")
    return mapped, packed, architecture


class XilinxOpenparfAtomicTest(unittest.TestCase):
    def test_capability_contract_qualifies_only_the_atomic_subset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root)
            matrix = probe_openparf_native_capabilities(
                mapped, packed, architecture
            )
        self.assertTrue(matrix["native_atomic_lut_ff"]["eligible"])
        self.assertEqual(
            matrix["native_atomic_lut_ff"]["adapter_validation"], "pass"
        )
        self.assertEqual(
            matrix["stages"]["detailed_placement"], {
                "status": "adapter_required",
                "evidence": [
                    "openparf/ops/ism_dp/ism_dp.py",
                    "src/emuflow/xilinx_openparf_atomic.py",
                ],
                "adapter_validation": "pass",
            }
        )
        decision = qualify_xilinx_placer_capabilities(
            matrix,
            required_primitives=("LUT6", "FDRE"),
            required_constraints=("bel_site_mode",),
        )
        self.assertEqual(decision["status"], "pass")

    def test_export_enables_native_direct_legalization_and_ism(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root)
            output = root / "output"
            manifest = export_xilinx_openparf_atomic(
                mapped, packed, architecture, output
            )
            config = json.loads((output / "openparf.json").read_text())
            library = (output / "design.lib").read_text()
            sites = (output / "design.scl").read_text()
            nodes = (output / "design.nodes").read_text()
        self.assertEqual(manifest["schema"], OPENPARF_ATOMIC_MANIFEST_SCHEMA)
        self.assertEqual(manifest["runtime_validation"], "unverified")
        self.assertEqual(config["generic_cluster_placement_flag"], 0)
        self.assertEqual(config["legalize_flag"], 1)
        self.assertEqual(config["detailed_place_flag"], 1)
        self.assertEqual(config["resource_categories"], {"FF": "FF", "LUT": "LUTL"})
        self.assertEqual(config["CLB_capacity"], 16)
        self.assertEqual(config["BLE_capacity"], 2)
        self.assertIn("PIN C INPUT CLOCK", library)
        self.assertIn("PIN CE INPUT CTRL_CE", library)
        self.assertIn("PIN R INPUT CTRL_SR", library)
        self.assertIn("LUT 16", sites)
        self.assertIn("FF 16", sites)
        self.assertIn("a0 FDRE", nodes)
        self.assertIn("a1 LUT6", nodes)

    def test_import_aggregates_atoms_into_a_physical_certificate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root)
            output = root / "output"
            export_xilinx_openparf_atomic(mapped, packed, architecture, output)
            placement = output / "placed.pl"
            placement.write_text("a0 0 0 0\na1 0 0 0\n", encoding="utf-8")
            certificate = validate_xilinx_openparf_atomic_placement(
                placement, output / "name_map.json", mapped, architecture,
                output / "certificate.json",
            )
        self.assertEqual(certificate["schema"], OPENPARF_ATOMIC_PLACEMENT_SCHEMA)
        self.assertEqual(certificate["runtime_validation"], "unverified")
        self.assertEqual(certificate["summary"], {
            "atoms": 2, "occupied_sites": 1, "luts": 1, "ffs": 1,
        })
        assignments = certificate["clusters"][0]["assignments"]
        self.assertEqual(
            {(item["instance"], item["bel"]) for item in assignments},
            {("ff", "AFF"), ("lut", "A6LUT")},
        )
        self.assertEqual(
            {item["source_cluster"] for item in assignments}, {"ordinary"}
        )

    def test_odd_lut_slot_and_control_set_violation_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root)
            output = root / "output"
            export_xilinx_openparf_atomic(mapped, packed, architecture, output)
            odd = output / "odd.pl"
            odd.write_text("a0 0 0 0\na1 0 0 1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "paired LUT"):
                validate_xilinx_openparf_atomic_placement(
                    odd, output / "name_map.json", mapped, architecture
                )

            mapped_value = json.loads(mapped.read_text())
            mapped_value["modules"]["top"]["cells"]["ff2"] = _cell(
                "FDRE", {"C": [99], "CE": ["1"], "D": [1],
                         "Q": [3], "R": ["0"]}
            )
            mapped.write_text(json.dumps(mapped_value), encoding="utf-8")
            packed_value = json.loads(packed.read_text())
            packed_value["clusters"][0]["assignments"].append(
                {"instance": "ff2", "cell_type": "FDRE", "bel": "BFF"}
            )
            packed.write_text(json.dumps(packed_value), encoding="utf-8")
            output2 = root / "output2"
            export_xilinx_openparf_atomic(mapped, packed, architecture, output2)
            # Sorted atoms are ff, ff2, lut. z=0 and z=2 share the lower-half
            # CK/SR domain but have distinct clocks.
            controls = output2 / "controls.pl"
            controls.write_text(
                "a0 0 0 0\na1 0 0 2\na2 0 0 4\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValidationError, "control-set"):
                validate_xilinx_openparf_atomic_placement(
                    controls, output2 / "name_map.json", mapped, architecture
                )

    def test_relative_hard_and_coordinate_ambiguity_fail_before_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root)
            value = json.loads(packed.read_text())
            value["clusters"][0]["relative_constraints"] = {"keep": True}
            packed.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "relative/physical"):
                export_xilinx_openparf_atomic(
                    mapped, packed, architecture, root / "relative"
                )

            mapped, packed, architecture = _fixture(root, coincident=True)
            with self.assertRaisesRegex(ValidationError, "coincident physical"):
                export_xilinx_openparf_atomic(
                    mapped, packed, architecture, root / "collision"
                )

            mapped, packed, architecture = _fixture(root)
            value = json.loads(mapped.read_text())
            value["modules"]["top"]["cells"]["carry"] = {
                "type": "CARRY8", "port_directions": {}, "connections": {},
            }
            mapped.write_text(json.dumps(value), encoding="utf-8")
            value = json.loads(packed.read_text())
            value["clusters"].append({
                "id": "carry", "kind": "carry", "assignments": [{
                    "instance": "carry", "cell_type": "CARRY8", "bel": "CARRY8",
                }],
            })
            packed.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "cluster kind 'carry'"):
                export_xilinx_openparf_atomic(
                    mapped, packed, architecture, root / "carry"
                )

    def test_dual_lut_ram_and_dsp_are_explicitly_out_of_scope(self):
        cases = (
            ("LUT6_2", "slice", "primitive 'LUT6_2'"),
            ("DSP48E2", "dsp", "cluster kind 'dsp'"),
            ("RAMB36E2", "bram", "cluster kind 'bram'"),
        )
        for primitive, kind, message in cases:
            with self.subTest(primitive=primitive), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                mapped, packed, architecture = _fixture(root)
                mapped_value = json.loads(mapped.read_text())
                mapped_value["modules"]["top"]["cells"]["macro"] = {
                    "type": primitive, "port_directions": {}, "connections": {},
                }
                mapped.write_text(json.dumps(mapped_value), encoding="utf-8")
                packed_value = json.loads(packed.read_text())
                packed_value["clusters"].append({
                    "id": "macro", "kind": kind, "assignments": [{
                        "instance": "macro", "cell_type": primitive,
                        "bel": primitive,
                    }],
                })
                packed.write_text(json.dumps(packed_value), encoding="utf-8")
                with self.assertRaisesRegex(ValidationError, message):
                    export_xilinx_openparf_atomic(
                        mapped, packed, architecture, root / "out"
                    )

    def test_internal_runner_has_no_fallback_and_keeps_runtime_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root)
            output = root / "output"

            def fake_run(_config, **_kwargs):
                placement = output / "results" / "xilinx_atomic_lut_ff.pl"
                placement.parent.mkdir(parents=True, exist_ok=True)
                placement.write_text("a0 0 0 0\na1 0 0 0\n", encoding="utf-8")
                return placement

            with mock.patch(
                "emuflow.xilinx_openparf_atomic.validate_openparf_runtime",
                return_value={
                    "status": "pass", "installation": "contract-fixture",
                    "python": "contract-fixture",
                },
            ), mock.patch(
                "emuflow.xilinx_openparf_atomic.run_openparf",
                side_effect=fake_run,
            ):
                report = run_xilinx_openparf_atomic_qualification(
                    mapped, packed, architecture, output
                )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["runtime"]["installation"], "contract-fixture")
        self.assertEqual(
            report["certificate"]["runtime_validation"], "unverified"
        )
        self.assertNotIn("fallback", report)

    def test_parser_rejects_missing_or_extra_atoms(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root)
            output = root / "output"
            export_xilinx_openparf_atomic(mapped, packed, architecture, output)
            missing = output / "missing.pl"
            missing.write_text("a0 0 0 0\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "cover every atom"):
                validate_xilinx_openparf_atomic_placement(
                    missing, output / "name_map.json", mapped, architecture
                )
            malformed = output / "malformed.pl"
            malformed.write_text("unknown 0 0 0\n", encoding="utf-8")
            with self.assertRaises(ImportError):
                validate_xilinx_openparf_atomic_placement(
                    malformed, output / "name_map.json", mapped, architecture
                )


if __name__ == "__main__":
    unittest.main()
