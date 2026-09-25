import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from emuflow.architecture import ArchitectureDB
from emuflow.errors import ValidationError
from emuflow.io import read_json
from emuflow.rapidwright_provider import validate_rapidwright_provider_manifest
from emuflow.xilinx_native_device_constraints import (
    require_xilinx_native_constraint_capability,
    validate_xilinx_native_device_constraints,
)


ROOT = Path(__file__).resolve().parents[1]
PINNED = ROOT / "resources/rapidwright/xcvu19p-fsva3824-2-e.provider.json"
JAVA_EXPORTER = (
    ROOT / "scripts/rapidwright/EmuFlowNativeDeviceConstraints.java"
)


def _architecture():
    carry_bel = {
        "name": "CARRY8",
        "type": "CARRY8",
        "z": 0,
        "compatible_cells": ["CARRY8"],
    }
    return {
        "schema": "emuflow.archdb/v1",
        "part": "xcvu19p-fsva3824-2-e",
        "source": {"format": "unit-test/v1"},
        "policy": {"name": "native-constraints-fixture"},
        "site_templates": {
            "SLICEL": {"bels": [carry_bel], "alternative_templates": []},
            "SLICEM": {"bels": [carry_bel], "alternative_templates": []},
        },
        "sites": [
            {
                "name": "SLICE_X0Y0",
                "type": "SLICEL",
                "template": "SLICEL",
                "x": 0,
                "y": 0,
                "physical_region": {"slr": "SLR0", "clock_region": "X0Y0"},
            },
            {
                "name": "SLICE_X0Y1",
                "type": "SLICEM",
                "template": "SLICEM",
                "x": 0,
                "y": 1,
                "physical_region": {"slr": "SLR0", "clock_region": "X0Y0"},
            },
        ],
    }


def _capabilities():
    return {
        "clock_region_site_capacity": "native_supported",
        "dedicated_adjacency.BRAM_CASCADE": "core_missing",
        "dedicated_adjacency.CARRY_NEXT": "native_supported",
        "dedicated_adjacency.DSP_CASCADE": "core_missing",
        "dedicated_adjacency.URAM_CASCADE": "core_missing",
        "half_column_clock_capacity": "unverified",
        "slr_site_capacity": "native_supported",
    }


def _artifact(architecture_path, manifest_path):
    checked = validate_rapidwright_provider_manifest(read_json(manifest_path))
    payload = {
        "capabilities": _capabilities(),
        "dedicated_adjacency": [
            {
                "chains": [["SLICE_X0Y0", "SLICE_X0Y1"]],
                "edge_count": 1,
                "endpoint_contract": "carry8-co7-ci-all-v1",
                "kind": "CARRY_NEXT",
                "native_proof_sha256": "b" * 64,
                "proof_method": (
                    "rapidwright-dedicated-sitepin-vector-"
                    "same-canonical-node-v1"
                ),
            }
        ],
        "site_capacity": [
            {
                "clock_region": "X0Y0",
                "site_type": "SLICEL",
                "sites": 1,
                "slr": "SLR0",
            },
            {
                "clock_region": "X0Y0",
                "site_type": "SLICEM",
                "sites": 1,
                "slr": "SLR0",
            },
        ],
        "source": {
            "architecture_sha256": hashlib.sha256(
                architecture_path.read_bytes()
            ).hexdigest(),
            "device": checked["device_identity"]["device"],
            "device_database_md5": checked["device_database_md5"],
            "full_part": checked["part"],
            "generator": {
                "revision": checked["revision"],
                "version": checked["version"],
            },
            "provider_manifest_sha256": hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest(),
            "route_backend": "rapidwright-native-device-database-v1",
        },
        "summary": {
            "capacity_buckets": 2,
            "clock_regions": 1,
            "dedicated_edges": 1,
            "dedicated_edges_by_kind": {
                "BRAM_CASCADE": 0,
                "CARRY_NEXT": 1,
                "DSP_CASCADE": 0,
                "URAM_CASCADE": 0,
            },
            "sites": 2,
            "slrs": 1,
        },
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "schema": "emuflow.xilinx-native-device-constraints/v2",
        "payload": payload,
        "payload_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _reseal(artifact):
    encoded = json.dumps(
        artifact["payload"], sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    artifact["payload_sha256"] = hashlib.sha256(encoded).hexdigest()


class XilinxNativeDeviceConstraintsTest(unittest.TestCase):
    def _fixture(self, root):
        architecture_path = root / "architecture.json"
        manifest_path = root / "provider.json"
        architecture_path.write_text(
            json.dumps(_architecture(), sort_keys=True), encoding="utf-8"
        )
        manifest_path.write_text(PINNED.read_text(encoding="utf-8"), encoding="utf-8")
        return architecture_path, manifest_path

    def test_carry_capacity_and_half_column_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            architecture_path, manifest_path = self._fixture(root)
            artifact = _artifact(architecture_path, manifest_path)
            report = validate_xilinx_native_device_constraints(
                artifact,
                ArchitectureDB.load(architecture_path),
                read_json(manifest_path),
                architecture_path=architecture_path,
                provider_manifest_path=manifest_path,
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["dedicated_edges"], 1)
            require_xilinx_native_constraint_capability(
                report, "dedicated_adjacency.CARRY_NEXT"
            )
            with self.assertRaisesRegex(ValidationError, "unverified"):
                require_xilinx_native_constraint_capability(
                    report, "half_column_clock_capacity"
                )

    def test_tampered_chain_or_resealed_semantics_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            architecture_path, manifest_path = self._fixture(root)
            architecture = ArchitectureDB.load(architecture_path)
            manifest = read_json(manifest_path)

            digest_tamper = _artifact(architecture_path, manifest_path)
            digest_tamper["payload"]["dedicated_adjacency"][0]["chains"][0].reverse()
            with self.assertRaisesRegex(ValidationError, "payload seal"):
                validate_xilinx_native_device_constraints(
                    digest_tamper,
                    architecture,
                    manifest,
                    architecture_path=architecture_path,
                    provider_manifest_path=manifest_path,
                )

            semantic_tampers = []
            endpoint = _artifact(architecture_path, manifest_path)
            endpoint["payload"]["dedicated_adjacency"][0][
                "endpoint_contract"
            ] = "wrong"
            semantic_tampers.append((endpoint, "endpoint contract"))
            capacity = _artifact(architecture_path, manifest_path)
            capacity["payload"]["site_capacity"][0]["sites"] = 2
            semantic_tampers.append((capacity, "capacity"))
            capability = _artifact(architecture_path, manifest_path)
            capability["payload"]["capabilities"][
                "half_column_clock_capacity"
            ] = "native_supported"
            semantic_tampers.append((capability, "half-column"))
            for artifact, message in semantic_tampers:
                with self.subTest(message=message):
                    _reseal(artifact)
                    with self.assertRaises(ValidationError):
                        validate_xilinx_native_device_constraints(
                            artifact,
                            architecture,
                            manifest,
                            architecture_path=architecture_path,
                            provider_manifest_path=manifest_path,
                        )

    def test_core_missing_carry_has_no_edges_and_fails_capability_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            architecture_path, manifest_path = self._fixture(root)
            artifact = _artifact(architecture_path, manifest_path)
            artifact["payload"]["capabilities"][
                "dedicated_adjacency.CARRY_NEXT"
            ] = "core_missing"
            artifact["payload"]["dedicated_adjacency"] = []
            artifact["payload"]["summary"]["dedicated_edges"] = 0
            artifact["payload"]["summary"]["dedicated_edges_by_kind"][
                "CARRY_NEXT"
            ] = 0
            _reseal(artifact)
            report = validate_xilinx_native_device_constraints(
                artifact,
                ArchitectureDB.load(architecture_path),
                read_json(manifest_path),
                architecture_path=architecture_path,
                provider_manifest_path=manifest_path,
            )
            self.assertEqual(report["dedicated_edges"], 0)
            with self.assertRaisesRegex(ValidationError, "core_missing"):
                require_xilinx_native_constraint_capability(
                    report, "dedicated_adjacency.CARRY_NEXT"
                )

    def test_java_exporter_uses_native_connectivity_not_coordinates(self):
        source = JAVA_EXPORTER.read_text(encoding="utf-8")
        for required in (
            "isDedicatedSitePin",
            "getConnectedSitePinName",
            "getSitePin(site)",
            "getExternalNode(site)",
            "getAllWiresInNode",
            "CARRY_NEXT",
            "DSP_CASCADE",
            "BRAM_CASCADE",
            "URAM_CASCADE",
            "PCOUT",
            "CAS_OUT_",
        ):
            self.assertIn(required, source)
        for forbidden in (
            "getNeighborSite",
            "getInstanceX",
            "getInstanceY",
            "IntentCode",
            "NODE_DEDICATED",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
