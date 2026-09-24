import copy
import unittest

from emuflow.errors import ValidationError
from emuflow.xilinx_placer_capability import (
    XILINX_PLACER_CAPABILITY_SCHEMA,
    XILINX_PLACER_REQUIRED_STAGES,
    qualify_xilinx_placer_capabilities,
    validate_xilinx_placer_capability_report,
)


class XilinxPlacerCapabilityTest(unittest.TestCase):
    def _entry(self, status="native_supported", **extra):
        return {"status": status, "evidence": ["module.py:10"], **extra}

    def _report(self):
        return {
            "schema": XILINX_PLACER_CAPABILITY_SCHEMA,
            "provider": "fixture-placer",
            "revision": "0123456789abcdef",
            "stages": {
                stage: self._entry() for stage in XILINX_PLACER_REQUIRED_STAGES
            },
            "primitives": {
                "LUT6": self._entry(),
                "DSP48E2": self._entry(
                    "adapter_required", adapter_validation="pass"
                ),
            },
            "constraints": {"clock_region": self._entry()},
        }

    def test_qualified_report_passes_for_required_population(self):
        report = self._report()
        checked = validate_xilinx_placer_capability_report(report)
        decision = qualify_xilinx_placer_capabilities(
            checked,
            required_primitives=("LUT6", "DSP48E2"),
            required_constraints=("clock_region",),
        )
        self.assertEqual(decision["status"], "pass")
        self.assertEqual(decision["blocked_entries"], [])

    def test_missing_adapter_validation_fails_closed(self):
        report = self._report()
        report["primitives"]["DSP48E2"]["adapter_validation"] = "missing"
        decision = qualify_xilinx_placer_capabilities(
            report,
            required_primitives=("DSP48E2",),
            required_constraints=("clock_region",),
        )
        self.assertEqual(decision["status"], "fail")
        self.assertEqual(
            decision["blocked_entries"], ["primitives.DSP48E2"]
        )

    def test_unverified_stage_and_absent_primitive_fail_closed(self):
        report = self._report()
        report["stages"]["packing"] = self._entry("unverified")
        decision = qualify_xilinx_placer_capabilities(
            report,
            required_primitives=("LUT6", "RAMB36E2"),
            required_constraints=("clock_region",),
        )
        self.assertEqual(decision["status"], "fail")
        self.assertEqual(decision["missing_entries"], ["primitives.RAMB36E2"])
        self.assertIn("stages.packing", decision["blocked_entries"])

    def test_report_rejects_ambiguous_adapter_state(self):
        report = self._report()
        report["primitives"]["LUT6"]["adapter_validation"] = "pass"
        with self.assertRaisesRegex(ValidationError, "only for adapter_required"):
            validate_xilinx_placer_capability_report(report)

    def test_report_requires_exact_stage_population(self):
        report = copy.deepcopy(self._report())
        del report["stages"]["detailed_placement"]
        with self.assertRaisesRegex(ValidationError, "exactly"):
            validate_xilinx_placer_capability_report(report)


if __name__ == "__main__":
    unittest.main()
