import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.errors import ValidationError
from emuflow.io import read_json
from emuflow.rapidwright_provider import validate_rapidwright_provider_manifest
from emuflow.xilinx_openparf_carry8 import (
    OPENPARF_CARRY8_PLACEMENT_SCHEMA,
    export_xilinx_openparf_carry8,
    run_xilinx_openparf_carry8_qualification,
    validate_xilinx_openparf_carry8_placement,
)
from emuflow.xilinx_packing import pack_xilinx_sites


ROOT = Path(__file__).resolve().parents[1]
PINNED = ROOT / "resources/rapidwright/xcvu19p-fsva3824-2-e.provider.json"


def _cell(cell_type, connections):
    outputs = {"O", "O5", "O6", "Q", "CO"}
    return {
        "type": cell_type,
        "port_directions": {
            port: ("output" if port in outputs else "input")
            for port in connections
        },
        "connections": connections,
    }


def _add_carry(cells, name, *, ci, co_base, net_base):
    di = []
    select = []
    for index in range(8):
        di_net = net_base + 2 * index
        s_net = di_net + 1
        adapter = f"{name}$lut{index}"
        cells[adapter] = _cell("LUT6_2", {
            "I0": [net_base + 100 + 2 * index],
            "I1": [net_base + 101 + 2 * index],
            "I2": ["0"], "I3": ["0"], "I4": ["0"], "I5": ["1"],
            "O5": [di_net], "O6": [s_net],
        })
        di.append(di_net)
        select.append(s_net)
    cells[name] = _cell("CARRY8", {
        "CI": [ci], "CI_TOP": ["0"], "DI": di, "S": select,
        "CO": list(range(co_base, co_base + 8)),
        "O": list(range(co_base + 8, co_base + 16)),
    })


def _write_fixture(root):
    mapped = root / "mapped.json"
    packed = root / "packed.json"
    architecture = root / "architecture.json"
    provider = root / "provider.json"
    native = root / "native.json"
    cells = {}
    _add_carry(cells, "carry0", ci=1, co_base=10, net_base=1000)
    _add_carry(cells, "carry1", ci=17, co_base=40, net_base=2000)
    cells["ff"] = _cell("FDRE", {
        "C": [5000], "CE": ["1"], "D": [48], "Q": [5001], "R": ["0"],
    })
    mapped.write_text(json.dumps({
        "modules": {"top": {"attributes": {"top": "1"}, "cells": cells}},
    }), encoding="utf-8")
    pack_xilinx_sites(mapped, packed, top="top")

    lut_bels = [{
        "name": f"{letter}6LUT", "type": "LUT6", "z": 2 * index + 1,
        "compatible_cells": [*[f"LUT{width}" for width in range(1, 7)], "LUT6_2"],
    } for index, letter in enumerate("ABCDEFGH")]
    ff_bels = [{
        "name": name, "type": "FF", "z": index,
        "compatible_cells": ["FDCE", "FDPE", "FDRE", "FDSE"],
    } for index, name in enumerate(
        name for letter in "ABCDEFGH" for name in (f"{letter}FF", f"{letter}FF2")
    )]
    carry_bel = {
        "name": "CARRY8", "type": "CARRY8", "z": 0,
        "compatible_cells": ["CARRY8"],
    }
    sites = []
    for x in range(4):
        for y in range(4):
            sites.append({
                "name": f"SLICE_X{x}Y{y}", "type": "SLICEL", "template": "SLICEL",
                "x": x, "y": y, "tile": {"grid_col": x, "grid_row": y},
                "physical_region": {"slr": "SLR0", "clock_region": "X0Y0"},
            })
    architecture.write_text(json.dumps({
        "schema": "emuflow.archdb/v1",
        "part": "xcvu19p-fsva3824-2-e",
        "source": {"format": "unit-test/v1"},
        "policy": {"name": "carry8-native-test"},
        "site_templates": {
            "SLICEL": {
                "bels": [*lut_bels, *ff_bels, carry_bel],
                "alternative_templates": [],
            },
        },
        "sites": sites,
    }, sort_keys=True), encoding="utf-8")
    provider.write_text(PINNED.read_text(encoding="utf-8"), encoding="utf-8")
    checked = validate_rapidwright_provider_manifest(read_json(provider))
    chains = [[f"SLICE_X{x}Y{y}" for y in range(4)] for x in range(4)]
    payload = {
        "capabilities": {
            "clock_region_site_capacity": "native_supported",
            "dedicated_adjacency.BRAM_CASCADE": "unverified",
            "dedicated_adjacency.CARRY_NEXT": "native_supported",
            "dedicated_adjacency.DSP_CASCADE": "unverified",
            "dedicated_adjacency.URAM_CASCADE": "unverified",
            "half_column_clock_capacity": "unverified",
            "slr_site_capacity": "native_supported",
        },
        "dedicated_adjacency": [{
            "chains": chains,
            "edge_count": 12,
            "kind": "CARRY_NEXT",
            "native_proof_sha256": "b" * 64,
            "proof_method": "rapidwright-primitive-bel-sitepin-same-canonical-node-v1",
            "source_endpoint": {
                "bel": "CARRY8", "bel_pin": "CO7", "logical_port": "CO",
                "selection": {"kind": "bit", "index": 7}, "site_pin": "COUT",
            },
            "target_endpoint": {
                "bel": "CARRY8", "bel_pin": "CIN", "logical_port": "CI",
                "selection": {"kind": "all"}, "site_pin": "CIN",
            },
        }],
        "site_capacity": [{
            "clock_region": "X0Y0", "site_type": "SLICEL", "sites": 16,
            "slr": "SLR0",
        }],
        "source": {
            "architecture_sha256": hashlib.sha256(architecture.read_bytes()).hexdigest(),
            "device": checked["device_identity"]["device"],
            "device_database_md5": checked["device_database_md5"],
            "full_part": checked["part"],
            "generator": {"revision": checked["revision"], "version": checked["version"]},
            "provider_manifest_sha256": hashlib.sha256(provider.read_bytes()).hexdigest(),
            "route_backend": "rapidwright-native-device-database-v1",
        },
        "summary": {
            "capacity_buckets": 1, "clock_regions": 1, "dedicated_edges": 12,
            "sites": 16, "slrs": 1,
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    native.write_text(json.dumps({
        "schema": "emuflow.xilinx-native-device-constraints/v1",
        "payload": payload,
        "payload_sha256": hashlib.sha256(encoded).hexdigest(),
    }, sort_keys=True), encoding="utf-8")
    return mapped, packed, architecture, native, provider


def _write_placement(path, name_map, *, break_chain=False):
    macro_site = {"carry0": (0, 0), "carry1": ((1, 1) if break_chain else (0, 1))}
    macro_bels = {}
    for macro in name_map["carry_macros"]:
        carry = next(item for item in macro["members"] if item["cell_type"] == "CARRY8")
        for member in macro["members"]:
            macro_bels[member["instance"]] = (carry["instance"], member["bel"])
    rows = []
    for atom in name_map["atoms"]:
        instance = atom["instance"]
        if instance == "ff":
            x, y, z = 1, 0, 0
        else:
            carry, bel = macro_bels[instance]
            x, y = macro_site[carry]
            z = 0 if bel == "CARRY8" else 2 * "ABCDEFGH".index(bel[0]) + 1
        rows.append(f"{atom['openparf']} {x} {y} {z}")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


class XilinxOpenparfCarry8Test(unittest.TestCase):
    def test_export_is_unplaced_and_enables_native_full_slice_chain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture, native, provider = _write_fixture(root)
            output = root / "openparf"
            report = export_xilinx_openparf_carry8(
                mapped, packed, architecture, native, provider, output, top="top"
            )
            config = read_json(output / "openparf.json")
            library = (output / "design.lib").read_text(encoding="utf-8")
            placement_seed = (output / "design.pl").read_text(encoding="utf-8")
        self.assertEqual(report["macro_units"], 2)
        self.assertFalse(report["preplacement"])
        self.assertEqual(config["carry_chain_module_name"], "CARRY8")
        self.assertEqual(config["carry_chain_at_name"], "CARRY8")
        self.assertEqual(config["carry_chain_legalization_flag"], 1)
        self.assertEqual(config["resource_categories"]["CARRY8"], "Carry")
        self.assertIn("PIN CI INPUT CAS", library)
        self.assertIn("PIN CO[7] OUTPUT CAS", library)
        self.assertEqual(placement_seed, "")

    def test_exact_macro_and_native_adjacency_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture, native, provider = _write_fixture(root)
            output = root / "openparf"
            export_xilinx_openparf_carry8(
                mapped, packed, architecture, native, provider, output, top="top"
            )
            name_map = read_json(output / "name_map.json")
            placement = root / "legal.pl"
            _write_placement(placement, name_map)
            certificate = validate_xilinx_openparf_carry8_placement(
                placement, output / "name_map.json", mapped, architecture,
                native, provider,
            )
            self.assertEqual(certificate["schema"], OPENPARF_CARRY8_PLACEMENT_SCHEMA)
            self.assertEqual(certificate["summary"]["carry8_macros"], 2)
            self.assertEqual(certificate["summary"]["native_carry_edges"], 1)

            broken = root / "broken.pl"
            _write_placement(broken, name_map, break_chain=True)
            with self.assertRaisesRegex(ValidationError, "native adjacency"):
                validate_xilinx_openparf_carry8_placement(
                    broken, output / "name_map.json", mapped, architecture,
                    native, provider,
                )

    def test_core_sources_model_eight_lut_full_slice_units(self):
        chain_info = (
            ROOT / "engines/openparf/openparf/custom_data/chain_info/src/chain_info.cpp"
        ).read_text(encoding="utf-8")
        legalizer = (
            ROOT / "engines/openparf/openparf/ops/chain_legalizer/src/chain_legalizer.cpp"
        ).read_text(encoding="utf-8")
        self.assertIn("associated_luts_per_unit = is_carry8 ? 8 : 4", chain_info)
        self.assertIn("luts_per_unit == 4 || luts_per_unit == 8", legalizer)
        self.assertIn("luts_per_unit == 8 ? 1.0 : 0.5", legalizer)

    def test_runner_invokes_native_runtime_once_without_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture, native, provider = _write_fixture(root)
            output = root / "openparf"

            def fake_run(config_path, **_kwargs):
                self.assertEqual(config_path, output / "openparf.json")
                placement = output / "results" / "xilinx_carry8_native.pl"
                placement.parent.mkdir(parents=True, exist_ok=True)
                _write_placement(placement, read_json(output / "name_map.json"))
                return placement

            with mock.patch(
                "emuflow.xilinx_openparf_carry8.validate_openparf_runtime",
                return_value={"installation": "", "python": ""},
            ) as validate_runtime, mock.patch(
                "emuflow.xilinx_openparf_carry8.run_openparf",
                side_effect=fake_run,
            ) as run_openparf:
                report = run_xilinx_openparf_carry8_qualification(
                    mapped, packed, architecture, native, provider, output,
                    top="top",
                )

        validate_runtime.assert_called_once_with(
            install_root=None, python_executable=None
        )
        run_openparf.assert_called_once()
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["certificate"]["summary"]["carry8_macros"], 2)
        self.assertEqual(report["manifest"]["fallback"], "forbidden")
        self.assertFalse(report["manifest"]["preplacement"])


if __name__ == "__main__":
    unittest.main()
