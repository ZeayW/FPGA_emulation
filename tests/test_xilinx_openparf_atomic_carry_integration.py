from tests.openparf_export_fixture import export_atomic_facts as export_xilinx_openparf_atomic
from tests.openparf_bridge_fixture import materialize_bridge_fixture as materialize_xilinx_openparf_atomic_contract
import tempfile
import unittest
from pathlib import Path

from emuflow.common.io import read_json, write_json
from emuflow.common.errors import ValidationError
from emuflow.platform.architecture import ArchitectureDB
from unittest import mock
from emuflow.physical.openparf.xilinx_openparf_atomic import (
    _native_carry_y_axis_order,
    build_xilinx_openparf_atomic_source,
    read_xilinx_openparf_atomic_source,
    read_xilinx_openparf_atomic_name_map,
    validate_xilinx_openparf_atomic_placement,
)
from emuflow.physical.xilinx_packing import validate_xilinx_packing
from emuflow.physical.xilinx_placement import validate_xilinx_placement
from tests.xilinx_carry_fixture import _write_fixture


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
    def test_native_carry_axis_rejects_mixed_physical_directions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _mapped, _packed, architecture, native, _provider = _write_fixture(root)
            native_value = read_json(native)
            native_value["payload"]["dedicated_adjacency"][0]["chains"].append(
                ["SLICE_X3Y1", "SLICE_X3Y0"]
            )
            architecture_db = ArchitectureDB.load(architecture)
            with self.assertRaisesRegex(
                ValidationError, "disagree on physical Y orientation"
            ), mock.patch.object(
                architecture_db, "site_named", side_effect=AssertionError
            ):
                _native_carry_y_axis_order(
                    architecture_db, native_value
                )

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
            clusters = read_xilinx_openparf_atomic_source(source)["clusters"]
            self.assertEqual(len(clusters), 2)
            for cluster in clusters:
                self.assertEqual(cluster["kind"], "hard")
                self.assertEqual(cluster["site_templates"], ["RAMB18E2"])
                self.assertEqual(
                    cluster["assignments"][0]["bel_candidates"],
                    ["RAMB18E2_L", "RAMB18E2_U"],
                )

    def test_atomic_certificate_rejects_broken_carry_adjacency_and_lut_roles(self):
        for broken in ("chain-gap", "lut-role-collision"):
            with self.subTest(broken=broken), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                mapped, _packed, architecture, native, provider = _write_fixture(root)
                source, output = root / "atomic-source.json", root / "openparf"
                build_xilinx_openparf_atomic_source(mapped, source, top="top")
                export_xilinx_openparf_atomic(
                    mapped, source, architecture, output, top="top",
                    native_constraints_path=native, provider_manifest_path=provider,
                )
                placement = root / "native.pl"
                with read_xilinx_openparf_atomic_name_map(output / "name_map.json") as name_map:
                    _write_atomic_placement(placement, name_map)
                    instances = {atom["openparf"]: atom["instance"] for atom in name_map["atoms"]}
                rows = []
                for row in placement.read_text().splitlines():
                    fields = row.split()
                    instance = instances[fields[0]]
                    if broken == "chain-gap" and instance.startswith("carry1"):
                        fields[2] = "2"
                    if broken == "lut-role-collision" and instance == "carry0$lut0":
                        fields[3] = "3"
                    rows.append(" ".join(fields))
                placement.write_text("\n".join(rows) + "\n")
                with self.assertRaises(ValidationError):
                    validate_xilinx_openparf_atomic_placement(
                        placement, output / "name_map.json", mapped, architecture,
                        root / "certificate.json", packed_path=source,
                        native_constraints_path=native, provider_manifest_path=provider,
                    )

    def test_unified_atomic_route_preserves_full_slice_and_chain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, _packed, architecture, native, provider = _write_fixture(root)
            source = root / "atomic-source.json"
            build_xilinx_openparf_atomic_source(mapped, source, top="top")
            source_value = read_xilinx_openparf_atomic_source(source)
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
            _write_atomic_placement(placement, read_xilinx_openparf_atomic_name_map(output / "name_map.json"))
            certificate_path = root / "certificate.json"
            certificate = validate_xilinx_openparf_atomic_placement(
                placement,
                output / "name_map.json",
                mapped,
                architecture,
                certificate_path,
                packed_path=source,
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
