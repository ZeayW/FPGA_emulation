from tests.openparf_export_fixture import export_atomic_facts as export_xilinx_openparf_atomic
from tests.openparf_bridge_fixture import materialize_bridge_fixture as materialize_xilinx_openparf_atomic_contract
from contextlib import closing
import json
import fcntl
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from tests import openparf_input_reference as input_reference
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from emuflow_tools.platform.architecture import ArchitectureDB
from emuflow.common.errors import ImportError, ValidationError
import tests.openparf_placement_reference as atomic_adapter
from tests.yosys_reference import import_yosys_json
from emuflow_tools.physical.openparf.xilinx_openparf_atomic import build_xilinx_openparf_atomic_source
from tests.openparf_placement_reference import (OPENPARF_ATOMIC_MANIFEST_SCHEMA, OPENPARF_ATOMIC_PLACEMENT_DATABASE_SCHEMA, OPENPARF_ATOMIC_PLACEMENT_SCHEMA, OPENPARF_ATOMIC_SOURCE_SCHEMA, OPENPARF_PHYSICAL_MACRO_CONSTRAINT_SCHEMA, legalize_xilinx_openparf_ff_control_sets, iter_xilinx_openparf_atomic_placement_clusters, load_xilinx_openparf_atomic_placement_clusters, load_xilinx_openparf_atomic_sites, open_xilinx_openparf_atomic_placement, validate_xilinx_openparf_atomic_placement, materialize_xilinx_openparf_atomic_source, read_xilinx_openparf_atomic_name_map)
from emuflow_tools.physical.openparf.xilinx_openparf_source_storage import OPENPARF_ATOMIC_SOURCE_STORAGE_SCHEMA
from tests.xilinx_site_reference import validate_xilinx_packing
from tests.xilinx_site_reference import pack_xilinx_sites
from tests.xilinx_site_reference import validate_xilinx_placement
from tests.openparf_runtime_fixture import (
    write_openparf_hardblock_cascade_fixture,
    write_openparf_ramb18_fixture,
    write_openparf_runtime_fixture,
)


def _fixture_name_map(path):
    # Unit fixtures deliberately retain their small reference records after
    # the temporary publication disappears. Never retain its lazy reader lease.
    with read_xilinx_openparf_atomic_name_map(path) as view:
        return view.materialize()


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


def _fixture(
    root: Path, *, coincident=False, mixed=False, clock_regions=False,
    multi_slr=False,
):
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
    if clock_regions:
        columns = sorted({site["tile"]["grid_col"] for site in sites})
        rows = sorted({site["tile"]["grid_row"] for site in sites})
        for site in sites:
            row = rows.index(site["tile"]["grid_row"])
            site["physical_region"] = {
                "slr": f"SLR{row}" if multi_slr else "SLR0",
                "clock_region": (
                    f"X{columns.index(site['tile']['grid_col'])}"
                    f"Y{row if multi_slr else 0}"
                ),
            }
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


def _mux_tree_cells(root_type):
    cells = {}
    net = 100
    leaf_count = {"MUXF7": 2, "MUXF8": 4, "MUXF9": 8}[root_type]
    mux7 = []
    for index in range(leaf_count // 2):
        outputs = []
        for side in range(2):
            name = f"lut{index}_{side}"
            cells[name] = _cell(
                "LUT6", {"I0": [10 + 2 * index + side], "O": [net]}
            )
            outputs.append(net)
            net += 1
        name = f"mux7_{index}"
        cells[name] = _cell("MUXF7", {
            "I0": [outputs[0]], "I1": [outputs[1]],
            "S": [30 + index], "O": [net],
        })
        mux7.append((name, net))
        net += 1
    if root_type == "MUXF7":
        root = mux7[0]
    else:
        mux8 = []
        for index in range(len(mux7) // 2):
            name = f"mux8_{index}"
            cells[name] = _cell("MUXF8", {
                "I0": [mux7[2 * index][1]],
                "I1": [mux7[2 * index + 1][1]],
                "S": [40 + index], "O": [net],
            })
            mux8.append((name, net))
            net += 1
        if root_type == "MUXF8":
            root = mux8[0]
        else:
            cells["mux9"] = _cell("MUXF9", {
                "I0": [mux8[0][1]], "I1": [mux8[1][1]],
                "S": [50], "O": [net],
            })
            root = ("mux9", net)
            net += 1
    cells["ff"] = _cell("FDRE", {
        "C": [60], "CE": ["1"], "D": [root[1]],
        "Q": [net], "R": ["0"],
    })
    return cells


def _add_mux_bels(architecture, primitives):
    value = json.loads(architecture.read_text(encoding="utf-8"))
    roles = {
        "MUXF7": ("F7MUX_AB", "F7MUX_CD", "F7MUX_EF", "F7MUX_GH"),
        "MUXF8": ("F8MUX_BOT", "F8MUX_TOP"),
        "MUXF9": ("F9MUX",),
    }
    for primitive in sorted(primitives):
        value["site_templates"]["SLICEL"]["bels"].extend([
            {
                "name": bel, "type": primitive, "z": index,
                "compatible_cells": [primitive], "placement_mode": "SLICEL",
            }
            for index, bel in enumerate(roles[primitive])
        ])
    architecture.write_text(json.dumps(value), encoding="utf-8")


class XilinxOpenparfAtomicTest(unittest.TestCase):
    def test_density_headroom_uses_only_qualified_lut_bels(self):
        # Sixteen z slots encode eight BLEs; paired LUTs are disabled. A
        # seven-LUT region exceeds the 75% density target of the eight BELs,
        # even though it would fit a fictitious sixteen-LUT density model.
        sites = [({"name": "SLICE_X0Y0"}, {"LUT": 16, "FF": 16})]
        with self.assertRaisesRegex(ValidationError, "headroom for LUT"):
            input_reference._validate_native_density_contract(
                sites, [{"resource": "LUT"}] * 7
            )
        units, contract = input_reference._validate_native_density_contract(
            sites, [{"resource": "LUT"}] * 5 + [{"resource": "FF"}]
        )
        self.assertEqual(units, {"LUT": 8, "FF": 16})
        self.assertEqual(contract["LUT"]["movable_area"], 5 / 8)
        self.assertEqual(contract["LUT"]["filler_units"], 3)
        self.assertEqual(contract["FF"]["movable_area"], 1 / 16)
        self.assertEqual(sites[0][1]["LUT"], 16)

    def test_all_clock_region_sites_remain_available_to_placement(self):
        sites = []
        for x in range(2):
            for y in range(4):
                sites.append(({
                    "name": f"SLICE_X{x}Y{y}",
                    "type": "SLICEL", "x": x, "y": y,
                    "physical_region": {
                        "slr": "SLR0", "clock_region": "X0Y0",
                    },
                }, {"LUT": 16, "FF": 16}))

        available, contract = input_reference._expose_all_clock_region_sites(sites)

        self.assertEqual(contract["status"], "pass")
        self.assertEqual(contract["limit"], 1.0)
        self.assertEqual(contract["policy"], "full-qualified-site-domain")
        self.assertEqual(contract["original_sites"], 8)
        self.assertEqual(contract["available_sites"], 8)
        self.assertEqual(contract["reserved_sites"], 0)
        by_x = {}
        for site, _resources in available:
            by_x.setdefault(site["x"], []).append(site["y"])
        self.assertEqual(sorted(by_x[0]), [0, 1, 2, 3])
        self.assertEqual(sorted(by_x[1]), [0, 1, 2, 3])

    def test_site_geometry_preserves_empty_physical_rows(self):
        sites = [
            (
                {
                    "name": f"SLICE_X{x}Y{y}", "type": "SLICEL",
                    "x": x, "y": y,
                    "tile": {"grid_col": x, "grid_row": physical_y},
                },
                {"LUT": 16, "FF": 16},
            )
            for x in range(2)
            for y, physical_y in ((0, 0), (1, 3))
        ]
        _prefix, suffix, coordinates = input_reference._render_site_geometry(
            sites
        )
        self.assertEqual(coordinates["y_axis"], [0, 1, 2, 3])
        self.assertIn("SITEMAP 2 4\n", suffix)
        rows = [
            tuple(map(int, line.split()[:2]))
            for line in suffix.split("SITEMAP 2 4\n", 1)[1].split(
                "END SITEMAP", 1
            )[0].strip().splitlines()
        ]
        self.assertEqual(rows, [(0, 0), (0, 1), (0, 3), (1, 0), (1, 1), (1, 3)])
        self.assertIn("SITE EMUFLOW_UNAVAILABLE\nEND SITE", _prefix)
        self.assertEqual(
            [item["placement_y"] for item in coordinates["sites"]],
            [0.5, 3.5, 0.5, 3.5],
        )

    def test_logic_gaps_do_not_stretch_sites_or_change_hardblock_spans(self):
        sites = [
            ({"name": "SLICE_X0Y0", "type": "SLICEL", "x": 0, "y": 0},
             {"LUT": 16, "FF": 16}),
            ({"name": "SLICE_X0Y3", "type": "SLICEL", "x": 0, "y": 3},
             {"LUT": 16, "FF": 16}),
            ({"name": "DSP48E2_X1Y0", "type": "DSP48E2", "x": 1, "y": 0},
             {"DSP48E2": 1}),
            ({"name": "DSP48E2_X1Y5", "type": "DSP48E2", "x": 1, "y": 5},
             {"DSP48E2": 1}),
        ]
        _prefix, suffix, coordinates = input_reference._render_site_geometry(sites)
        rows = [line.split() for line in suffix.split("SITEMAP 2 6\n", 1)[1]
                .split("END SITEMAP", 1)[0].strip().splitlines()]
        # Apply the native reader's next-entry bounding-box rule to the
        # exported grammar, including the trailing unavailable interval.
        logic_area = 0
        for index, (x, y, site_type) in enumerate(rows):
            if x != "0" or site_type == "EMUFLOW_UNAVAILABLE":
                continue
            upper = int(rows[index + 1][1]) if (
                index + 1 < len(rows) and rows[index + 1][0] == x
            ) else 6
            logic_area += upper - int(y)
        self.assertEqual(logic_area, 2)
        self.assertIn(["0", "4", "EMUFLOW_UNAVAILABLE"], rows)
        hard_sites = [item for item in coordinates["sites"]
                      if "DSP48E2" in item["resources"]]
        self.assertEqual([item["placement_y"] for item in hard_sites], [2.5, 5.5])

    def test_slr_seams_cover_both_dense_y_orientations(self):
        sites = []
        for slr_index, (slr, start) in enumerate(
            (("SLR0", 0), ("SLR1", 4))
        ):
            for physical_y in range(start, start + 3):
                sites.append(({
                    "name": f"SLICE_X0Y{physical_y}",
                    "type": "SLICEL",
                    "x": 0,
                    "y": physical_y,
                    "tile": {"grid_col": 0, "grid_row": physical_y},
                    "physical_region": {
                        "slr": slr,
                        "clock_region": f"X0Y{slr_index}",
                    },
                }, {"LUT": 16, "FF": 16}))

        for order in ("ascending", "descending"):
            _prefix, _suffix, coordinates = (
                input_reference._render_site_geometry(
                    sites, y_axis_order=order
                )
            )
            regions = coordinates["super_logic_regions"]["regions"]
            self.assertEqual(
                sorted(tuple(region["bbox"]) for region in regions),
                [(0, 0, 0, 3), (0, 4, 0, 7)],
            )
            clock_regions = coordinates["clock_regions"]["regions"]
            self.assertEqual(
                sorted(tuple(region["bbox"]) for region in clock_regions),
                [(0, 0, 0, 3), (0, 4, 0, 7)],
            )
            for y in range(8):
                self.assertEqual(sum(region["bbox"][1] <= y <= region["bbox"][3]
                                     for region in clock_regions), 1)

    def test_clock_grid_covers_real_nvdla_seam_shape_without_moving_sites(self):
        observed = [(2, 62), (64, 124), (126, 186), (188, 248), (250, 310),
                    (313, 373), (375, 435), (437, 497), (499, 559), (561, 621)]
        regions = {
            f"X0Y{i}": {"index": (0, i), "sites": [(0, low), (448, high)]}
            for i, (low, high) in enumerate(observed)
        }
        input_reference._complete_clock_region_boxes(
            regions, 449, 622, y_axis_order="descending",
        )
        for y in range(622):
            self.assertEqual(sum(r["bbox"][1] <= y <= r["bbox"][3]
                                 for r in regions.values()), 1)
        for i, (low, high) in enumerate(observed):
            xl, yl, xh, yh = regions[f"X0Y{i}"]["bbox"]
            self.assertEqual((xl, xh), (0, 448))
            self.assertLessEqual(yl, low)
            self.assertGreaterEqual(yh, high)
        self.assertEqual(regions["X0Y0"]["bbox"], (0, 0, 448, 62))
        self.assertEqual(regions["X0Y5"]["bbox"], (0, 311, 448, 373))

    def test_placement_geometry_does_not_materialize_every_site_bel_list(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _mapped, _packed, architecture_path = _fixture(root, mixed=True)
            architecture = ArchitectureDB.load(architecture_path)
            with mock.patch.object(
                ArchitectureDB,
                "sites",
                new_callable=mock.PropertyMock,
                side_effect=AssertionError(
                    "placement geometry must scan compact raw sites"
                ),
            ):
                sites = input_reference._placement_sites(
                    architecture,
                    ["DSP48E2", "RAMB36E2", "URAM288"],
                )
            self.assertEqual(len(sites), 10)
            self.assertEqual(
                sum("LUT" in resources for _site, resources in sites),
                4,
            )
            self.assertEqual(
                {
                    resource: sum(
                        resources.get(resource, 0)
                        for _site, resources in sites
                    )
                    for resource in ("DSP48E2", "RAMB36E2", "URAM288")
                },
                {"DSP48E2": 2, "RAMB36E2": 2, "URAM288": 2},
            )

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
                mapped, atomic_source, architecture, output, top="top",
                packed_value=source,
            )
            serialized_source = json.loads(
                atomic_source.read_text(encoding="utf-8")
            )
            compatibility_source = root / "atomic-source-compatibility.json"
            materialized_source = materialize_xilinx_openparf_atomic_source(
                atomic_source, compatibility_source
            )
            lazy_clusters = list(source["clusters"])
            imported_clocks = import_yosys_json(mapped).value["clocks"]
        self.assertEqual(source["schema"], OPENPARF_ATOMIC_SOURCE_SCHEMA)
        self.assertNotIsInstance(source["clusters"], list)
        self.assertEqual(
            serialized_source["schema"], OPENPARF_ATOMIC_SOURCE_STORAGE_SCHEMA
        )
        self.assertNotIn("clusters", serialized_source)
        self.assertEqual(
            materialized_source["clusters"], lazy_clusters
        )
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

    def test_retired_python_bookshelf_renderers_are_not_exposed(self):
        self.assertFalse(hasattr(atomic_adapter, "_render_library"))
        self.assertFalse(hasattr(atomic_adapter, "_render_nets"))
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "design.nets"
            output.write_text("prior generation\n")
            with self.assertRaisesRegex(ValidationError, "current typed owners"):
                input_reference._render_library_and_nets({}, [], {}, nets_path=output)
            self.assertEqual(output.read_text(), "prior generation\n")
            mapped, packed, architecture = _fixture(Path(temporary))
            with self.assertRaisesRegex(ValidationError, "current mapped-index owner"):
                atomic_adapter.export_xilinx_openparf_atomic(
                    mapped, packed, architecture, Path(temporary) / "untyped-export"
                )

    def test_non_degenerate_runtime_fixture_export_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = write_openparf_runtime_fixture(root)
            output = root / "output"
            manifest = export_xilinx_openparf_atomic(
                mapped, packed, architecture, output
            )
            config = json.loads((output / "openparf.json").read_text())
            names = _fixture_name_map(output / "name_map.json")
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
            name_selector = json.loads(
                (output / "name_map.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("atoms", name_selector)
            self.assertIn(name_selector["artifact"], manifest["files"])
            self.assertTrue((output / name_selector["artifact"]).is_file())
            self.assertEqual(manifest["name_map_storage"]["atoms"], 128)
            self.assertEqual(
                manifest["name_map_storage"]["store"]["sha256"],
                name_selector["store_sha256"],
            )

        declarations = [
            line.split() for line in net_lines if line.startswith("net ")
        ]
        self.assertEqual(manifest["atoms"], 128)
        self.assertEqual(manifest["resources"], {"FF": 64, "LUT": 64})
        self.assertEqual(
            manifest["resource_unit_capacity"], {"LUT": 8, "FF": 16}
        )
        self.assertEqual(manifest["placement_region"], {
            "logic_sites": 16,
            "dense_width": 4,
            "dense_height": 4,
            "occupied_fraction": 1.0,
            "available_logic_sites": 16,
        })
        self.assertEqual(manifest["density_contract"]["LUT"], {
            "movable_area": 8.0,
            "placeable_area": 16.0,
            "target_area": 12.0,
            "filler_units": 64,
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
        self.assertTrue(config["emuflow_stable_global_placement"])
        self.assertEqual(config["emuflow_min_feasible_iterations"], 96)
        self.assertEqual(config["emuflow_convergence_patience"], 64)
        self.assertEqual(config["emuflow_relative_hpwl_improvement"], 1.0e-4)
        self.assertEqual(
            config["emuflow_post_adjustment_iteration_reserve"], 384
        )
        self.assertEqual(config["emuflow_diagnostic_handoff_overflow"], 0.25)
        self.assertEqual(config["emuflow_diagnostic_handoff_patience"], 64)
        self.assertEqual(config["stop_overflow"], 0.10)
        self.assertEqual(config["emuflow_high_fanout_weight_threshold"], 64)
        self.assertEqual(config["emuflow_high_fanout_weight_exponent"], 0.5)
        self.assertEqual(config["emuflow_high_fanout_weight_maximum"], 4.0)
        self.assertEqual(config["wlw_psi_step_size"], 0.002)
        self.assertEqual(config["wlw_delta_sll_ema_weight"], 0.9)
        self.assertEqual(config["gp_adjust_area"], 1)
        self.assertEqual(config["gp_adjust_area_types"], ["LUT", "FF"])
        self.assertEqual(config["gp_max_adjust_area_iters"], 6)
        self.assertEqual(config["gp_adjust_area_overflow_threshold"], 0.15)
        self.assertEqual(config["gp_adjust_route_area"], 1)
        self.assertEqual(config["gp_adjust_area_route_opt_adjust_exponent"], 2.0)
        self.assertEqual(config["gp_adjust_area_max_route_opt_adjust_rate"], 2.0)
        self.assertEqual(config["gp_adjust_pin_area"], 1)
        self.assertEqual(config["gp_adjust_area_max_pin_opt_adjust_rate"], 1.6)
        self.assertEqual(config["gp_adjust_resource_area"], 0)
        self.assertEqual(manifest["routability_contract"], {
            "provider": "openparf-rudy-pin-stable-feasible-full-sites-v8",
            "area_types": ["LUT", "FF"],
            "global_placement_stop_overflow": 0.10,
            "diagnostic_legalization_handoff": {
                "maximum_logic_overflow": 0.25,
                "hpwl_patience": 64,
                "requires_exact_legalization_evidence": True,
                "authorizes_routing_release": False,
            },
            "adjustment_overflow_threshold": 0.15,
            "maximum_iterations": 6,
            "route_adjustment_exponent": 2.0,
            "maximum_route_adjustment_rate": 2.0,
            "maximum_pin_adjustment_rate": 1.6,
            "resource_area_adjustment": False,
            "density_filler_sampling": {
                "provider": "density-bin-bounded-fillers-v2",
                "preferred_maximum_rows_per_area_type": 65536,
                "maximum_bin_span": [0.5, 0.5],
            },
            "sll_weighting": {
                "provider": "leaps-fixed-adam-step-v2",
                "step_size": 0.002,
                "delta_sll_ema_weight": 0.9,
                "accepted_range": [0.0, 1000.0],
            },
            "convergence": {
                "provider": "best-feasible-hpwl-patience-v1",
                "minimum_feasible_iterations": 96,
                "patience": 64,
                "relative_hpwl_improvement": 1.0e-4,
                "maximum_iterations_is_failure": True,
                "post_adjustment_iteration_reserve": 384,
            },
        })
        self.assertNotIn("fallback", config)

    def test_export_limits_native_site_database_to_allowed_slrs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root)
            value = json.loads(architecture.read_text(encoding="utf-8"))
            original = value["sites"]
            for site in original:
                site["physical_region"] = {
                    "slr": "SLR1", "clock_region": "X0Y1",
                }
            replicas = []
            for site in original:
                replica = json.loads(json.dumps(site))
                replica["name"] += "_SLR0"
                replica["y"] += 10
                replica["tile"]["grid_row"] += 10
                replica["physical_region"] = {
                    "slr": "SLR0", "clock_region": "X0Y0",
                }
                replicas.append(replica)
            value["sites"] = replicas + original
            architecture.write_text(json.dumps(value), encoding="utf-8")

            output = root / "output"
            manifest = export_xilinx_openparf_atomic(
                mapped, packed, architecture, output,
                allowed_slrs=("SLR1",),
            )
            sites = load_xilinx_openparf_atomic_sites(output / "name_map.json")

        self.assertEqual(manifest["placement_region"]["allowed_slrs"], ["SLR1"])
        self.assertTrue(sites)
        self.assertTrue(all(not item["site"].endswith("_SLR0") for item in sites))

    def test_clock_region_grid_is_exported_without_unsafe_native_enforcement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(
                root, clock_regions=True
            )
            output = root / "output"
            manifest = export_xilinx_openparf_atomic(
                mapped, packed, architecture, output
            )
            config = json.loads((output / "openparf.json").read_text())
            sites = (output / "design.scl").read_text()

        self.assertIn("CLOCKREGIONS 2 1", sites)
        self.assertIn("CLOCKREGION X0Y0 : 0 0 0 1 1 0", sites)
        self.assertIn("CLOCKREGION X1Y0 : 1 0 1 1 1 1", sites)
        self.assertIn("END CLOCKREGIONS", sites)
        self.assertIn("SUPERLOGICREGIONS 1 1", sites)
        self.assertIn(
            "SUPERLOGICREGION SLR0 ULTRASCALE_PLUS : 0 0 2 2", sites
        )
        self.assertIn("END SUPERLOGICREGIONS", sites)
        self.assertEqual(config["honor_clock_region_constraints"], 0)
        self.assertEqual(config["confine_clock_region_flag"], 0)
        self.assertEqual(config["count_ck_cr"], 0)
        self.assertEqual(config["honor_half_column_constraints"], 0)
        self.assertEqual(config["maximum_clock_per_clock_region"], 24)
        self.assertEqual(config["maximum_clock_per_half_column"], 0)
        self.assertEqual(manifest["clock_region_contract"], {
            "width": 2,
            "height": 1,
            "geometry": "complete-dense-grid-empty-seams-no-fabricated-sites-v1",
            "maximum_clocks_per_region": 24,
            "native_enforcement": "disabled",
            "native_enforcement_reason": (
                "OpenPARF UTPlaceFX cannot safely represent the current "
                "multi-resource UltraScale+ site model or discover its clock "
                "sources"
            ),
            "regions": [
                {
                    "name": "X0Y0", "x": 0, "y": 0, "slr": "SLR0",
                    "bbox": [0, 0, 0, 1],
                },
                {
                    "name": "X1Y0", "x": 1, "y": 0, "slr": "SLR0",
                    "bbox": [1, 0, 1, 1],
                },
            ],
        })
        self.assertEqual(config["slr_aware_flag"], 0)
        self.assertEqual(manifest["slr_contract"], {
            "width": 1,
            "height": 1,
            "native_enforcement": "not-required",
            "regions": [{
                "name": "SLR0", "x": 0, "y": 0,
                "bbox": [0, 0, 1, 1],
            }],
        })

    def test_multi_slr_grid_enables_native_slr_objective(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(
                root, clock_regions=True, multi_slr=True
            )
            output = root / "output"
            manifest = export_xilinx_openparf_atomic(
                mapped, packed, architecture, output
            )
            config = json.loads((output / "openparf.json").read_text())
            sites = (output / "design.scl").read_text()

        self.assertIn("CLOCKREGIONS 2 2", sites)
        self.assertIn("SUPERLOGICREGIONS 1 2", sites)
        self.assertIn(
            "SUPERLOGICREGION SLR0 ULTRASCALE_PLUS : 0 0 2 1", sites
        )
        self.assertIn(
            "SUPERLOGICREGION SLR1 ULTRASCALE_PLUS : 0 1 2 1", sites
        )
        self.assertEqual(config["slr_aware_flag"], 1)
        self.assertEqual(manifest["slr_contract"], {
            "width": 1,
            "height": 2,
            "native_enforcement": "enabled",
            "regions": [
                {"name": "SLR0", "x": 0, "y": 0,
                 "bbox": [0, 0, 1, 0]},
                {"name": "SLR1", "x": 0, "y": 1,
                 "bbox": [0, 1, 1, 1]},
            ],
        })

    def test_multi_slr_grid_preserves_and_assigns_empty_seam_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(
                root, clock_regions=True, multi_slr=True
            )
            value = json.loads(architecture.read_text())
            # Model the real UltraScale+ shape: each SLR has the same row
            # pitch, with an unoccupied seam before the next SLR.  The final
            # seam is implicit because no site occupies it.
            for site in value["sites"]:
                if site["physical_region"]["slr"] == "SLR1":
                    site["tile"]["grid_row"] += 2
            architecture.write_text(json.dumps(value))
            output = root / "output"
            manifest = export_xilinx_openparf_atomic(
                mapped, packed, architecture, output
            )
            sites = (output / "design.scl").read_text()

        self.assertIn("SITEMAP 2 6", sites)
        self.assertIn(
            "SUPERLOGICREGION SLR0 ULTRASCALE_PLUS : 0 0 2 3", sites
        )
        self.assertIn(
            "SUPERLOGICREGION SLR1 ULTRASCALE_PLUS : 0 1 2 3", sites
        )
        self.assertEqual(
            [region["bbox"] for region in manifest["slr_contract"]["regions"]],
            [[0, 0, 1, 2], [0, 3, 1, 5]],
        )

    def test_non_degenerate_multi_slr_runtime_fixture_export_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = write_openparf_runtime_fixture(
                root, clock_regions=True, multi_slr=True
            )
            output = root / "output"
            manifest = export_xilinx_openparf_atomic(
                mapped, packed, architecture, output
            )
            config = json.loads((output / "openparf.json").read_text())
            sites = (output / "design.scl").read_text()

        self.assertIn("SUPERLOGICREGIONS 1 2", sites)
        self.assertEqual(config["slr_aware_flag"], 1)
        self.assertEqual(manifest["slr_contract"]["native_enforcement"], "enabled")
        self.assertEqual(
            [region["name"] for region in manifest["slr_contract"]["regions"]],
            ["SLR0", "SLR1"],
        )

    def test_partial_or_incomplete_clock_region_grid_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(
                root, clock_regions=True
            )
            value = json.loads(architecture.read_text())
            del value["sites"][0]["physical_region"]
            architecture.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValidationError, "coverage is partial"):
                export_xilinx_openparf_atomic(
                    mapped, packed, architecture, root / "partial"
                )

            gap_root = root / "gap"
            gap_root.mkdir()
            mapped, packed, architecture = _fixture(
                gap_root, clock_regions=True
            )
            value = json.loads(architecture.read_text())
            for site in value["sites"]:
                if site["physical_region"]["clock_region"] == "X1Y0":
                    site["physical_region"]["clock_region"] = "X2Y0"
            architecture.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValidationError, "grid is incomplete"):
                export_xilinx_openparf_atomic(
                    mapped, packed, architecture, root / "gap-output"
                )

    def test_site_database_descriptor_and_metadata_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = write_openparf_runtime_fixture(root)
            output = root / "output"
            export_xilinx_openparf_atomic(mapped, packed, architecture, output)
            name_map_path = output / "name_map.json"
            name_map = read_xilinx_openparf_atomic_name_map(name_map_path)

            site_database = output / name_map["site_database"]["file"]
            name_map.close()
            site_database.unlink()
            with self.assertRaisesRegex(ValidationError, "database is missing"):
                load_xilinx_openparf_atomic_sites(name_map_path)

            export_xilinx_openparf_atomic(mapped, packed, architecture, output)
            with closing(sqlite3.connect(output / "site-map.sqlite3")) as database, database:
                database.execute(
                    "UPDATE metadata SET value = 'wrong' WHERE key = 'schema'"
                )
            with self.assertRaisesRegex(ValidationError, "metadata is invalid"):
                load_xilinx_openparf_atomic_sites(name_map_path)

    def test_all_placement_consumers_reject_mixed_packed_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = write_openparf_runtime_fixture(root)
            output = root / "output"
            manifest = export_xilinx_openparf_atomic(
                mapped, packed, architecture, output
            )
            packed.write_text(
                packed.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
            mismatched_packed_sha = atomic_adapter._sha256(packed)
            self.assertEqual(
                manifest["name_map_storage"]["source"]["mapped_sha256"],
                atomic_adapter._sha256(mapped),
            )
            with self.assertRaisesRegex(ValidationError, "packed source seal"):
                load_xilinx_openparf_atomic_sites(
                    output / "name_map.json",
                    packed_sha256=mismatched_packed_sha,
                )
            with self.assertRaisesRegex(ValidationError, "packed source seal"):
                legalize_xilinx_openparf_ff_control_sets(
                    output / "missing.pl",
                    output / "name_map.json",
                    mapped,
                    output / "legalization.json",
                    packed_path=packed,
                )
            with self.assertRaisesRegex(ValidationError, "packed source seal"):
                validate_xilinx_openparf_atomic_placement(
                    output / "missing.pl",
                    output / "name_map.json",
                    mapped,
                    architecture,
                    packed_path=packed,
                )

    def test_parallel_exports_share_immutable_device_geometry_and_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture_path = write_openparf_runtime_fixture(
                root, include_hard=True
            )
            architecture = ArchitectureDB.load(architecture_path)
            outputs = [root / "fpga0", root / "fpga1"]
            with ThreadPoolExecutor(max_workers=2) as executor:
                manifests = list(executor.map(
                    lambda output: export_xilinx_openparf_atomic(
                        mapped,
                        packed,
                        architecture_path,
                        output,
                        architecture=architecture,
                    ),
                    outputs,
                ))

            self.assertEqual(manifests[0]["placement_region"],
                             manifests[1]["placement_region"])
            databases = [output / "site-map.sqlite3" for output in outputs]
            stats = [path.stat() for path in databases]
            self.assertEqual(databases[0].read_bytes(), databases[1].read_bytes())
            self.assertEqual(
                (outputs[0] / "design.scl").read_text(encoding="utf-8"),
                (outputs[1] / "design.scl").read_text(encoding="utf-8"),
            )

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
            names = _fixture_name_map(output / "name_map.json")
            hard_atoms = [
                atom for atom in names["atoms"]
                if atom["resource"] in {"DSP48E2", "RAMB36E2", "URAM288"}
            ]
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
            "LUT": 8, "FF": 16, "DSP48E2": 1,
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
            names = _fixture_name_map(output / "name_map.json")
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
            names = _fixture_name_map(output / "name_map.json")
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
        lut_dimensions = config["gp_model2area_types_map"]["LUT6"]["LUT"]
        self.assertAlmostEqual(lut_dimensions[0] * lut_dimensions[1], 1 / 8)
        self.assertEqual(config["gp_model2area_types_map"]["FDRE"]["FF"], [0.25, 0.25])
        self.assertEqual(config["CLB_capacity"], 16)
        self.assertEqual(config["BLE_capacity"], 2)
        self.assertEqual(config["allow_paired_luts"], 0)
        self.assertEqual(
            config["gp_max_fillers_per_area_type"],
            {"FF": 65536, "LUT": 65536},
        )
        self.assertEqual(config["gp_max_logic_filler_bin_span"], 0.5)
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

    def test_import_persists_compact_queryable_certificate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root)
            output = root / "output"
            export_xilinx_openparf_atomic(mapped, packed, architecture, output)
            placement = output / "placed.pl"
            placement.write_text("a0 0 0 0\na1 0 0 1\n", encoding="utf-8")
            certificate_path = output / "certificate.json"
            returned = validate_xilinx_openparf_atomic_placement(
                placement,
                output / "name_map.json",
                mapped,
                architecture,
                certificate_path,
            )
            persisted = json.loads(certificate_path.read_text(encoding="utf-8"))
            self.assertNotIn("clusters", persisted)
            self.assertEqual(
                persisted["cluster_storage"]["schema"],
                OPENPARF_ATOMIC_PLACEMENT_DATABASE_SCHEMA,
            )
            self.assertEqual(
                load_xilinx_openparf_atomic_placement_clusters(
                    certificate_path, persisted
                ),
                returned["clusters"],
            )
            self.assertEqual(
                list(iter_xilinx_openparf_atomic_placement_clusters(
                    certificate_path, persisted
                )),
                returned["clusters"],
            )
            database_path = output / persisted["cluster_storage"]["file"]
            with database_path.open("ab") as stream:
                stream.write(b"tamper")
            with self.assertRaisesRegex(ValidationError, "database seal"):
                list(iter_xilinx_openparf_atomic_placement_clusters(
                    certificate_path, persisted
                ))

    def test_production_import_streams_directly_to_content_addressed_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root)
            output = root / "output"
            export_xilinx_openparf_atomic(mapped, packed, architecture, output)
            placement = output / "placed.pl"
            placement.write_text("a0 0 0 0\na1 0 0 1\n", encoding="utf-8")
            certificate_path = output / "placement-certificate.json"
            certificate = validate_xilinx_openparf_atomic_placement(
                placement, output / "name_map.json", mapped, architecture,
                certificate_path, materialize_result=False,
            )
            self.assertNotIn("clusters", certificate)
            descriptor = certificate["cluster_storage"]
            self.assertEqual(
                descriptor["file"],
                f"placement-certificate.{descriptor['sha256']}.sqlite",
            )
            self.assertEqual(descriptor["clusters"], 1)
            self.assertEqual(descriptor["assignments"], 2)
            self.assertEqual(
                [
                    assignment["instance"]
                    for cluster in iter_xilinx_openparf_atomic_placement_clusters(
                        certificate_path, certificate
                    )
                    for assignment in cluster["assignments"]
                ],
                ["ff", "lut"],
            )

    def test_production_bridge_rejects_inline_legacy_certificate(self):
        from tests.physical_handoff_native_fixture import prepare_native_handoff
        from emuflow_tools.physical.handoff import materialize_xilinx_openparf_atomic_contract as native_handoff
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            facts = prepare_native_handoff(root / "sources")
            certificate = json.loads(facts.certificate.read_text())
            certificate["clusters"] = []
            facts.certificate.write_text(json.dumps(certificate))
            with self.assertRaisesRegex(ValidationError, "inline clusters are retired"):
                native_handoff(facts.mapped, facts.architecture, facts.certificate,
                    root / "handoff", source_packed_path=facts.source)


    def test_production_certificate_binds_packed_source_bytes(self):
        from tests.physical_handoff_native_fixture import prepare_native_handoff, build_native_handoff
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            facts = prepare_native_handoff(root / "sources")
            facts.source.write_text(facts.source.read_text() + "\n")
            with self.assertRaisesRegex(ValidationError, "source|seal"):
                build_native_handoff(facts, root / "handoff")


    def test_certificate_publication_is_selector_last_and_reader_leased(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root)
            output = root / "output"
            export_xilinx_openparf_atomic(mapped, packed, architecture, output)
            placement = output / "placed.pl"
            placement.write_text("a0 0 0 0\na1 0 0 1\n", encoding="utf-8")
            certificate_path = output / "placement-certificate.json"
            first = validate_xilinx_openparf_atomic_placement(
                placement, output / "name_map.json", mapped, architecture,
                certificate_path, materialize_result=False,
            )
            selector_bytes = certificate_path.read_bytes()
            first_store = output / first["cluster_storage"]["file"]
            placement.write_text("a0 0 0 0\na1 0 1 1\n", encoding="utf-8")
            real_replace = os.replace

            def fail_selector(source, destination):
                if Path(destination) == certificate_path:
                    raise OSError("injected selector commit failure")
                return real_replace(source, destination)

            with mock.patch(
                'tests.openparf_placement_reference.os.replace',
                side_effect=fail_selector,
            ):
                with self.assertRaisesRegex(OSError, "injected"):
                    validate_xilinx_openparf_atomic_placement(
                        placement, output / "name_map.json", mapped,
                        architecture, certificate_path,
                        materialize_result=False,
                    )
            self.assertEqual(certificate_path.read_bytes(), selector_bytes)
            self.assertTrue(first_store.is_file())
            self.assertEqual(
                len(list(output.glob("placement-certificate.*.sqlite"))), 1
            )
            self.assertEqual(
                len(list(iter_xilinx_openparf_atomic_placement_clusters(
                    certificate_path, first
                ))),
                1,
            )

            view = open_xilinx_openparf_atomic_placement(certificate_path)
            writer_started = threading.Event()

            def publish_replacement():
                writer_started.set()
                return validate_xilinx_openparf_atomic_placement(
                    placement, output / "name_map.json", mapped, architecture,
                    certificate_path, materialize_result=False,
                )

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(publish_replacement)
                self.assertTrue(writer_started.wait(timeout=1))
                time.sleep(0.05)
                self.assertFalse(future.done())
                self.assertEqual(
                    next(view.iter_clusters())["site"], "SLICE_X0Y0"
                )
                view.close()
                second = future.result(timeout=5)
            self.assertNotEqual(
                second["cluster_storage"]["sha256"],
                first["cluster_storage"]["sha256"],
            )
            self.assertFalse(first_store.exists())
            self.assertEqual(
                len(list(output.glob("placement-certificate.*.sqlite"))), 1
            )

    def test_typed_certificate_rejects_resealed_schema_extension(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root)
            output = root / "output"
            export_xilinx_openparf_atomic(mapped, packed, architecture, output)
            placement = output / "placed.pl"
            placement.write_text("a0 0 0 0\na1 0 0 1\n", encoding="utf-8")
            certificate_path = output / "placement-certificate.json"
            certificate = validate_xilinx_openparf_atomic_placement(
                placement, output / "name_map.json", mapped, architecture,
                certificate_path, materialize_result=False,
            )
            old_store = output / certificate["cluster_storage"]["file"]
            mutated = output / "mutated.sqlite"
            shutil.copy2(old_store, mutated)
            with closing(sqlite3.connect(mutated)) as database, database:
                database.execute("CREATE TABLE injected(value TEXT)")
            digest = atomic_adapter._sha256(mutated)
            new_store = output / f"placement-certificate.{digest}.sqlite"
            mutated.rename(new_store)
            descriptor = dict(certificate["cluster_storage"])
            descriptor.update({
                "file": new_store.name,
                "bytes": new_store.stat().st_size,
                "sha256": digest,
            })
            certificate["cluster_storage"] = descriptor
            certificate_path.write_text(json.dumps(certificate), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "database schema"):
                list(iter_xilinx_openparf_atomic_placement_clusters(
                    certificate_path, certificate
                ))

    def test_typed_certificate_rejects_resealed_column_contract_changes(self):
        def certificate_at(root):
            path = root / "certificate.json"
            value = atomic_adapter.write_xilinx_openparf_atomic_placement_certificate(
                path,
                {
                    "schema": OPENPARF_ATOMIC_PLACEMENT_SCHEMA,
                    "clusters": [{
                        "cluster": "openparf:SLICE_X0Y0",
                        "site": "SLICE_X0Y0", "site_type": "SLICEL",
                        "x": 0, "y": 0,
                        "assignments": [{
                            "instance": "u0", "cell_type": "LUT6",
                            "bel": "A6LUT", "physical_site": "SLICE_X0Y0",
                            "placement_mode": "SLICEL",
                            "source_cluster": "atomic-source:u0",
                            "bram_tile_group": None,
                        }],
                    }],
                },
            )
            return path, value

        mutations = {
            "extra": "ALTER TABLE assignments ADD COLUMN injected TEXT",
            "missing": """
                ALTER TABLE assignments RENAME TO old_assignments;
                CREATE TABLE assignments (
                    cluster_index INTEGER NOT NULL REFERENCES clusters(cluster_index),
                    assignment_index INTEGER NOT NULL,
                    instance TEXT NOT NULL UNIQUE,
                    cell_type TEXT NOT NULL,
                    bel TEXT NOT NULL,
                    physical_site TEXT NOT NULL,
                    placement_mode TEXT NOT NULL,
                    source_cluster TEXT NOT NULL,
                    PRIMARY KEY (cluster_index, assignment_index)
                ) WITHOUT ROWID;
                INSERT INTO assignments SELECT cluster_index,assignment_index,
                    instance,cell_type,bel,physical_site,placement_mode,
                    source_cluster FROM old_assignments;
                DROP TABLE old_assignments;
            """,
        }
        for name, statement in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                path, value = certificate_at(root)
                original = root / value["cluster_storage"]["file"]
                mutated = root / "mutated.sqlite"
                shutil.copy2(original, mutated)
                with closing(sqlite3.connect(mutated)) as database, database:
                    database.executescript(statement)
                digest = atomic_adapter._sha256(mutated)
                store = root / f"certificate.{digest}.sqlite"
                mutated.rename(store)
                descriptor = dict(value["cluster_storage"])
                descriptor.update({
                    "file": store.name, "bytes": store.stat().st_size,
                    "sha256": digest,
                })
                value["cluster_storage"] = descriptor
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(ValidationError, "column contract"):
                    list(iter_xilinx_openparf_atomic_placement_clusters(
                        path, value
                    ))

    def test_typed_certificate_rejects_orphan_even_when_resealed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "certificate.json"
            value = atomic_adapter.write_xilinx_openparf_atomic_placement_certificate(
                path,
                {
                    "schema": OPENPARF_ATOMIC_PLACEMENT_SCHEMA,
                    "clusters": [{
                        "cluster": "openparf:SLICE_X0Y0",
                        "site": "SLICE_X0Y0", "site_type": "SLICEL",
                        "x": 0, "y": 0,
                        "assignments": [{
                            "instance": "u0", "cell_type": "LUT6",
                            "bel": "A6LUT", "physical_site": "SLICE_X0Y0",
                            "placement_mode": "SLICEL",
                            "source_cluster": "atomic-source:u0",
                            "bram_tile_group": None,
                        }],
                    }],
                },
            )
            original = root / value["cluster_storage"]["file"]
            mutated = root / "mutated.sqlite"
            shutil.copy2(original, mutated)
            descriptor = dict(value["cluster_storage"])
            descriptor["assignments"] = 2
            header = dict(value)
            header.pop("cluster_storage")
            with closing(sqlite3.connect(mutated)) as database, database:
                database.execute(
                    "INSERT INTO assignments VALUES (?,?,?,?,?,?,?,?,?)",
                    (7, 0, "orphan", "LUT6", "B6LUT", "SLICE_X0Y1",
                     "SLICEL", "atomic-source:orphan", None),
                )
                database.execute(
                    "UPDATE metadata SET value='2' WHERE key='assignments'"
                )
                logical = atomic_adapter._logical_record_digest(header, database)
                database.execute(
                    "UPDATE metadata SET value=? WHERE key='logical_sha256'",
                    (logical,),
                )
            descriptor["logical_sha256"] = logical
            digest = atomic_adapter._sha256(mutated)
            store = root / f"certificate.{digest}.sqlite"
            mutated.rename(store)
            descriptor.update({
                "file": store.name, "bytes": store.stat().st_size,
                "sha256": digest,
            })
            value["cluster_storage"] = descriptor
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "coverage"):
                list(iter_xilinx_openparf_atomic_placement_clusters(path, value))

    def test_logical_digest_detects_tamper_and_is_layout_independent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "certificate.json"
            value = atomic_adapter.write_xilinx_openparf_atomic_placement_certificate(
                path,
                {
                    "schema": OPENPARF_ATOMIC_PLACEMENT_SCHEMA,
                    "clusters": [{
                        "cluster": "openparf:SLICE_X0Y0",
                        "site": "SLICE_X0Y0", "site_type": "SLICEL",
                        "x": 0, "y": 0,
                        "assignments": [{
                            "instance": "u0", "cell_type": "LUT6",
                            "bel": "A6LUT", "physical_site": "SLICE_X0Y0",
                            "placement_mode": "SLICEL",
                            "source_cluster": "atomic-source:u0",
                            "bram_tile_group": None,
                        }],
                    }],
                },
            )
            original_descriptor = dict(value["cluster_storage"])
            original = root / original_descriptor["file"]

            layout = root / "layout.sqlite"
            shutil.copy2(original, layout)
            with closing(sqlite3.connect(layout)) as database, database:
                database.execute("PRAGMA page_size=4096")
                database.execute("VACUUM")
            layout_sha = atomic_adapter._sha256(layout)
            self.assertNotEqual(layout_sha, original_descriptor["sha256"])
            layout_store = root / f"certificate.{layout_sha}.sqlite"
            layout.rename(layout_store)
            layout_descriptor = dict(original_descriptor)
            layout_descriptor.update({
                "file": layout_store.name,
                "bytes": layout_store.stat().st_size,
                "sha256": layout_sha,
            })
            value["cluster_storage"] = layout_descriptor
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(
                len(list(iter_xilinx_openparf_atomic_placement_clusters(
                    path, value
                ))),
                1,
            )
            self.assertEqual(
                layout_descriptor["logical_sha256"],
                original_descriptor["logical_sha256"],
            )

            tampered = root / "tampered.sqlite"
            shutil.copy2(original, tampered)
            with closing(sqlite3.connect(tampered)) as database, database:
                database.execute(
                    "UPDATE assignments SET cell_type='LUT5' WHERE instance='u0'"
                )
            tampered_sha = atomic_adapter._sha256(tampered)
            tampered_store = root / f"certificate.{tampered_sha}.sqlite"
            tampered.rename(tampered_store)
            tampered_descriptor = dict(original_descriptor)
            tampered_descriptor.update({
                "file": tampered_store.name,
                "bytes": tampered_store.stat().st_size,
                "sha256": tampered_sha,
            })
            value["cluster_storage"] = tampered_descriptor
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "logical digest"):
                list(iter_xilinx_openparf_atomic_placement_clusters(path, value))

    def test_legacy_v2_certificate_store_requires_explicit_materializer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            certificate_path = root / "certificate.json"
            certificate = atomic_adapter.write_xilinx_openparf_atomic_placement_certificate(
                certificate_path,
                {
                    "schema": OPENPARF_ATOMIC_PLACEMENT_SCHEMA,
                    "clusters": [{
                        "cluster": "openparf:SLICE_X0Y0",
                        "site": "SLICE_X0Y0", "site_type": "SLICEL",
                        "x": 0, "y": 0,
                        "assignments": [{
                            "instance": "u0", "cell_type": "LUT6",
                            "bel": "A6LUT", "physical_site": "SLICE_X0Y0",
                            "placement_mode": "SLICEL",
                            "source_cluster": "atomic-source:u0",
                            "bram_tile_group": None,
                        }],
                    }],
                },
            )
            current_store = root / certificate["cluster_storage"]["file"]
            legacy_store = root / "certificate.sqlite3"
            shutil.copy2(current_store, legacy_store)
            with closing(sqlite3.connect(legacy_store)) as database, database:
                database.execute("PRAGMA application_id = 0")
                database.execute("PRAGMA user_version = 0")
                database.execute(
                    "UPDATE metadata SET value=? WHERE key='schema'",
                    ("emuflow.openparf-atomic-placement-database/v2",),
                )
                database.execute(
                    "DELETE FROM metadata WHERE key='logical_sha256'"
                )
            descriptor = {
                "schema": "emuflow.openparf-atomic-placement-database/v2",
                "file": legacy_store.name,
                "bytes": legacy_store.stat().st_size,
                "sha256": atomic_adapter._sha256(legacy_store),
                "clusters": 1,
                "assignments": 1,
            }
            legacy = {
                "schema": OPENPARF_ATOMIC_PLACEMENT_SCHEMA,
                "cluster_storage": descriptor,
            }
            certificate_path.write_text(json.dumps(legacy), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "explicit compatibility mode"
            ):
                list(iter_xilinx_openparf_atomic_placement_clusters(
                    certificate_path, legacy
                ))
            materialized = load_xilinx_openparf_atomic_placement_clusters(
                certificate_path, legacy
            )
            self.assertEqual(materialized[0]["assignments"][0]["instance"], "u0")

    def test_compact_certificate_serializes_bram_tile_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root)
            output = root / "output"
            export_xilinx_openparf_atomic(mapped, packed, architecture, output)
            placement = output / "placed.pl"
            placement.write_text("a0 0 0 0\na1 0 0 1\n", encoding="utf-8")
            certificate = validate_xilinx_openparf_atomic_placement(
                placement,
                output / "name_map.json",
                mapped,
                architecture,
            )
            tile_group = {"tile": "RAMB_X0Y0", "half": "lower"}
            certificate["clusters"][0]["assignments"][0][
                "bram_tile_group"
            ] = tile_group
            certificate_path = output / "certificate-with-bram-group.json"
            atomic_adapter.write_xilinx_openparf_atomic_placement_certificate(
                certificate_path, certificate
            )
            persisted = json.loads(certificate_path.read_text(encoding="utf-8"))
            database_path = output / persisted["cluster_storage"]["file"]
            with closing(sqlite3.connect(database_path)) as database, database:
                encoded = database.execute(
                    "SELECT bram_tile_group FROM assignments "
                    "WHERE bram_tile_group IS NOT NULL"
                ).fetchone()[0]
            self.assertEqual(json.loads(encoded), tile_group)

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

    def test_fdce_clear_conflict_is_repaired_before_physical_bridge(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root)
            mapped_value = json.loads(mapped.read_text())
            cells = mapped_value["modules"]["top"]["cells"]
            cells["ff"] = _cell(
                "FDCE", {"C": [10], "CE": ["1"], "CLR": [20],
                         "D": [1], "Q": [2]}
            )
            cells["ff2"] = _cell(
                "FDCE", {"C": [10], "CE": ["1"], "CLR": [21],
                         "D": [1], "Q": [3]}
            )
            mapped.write_text(json.dumps(mapped_value), encoding="utf-8")
            packed_value = json.loads(packed.read_text())
            packed_value["clusters"][0]["assignments"] = [
                {"instance": "lut", "cell_type": "LUT6", "bel": "A6LUT"},
                {"instance": "ff", "cell_type": "FDCE", "bel": "AFF"},
                {"instance": "ff2", "cell_type": "FDCE", "bel": "BFF"},
            ]
            packed.write_text(json.dumps(packed_value), encoding="utf-8")
            output = root / "output"
            export_xilinx_openparf_atomic(mapped, packed, architecture, output)
            placement = output / "controls.pl"
            placement.write_text(
                "a0 0 0 0\na1 0 0 2\na2 0 0 5\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValidationError, "control-set"):
                validate_xilinx_openparf_atomic_placement(
                    placement, output / "name_map.json", mapped, architecture
                )
            report = legalize_xilinx_openparf_ff_control_sets(
                placement, output / "name_map.json", mapped,
                output / "ff-control-set-legalization.json",
            )
            self.assertEqual(report["illegal_halves"], 1)
            self.assertEqual(report["moved_ff_atoms"], 1)
            validate_xilinx_openparf_atomic_placement(
                placement, output / "name_map.json", mapped, architecture
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
            with self.assertRaisesRegex(
                ValidationError, "must expose eight DI and S bits"
            ):
                export_xilinx_openparf_atomic(
                    mapped, packed, architecture, root / "carry"
                )

    def test_dual_lut_and_unsupported_hard_cluster_kinds_fail_closed(self):
        cases = (
            ("LUT6_2", "slice", "physical macro topology is incomplete"),
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
        self.assertEqual(
            config["gp_max_fillers_per_area_type"],
            {
                "DSP48E2": 1,
                "FF": 65536,
                "LUT": 65536,
                "RAMB36E2": 1,
                "URAM288": 1,
            },
        )
        self.assertEqual(config["gp_max_logic_filler_bin_span"], 0.5)
        self.assertIn("PIN A[0] INPUT", library)
        self.assertIn("PIN P[1] OUTPUT", library)
        defined, resource_models = _defined_and_resource_models(library, sites)
        self.assertEqual(resource_models, defined)
        self.assertEqual(defined, {
            "DSP48E2", "FDRE", "LUT6", "RAMB36E2", "URAM288",
        })


    def test_mixed_fixture_import_checks_hard_site_bel_and_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root, mixed=True)
            output = root / "output"
            export_xilinx_openparf_atomic(mapped, packed, architecture, output)
            names = _fixture_name_map(output / "name_map.json")
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
            from tests.openparf_device_fixture import seal_native_input
            native = seal_native_input(architecture, native_path, provider_path, native)
            output = root / "output"
            with mock.patch(
                "tests.openparf_placement_reference.load_xilinx_native_device_constraints",
                return_value=(native, {"status": "pass"}),
            ), mock.patch(
                "tests.openparf_placement_reference.require_xilinx_native_constraint_capability"
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
            self.assertTrue(all("windows" not in item for item in groups))
            self.assertEqual(
                {item["window_set"] for item in groups},
                {"singleton-resource:RAMB18E2"},
            )
            window_set = next(
                item for item in contract["window_sets"]
                if item["id"] == "singleton-resource:RAMB18E2"
            )
            self.assertEqual(len(window_set["windows"]), 4)
            self.assertTrue(all(item["window_count"] == 4 for item in groups))

            lo_window = window_set["windows"][0][0]
            hi_window = next(
                window[0] for window in window_set["windows"]
                if window[0]["anchor"] == lo_window["anchor"]
                and window[0]["z"] != lo_window["z"]
            )
            names = _fixture_name_map(output / "name_map.json")
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
                "tests.openparf_placement_reference.load_xilinx_native_device_constraints",
                return_value=(native, {"status": "pass"}),
            ), mock.patch(
                "tests.openparf_placement_reference.require_xilinx_native_constraint_capability"
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
            groups = [item["bram_tile_group"] for item in assignments]
            self.assertEqual(
                {item["role"] for item in groups}, {"lower", "upper"}
            )
            self.assertEqual(
                {item["anchor"] for item in groups}, {lo_window["anchor"]}
            )
            self.assertEqual(
                {tuple(item["claims"]) for item in groups},
                {
                    (f"bram:{lo_window['tile']}:lower",),
                    (f"bram:{lo_window['tile']}:upper",),
                },
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
            _add_mux_bels(architecture, {"MUXF7", "MUXF8", "MUXF9"})
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
            self.assertNotIn("windows", group)
            self.assertEqual(group["window_count"], 4)
            self.assertEqual(
                group["window_template"]["kind"], "same-site-slice/v1"
            )
            self.assertEqual(
                [member["z"] for member in group["window_template"]["members"]],
                [1, 3, 0],
            )
            names = _fixture_name_map(output / "name_map.json")
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
            mapped_value = json.loads(mapped.read_text(encoding="utf-8"))
            with mock.patch(
                'tests.openparf_placement_reference.build_xilinx_physical_macro_contract',
                wraps=atomic_adapter.build_xilinx_physical_macro_contract,
            ) as build_macro_contract:
                certificate = validate_xilinx_openparf_atomic_placement(
                    placement,
                    output / "name_map.json",
                    mapped,
                    architecture,
                    mapped_value=mapped_value,
                )
            validation_call = build_macro_contract.call_args_list[-1]
            self.assertIs(validation_call.kwargs["source"], mapped_value)
            certificate_path = output / "placement-certificate.json"
            certificate_path.write_text(
                json.dumps(certificate, sort_keys=True), encoding="utf-8"
            )
            bridge_packed = root / "bridge-packed.json"
            bridge_placement = root / "bridge-placement.json"
            bridge = materialize_xilinx_openparf_atomic_contract(
                mapped,
                architecture,
                certificate_path,
                bridge_packed,
                bridge_placement,
            )
            self.assertEqual(bridge["placed_cells"], 4)
            self.assertEqual(
                validate_xilinx_packing(
                    mapped,
                    bridge_packed,
                    architecture_path=architecture,
                )["cells"],
                4,
            )
            self.assertEqual(
                validate_xilinx_placement(
                    bridge_packed, architecture, bridge_placement
                )["cells"],
                4,
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

    def test_muxf8_and_muxf9_cones_export_complete_certifiable_site_macros(self):
        expected = {
            "MUXF8": (7, {"MUXF7", "MUXF8"}),
            "MUXF9": (15, {"MUXF7", "MUXF8", "MUXF9"}),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for root_type, (member_count, resources) in expected.items():
                case = root / root_type
                case.mkdir()
                mapped, packed, architecture = _fixture(case)
                cells = _mux_tree_cells(root_type)
                mapped.write_text(json.dumps({
                    "modules": {
                        "top": {"attributes": {"top": "1"}, "cells": cells}
                    },
                }), encoding="utf-8")
                pack_xilinx_sites(mapped, packed, top="top")
                _add_mux_bels(
                    architecture,
                    {"MUXF7", "MUXF8", "MUXF9"},
                )
                output = case / "output"
                export_xilinx_openparf_atomic(
                    mapped, packed, architecture, output
                )
                sites = (output / "design.scl").read_text(encoding="utf-8")
                self.assertIn("  MUXF9 1", sites)
                self.assertIn("  MUXF9 MUXF9", sites)
                contract = json.loads(
                    (output / "physical-macro-groups.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(len(contract["groups"]), 1)
                group = contract["groups"][0]
                self.assertEqual(len(group["instances"]), member_count)
                self.assertEqual(set(group["owned_resources"]), resources)
                self.assertNotIn("windows", group)
                self.assertEqual(group["window_count"], 4)
                self.assertEqual(
                    len(group["window_template"]["members"]), member_count
                )

                sites = {
                    site["site"]: site
                    for site in load_xilinx_openparf_atomic_sites(
                        output / "name_map.json"
                    )
                }
                macro_site_entry = next(
                    site for site in sites.values() if "LUT" in site["resources"]
                )
                lines = []
                for instance, entry in zip(
                    group["instances"], group["window_template"]["members"]
                ):
                    lines.append(
                        f"{instance} {macro_site_entry['dense_x']} "
                        f"{macro_site_entry['dense_y']} {entry['z']}"
                    )
                names = _fixture_name_map(output / "name_map.json")
                ff = next(atom for atom in names["atoms"] if atom["instance"] == "ff")
                macro_site = macro_site_entry["site"]
                ff_site = next(site for site in sites.values()
                               if site["site"] != macro_site and "FF" in site["resources"])
                lines.append(
                    f"{ff['openparf']} {ff_site['dense_x']} {ff_site['dense_y']} 0"
                )
                placement = output / "mux.pl"
                placement.write_text("\n".join(lines) + "\n", encoding="utf-8")
                certificate = validate_xilinx_openparf_atomic_placement(
                    placement, output / "name_map.json", mapped, architecture
                )
                self.assertEqual(certificate["status"], "pass")
                certificate_path = output / "placement-certificate.json"
                certificate_path.write_text(
                    json.dumps(certificate, sort_keys=True), encoding="utf-8"
                )
                bridge_packed = case / "bridge-packed.json"
                bridge_placement = case / "bridge-placement.json"
                bridge = materialize_xilinx_openparf_atomic_contract(
                    mapped,
                    architecture,
                    certificate_path,
                    bridge_packed,
                    bridge_placement,
                )
                self.assertEqual(bridge["placed_cells"], member_count + 1)
                self.assertEqual(
                    validate_xilinx_packing(
                        mapped,
                        bridge_packed,
                        architecture_path=architecture,
                    )["cells"],
                    member_count + 1,
                )
                self.assertEqual(
                    validate_xilinx_placement(
                        bridge_packed, architecture, bridge_placement
                    )["cells"],
                    member_count + 1,
                )


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

    def test_validator_joins_typed_atoms_without_materializing_name_map(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root)
            output = root / "output"
            export_xilinx_openparf_atomic(mapped, packed, architecture, output)
            placement = output / "shuffled.pl"
            placement.write_text(
                "a1 0 0 1 trailing-solver-field\n"
                "# solver comment\n"
                "a0 0 0 0\n",
                encoding="utf-8",
            )
            with mock.patch(
                "emuflow_tools.physical.openparf.xilinx_openparf_name_map_storage."
                "_AtomSequence.__iter__",
                side_effect=AssertionError("atom sequence was materialized"),
            ):
                certificate = validate_xilinx_openparf_atomic_placement(
                    placement, output / "name_map.json", mapped, architecture
                )
            self.assertEqual(certificate["status"], "pass")
            self.assertEqual(certificate["summary"]["atoms"], 2)
            self.assertFalse(list(output.glob(".openparf-placement-*.sqlite3")))

    def test_typed_placement_join_explicitly_enables_file_uri_attachment(self):
        real_connect = sqlite3.connect
        attached = []

        class ExplicitUriConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql.startswith("ATTACH DATABASE") and parameters[0].startswith("file:"):
                    if not self.uri_enabled:
                        raise sqlite3.OperationalError("URI attachment was not enabled")
                    attached.append(parameters[0])
                return super().execute(sql, parameters)

        def connect(*args, **kwargs):
            kwargs["factory"] = ExplicitUriConnection
            connection = real_connect(*args, **kwargs)
            connection.uri_enabled = kwargs.get("uri", False)
            return connection

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root)
            output = root / "output"
            export_xilinx_openparf_atomic(mapped, packed, architecture, output)
            placement = output / "placed.pl"
            placement.write_text("a0 0 0 0\na1 0 0 1\n", encoding="utf-8")
            with mock.patch.object(atomic_adapter.sqlite3, "connect", side_effect=connect):
                certificate = validate_xilinx_openparf_atomic_placement(
                    placement, output / "name_map.json", mapped, architecture)
            self.assertEqual(certificate["status"], "pass")
            self.assertTrue(attached)
            self.assertTrue(all(uri.endswith("?mode=ro&immutable=1") for uri in attached))

    def test_validator_releases_system_index_and_name_map_lease_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root)
            output = root / "output"
            export_xilinx_openparf_atomic(mapped, packed, architecture, output)
            placement = output / "placed.pl"
            placement.write_text("a0 0 0 0\na1 0 0 1\n", encoding="utf-8")
            created = []
            real_mkstemp = tempfile.mkstemp

            def tracked_mkstemp(*args, **kwargs):
                descriptor, path = real_mkstemp(*args, **kwargs)
                if kwargs.get("prefix") == "emuflow-openparf-placement-":
                    created.append(Path(path))
                return descriptor, path

            with mock.patch(
                "tests.openparf_placement_reference.tempfile.mkstemp",
                side_effect=tracked_mkstemp,
            ), mock.patch(
                'tests.openparf_placement_reference.load_xilinx_openparf_atomic_sites',
                side_effect=ValidationError("injected site failure"),
            ):
                with self.assertRaisesRegex(ValidationError, "injected site"):
                    validate_xilinx_openparf_atomic_placement(
                        placement, output / "name_map.json", mapped, architecture
                    )
            self.assertEqual(len(created), 1)
            self.assertFalse(created[0].exists())
            lock_path = output / ".name_map.json.lock"
            descriptor = os.open(lock_path, os.O_RDWR)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def test_parser_preserves_earliest_source_error_across_batches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _fixture(root)
            output = root / "output"
            export_xilinx_openparf_atomic(mapped, packed, architecture, output)
            cases = (
                (
                    "unknown-before-duplicate.pl",
                    "a999 0 0 0\na0 0 0 0\na0 0 0 1\n",
                ),
                (
                    "unknown-before-malformed.pl",
                    "a999 0 0 0\nmalformed\n",
                ),
                (
                    "wrong-prefix-before-malformed.pl",
                    "b0 0 0 0\nmalformed\n",
                ),
                (
                    "wrong-prefix-before-duplicate.pl",
                    "x0 0 0 0\na0 0 0 0\na0 0 0 1\n",
                ),
            )
            for filename, contents in cases:
                placement = output / filename
                placement.write_text(contents, encoding="utf-8")
                with self.assertRaisesRegex(
                    ImportError, rf"{filename}:1: invalid atomic placement row"
                ):
                    validate_xilinx_openparf_atomic_placement(
                        placement, output / "name_map.json", mapped, architecture
                    )

            duplicate_first = output / "duplicate-before-malformed.pl"
            duplicate_first.write_text(
                "a0 0 0 0\na0 0 0 1\nmalformed\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValidationError, "duplicates an atom"):
                validate_xilinx_openparf_atomic_placement(
                    duplicate_first,
                    output / "name_map.json",
                    mapped,
                    architecture,
                )

            for filename, second_row in (
                ("duplicate-before-nonnumeric.pl", "a0 q 0 1"),
                ("duplicate-before-nondiscrete.pl", "a0 0.5 0 1"),
            ):
                duplicate_coordinate = output / filename
                duplicate_coordinate.write_text(
                    f"a0 0 0 0\n{second_row}\n", encoding="utf-8"
                )
                with self.assertRaisesRegex(
                    ValidationError, "duplicates an atom"
                ):
                    validate_xilinx_openparf_atomic_placement(
                        duplicate_coordinate,
                        output / "name_map.json",
                        mapped,
                        architecture,
                    )

            duplicate = output / "duplicate.pl"
            duplicate.write_text(
                "a0 0 0 0\na0 0 0 1\na1 0 0 1\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValidationError, "duplicates an atom"):
                validate_xilinx_openparf_atomic_placement(
                    duplicate, output / "name_map.json", mapped, architecture
                )
            self.assertFalse(list(output.glob(".openparf-placement-*.sqlite3")))

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
            from tests.openparf_device_fixture import seal_native_input
            native = seal_native_input(architecture, native_path, provider_path, native)
            output = root / "output"
            with mock.patch(
                "tests.openparf_placement_reference.load_xilinx_native_device_constraints",
                return_value=(native, {"status": "pass"}),
            ), mock.patch(
                "tests.openparf_placement_reference.require_xilinx_native_constraint_capability"
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
            names = _fixture_name_map(output / "name_map.json")
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
