import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from emuflow.xilinx_physical_backend import (
    _physical_clock_periods,
    _select_xilinx_slr_window,
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

    def test_slr_window_uses_central_window_and_expands_for_capacity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arch = root / "arch.json"
            packed = root / "packed.json"
            sites = []
            for index in range(4):
                for x in range(4):
                    sites.append({
                        "name": f"SLICE_X{x}Y{index}", "type": "SLICEL",
                        "template": "SLICEL",
                        "x": x, "y": index,
                        "tile": {"grid_col": x, "grid_row": index * 10},
                        "physical_region": {"slr": f"SLR{index}"},
                    })
            arch.write_text(__import__("json").dumps({
                "schema": "emuflow.archdb/v1", "part": "test",
                "source": {"format": "test/v1"}, "policy": {"name": "test"},
                "site_templates": {"SLICEL": {
                    "bels": [{
                        "name": "A6LUT", "type": "LUT6", "z": 0,
                        "compatible_cells": ["LUT6"],
                    }],
                    "alternative_templates": [],
                }},
                "sites": sites,
            }), encoding="utf-8")
            packed.write_text(__import__("json").dumps({
                "schema": "emuflow.packed-site-netlist/v1",
                "clusters": [
                    {"id": f"c{i}", "kind": "slice", "assignments": []}
                    for i in range(5)
                ],
            }), encoding="utf-8")
            self.assertEqual(
                _select_xilinx_slr_window(packed, arch),
                ("SLR0", "SLR1", "SLR2"),
            )

    def test_production_backend_uses_compact_two_slr_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            guidance = root / "physical" / "openparf-guidance" / "guidance.json"

            def stop_after_placement_check(*_args, **kwargs):
                self.assertEqual(
                    kwargs["constraints_path"], root / "physical" / "placement-region.json"
                )
                raise RuntimeError("slr-window-observed")

            with (
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
                    "emuflow.xilinx_physical_backend._select_xilinx_slr_window",
                    return_value=("SLR1", "SLR2"),
                ),
                mock.patch(
                    "emuflow.xilinx_physical_backend.run_xilinx_openparf_guidance",
                    side_effect=lambda *_args, **kwargs: self.assertEqual(
                        kwargs["slrs"], ("SLR1", "SLR2")
                    ),
                ),
                mock.patch(
                    "emuflow.xilinx_physical_backend.place_xilinx_clusters",
                    side_effect=lambda *_args, **kwargs: (
                        self.assertEqual(kwargs["guidance_path"], guidance)
                        or self.assertEqual(
                            kwargs["constraints_path"],
                            root / "physical" / "placement-region.json",
                        )
                        or {"summary": {"clusters": 1}}
                    ),
                ),
                mock.patch(
                    "emuflow.xilinx_physical_backend.validate_xilinx_placement",
                    side_effect=stop_after_placement_check,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "slr-window-observed"):
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
