import unittest

from emuflow.errors import ValidationError
from emuflow.xilinx_placer_capability import XILINX_PLACER_REQUIRED_STAGES
from emuflow.xilinx_placer_comparison import (
    XILINX_PLACER_COMPARISON_SCHEMA,
    compare_xilinx_placer_candidates,
)


def _entry(status="native_supported", adapter_validation=None):
    value = {"status": status, "evidence": ["test"]}
    if adapter_validation is not None:
        value["adapter_validation"] = adapter_validation
    return value


def _report(provider, *, detailed="native_supported", lut="native_supported"):
    stages = {stage: _entry() for stage in XILINX_PLACER_REQUIRED_STAGES}
    stages["detailed_placement"] = _entry(detailed)
    primitives = {"LUT6": _entry(lut)}
    if lut == "adapter_required":
        primitives["LUT6"] = _entry(lut, "missing")
    return {
        "schema": "emuflow.xilinx-placer-capability/v1",
        "provider": provider,
        "revision": "test-revision",
        "stages": stages,
        "primitives": primitives,
        "constraints": {"clock_region": _entry()},
    }


class XilinxPlacerComparisonTest(unittest.TestCase):
    def test_comparison_is_deterministic_and_does_not_pick_default(self):
        result = compare_xilinx_placer_candidates(
            [_report("z-ready"), _report("a-blocked", detailed="core_missing")],
            required_primitives=["LUT6", "LUT6"],
            required_constraints=["clock_region"],
        )
        self.assertEqual(result["schema"], XILINX_PLACER_COMPARISON_SCHEMA)
        self.assertEqual(
            [item["provider"] for item in result["candidates"]],
            ["a-blocked", "z-ready"],
        )
        self.assertEqual(result["qor_eligible_providers"], ["z-ready"])
        self.assertEqual(result["default_selection"]["status"], "deferred")
        self.assertEqual(
            result["candidates"][0]["physical_qor_gate"], "blocked"
        )
        self.assertEqual(
            result["candidates"][1]["physical_qor_gate"], "required"
        )

    def test_unvalidated_adapter_does_not_enter_qor_gate(self):
        result = compare_xilinx_placer_candidates(
            [_report("adapter", lut="adapter_required")],
            required_primitives=["LUT6"],
            required_constraints=["clock_region"],
        )
        self.assertEqual(result["qor_eligible_providers"], [])
        self.assertIn(
            "primitives.LUT6",
            result["candidates"][0]["capability_gate"]["blocked_entries"],
        )

    def test_duplicate_provider_is_rejected(self):
        with self.assertRaisesRegex(ValidationError, "duplicate"):
            compare_xilinx_placer_candidates(
                [_report("same"), _report("same")],
                required_primitives=["LUT6"],
                required_constraints=["clock_region"],
            )


if __name__ == "__main__":
    unittest.main()
