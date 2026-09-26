import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.errors import ValidationError
from emuflow.openparf_native_capabilities import (
    CAPABILITY_STATUSES,
    OPENPARF_NATIVE_CAPABILITY_SCHEMA,
    audit_pinned_openparf_carry_path,
    probe_openparf_native_capabilities,
    probe_xilinx_openparf_carry_native_support,
)
from emuflow.xilinx_placer_capability import (
    qualify_xilinx_placer_capabilities,
    validate_xilinx_placer_capability_report,
)
from emuflow.xilinx_openparf import (
    export_xilinx_cluster_bookshelf,
    run_xilinx_openparf_native_legalization_smoke,
    validate_xilinx_openparf_native_smoke_placement,
)
from emuflow.xilinx_physical_macros import (
    derive_xilinx_physical_macro_contract,
)


ROOT = Path(__file__).resolve().parents[1]


def _write_fixture(root: Path, *, cascade: bool = False, collision: bool = False):
    mapped = root / "mapped.json"
    packed = root / "packed.json"
    architecture = root / "architecture.json"
    cells = {
        "lut": {
            "type": "LUT6",
            "port_directions": {"O": "output"},
            "connections": {"O": [1]},
        },
        "ff": {
            "type": "FDRE",
            "port_directions": {"D": "input"},
            "connections": {"D": [1]},
        },
    }
    clusters = [
        {
            "id": "slice-a",
            "kind": "slice",
            "assignments": [{"instance": "lut", "cell_type": "LUT6", "bel": "A6LUT"}],
        },
        {
            "id": "slice-b",
            "kind": "slice",
            "assignments": [{"instance": "ff", "cell_type": "FDRE", "bel": "AFF"}],
        },
    ]
    sites = [
        {
            "name": "SLICE_X0Y0", "type": "SLICEL", "template": "SLICEL",
            "x": 0, "y": 0, "tile": {"grid_col": 0, "grid_row": 0},
        },
        {
            "name": "SLICE_X0Y1", "type": "SLICEL", "template": "SLICEL",
            "x": 0, "y": 1, "tile": {"grid_col": 0, "grid_row": 1},
        },
    ]
    templates = {
        "SLICEL": {
            "bels": [
                {"name": "A6LUT", "type": "LUT6", "z": 0, "compatible_cells": ["LUT6"]},
                {"name": "AFF", "type": "FF", "z": 1, "compatible_cells": ["FDRE"]},
            ],
            "alternative_templates": [],
        },
    }
    if collision:
        cells["dsp"] = {"type": "DSP48E2", "port_directions": {}, "connections": {}}
        clusters.append({
            "id": "dsp", "kind": "hard",
            "assignments": [{"instance": "dsp", "cell_type": "DSP48E2", "bel": "DSP_ALU"}],
        })
        templates["DSP48E2"] = {
            "bels": [{
                "name": "DSP_ALU", "type": "DSP48E2", "z": 0,
                "compatible_cells": ["DSP48E2"],
            }],
            "alternative_templates": [],
        }
        sites.append({
            "name": "DSP48E2_X0Y0", "type": "DSP48E2", "template": "DSP48E2",
            "x": 1, "y": 0, "tile": {"grid_col": 0, "grid_row": 0},
        })
    mapped.write_text(json.dumps({
        "modules": {"top": {"attributes": {"top": "1"}, "cells": cells}}
    }), encoding="utf-8")
    packed.write_text(json.dumps({
        "schema": "emuflow.packed-site-netlist/v1",
        "top": "top",
        "clusters": clusters,
        "cascade_chains": ([{"id": "chain", "instances": ["lut", "ff"]}] if cascade else []),
    }), encoding="utf-8")
    architecture.write_text(json.dumps({
        "schema": "emuflow.archdb/v1", "part": "test",
        "source": {"format": "test/v1"}, "policy": {"name": "test"},
        "site_templates": templates, "sites": sites,
    }), encoding="utf-8")
    return mapped, packed, architecture


def _mapped_cell(cell_type, connections, outputs):
    return {
        "type": cell_type,
        "port_directions": {
            port: ("output" if port in outputs else "input")
            for port in connections
        },
        "connections": connections,
    }


def _add_carry(cells, name, *, ci, co, net_base):
    di = []
    select = []
    for index in range(8):
        di_output = net_base + 2 * index
        select_output = di_output + 1
        cells[f"{name}$lut6_2_{index}"] = _mapped_cell(
            "LUT6_2",
            {
                "I0": [net_base + 100 + index],
                "O5": [di_output],
                "O6": [select_output],
            },
            {"O5", "O6"},
        )
        di.append(di_output)
        select.append(select_output)
    cells[name] = _mapped_cell(
        "CARRY8",
        {
            "CI": [ci],
            "CI_TOP": ["0"],
            "DI": di,
            "S": select,
            "CO": list(range(co, co + 8)),
            "O": list(range(co + 8, co + 16)),
        },
        {"CO", "O"},
    )


def _write_two_carry_fixture(root: Path):
    cells = {}
    _add_carry(cells, "carry0", ci=1, co=1000, net_base=2000)
    _add_carry(cells, "carry1", ci=1007, co=1100, net_base=3000)
    mapped = root / "carry-mapped.json"
    contract = root / "physical-macros.json"
    mapped.write_text(json.dumps({
        "modules": {
            "top": {"attributes": {"top": "1"}, "cells": cells}
        }
    }), encoding="utf-8")
    derive_xilinx_physical_macro_contract(
        mapped, contract, top="top"
    )
    return mapped, contract


class OpenparfNativeCapabilitiesTest(unittest.TestCase):
    def test_optional_learned_delay_dependencies_are_lazy(self):
        root = Path(__file__).resolve().parents[1]
        collections = (
            root / "engines/openparf/openparf/placement/op_collections.py"
        ).read_text(encoding="utf-8")
        prefix, delay_builder = collections.split(
            "def build_estimate_delay_op", maxsplit=1
        )
        self.assertNotIn("from hummingbird", prefix)
        self.assertIn("from hummingbird.ml import load", delay_builder)
        delay_op = (
            root
            / "engines/openparf/openparf/ops/delay_estimation/delay_estimation.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("from hummingbird", delay_op)

    def test_probe_reports_source_backed_capability_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _write_fixture(root)
            output = root / "capabilities.json"
            matrix = probe_openparf_native_capabilities(
                mapped, packed, architecture, output_path=output
            )
            serialized = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(matrix, serialized)
        self.assertEqual(matrix["schema"], OPENPARF_NATIVE_CAPABILITY_SCHEMA)
        validate_xilinx_placer_capability_report(matrix)
        feature = {item["feature"]: item for item in matrix["provider_features"]}
        self.assertEqual(feature["continuous_global_placement"]["status"], "native_supported")
        self.assertEqual(feature["single_site_resource_mcf_legalization"]["status"], "adapter_required")
        self.assertEqual(feature["packed_cluster_detailed_placement"]["status"], "core_missing")
        self.assertEqual(feature["atomic_lut_ff_detailed_placement"]["status"], "adapter_required")
        self.assertTrue(matrix["native_packed_cluster_smoke"]["eligible"])
        for item in matrix["provider_features"] + matrix["primitive_inventory"]:
            self.assertIn(item["status"], CAPABILITY_STATUSES)
        decision = qualify_xilinx_placer_capabilities(
            matrix,
            required_primitives=("LUT6", "FDRE"),
            required_constraints=("clock_region",),
        )
        self.assertEqual(decision["status"], "fail")
        self.assertIn("stages.detailed_placement", decision["blocked_entries"])

    def test_probe_rejects_cascade_and_ambiguous_tile_for_native_smoke(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _write_fixture(
                root, cascade=True, collision=True
            )
            matrix = probe_openparf_native_capabilities(mapped, packed, architecture)
            with self.assertRaisesRegex(ValidationError, "dedicated cascades"):
                export_xilinx_cluster_bookshelf(
                    mapped,
                    packed,
                    architecture,
                    root / "out",
                    native_packed_cluster_legalization=True,
                )
        self.assertFalse(matrix["native_packed_cluster_smoke"]["eligible"])
        self.assertTrue(matrix["design_hazards"]["ambiguous_physical_coordinates"])

    def test_missing_pinned_source_is_unverified_or_core_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _write_fixture(root)
            matrix = probe_openparf_native_capabilities(
                mapped, packed, architecture, source_root=root / "missing"
            )
        feature = {item["feature"]: item for item in matrix["provider_features"]}
        self.assertEqual(feature["continuous_global_placement"]["status"], "unverified")
        self.assertEqual(feature["single_site_resource_mcf_legalization"]["status"], "core_missing")
        self.assertFalse(matrix["native_packed_cluster_smoke"]["eligible"])

    def test_probe_classifies_complete_current_primitive_inventory(self):
        placed = (
            *(f"LUT{width}" for width in range(1, 7)),
            "LUT6_2", "FDCE", "FDPE", "FDRE", "FDSE",
            "MUXF7", "MUXF8", "MUXF9", "CARRY8", "DSP48E2",
            "RAMB18E2", "RAMB36E2", "URAM288",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _write_fixture(root)
            value = json.loads(mapped.read_text(encoding="utf-8"))
            value["modules"]["top"]["cells"] = {
                **{
                    f"placed-{index}": {
                        "type": primitive,
                        "port_directions": {},
                        "connections": {},
                    }
                    for index, primitive in enumerate(placed)
                },
                "gnd": {"type": "GND", "port_directions": {}, "connections": {}},
                "unknown": {
                    "type": "FUTURE_PRIMITIVE",
                    "port_directions": {},
                    "connections": {},
                },
            }
            mapped.write_text(json.dumps(value), encoding="utf-8")
            matrix = probe_openparf_native_capabilities(mapped, packed, architecture)
        for primitive in placed:
            self.assertEqual(
                matrix["primitives"][primitive]["status"], "adapter_required"
            )
        self.assertEqual(
            matrix["primitives"]["CARRY8"]["adapter_validation"], "missing"
        )
        self.assertEqual(matrix["primitives"]["GND"]["status"], "native_supported")
        self.assertEqual(
            matrix["primitives"]["FUTURE_PRIMITIVE"]["status"], "unverified"
        )
        self.assertFalse(matrix["native_packed_cluster_smoke"]["eligible"])

    def test_carry_probe_requires_explicit_runtime_qualification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, contract = _write_two_carry_fixture(root)
            output = root / "carry-capability.json"
            with mock.patch("emuflow.openparf.run_openparf") as run_openparf:
                report = probe_xilinx_openparf_carry_native_support(
                    mapped, contract, top="top", output_path=output
                )
            serialized = json.loads(output.read_text(encoding="utf-8"))

        run_openparf.assert_not_called()
        self.assertEqual(report, serialized)
        validate_xilinx_placer_capability_report(report)
        self.assertEqual(report["qualification"]["status"], "unverified")
        self.assertFalse(report["qualification"]["runtime_launched"])
        self.assertEqual(report["qualification"]["fallback"], "forbidden")
        self.assertEqual(report["qualification"]["preplacement"], "forbidden")
        reproduction = report["minimum_reproduction"]
        self.assertEqual(reproduction["carry8_units"], 2)
        self.assertEqual(reproduction["lut6_2_adapters"], 16)
        self.assertEqual(reproduction["carry_chain_lengths"], [2])
        self.assertEqual(reproduction["associated_luts_per_unit"], 8)
        self.assertEqual(
            [item["id"] for item in report["remaining_qualification_steps"]],
            [
                "compiled-openparf-carry8-run",
                "rapidwright-native-legality",
                "rapidwright-route-bridge",
                "opensta-timing-gate",
            ],
        )
        self.assertEqual(
            report["constraints"]["ordered_carry8_chain"]["status"],
            "unverified",
        )
        decision = qualify_xilinx_placer_capabilities(
            report,
            required_primitives=("CARRY8", "LUT6_2"),
            required_constraints=(
                "indivisible_carry8_lut6_2_macro",
                "ordered_carry8_chain",
            ),
        )
        self.assertEqual(decision["status"], "fail")

    def test_carry_source_audit_records_exact_native_assumptions(self):
        audit = audit_pinned_openparf_carry_path(ROOT / "engines/openparf")
        self.assertEqual(audit["status"], "native_supported")
        self.assertEqual(
            set(audit["checks"]),
            {"parser", "shape_db", "chain_info", "chain_legalizer", "placer"},
        )
        self.assertEqual(audit["checks"]["parser"]["status"], "native_supported")
        self.assertEqual(audit["checks"]["placer"]["status"], "native_supported")
        self.assertEqual(audit["checks"]["shape_db"]["status"], "adapter_required")
        self.assertEqual(audit["checks"]["chain_info"]["status"], "native_supported")
        self.assertEqual(
            audit["checks"]["chain_legalizer"]["status"], "native_supported"
        )
        self.assertIn(
            "associated_luts_per_unit = is_carry8 ? 8 : 4",
            audit["checks"]["chain_info"]["markers"],
        )
        self.assertIn(
            "luts_per_unit == 8 ? 1.0 : 0.5",
            audit["checks"]["chain_legalizer"]["markers"],
        )
        self.assertEqual(
            audit["checks"]["shape_db"]["placement_or_operator_consumers"],
            [],
        )
        parser = (
            ROOT / "engines/openparf/openparf/io/bookshelf/bookshelf_parser.yy"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "KWD_OUTPUT KWD_CAS ENDL    { driver.addCellOutputCasPinCbk",
            parser,
        )
        placer = (
            ROOT / "engines/openparf/openparf/placement/placer.py"
        ).read_text(encoding="utf-8")
        carry_mask = placer.split(
            "if self.params.carry_chain_legalization_flag:", 2
        )[-1]
        self.assertIn("self.data_cls.chain_cla_ids.bs", carry_mask)
        self.assertIn("self.data_cls.chain_lut_ids.bs", carry_mask)
        self.assertIn(
            "pos_xyz = self.data_cls.inst_locs_xyz.to(",
            placer,
        )
        self.assertIn(
            "pos[movable_range[0] : movable_range[1]]",
            placer,
        )
        self.assertNotIn("assert self.data_cls.io_pos_xyz is not None", placer)
        self.assertNotIn(
            "if self.params.io_legalization_flag:", carry_mask.split(
                "loc_xyz = self.op_cls.ism_dp_op", 1
            )[0],
        )

    def test_carry_probe_is_unverified_when_pinned_source_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, contract = _write_two_carry_fixture(root)
            report = probe_xilinx_openparf_carry_native_support(
                mapped,
                contract,
                top="top",
                source_root=root / "missing-openparf",
            )
        self.assertEqual(report["qualification"]["status"], "unverified")
        self.assertTrue(all(
            entry["status"] == "unverified"
            for entry in report["source_audit"]["checks"].values()
        ))

    def test_native_smoke_export_enables_only_generic_mcf_legalization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _write_fixture(root)
            out = root / "out"
            manifest = export_xilinx_cluster_bookshelf(
                mapped,
                packed,
                architecture,
                out,
                native_packed_cluster_legalization=True,
            )
            config = json.loads((out / "openparf.json").read_text(encoding="utf-8"))
            sites = (out / "design.scl").read_text(encoding="utf-8")
        self.assertEqual(manifest["mode"], "native-packed-cluster-mcf-smoke")
        self.assertEqual(config["legalize_flag"], 1)
        self.assertEqual(config["detailed_place_flag"], 0)
        self.assertFalse(config["emuflow_continuous_global_guidance"])
        self.assertEqual(config["resource_categories"], {"X_SLICE": "SSSIR"})
        self.assertNotIn("X_SLICE_AUX", sites)

    def test_native_smoke_runner_has_no_greedy_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = _write_fixture(root)
            out = root / "out"

            def fake_openparf(_config, **_kwargs):
                result = out / "results" / "xilinx_clusters.pl"
                result.parent.mkdir(parents=True, exist_ok=True)
                result.write_text("c0 0 0 0\nc1 0 1 0\n", encoding="utf-8")
                return result

            with mock.patch(
                "emuflow.xilinx_openparf.run_openparf", side_effect=fake_openparf
            ):
                report = run_xilinx_openparf_native_legalization_smoke(
                    mapped, packed, architecture, out
                )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["validation"]["unique_sites"], 2)

            overlap = out / "overlap.pl"
            overlap.write_text("c0 0 0 0\nc1 0 0 0\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "overlaps"):
                validate_xilinx_openparf_native_smoke_placement(
                    overlap, out / "name_map.json"
                )


if __name__ == "__main__":
    unittest.main()
