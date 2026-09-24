import copy
import json
import tempfile
import unittest
from pathlib import Path

from emuflow.amf_placer import (
    AMF_PLACER_ADAPTER_CONTRACT_SCHEMA,
    build_amf_placer_adapter_contract,
    classify_amf_primitive,
    probe_amf_placer_capabilities,
    validate_amf_placer_adapter_contract,
)
from emuflow.errors import ValidationError
from emuflow.xilinx_placer_capability import (
    XILINX_PLACER_CAPABILITY_STATUSES,
    qualify_xilinx_placer_capabilities,
    validate_xilinx_placer_capability_report,
)


class AMFPlacerCapabilityTest(unittest.TestCase):
    def test_inventory_uses_the_common_four_state_contract(self) -> None:
        report = probe_amf_placer_capabilities()
        cells = report["primitives"]
        checked = validate_xilinx_placer_capability_report(report)

        self.assertEqual(checked["provider"], report["provider"])
        self.assertFalse(report["production_provider_ready"])
        self.assertTrue(
            {item["status"] for item in cells.values()}
            <= set(XILINX_PLACER_CAPABILITY_STATUSES)
        )
        for cell_type in (
            "LUT1", "LUT6_2", "FDCE", "FDSE", "CARRY8", "DSP48E2",
            "MUXF7", "MUXF8", "RAMB18E2", "RAMB36E2",
        ):
            self.assertEqual(cells[cell_type]["status"], "native_supported")
        for cell_type in ("GND", "VCC"):
            self.assertEqual(cells[cell_type]["status"], "adapter_required")
            self.assertEqual(cells[cell_type]["adapter_validation"], "missing")
        for cell_type in ("MUXF9", "URAM288"):
            self.assertEqual(cells[cell_type]["status"], "core_missing")

    def test_unknown_primitive_remains_unverified(self) -> None:
        capability = classify_amf_primitive("FUTURE_PRIMITIVE")
        self.assertEqual(capability["status"], "unverified")

    def test_common_qualification_fails_closed(self) -> None:
        report = probe_amf_placer_capabilities()
        decision = qualify_xilinx_placer_capabilities(
            report,
            required_primitives=("LUT6", "FDRE"),
            required_constraints=("design_netlist_import",),
        )
        self.assertEqual(decision["status"], "fail")
        self.assertIn(
            "stages.physical_export", decision["blocked_entries"]
        )
        self.assertIn(
            "constraints.design_netlist_import",
            decision["blocked_entries"],
        )

    def test_adapter_contract_roundtrip_is_stable_and_disabled(self) -> None:
        contract = build_amf_placer_adapter_contract()
        self.assertEqual(contract["schema"], AMF_PLACER_ADAPTER_CONTRACT_SCHEMA)
        serialized = json.dumps(contract, sort_keys=True, separators=(",", ":"))
        restored = json.loads(serialized)
        self.assertEqual(validate_amf_placer_adapter_contract(restored), contract)
        self.assertFalse(contract["production_provider_ready"])

        bad = copy.deepcopy(contract)
        bad["production_provider_ready"] = True
        with self.assertRaisesRegex(ValidationError, "must remain disabled"):
            validate_amf_placer_adapter_contract(bad)

    def test_source_probe_requires_public_basic_release_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {
                "README.MD": (
                    "basic implementation of AMF-Placer 2.0\n"
                    "VCU108\nclock tree synthesis\n"
                ),
                "src/lib/HiFPlacer/designInfo/DesignInfo.h": "\n".join(
                    f'"{cell_type}"'
                    for cell_type in (
                        "LUT1", "LUT2", "LUT3", "LUT4", "LUT5", "LUT6",
                        "LUT6_2", "FDCE", "FDPE", "FDRE", "FDSE", "CARRY8",
                        "DSP48E2", "MUXF7", "MUXF8", "RAMB18E2", "RAMB36E2",
                    )
                ),
                "src/lib/HiFPlacer/designInfo/DesignInfo.cc": (
                    "vivado extracted design information file\n"
                ),
                "src/lib/HiFPlacer/deviceInfo/DeviceInfo.cc": "device parser\n",
                "src/app/AMFPlacer/AMFPlacer.h": (
                    "vivado extracted device information file\n"
                    "InitialPacker GlobalPlacer ParallelCLBPacker\n"
                ),
                "src/lib/HiFPlacer/placement/packing/ParallelCLBPacker.h": (
                    "timingDrivenDetailedPlacement\n"
                ),
            }
            for relative, contents in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents, encoding="utf-8")

            report = probe_amf_placer_capabilities(public_source_root=root)
            self.assertEqual(report["audit"]["source"]["status"], "pass")

            design_types = root / "src/lib/HiFPlacer/designInfo/DesignInfo.h"
            design_types.write_text(
                design_types.read_text(encoding="utf-8") + '\n"MUXF9"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "no_muxf9_core_type"):
                probe_amf_placer_capabilities(public_source_root=root)

    def test_public_feature_gaps_are_not_promoted_from_paper_claims(self) -> None:
        report = probe_amf_placer_capabilities()
        stages = report["stages"]
        constraints = report["constraints"]
        self.assertEqual(stages["packing"]["status"], "native_supported")
        self.assertEqual(
            stages["detailed_placement"]["status"], "native_supported"
        )
        self.assertEqual(constraints["multi_slr"]["status"], "core_missing")
        self.assertEqual(
            constraints["xilinx_ultrascaleplus_xcvu19p"]["status"],
            "unverified",
        )
        self.assertEqual(
            constraints["vivado_free_runtime"]["status"],
            "adapter_required",
        )


if __name__ == "__main__":
    unittest.main()
