import json
import sqlite3
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
from emuflow.yosys import import_yosys_json
from emuflow.xilinx_openparf_atomic import (
    OPENPARF_ATOMIC_MANIFEST_SCHEMA,
    OPENPARF_ATOMIC_PLACEMENT_SCHEMA,
    OPENPARF_ATOMIC_SOURCE_SCHEMA,
    OPENPARF_PHYSICAL_MACRO_CONSTRAINT_SCHEMA,
    build_xilinx_openparf_atomic_source,
    export_xilinx_openparf_atomic,
    load_xilinx_openparf_atomic_sites,
    run_xilinx_openparf_atomic_qualification,
    validate_xilinx_openparf_atomic_placement,
)
from tests.openparf_runtime_fixture import (
    write_openparf_hardblock_cascade_fixture,
    write_openparf_ramb18_fixture,
    write_openparf_runtime_fixture,
)


def _cell(cell_type, connections):
    outputs = {"O", "Q", "P", "DOADO", "DOUT_A"}
    return {
        "type": cell_type,
        "port_directions": {
            port: ("output" if port in outputs else "input")
            for port in connections
        },
        "connections": connections,
    }


def _defined_and_resource_models(library: str, sites: str):
    defined = {
        line.split()[1] for line in library.splitlines()
        if line.startswith("CELL ")
    }
    resource_models = set()
    in_resources = False
    for line in sites.splitlines():
        if line == "RESOURCES":
            in_resources = True
        elif line == "END RESOURCES":
            in_resources = False
        elif in_resources:
            resource_models.update(line.split()[1:])
    return defined, resource_models


def _resource_rows(sites: str):
    rows = []
    in_resources = False
    for line in sites.splitlines():
        if line == "RESOURCES":
            in_resources = True
        elif line == "END RESOURCES":
            in_resources = False
        elif in_resources:
            rows.append(line.split())
    return rows


def _fixture(root: Path, *, coincident=False, mixed=False):
    mapped = root / "mapped.json"
    packed = root / "packed.json"
    architecture = root / "architecture.json"
    cells = {
            "lut": _cell("LUT6", {"I0": [10], "O": [1]}),
            "ff": _cell(
                "FDRE", {"C": [10], "CE": ["1"], "D": [1],
                         "Q": [2], "R": ["0"]}
            ),
    }
    if mixed:
        cells.update({
            "dsp": _cell("DSP48E2", {"A": [3, 4], "P": [5, 6]}),
            "bram": _cell("RAMB36E2", {"ADDRARDADDR": [7, 8], "DOADO": [9]}),
            "uram": _cell("URAM288", {"ADDR_A": [11, 12], "DOUT_A": [13]}),
        })
    mapped.write_text(json.dumps({
        "modules": {"top": {"attributes": {"top": "1"}, "cells": cells}},
    }), encoding="utf-8")
    clusters = [{
        "id": "ordinary", "kind": "slice",
        "site_templates": ["SLICEL", "SLICEM"],
        "control_set": "ordinary-control-set",
        "assignments": [
            {"instance": "lut", "cell_type": "LUT6", "bel": "A6LUT"},
            {"instance": "ff", "cell_type": "FDRE", "bel": "AFF"},
        ],
    }]
    if mixed:
        for instance, primitive, template in (
            ("dsp", "DSP48E2", "DSP48E2"),
            ("bram", "RAMB36E2", "RAMB36E2"),
            ("uram", "URAM288", "URAM288"),
        ):
            clusters.append({
                "id": instance, "kind": "hard", "site_templates": [template],
                "control_set": None,
                "assignments": [{
                    "instance": instance, "cell_type": primitive,
                    "bel": primitive, "bel_candidates": [primitive],
                }],
            })
    packed.write_text(json.dumps({
        "schema": "emuflow.packed-site-netlist/v1", "top": "top",
        "clusters": clusters,
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
        {
            "name": "SLICE_X1Y0", "type": "SLICEL", "template": "SLICEL",
            "x": 1, "y": 0, "tile": {"grid_col": 4, "grid_row": 9},
        },
        {
            "name": "SLICE_X1Y1", "type": "SLICEL", "template": "SLICEL",
            "x": 1, "y": 1, "tile": {"grid_col": 5, "grid_row": 9},
        },
    ]
    templates = {"SLICEL": {
        "bels": [*lut_bels, *ff_bels], "alternative_templates": [],
    }}
    if mixed:
        for offset, (primitive, site_type) in enumerate((
            ("DSP48E2", "DSP48E2"),
            ("RAMB36E2", "RAMB36E2"),
            ("URAM288", "URAM288"),
        ), start=1):
            templates[site_type] = {
                "bels": [{
                    "name": primitive, "type": primitive, "z": 0,
                    "compatible_cells": [primitive],
                    "placement_mode": site_type,
                }],
                "alternative_templates": [],
            }
            for site_index in range(2):
                sites.append({
                    "name": f"{site_type}_X{site_index}Y0", "type": site_type,
                    "template": site_type,
                    "x": 10 + offset + 3 * site_index, "y": 0,
                    "tile": {
                        "grid_col": 10 + offset + 3 * site_index,
                        "grid_row": 8,
                    },
                })
    architecture.write_text(json.dumps({
        "schema": "emuflow.archdb/v1", "part": "fixture",
        "source": {"format": "test/v1"}, "policy": {"name": "test"},
        "site_templates": templates,
        "sites": sites,
    }), encoding="utf-8")
    return mapped, packed, architecture


class XilinxOpenparfAtomicTest(unittest.TestCase):
    def test_real_width_hardblock_cascade_fixtures_use_the_real_packer(self):
        expected = {
            "DSP48E2": ("ACOUT", "ACIN", 30),
            "RAMB36E2": ("CASDOUTA", "CASDINA", 32),
            "URAM288": ("CAS_OUT_DOUT_A", "CAS_IN_DOUT_A", 72),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for primitive, (output_port, input_port, width) in expected.items():
                mapped, packed, _architecture = (
                    write_openparf_hardblock_cascade_fixture(
                        root / primitive, primitive
                    )
                )
                mapped_value = json.loads(mapped.read_text(encoding="utf-8"))
                cells = mapped_value["modules"]["top"]["cells"]
                self.assertEqual(
                    len(cells["hardblock_head"]["connections"][output_port]),
                    width,
                )
                self.assertEqual(
                    cells["hardblock_head"]["connections"][output_port],
                    cells["hardblock_tail"]["connections"][input_port],
                )
                packed_value = json.loads(packed.read_text(encoding="utf-8"))
                self.assertEqual(len(packed_value["cascade_chains"]), 1)
                chain = packed_value["cascade_chains"][0]
                self.assertEqual(chain["cell_type"], primitive)
                self.assertEqual(
                    chain["instances"], ["hardblock_head", "hardblock_tail"]
                )
    def test_singleton_source_feeds_export_without_legacy_site_packing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, _packed, architecture = write_openparf_runtime_fixture(
                root, include_hard=True
            )
            atomic_source = root / "atomic-source.json"
            source = build_xilinx_openparf_atomic_source(
                mapped, atomic_source, top="top"
            )
            output = root / "openparf"
            manifest = export_xilinx_openparf_atomic(
                mapped, atomic_source, architecture, output, top="top"
            )
            imported_clocks = import_yosys_json(mapped).value["clocks"]
        self.assertEqual(source["schema"], OPENPARF_ATOMIC_SOURCE_SCHEMA)
        self.assertEqual(imported_clocks, [{
            "id": "clk", "name": "clk", "source_port": "clk",
            "period_ns": None,
        }])
        self.assertEqual(source["summary"], {
            "physical_atoms": 131, "constant_cells": 0,
        })
        self.assertEqual(manifest["atoms"], 131)
        self.assertEqual(manifest["resources"], {
            "DSP48E2": 1, "FF": 64, "LUT": 64,
            "RAMB36E2": 1, "URAM288": 1,
        })

    def test_non_degenerate_runtime_fixture_export_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = write_openparf_runtime_fixture(root)
            output = root / "output"
            manifest = export_xilinx_openparf_atomic(
                mapped, packed, architecture, output
            )
            config = json.loads((output / "openparf.json").read_text())
            names = json.loads((output / "name_map.json").read_text())
            coordinate_sites = load_xilinx_openparf_atomic_sites(
                output / "name_map.json"
            )
            selected_sites = load_xilinx_openparf_atomic_sites(
                output / "name_map.json", coordinates=[(0, 0)]
            )
            site_database_exists = (
                output / names["site_database"]["file"]
            ).is_file()
            sites = (output / "design.scl").read_text()
            net_lines = (output / "design.nets").read_text().splitlines()

        declarations = [
            line.split() for line in net_lines if line.startswith("net ")
        ]
        self.assertEqual(manifest["atoms"], 128)
        self.assertEqual(manifest["resources"], {"FF": 64, "LUT": 64})
        self.assertEqual(
            manifest["resource_unit_capacity"], {"LUT": 16, "FF": 16}
        )
        self.assertEqual(manifest["placement_region"], {
            "logic_sites": 16,
            "dense_width": 4,
            "dense_height": 4,
            "occupied_fraction": 1.0,
        })
        self.assertEqual(manifest["density_contract"]["LUT"], {
            "movable_area": 4.0,
            "placeable_area": 16.0,
            "target_area": 12.0,
            "filler_units": 192,
        })
        self.assertEqual(manifest["net_export"], {
            "emitted": 129, "dropped_single_endpoint": 0,
        })
        self.assertEqual(len(declarations), 129)
        self.assertTrue(all(int(fields[2]) >= 2 for fields in declarations))
        self.assertEqual(
            sorted(int(fields[2]) for fields in declarations),
            [2] * 128 + [64],
        )
        self.assertEqual(len(coordinate_sites), 16)
        self.assertEqual(
            names["schema"], "emuflow.openparf-atomic-name-map/v2"
        )
        self.assertNotIn("sites", names["coordinate_system"])
        self.assertNotIn("hardblock_groups", names)
        self.assertEqual(names["site_database"]["sites"], 16)
        self.assertTrue(site_database_exists)
        self.assertEqual(
            [(item["dense_x"], item["dense_y"]) for item in selected_sites],
            [(0, 0)],
        )
        self.assertEqual(64 / (16 * 8), 0.5)
        self.assertLessEqual(manifest["resources"]["FF"], 16 * 16)
        self.assertEqual([row[0] for row in _resource_rows(sites)], ["LUT", "FF"])
        self.assertEqual(list(config["gp_model2area_types_map"]), ["LUT6", "FDRE"])
        target_density = config["target_density"]
        for resource, primitive in (("LUT", "LUT6"), ("FF", "FDRE")):
            width, height = config["gp_model2area_types_map"][primitive][resource]
            unit_area = width * height
            movable_area = manifest["resources"][resource] * unit_area
            placeable_area = sum(
                item["resources"].get(resource, 0) * unit_area
                for item in coordinate_sites
            )
            filler_count = int((placeable_area - movable_area) / unit_area)
            self.assertGreater(movable_area, 0)
            self.assertLess(movable_area, target_density * placeable_area)
            self.assertGreater(filler_count, 0)
        self.assertEqual(config["generic_cluster_placement_flag"], 0)
        self.assertEqual(config["global_place_flag"], 1)
        self.assertEqual(config["legalize_flag"], 1)
        self.assertEqual(config["detailed_place_flag"], 1)
        self.assertNotIn("fallback", config)

    def test_site_database_descriptor_and_metadata_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = write_openparf_runtime_fixture(root)
            output = root / "output"
            export_xilinx_openparf_atomic(mapped, packed, architecture, output)
            name_map_path = output / "name_map.json"
            name_map = json.loads(name_map_path.read_text(encoding="utf-8"))

            site_database = output / name_map["site_database"]["file"]
            site_database.unlink()
            with self.assertRaisesRegex(ValidationError, "database is missing"):
                load_xilinx_openparf_atomic_sites(name_map_path)

            export_xilinx_openparf_atomic(mapped, packed, architecture, output)
            with sqlite3.connect(output / "site-map.sqlite3") as database:
                database.execute(
                    "UPDATE metadata SET value = 'wrong' WHERE key = 'schema'"
                )
            with self.assertRaisesRegex(ValidationError, "metadata is invalid"):
                load_xilinx_openparf_atomic_sites(name_map_path)

    def test_collinear_and_disconnected_logic_crops_fail_before_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = write_openparf_runtime_fixture(root)
            value = json.loads(architecture.read_text())
            for index, site in enumerate(value["sites"]):
                site["tile"] = {"grid_col": 0, "grid_row": index}
            architecture.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "non-degenerate two-dimensional"
            ):
                export_xilinx_openparf_atomic(
                    mapped, packed, architecture, root / "collinear"
                )

            for index, site in enumerate(value["sites"]):
                local = index % 8
                coordinate = (
                    (local // 4, local % 4)
                    if index < 8
                    else (3 + local // 4, 5 + local % 4)
                )
                site["tile"] = {
                    "grid_col": coordinate[0], "grid_row": coordinate[1],
                }
            architecture.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "disconnected islands"):
                export_xilinx_openparf_atomic(
                    mapped, packed, architecture, root / "disconnected"
                )

    def test_non_degenerate_mixed_runtime_fixture_export_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = write_openparf_runtime_fixture(
                root, include_hard=True
            )
            output = root / "output"
            manifest = export_xilinx_openparf_atomic(
                mapped, packed, architecture, output
            )
            config = json.loads((output / "openparf.json").read_text())
            names = json.loads((output / "name_map.json").read_text())
            coordinate_sites = load_xilinx_openparf_atomic_sites(
                output / "name_map.json"
            )
            net_lines = (output / "design.nets").read_text().splitlines()

        self.assertEqual(manifest["atoms"], 131)
        self.assertEqual(manifest["resources"], {
            "DSP48E2": 1, "FF": 64, "LUT": 64,
            "RAMB36E2": 1, "URAM288": 1,
        })
        self.assertEqual(manifest["resource_unit_capacity"], {
            "LUT": 16, "FF": 16, "DSP48E2": 1,
            "RAMB36E2": 1, "URAM288": 1,
        })
        self.assertEqual(manifest["net_export"], {
            "emitted": 132, "dropped_single_endpoint": 0,
        })
        declarations = [
            line.split() for line in net_lines if line.startswith("net ")
        ]
        self.assertEqual(
            sorted(int(fields[2]) for fields in declarations),
            [2] * 131 + [64],
        )
        hard_atoms = [
            atom for atom in names["atoms"]
            if atom["resource"] in {"DSP48E2", "RAMB36E2", "URAM288"}
        ]
        self.assertEqual(len(hard_atoms), 3)
        pin_rows = [
            line.strip() for line in net_lines
            if line.startswith("  ")
        ]
        for atom in hard_atoms:
            self.assertEqual(
                sum(row.startswith(atom["openparf"] + " ") for row in pin_rows),
                2,
            )
            hard_sites = [
                item for item in coordinate_sites
                if item["resources"].get(atom["resource"]) == 1
            ]
            self.assertEqual(len(hard_sites), 2)
        self.assertEqual(config["resource_categories"], {
            "LUT": "LUTL", "FF": "FF", "DSP48E2": "SSSIR",
            "RAMB36E2": "SSSIR", "URAM288": "SSSIR",
        })
        self.assertEqual(config["generic_cluster_placement_flag"], 0)
        self.assertEqual(config["global_place_flag"], 1)
        self.assertEqual(config["legalize_flag"], 1)
        self.assertEqual(config["detailed_place_flag"], 1)
        self.assertNotIn("fallback", config)

    def test_cotiled_hard_sites_become_one_capacity_site_with_exact_slots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = write_openparf_runtime_fixture(
                root, include_hard=True
            )
            value = json.loads(architecture.read_text())
            dsp_sites = [
                site for site in value["sites"] if site["type"] == "DSP48E2"
            ]
            self.assertEqual(len(dsp_sites), 2)
            dsp_sites[0]["tile"] = {
                "grid_col": 20, "grid_row": 7, "site_index": 0,
            }
            dsp_sites[1]["tile"] = {
                "grid_col": 20, "grid_row": 7, "site_index": 1,
            }
            architecture.write_text(json.dumps(value), encoding="utf-8")
            output = root / "output"
            export_xilinx_openparf_atomic(mapped, packed, architecture, output)
            names = json.loads((output / "name_map.json").read_text())
            coordinate_sites = load_xilinx_openparf_atomic_sites(
                output / "name_map.json"
            )
            dsp_tiles = [
                site for site in coordinate_sites
                if site["resources"].get("DSP48E2")
            ]
            self.assertEqual(len(dsp_tiles), 1)
            self.assertEqual(dsp_tiles[0]["resources"]["DSP48E2"], 2)
            self.assertEqual(
                dsp_tiles[0]["physical_sites"]["DSP48E2"],
                ["DSP48E2_X0Y0", "DSP48E2_X1Y0"],
            )

            resource_indexes = {"LUT": 0, "FF": 0}
            rows = []
            for atom in names["atoms"]:
                resource = atom["resource"]
                candidates = [
                    site for site in coordinate_sites
                    if site["resources"].get(resource)
                ]
                if resource in resource_indexes:
                    index = resource_indexes[resource]
                    resource_indexes[resource] += 1
                    site = candidates[index // 4]
                    z = 2 * (index % 4) + (1 if resource == "LUT" else 0)
                else:
                    site = candidates[0]
                    z = 1 if resource == "DSP48E2" else 0
                rows.append(
                    f"{atom['openparf']} {site['dense_x']} {site['dense_y']} {z}"
                )
            placement = output / "cotiled.pl"
            placement.write_text("\n".join(rows) + "\n", encoding="utf-8")
            certificate = validate_xilinx_openparf_atomic_placement(
                placement, output / "name_map.json", mapped, architecture
            )
            dsp_cluster = next(
                cluster
                for cluster in certificate["clusters"]
                if any(
                    assignment["cell_type"] == "DSP48E2"
                    for assignment in cluster["assignments"]
                )
            )
            self.assertEqual(dsp_cluster["site"], "DSP48E2_X1Y0")

    def test_real_style_runtime_placement_uses_odd_lut6_slots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = write_openparf_runtime_fixture(root)
            output = root / "output"
            export_xilinx_openparf_atomic(
                mapped, packed, architecture, output
            )
            names = json.loads((output / "name_map.json").read_text())
            sites = load_xilinx_openparf_atomic_sites(
                output / "name_map.json"
            )
            resource_indexes = {"LUT": 0, "FF": 0}
            rows = []
            first_lut_row = None
            for atom in names["atoms"]:
                resource = atom["resource"]
                index = resource_indexes[resource]
                resource_indexes[resource] += 1
                site = sites[index // 4]
                z = 2 * (index % 4) + (1 if resource == "LUT" else 0)
                row = (
                    f"{atom['openparf']} {site['dense_x']} "
                    f"{site['dense_y']} {z}"
                )
                if resource == "LUT" and first_lut_row is None:
                    first_lut_row = len(rows)
                rows.append(row)
            placement = output / "real-style.pl"
            placement.write_text("\n".join(rows) + "\n", encoding="utf-8")
            certificate = validate_xilinx_openparf_atomic_placement(
                placement, output / "name_map.json", mapped, architecture
            )
            self.assertEqual(certificate["summary"]["luts"], 64)
            self.assertEqual(certificate["summary"]["ffs"], 64)
            lut_assignments = [
                assignment
                for cluster in certificate["clusters"]
                for assignment in cluster["assignments"]
                if assignment["cell_type"] == "LUT6"
            ]
            self.assertEqual(len(lut_assignments), 64)
            self.assertTrue(
                all(item["bel"] in {f"{letter}6LUT" for letter in "ABCD"}
                    for item in lut_assignments)
            )

            even_rows = list(rows)
            fields = even_rows[first_lut_row].split()
            fields[3] = "0"
            even_rows[first_lut_row] = " ".join(fields)
            even = output / "even-lut6.pl"
            even.write_text("\n".join(even_rows) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "paired LUT"):
                validate_xilinx_openparf_atomic_placement(
                    even, output / "name_map.json", mapped, architecture
                )

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
            nets = (output / "design.nets").read_text()
        self.assertEqual(manifest["schema"], OPENPARF_ATOMIC_MANIFEST_SCHEMA)
        self.assertEqual(manifest["runtime_validation"], "unverified")
        self.assertEqual(config["generic_cluster_placement_flag"], 0)
        self.assertEqual(config["legalize_flag"], 1)
        self.assertEqual(config["detailed_place_flag"], 1)
        self.assertEqual(config["resource_categories"], {"FF": "FF", "LUT": "LUTL"})
        self.assertEqual(list(config["gp_model2area_types_map"]), ["LUT6", "FDRE"])
        self.assertEqual(config["gp_model2area_types_map"]["LUT6"]["LUT"], [0.25, 0.25])
        self.assertEqual(config["gp_model2area_types_map"]["FDRE"]["FF"], [0.25, 0.25])
        self.assertEqual(config["CLB_capacity"], 16)
        self.assertEqual(config["BLE_capacity"], 2)
        self.assertIn("PIN C INPUT CLOCK", library)
        self.assertIn("PIN CE INPUT CTRL_CE", library)
        self.assertIn("PIN R INPUT CTRL_SR", library)
        self.assertIn("LUT 16", sites)
        self.assertIn("FF 16", sites)
        self.assertEqual([row[0] for row in _resource_rows(sites)], ["LUT", "FF"])
        self.assertIn("a0 FDRE", nodes)
        self.assertIn("a1 LUT6", nodes)
        self.assertEqual(manifest["nets"], 2)
        self.assertEqual(manifest["net_export"], {
            "emitted": 2, "dropped_single_endpoint": 1,
        })
        declarations = [
            line.split() for line in nets.splitlines() if line.startswith("net ")
        ]
        self.assertTrue(declarations)
        self.assertTrue(all(int(fields[2]) >= 2 for fields in declarations))
        self.assertIn("net n0 2\n  a1 O\n  a0 D\nendnet", nets)
        self.assertIn("net n1 2\n  a0 C\n  a1 I0\nendnet", nets)
        self.assertEqual(
            _defined_and_resource_models(library, sites),
            ({"FDRE", "LUT6"}, {"FDRE", "LUT6"}),
        )

    def test_net_export_still_rejects_multiple_drivers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root)
            mapped_value = json.loads(mapped.read_text())
            mapped_value["modules"]["top"]["cells"]["lut2"] = _cell(
                "LUT6", {"I0": [11], "O": [1]}
            )
            mapped.write_text(json.dumps(mapped_value), encoding="utf-8")
            packed_value = json.loads(packed.read_text())
            packed_value["clusters"][0]["assignments"].append({
                "instance": "lut2", "cell_type": "LUT6", "bel": "B6LUT",
            })
            packed.write_text(json.dumps(packed_value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "multiple.*drivers"):
                export_xilinx_openparf_atomic(
                    mapped, packed, architecture, root / "multi-driver"
                )

    def test_import_aggregates_atoms_into_a_physical_certificate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root)
            output = root / "output"
            export_xilinx_openparf_atomic(mapped, packed, architecture, output)
            placement = output / "placed.pl"
            placement.write_text("a0 0 0 0\na1 0 0 1\n", encoding="utf-8")
            certificate = validate_xilinx_openparf_atomic_placement(
                placement, output / "name_map.json", mapped, architecture,
                output / "certificate.json",
            )
        self.assertEqual(certificate["schema"], OPENPARF_ATOMIC_PLACEMENT_SCHEMA)
        self.assertEqual(certificate["runtime_validation"], "unverified")
        self.assertEqual(certificate["summary"], {
            "atoms": 2, "occupied_sites": 1, "luts": 1, "ffs": 1,
            "hard_resources": {},
        })
        assignments = certificate["clusters"][0]["assignments"]
        self.assertEqual(
            {(item["instance"], item["bel"]) for item in assignments},
            {("ff", "AFF"), ("lut", "A6LUT")},
        )
        self.assertEqual(
            {item["source_cluster"] for item in assignments}, {"ordinary"}
        )

    def test_even_lut_slot_and_control_set_violation_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root)
            output = root / "output"
            export_xilinx_openparf_atomic(mapped, packed, architecture, output)
            even = output / "even.pl"
            even.write_text("a0 0 0 0\na1 0 0 0\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "paired LUT"):
                validate_xilinx_openparf_atomic_placement(
                    even, output / "name_map.json", mapped, architecture
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
                "a0 0 0 0\na1 0 0 2\na2 0 0 5\n", encoding="utf-8"
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

    def test_dual_lut_and_unsupported_hard_cluster_kinds_fail_closed(self):
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

    def test_mixed_fixture_exports_one_native_mcf_direct_lg_ism_flow(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root, mixed=True)
            output = root / "output"
            manifest = export_xilinx_openparf_atomic(
                mapped, packed, architecture, output
            )
            config = json.loads((output / "openparf.json").read_text())
            sites = (output / "design.scl").read_text()
            library = (output / "design.lib").read_text()
        self.assertEqual(manifest["resources"], {
            "DSP48E2": 1, "FF": 1, "LUT": 1,
            "RAMB36E2": 1, "URAM288": 1,
        })
        self.assertEqual(config["generic_cluster_placement_flag"], 0)
        self.assertEqual(config["legalize_flag"], 1)
        self.assertEqual(config["detailed_place_flag"], 1)
        self.assertEqual(config["resource_categories"], {
            "DSP48E2": "SSSIR", "FF": "FF", "LUT": "LUTL",
            "RAMB36E2": "SSSIR", "URAM288": "SSSIR",
        })
        self.assertIn("DSP48E2 1", sites)
        self.assertIn("RAMB36E2 1", sites)
        self.assertIn("URAM288 1", sites)
        self.assertEqual(
            [row[0] for row in _resource_rows(sites)],
            ["LUT", "FF", "DSP48E2", "RAMB36E2", "URAM288"],
        )
        self.assertEqual(
            list(config["gp_model2area_types_map"]),
            ["LUT6", "FDRE", "DSP48E2", "RAMB36E2", "URAM288"],
        )
        self.assertEqual(
            config["gp_model2area_types_map"]["DSP48E2"]["DSP48E2"],
            [1.0, 1.0],
        )
        self.assertIn("PIN A[0] INPUT", library)
        self.assertIn("PIN P[1] OUTPUT", library)
        defined, resource_models = _defined_and_resource_models(library, sites)
        self.assertEqual(resource_models, defined)
        self.assertEqual(defined, {
            "DSP48E2", "FDRE", "LUT6", "RAMB36E2", "URAM288",
        })

    def test_mixed_capability_is_source_backed_and_adapter_qualified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root, mixed=True)
            matrix = probe_openparf_native_capabilities(
                mapped, packed, architecture
            )
        feature = next(
            item for item in matrix["provider_features"]
            if item["feature"] == "mixed_atomic_sssir_native_flow"
        )
        self.assertEqual(feature["status"], "native_supported")
        for primitive in ("DSP48E2", "RAMB36E2", "URAM288"):
            self.assertEqual(matrix["primitives"][primitive], {
                "status": "adapter_required",
                "evidence": [
                    "src/emuflow/xilinx_openparf_atomic.py",
                    "openparf/ops/mcf_lg/mcf_lg.py",
                ],
                "adapter_validation": "pass",
                "reason": (
                    "the atomic adapter preserves connectivity, LUT/FF control "
                    "sets, discrete resource occupancy, and physical BEL compatibility"
                ),
            })

    def test_mixed_fixture_import_checks_hard_site_bel_and_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root, mixed=True)
            output = root / "output"
            export_xilinx_openparf_atomic(mapped, packed, architecture, output)
            names = json.loads((output / "name_map.json").read_text())
            coordinate_sites = load_xilinx_openparf_atomic_sites(
                output / "name_map.json"
            )
            coordinates = {
                item["site"]: (item["dense_x"], item["dense_y"])
                for item in coordinate_sites
            }
            target_site = {
                "LUT": "SLICE_X0Y0", "FF": "SLICE_X0Y0",
                "DSP48E2": "DSP48E2_X0Y0",
                "RAMB36E2": "RAMB36E2_X0Y0",
                "URAM288": "URAM288_X0Y0",
            }
            rows = []
            for atom in names["atoms"]:
                x, y = coordinates[target_site[atom["resource"]]]
                z = 1 if atom["resource"] == "LUT" else 0
                rows.append(f"{atom['openparf']} {x} {y} {z}")
            placement = output / "mixed.pl"
            placement.write_text("\n".join(rows) + "\n", encoding="utf-8")
            certificate = validate_xilinx_openparf_atomic_placement(
                placement, output / "name_map.json", mapped, architecture
            )
            wrong = output / "wrong.pl"
            wrong_rows = list(rows)
            hard_index = next(
                index for index, atom in enumerate(names["atoms"])
                if atom["resource"] == "DSP48E2"
            )
            slice_x, slice_y = coordinates["SLICE_X0Y0"]
            wrong_rows[hard_index] = (
                f"{names['atoms'][hard_index]['openparf']} {slice_x} {slice_y} 0"
            )
            wrong.write_text("\n".join(wrong_rows) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "required resource"):
                validate_xilinx_openparf_atomic_placement(
                    wrong, output / "name_map.json", mapped, architecture
                )
        self.assertEqual(certificate["summary"]["hard_resources"], {
            "DSP48E2": 1, "RAMB36E2": 1, "URAM288": 1,
        })
        assignments = {
            item["instance"]: item
            for cluster in certificate["clusters"]
            for item in cluster["assignments"]
        }
        self.assertEqual(assignments["dsp"]["bel"], "DSP48E2")
        self.assertEqual(assignments["bram"]["bel"], "RAMB36E2")
        self.assertEqual(assignments["uram"]["bel"], "URAM288")

    def test_ramb18_exports_independent_source_sealed_half_claims(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = write_openparf_ramb18_fixture(root)
            native_path = root / "native.json"
            provider_path = root / "provider.json"
            native_path.write_text("{}", encoding="utf-8")
            provider_path.write_text("{}", encoding="utf-8")
            native = {"payload": {
                "dedicated_adjacency": [],
                "bram_tile_groups": [
                    {
                        "anchor": f"RAMB18_X{index}Y1",
                        "tile": f"BRAM_X{index}Y0",
                        "lower": {
                            "site": f"RAMB18_X{index}Y0",
                            "site_type": "RAMB180", "bel": "RAMB18E2_L",
                        },
                        "upper": {
                            "site": f"RAMB18_X{index}Y1",
                            "site_type": "RAMB181", "bel": "RAMB18E2_U",
                        },
                        "whole": {
                            "site": f"RAMB36_X{index}Y0",
                            "site_type": "RAMB36", "bel": "RAMB36E2",
                        },
                    }
                    for index in range(2)
                ],
            }}
            output = root / "output"
            with mock.patch(
                "emuflow.xilinx_openparf_atomic.load_xilinx_native_device_constraints",
                return_value=(native, {"status": "pass"}),
            ), mock.patch(
                "emuflow.xilinx_openparf_atomic.require_xilinx_native_constraint_capability"
            ):
                manifest = export_xilinx_openparf_atomic(
                    mapped, packed, architecture, output,
                    native_constraints_path=native_path,
                    provider_manifest_path=provider_path,
                )
            self.assertEqual(manifest["resources"]["RAMB18E2"], 2)
            contract = json.loads(
                (output / "physical-macro-groups.json").read_text()
            )
            groups = [
                item for item in contract["groups"]
                if item["resource"] == "RAMB18E2"
            ]
            self.assertEqual(len(groups), 2)
            self.assertEqual(
                {tuple(item["source_instances"]) for item in groups},
                {("ramb18_lo",), ("ramb18_hi",)},
            )
            self.assertTrue(all(item["kind"] == "singleton" for item in groups))
            self.assertTrue(all(len(item["windows"]) == 4 for item in groups))

            lo_window = groups[0]["windows"][0][0]
            hi_window = next(
                window[0] for window in groups[1]["windows"]
                if window[0]["anchor"] == lo_window["anchor"]
                and window[0]["z"] != lo_window["z"]
            )
            names = json.loads((output / "name_map.json").read_text())
            coordinate_sites = load_xilinx_openparf_atomic_sites(
                output / "name_map.json"
            )
            dense = {
                anchor: (entry["dense_x"], entry["dense_y"])
                for entry in coordinate_sites
                for anchor in entry["physical_sites"].get("RAMB18E2", [])
            }
            positions = {
                groups[0]["source_instances"][0]: lo_window,
                groups[1]["source_instances"][0]: hi_window,
            }
            slice_sites = [
                entry for entry in coordinate_sites
                if "LUT" in entry["resources"]
            ]
            rows = []
            for atom in names["atoms"]:
                window = positions.get(atom["instance"])
                if window is not None:
                    x, y = dense[window["anchor"]]
                    z = window["z"]
                else:
                    pair_index = int(atom["instance"].rsplit("_", 1)[1])
                    slice_site = slice_sites[pair_index // 8]
                    x, y = slice_site["dense_x"], slice_site["dense_y"]
                    z = (
                        2 * (pair_index % 8) + 1
                        if atom["resource"] == "LUT"
                        else 2 * (pair_index % 8)
                    )
                rows.append(f"{atom['openparf']} {x} {y} {z}")
            placement = output / "ramb18.pl"
            placement.write_text("\n".join(rows) + "\n", encoding="utf-8")
            with mock.patch(
                "emuflow.xilinx_openparf_atomic.load_xilinx_native_device_constraints",
                return_value=(native, {"status": "pass"}),
            ), mock.patch(
                "emuflow.xilinx_openparf_atomic.require_xilinx_native_constraint_capability"
            ):
                certificate = validate_xilinx_openparf_atomic_placement(
                    placement, output / "name_map.json", mapped, architecture,
                    native_constraints_path=native_path,
                    provider_manifest_path=provider_path,
                )
            assignments = [
                assignment for cluster in certificate["clusters"]
                for assignment in cluster["assignments"]
                if assignment["cell_type"] == "RAMB18E2"
            ]
            self.assertEqual(
                {item["physical_site"] for item in assignments},
                {lo_window["site"], hi_window["site"]},
            )
            self.assertEqual(
                {item["bel"] for item in assignments},
                {"RAMB18E2_L", "RAMB18E2_U"},
            )

    def test_half_site_and_hard_cascade_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root, mixed=True)
            value = json.loads(packed.read_text())
            bram = next(cluster for cluster in value["clusters"] if cluster["id"] == "bram")
            bram["site_mode"] = "RAMB18E2x1"
            packed.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "hard-resource support"):
                export_xilinx_openparf_atomic(
                    mapped, packed, architecture, root / "half-site"
                )

            mapped, packed, architecture = _fixture(root, mixed=True)
            value = json.loads(packed.read_text())
            value["cascade_chains"] = [{"kind": "dsp", "instances": ["dsp"]}]
            packed.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "cascade constraints"):
                export_xilinx_openparf_atomic(
                    mapped, packed, architecture, root / "cascade"
                )

    def test_muxf7_cone_is_legalized_as_one_source_sealed_slice_macro(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root)
            mapped.write_text(json.dumps({
                "modules": {"top": {"attributes": {"top": "1"}, "cells": {
                    "lut0": _cell("LUT6", {"I0": [10], "O": [1]}),
                    "lut1": _cell("LUT6", {"I0": [11], "O": [2]}),
                    "mux": _cell("MUXF7", {
                        "I0": [1], "I1": [2], "S": [3], "O": [4],
                    }),
                    "ff": _cell("FDRE", {
                        "C": [20], "CE": ["1"], "D": [4],
                        "Q": [5], "R": ["0"],
                    }),
                }}},
            }), encoding="utf-8")
            packed.write_text(json.dumps({
                "schema": "emuflow.packed-site-netlist/v1", "top": "top",
                "clusters": [
                    {
                        "id": "mux-cone", "kind": "slice",
                        "site_templates": ["SLICEL", "SLICEM"],
                        "control_set": None,
                        "assignments": [
                            {"instance": "lut0", "cell_type": "LUT6", "bel": "A6LUT"},
                            {"instance": "lut1", "cell_type": "LUT6", "bel": "B6LUT"},
                            {"instance": "mux", "cell_type": "MUXF7", "bel": "F7MUX_AB"},
                        ],
                    },
                    {
                        "id": "ordinary", "kind": "slice",
                        "site_templates": ["SLICEL", "SLICEM"],
                        "control_set": None,
                        "assignments": [
                            {"instance": "ff", "cell_type": "FDRE", "bel": "AFF"},
                        ],
                    },
                ],
                "cascade_chains": [],
            }), encoding="utf-8")
            architecture_value = json.loads(architecture.read_text(encoding="utf-8"))
            architecture_value["site_templates"]["SLICEL"]["bels"].extend([
                {
                    "name": bel, "type": "F7MUX", "z": index,
                    "compatible_cells": ["MUXF7"], "placement_mode": "SLICEL",
                }
                for index, bel in enumerate(
                    ("F7MUX_AB", "F7MUX_CD", "F7MUX_EF", "F7MUX_GH")
                )
            ])
            architecture.write_text(json.dumps(architecture_value), encoding="utf-8")
            output = root / "output"
            manifest = export_xilinx_openparf_atomic(
                mapped, packed, architecture, output
            )
            contract = json.loads(
                (output / "physical-macro-groups.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(contract["groups"]), 1)
            group = contract["groups"][0]
            self.assertEqual(group["kind"], "site_macro")
            self.assertEqual(group["owned_resources"], ["MUXF7"])
            self.assertEqual(len(group["windows"]), 4)
            self.assertTrue(all(
                len({entry["site"] for entry in window}) == 1
                for window in group["windows"]
            ))
            names = json.loads((output / "name_map.json").read_text(encoding="utf-8"))
            openparf = {atom["instance"]: atom["openparf"] for atom in names["atoms"]}
            sites = load_xilinx_openparf_atomic_sites(output / "name_map.json")
            slice_sites = [site for site in sites if "LUT" in site["resources"]]
            first, second = slice_sites[:2]
            placement = output / "mux.pl"
            placement.write_text("\n".join([
                f"{openparf['lut0']} {first['dense_x']} {first['dense_y']} 1",
                f"{openparf['lut1']} {first['dense_x']} {first['dense_y']} 3",
                f"{openparf['mux']} {first['dense_x']} {first['dense_y']} 0",
                f"{openparf['ff']} {second['dense_x']} {second['dense_y']} 0",
            ]) + "\n", encoding="utf-8")
            certificate = validate_xilinx_openparf_atomic_placement(
                placement, output / "name_map.json", mapped, architecture
            )
        self.assertTrue(
            manifest["constraint_policy"]["mux_site_macros_use_internal_legalizer"]
        )
        assignments = {
            item["instance"]: item
            for cluster in certificate["clusters"]
            for item in cluster["assignments"]
        }
        self.assertEqual(assignments["mux"]["bel"], "F7MUX_AB")
        self.assertEqual(
            assignments["lut0"]["physical_site"], assignments["mux"]["physical_site"]
        )
        self.assertEqual(
            assignments["lut1"]["physical_site"], assignments["mux"]["physical_site"]
        )

    def test_internal_runner_has_no_fallback_and_keeps_runtime_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root)
            output = root / "output"

            def fake_run(_config, **_kwargs):
                placement = output / "results" / "xilinx_atomic_lut_ff.pl"
                placement.parent.mkdir(parents=True, exist_ok=True)
                placement.write_text("a0 0 0 0\na1 0 0 1\n", encoding="utf-8")
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

    def test_exports_exact_native_dsp_chain_windows_for_in_core_legalizer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = write_openparf_runtime_fixture(
                root, include_hard=True
            )
            mapped_value = json.loads(mapped.read_text(encoding="utf-8"))
            cells = mapped_value["modules"]["top"]["cells"]
            cells["dsp"]["port_directions"]["ACOUT"] = "output"
            cells["dsp"]["connections"]["ACOUT"] = [40_000, 40_001]
            cells["dsp_next"] = {
                "type": "DSP48E2",
                "port_directions": {"ACIN": "input", "P": "output"},
                "connections": {"ACIN": [40_000, 40_001], "P": [40_002]},
            }
            mapped.write_text(json.dumps(mapped_value), encoding="utf-8")
            packed_value = json.loads(packed.read_text(encoding="utf-8"))
            packed_value["clusters"].append({
                "id": "dsp_next", "kind": "hard",
                "site_templates": ["DSP48E2"], "control_set": None,
                "assignments": [{
                    "instance": "dsp_next", "cell_type": "DSP48E2",
                    "bel": "DSP48E2", "bel_candidates": ["DSP48E2"],
                }],
            })
            packed_value["cascade_chains"] = [{
                "id": "dsp-chain", "cell_type": "DSP48E2",
                "instances": ["dsp", "dsp_next"], "links": [],
            }]
            packed.write_text(json.dumps(packed_value), encoding="utf-8")
            architecture_value = json.loads(
                architecture.read_text(encoding="utf-8")
            )
            architecture_value["sites"].append({
                "name": "DSP48E2_X2Y0", "type": "DSP48E2",
                "template": "DSP48E2", "x": 12, "y": 0,
                "tile": {"grid_col": 12, "grid_row": 0},
            })
            architecture.write_text(
                json.dumps(architecture_value), encoding="utf-8"
            )
            native_path = root / "native.json"
            provider_path = root / "provider.json"
            native_path.write_text("{}", encoding="utf-8")
            provider_path.write_text("{}", encoding="utf-8")
            native = {"payload": {
                "dedicated_adjacency": [{
                    "kind": "DSP_CASCADE",
                    "chains": [["DSP48E2_X0Y0", "DSP48E2_X1Y0"]],
                }],
                "bram_tile_groups": [
                    {
                        "anchor": f"RAMB36E2_X{index}Y0",
                        "tile": f"BRAM_TEST_X{index}",
                        "lower": {
                            "site": f"RAMB18_X{index}Y0",
                            "site_type": "RAMB180", "bel": "RAMB18E2_L",
                            "native_site_type": "RAMBFIFO18",
                            "native_bel": "RAMBFIFO18",
                        },
                        "upper": {
                            "site": f"RAMB18_X{index}Y1",
                            "site_type": "RAMB181", "bel": "RAMB18E2_U",
                            "native_site_type": "RAMB181",
                            "native_bel": "RAMB18E2_U",
                        },
                        "whole": {
                            "site": f"RAMB36_X{index}Y0",
                            "site_type": "RAMB36", "bel": "RAMB36E2",
                            "native_site_type": "RAMBFIFO36",
                            "native_bel": "RAMBFIFO36E2",
                        },
                    }
                    for index in range(2)
                ],
            }}
            output = root / "output"
            with mock.patch(
                "emuflow.xilinx_openparf_atomic.load_xilinx_native_device_constraints",
                return_value=(native, {"status": "pass"}),
            ), mock.patch(
                "emuflow.xilinx_openparf_atomic.require_xilinx_native_constraint_capability"
            ):
                report = export_xilinx_openparf_atomic(
                    mapped, packed, architecture, output,
                    native_constraints_path=native_path,
                    provider_manifest_path=provider_path,
                )
            contract = json.loads(
                (output / "physical-macro-groups.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                contract["schema"], OPENPARF_PHYSICAL_MACRO_CONSTRAINT_SCHEMA
            )
            chain = next(group for group in contract["groups"]
                         if group["id"] == "dsp-chain")
            self.assertEqual(chain["source_instances"], ["dsp", "dsp_next"])
            self.assertEqual(
                [site["site"] for site in chain["windows"][0]],
                ["DSP48E2_X0Y0", "DSP48E2_X1Y0"],
            )
            names = json.loads(
                (output / "name_map.json").read_text(encoding="utf-8")
            )
            coordinate_sites = load_xilinx_openparf_atomic_sites(
                output / "name_map.json"
            )
            coordinates = {
                site_name: item
                for item in coordinate_sites
                for site_names in item["physical_sites"].values()
                for site_name in site_names
            }
            for site in chain["windows"][0]:
                coordinate = coordinates[site["site"]]
                self.assertEqual(site["x"], coordinate["placement_x"])
                self.assertEqual(site["y"], coordinate["placement_y"])
            self.assertTrue(
                any(
                    site["x"] != coordinates[site["site"]]["dense_x"]
                    or site["y"] != coordinates[site["site"]]["dense_y"]
                    for site in chain["windows"][0]
                )
            )
            config = json.loads((output / "openparf.json").read_text())
            self.assertEqual(
                Path(config["typed_hardblock_chain_constraints"]),
                (output / "physical-macro-groups.json").resolve(),
            )
            self.assertTrue(
                report["constraint_policy"][
                    "typed_hardblock_chains_use_internal_legalizer"
                ]
            )


if __name__ == "__main__":
    unittest.main()
