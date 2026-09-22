import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from emuflow.xilinx_physical_backend import (
    _physical_clock_periods,
    run_rapidwright_partition_backend,
)


class XilinxPhysicalBackendTest(unittest.TestCase):
    def test_physical_clocks_only_include_emuir_clocks(self):
        runtime = {
            "fabric_clock": {"period_ns": 4.0},
            "virtual_dut_clock": {"nominal_period_ns": 40.0},
        }
        self.assertEqual(
            _physical_clock_periods({"clocks": [{"id": "clk"}]}, runtime),
            {"clk": 40.0},
        )
        self.assertEqual(
            _physical_clock_periods(
                {"clocks": [{"id": "clk"}, {"id": "fabric_clk"}]},
                runtime,
            ),
            {"clk": 40.0, "fabric_clk": 4.0},
        )

    def test_single_slr_certificate_is_checked_against_openparf_guidance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            guidance = root / "physical" / "openparf-guidance" / "guidance.json"
            guide_calls = []

            def record_guidance(*_args, **kwargs):
                guide_calls.append(kwargs.get("slr"))
                return {}

            plan_calls = []

            def record_plan(*_args, **kwargs):
                plan_calls.append(kwargs.get("required_slr"))
                return {"selected_slr": "SLR1"}

            def stop_after_certificate_check(*_args, **kwargs):
                self.assertEqual(kwargs["guidance_path"], guidance)
                self.assertEqual(guide_calls, [None, "SLR1"])
                self.assertEqual(plan_calls, [None, "SLR1"])
                raise RuntimeError("certificate-check-observed")

            patches = (
                mock.patch(
                    "emuflow.xilinx_physical_backend.ArchitectureDB.load",
                    return_value=SimpleNamespace(part="xcvu19p-test"),
                ),
                mock.patch(
                    "emuflow.xilinx_physical_backend.emit_xilinx_mapped_json",
                    return_value={"top": "top"},
                ),
                mock.patch(
                    "emuflow.xilinx_physical_backend.pack_xilinx_sites",
                    return_value={"summary": {}},
                ),
                mock.patch(
                    "emuflow.xilinx_physical_backend.validate_xilinx_packing",
                    return_value={},
                ),
                mock.patch(
                    "emuflow.xilinx_physical_backend.run_xilinx_openparf_guidance",
                    side_effect=record_guidance,
                ),
                mock.patch(
                    "emuflow.xilinx_physical_backend.plan_xilinx_single_slr",
                    side_effect=record_plan,
                ),
                mock.patch(
                    "emuflow.xilinx_physical_backend.validate_xilinx_single_slr_plan",
                    side_effect=stop_after_certificate_check,
                ),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                with self.assertRaisesRegex(RuntimeError, "certificate-check-observed"):
                    run_rapidwright_partition_backend(
                        fpga="fpga0",
                        part="xcvu19p-test",
                        merged_ir_path=root / "input.json",
                        architecture_path=root / "architecture.json",
                        runtime={},
                        original_cells=0,
                        transport_cells=0,
                        output_dir=root / "physical",
                        boundary_identity_path=root / "boundary.json",
                        rapidwright_jar=root / "rapidwright.jar",
                        java=root / "java",
                        classes_dir=root / "classes",
                        java_source=root / "route.java",
                        device_data_root=root / "device-data",
                        timing_data_dir=root / "timing-data",
                    )


if __name__ == "__main__":
    unittest.main()
