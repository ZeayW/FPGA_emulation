import tempfile
import unittest
from pathlib import Path

from emuflow.io import read_json, write_json
from emuflow.xilinx_openparf_atomic import (
    build_xilinx_openparf_atomic_source,
    export_xilinx_openparf_atomic,
    validate_xilinx_openparf_atomic_placement,
)
from emuflow.xilinx_openparf_bridge import (
    materialize_xilinx_openparf_atomic_contract,
)
from emuflow.xilinx_packing import validate_xilinx_packing
from emuflow.xilinx_placement import validate_xilinx_placement
from tests.test_xilinx_openparf_carry8 import _write_fixture


def _write_atomic_placement(path: Path, name_map: dict) -> None:
    rows = []
    for atom in name_map["atoms"]:
        instance = atom["instance"]
        if instance == "src_ff":
            x, y, z = 2, 0, 0
        elif instance == "ff":
            x, y, z = 2, 0, 2
        elif instance.startswith("carry0$lut"):
            x, y, z = 0, 0, 2 * int(instance.rsplit("lut", 1)[1]) + 1
        elif instance.startswith("carry1$lut"):
            x, y, z = 0, 1, 2 * int(instance.rsplit("lut", 1)[1]) + 1
        elif instance == "carry0":
            x, y, z = 0, 0, 0
        elif instance == "carry1":
            x, y, z = 0, 1, 0
        else:
            raise AssertionError(f"unexpected fixture atom {instance!r}")
        rows.append(f"{atom['openparf']} {x} {y} {z}")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


class XilinxOpenparfAtomicCarryIntegrationTest(unittest.TestCase):
    def test_atomic_source_keeps_ramb18_halves_unpaired(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped = root / "mapped.json"
            cells = {
                name: {
                    "type": "RAMB18E2",
                    "parameters": {},
                    "attributes": {},
                    "port_directions": {},
                    "connections": {},
                }
                for name in ("ram0", "ram1")
            }
            write_json(mapped, {
                "modules": {
                    "top": {
                        "attributes": {"top": "1"},
                        "ports": {},
                        "netnames": {},
                        "cells": cells,
                    },
                },
            }, compact=True)
            source = root / "atomic-source.json"
            build_xilinx_openparf_atomic_source(mapped, source, top="top")
            clusters = read_json(source)["clusters"]
            self.assertEqual(len(clusters), 2)
            for cluster in clusters:
                self.assertEqual(cluster["kind"], "hard")
                self.assertEqual(cluster["site_templates"], ["RAMB18E2"])
                self.assertEqual(
                    cluster["assignments"][0]["bel_candidates"],
                    ["RAMB18E2_L", "RAMB18E2_U"],
                )

    def test_unified_atomic_route_preserves_full_slice_and_chain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, _packed, architecture, native, provider = _write_fixture(root)
            source = root / "atomic-source.json"
            build_xilinx_openparf_atomic_source(mapped, source, top="top")
            source_value = read_json(source)
            self.assertEqual(
                [cluster["kind"] for cluster in source_value["clusters"]].count(
                    "carry"
                ),
                2,
            )
            self.assertEqual(len(source_value["cascade_chains"]), 1)

            output = root / "openparf"
            export_xilinx_openparf_atomic(
                mapped,
                source,
                architecture,
                output,
                top="top",
                native_constraints_path=native,
                provider_manifest_path=provider,
            )
            config = read_json(output / "openparf.json")
            self.assertEqual(config["carry_chain_legalization_flag"], 0)
            self.assertEqual(config["resource_categories"]["CARRY8"], "Carry")
            constraints = read_json(output / "physical-macro-groups.json")
            groups = constraints["groups"]
            self.assertEqual(
                sum(group["kind"] == "site_cascade" for group in groups), 1
            )
            carry_group = next(
                group for group in groups if group["kind"] == "site_cascade"
            )
            self.assertEqual(carry_group["owned_resources"], ["CARRY8"])
            self.assertEqual(
                carry_group["chain_template"]["kind"],
                "directed-site-chain/v1",
            )
            self.assertEqual(carry_group["chain_template"]["chain_length"], 2)
            self.assertEqual(len(constraints["site_chain_sets"]), 1)

            placement = root / "native.pl"
            _write_atomic_placement(placement, read_json(output / "name_map.json"))
            certificate_path = root / "certificate.json"
            certificate = validate_xilinx_openparf_atomic_placement(
                placement,
                output / "name_map.json",
                mapped,
                architecture,
                certificate_path,
                native_constraints_path=native,
                provider_manifest_path=provider,
            )
            self.assertEqual(certificate["summary"]["carry8_macros"], 2)
            self.assertEqual(certificate["summary"]["native_carry_edges"], 1)
            certificate["runtime_validation"] = "native-openparf"
            write_json(certificate_path, certificate, compact=True)

            packed_output = root / "packed.json"
            placement_output = root / "placement.json"
            materialize_xilinx_openparf_atomic_contract(
                mapped,
                architecture,
                certificate_path,
                packed_output,
                placement_output,
                top="top",
                source_packed_path=source,
                native_constraints_path=native,
                provider_manifest_path=provider,
            )
            validate_xilinx_packing(
                mapped, packed_output, top="top", architecture_path=architecture
            )
            physical = validate_xilinx_placement(
                packed_output,
                architecture,
                placement_output,
                native_constraints_path=native,
                provider_manifest_path=provider,
            )
            self.assertEqual(
                read_json(packed_output)["summary"]["cluster_kinds"]["carry"], 2
            )
            self.assertEqual(physical["cascade_chains"], 1)


if __name__ == "__main__":
    unittest.main()
