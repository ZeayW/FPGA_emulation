from tests.openparf_region_reference import _select_xilinx_slr_window
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from emuflow.physical.xilinx_physical_backend import (
    _compact_openparf_qualification,
    _finish_rapidwright_routed_backend,
    _physical_clock_periods,
    _run_rapidwright_routed_backend_tail,
    run_rapidwright_openparf_native_candidate_backend,
)


class XilinxPhysicalBackendTest(unittest.TestCase):
    def test_normal_route_hands_the_same_objects_to_one_timing_tail(self):
        root = Path("fixture")
        values = {"mapped_value": {}, "packed_value": {}, "placement_value": {}}
        kwargs = {
            "fpga": "F0", "part": "xcvu19p-test",
            **{name: root / name for name in (
                "merged_ir_path", "mapped_path", "packed_path", "placement_path",
                "boundary_identity_path", "rapidwright_jar", "java",
                "java_source", "device_data_root", "timing_data_dir",
            )},
            "mapped_report": {"top": "top"}, **values,
            "runtime": {}, "original_cells": 3, "transport_cells": 1,
            "output_dir": root, "opensta": "sta",
            "logic_identity_path": None, "local_identity_path": None,
            "provider": "fixture", "packing_stage": {},
            "placement_stage": {}, "placement_artifacts": {},
            "virtual_io_contract": {"sealed": "fixed-xdc-adapter"},
        }
        route_header = {"fixture": "bounded-header"}
        route_check = {"status": "pass", "route_sha256": "c" * 64}
        input_report = {"expanded_lut6_2_cells": 0, "transformed_dsp48e2_cells": 0}

        route_check['output'] = str(root / 'route.json')
        module = "emuflow.physical.xilinx_physical_backend."
        result = {'source_sha256':{'mapped_sha256':'a'*64}, 'export':input_report,
                  'route':route_check, 'manifest':route_header}
        with mock.patch(module + "run_native_rwroute_stage", return_value=result) as router, \
             mock.patch(module + "_finish_rapidwright_routed_backend", return_value={"done": True}) as finish:
            self.assertEqual(_run_rapidwright_routed_backend_tail(**kwargs), {"done": True})
        router.assert_called_once()
        self.assertTrue(router.call_args.kwargs['export_input'])
        finish.assert_called_once()
        handed = finish.call_args.kwargs
        for name, value in values.items():
            self.assertIs(handed[name], value)
        self.assertIs(handed["route_input_report"], input_report)
        self.assertIs(handed["route_report"], route_check)
        self.assertEqual(handed["route_value"], route_header)
        self.assertEqual(handed["source_sha256"]["mapped_sha256"], "a" * 64)
        self.assertIs(handed["virtual_io_contract"], kwargs["virtual_io_contract"])
        self.assertNotIn("java", handed)

    def test_completed_route_timing_never_invokes_export_or_router(self):
        root = Path("fixture")
        module = "emuflow.physical.xilinx_physical_backend."
        route_check = {"status": "pass", "route_sha256": "a" * 64}
        route_header = {"fixture": "bounded-header"}
        source = {"mapped_sha256": "b" * 64}
        with mock.patch(module + "run_native_rwroute_stage", side_effect=AssertionError("reroute forbidden")), \
             mock.patch(module + "build_xilinx_routed_timing", side_effect=RuntimeError("timing-stage-reached")) as timing:
            with self.assertRaisesRegex(RuntimeError, "timing-stage-reached"):
                _finish_rapidwright_routed_backend(
                    fpga="F0", part="xcvu19p-test", merged_ir_path=root / "ir",
                    mapped_path=root / "mapped", mapped_report={},
                    packed_path=root / "packed", placement_path=root / "placement",
                    mapped_value={}, packed_value={}, placement_value={},
                    runtime={}, original_cells=3, transport_cells=1, output_dir=root,
                    boundary_identity_path=root / "boundary", opensta="sta",
                    logic_identity_path=None, local_identity_path=None, provider="fixture",
                    packing_stage={}, placement_stage={}, placement_artifacts={},
                    source_sha256=source, route_path=root / "route.json",
                    route_input_report={}, route_report=route_check, route_value=route_header,
                )
        self.assertIs(timing.call_args.kwargs["route_value"], route_header)
        self.assertIs(timing.call_args.kwargs["source_sha256"], source)
        self.assertEqual(timing.call_args.kwargs["route_validation"], route_check)

    def test_resume_revalidates_before_timing_without_launching_router(self):
        from emuflow.common.errors import ValidationError

        for valid in (True, False):
            with self.subTest(valid=valid), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                route_path = root / "route.json"
                route_path.write_text('{"schema":"sealed-route"}', encoding="utf-8")
                arguments = {
                    "fpga": "fpga0", "part": "xcvu19p-test",
                    "mapped_report": {}, "mapped_value": {},
                    "packed_value": {}, "placement_value": {},
                    "runtime": {}, "original_cells": 1, "transport_cells": 0,
                    "output_dir": root, "opensta": None,
                    "logic_identity_path": None, "local_identity_path": None,
                    "provider": "test", "packing_stage": {},
                    "placement_stage": {}, "placement_artifacts": {},
                    "resume": True,
                }
                for name in (
                    "merged_ir_path", "mapped_path", "packed_path",
                    "placement_path", "boundary_identity_path", "rapidwright_jar",
                    "java", "java_source", "device_data_root",
                    "timing_data_dir",
                ):
                    arguments[name] = root / name
                routed={'source_sha256':{}, 'export':{}, 'route':{'status':'pass','output':str(route_path)},
                        'manifest':{'schema':'sealed-route'}}
                with (
                    mock.patch('emuflow.physical.xilinx_physical_backend.run_native_rwroute_stage',
                        return_value=routed, side_effect=None if valid else ValidationError('input mismatch')) as route,
                    mock.patch('emuflow.physical.xilinx_physical_backend.build_xilinx_routed_timing',
                        side_effect=RuntimeError('timing-boundary-reached')) as timing,
                ):
                    if valid:
                        with self.assertRaisesRegex(RuntimeError,'timing-boundary-reached'):
                            _run_rapidwright_routed_backend_tail(**arguments)
                        self.assertEqual(timing.call_args.kwargs['route_value'],{'schema':'sealed-route'})
                    else:
                        with self.assertRaisesRegex(ValidationError,'input mismatch'):
                            _run_rapidwright_routed_backend_tail(**arguments)
                        timing.assert_not_called()
                    route.assert_called_once()
                    self.assertTrue(route.call_args.kwargs['resume'])
                    self.assertTrue(route.call_args.kwargs['export_input'])

    def test_native_qualification_report_references_large_certificate(self):
        with tempfile.TemporaryDirectory() as temporary:
            certificate_path = Path(temporary) / "placement-certificate.json"
            certificate_path.write_text("sealed-certificate\n", encoding="utf-8")
            report = _compact_openparf_qualification({
                "status": "pass",
                "runtime": {"installation": "/versioned/openparf"},
                "certificate": {
                    "schema": "emuflow.openparf-atomic-placement/v2",
                    "status": "pass",
                    "part": "xcvu19p-test",
                    "provider": "native-openparf",
                    "runtime_validation": "native-openparf",
                    "source": {"mapped_sha256": "a" * 64},
                    "summary": {"atoms": 200_000, "occupied_sites": 10_000},
                    "native_convergence": {
                        "metrics": {
                            "stop_reason": "feasible-hpwl-patience",
                            "iterations": 1700,
                        },
                    },
                    "clusters": [{"large": "payload"}],
                },
            }, certificate_path)
        self.assertNotIn("clusters", report["certificate"])
        self.assertEqual(report["certificate"]["summary"]["atoms"], 200_000)
        self.assertEqual(
            report["certificate"]["native_convergence"]["metrics"][
                "stop_reason"
            ],
            "feasible-hpwl-patience",
        )
        self.assertEqual(
            report["certificate"]["artifact"]["bytes"],
            len("sealed-certificate\n"),
        )

    def test_native_candidate_bypasses_legacy_placement_call_graph(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            physical = root / "physical"
            physical_handoff_path = physical / "physical-handoff.sqlite"
            published_handoff_path = physical / "handoff-generation/physical-handoff.sqlite"
            packed_path = published_handoff_path
            placement_path = published_handoff_path
            physical.mkdir(parents=True)
            native_constraints = root / "native-constraints.json"
            provider_manifest = root / "provider-manifest.json"
            mapped_value = {"modules": {}}
            architecture_value = {"site_templates": {}}
            architecture_object = SimpleNamespace(
                part="xcvu19p-test", value=architecture_value
            )
            atomic_source = {"summary": {"physical_atoms": 2}}

            def publish_atomic_source(_mapped, output, **_kwargs):
                output.write_text("{}\n", encoding="utf-8")
                return atomic_source

            def stop_at_export(_input, output, **kwargs):
                self.assertEqual(kwargs['mapped_path'], physical / 'partition.mapped.sqlite')
                self.assertEqual(kwargs['handoff_path'], packed_path)
                self.assertEqual(output, physical / 'route.json')
                self.assertTrue(kwargs['export_input'])
                self.assertNotIn('mapped_value',kwargs)
                raise RuntimeError('native-bridge-reached-rwroute')

            def qualify(*_args, **_kwargs):
                certificate_path = (
                    physical / "openparf-native/placement-certificate.json"
                )
                certificate_path.parent.mkdir(parents=True, exist_ok=True)
                certificate_path.write_text("{}\n", encoding="utf-8")
                from tests.openparf_input_reference import _write_site_database
                _write_site_database(certificate_path.with_name("site-map.sqlite3"), {
                    "coordinate_system":"native-device-tile-column-row-v1",
                    "architecture_sha256":"0"*64,
                    "sites":[{"dense_x":0,"dense_y":0,"physical_x":0,"physical_y":0,
                        "placement_x":0.5,"placement_y":0.5,"site":"S0",
                        "resources":{"FF":16},"physical_sites":{"FF":["S0"]}}]})
                from emuflow.common.io import file_sha256
                self.assertEqual(_kwargs['expected_part'],'xcvu19p-test')
                return {
                    "status": "pass",
                    "certificate_path":str(certificate_path),
                    "allowed_slrs":["SLR1"],"region_path":str(physical/'placement-region.json'),
                    "handoff_generation":[0]*7,
                    "bridge":{"status":"pass","physical_handoff":str(published_handoff_path),
                        "physical_handoff_sha256":"0"*64,
                        "validation":{"packing":{"status":"pass"},"placement":{"status":"pass"}}},
                    "manifest":{"site_database":{"sha256":file_sha256(
                        certificate_path.with_name("site-map.sqlite3"))}},
                    "certificate": {
                        "schema": "emuflow.openparf-atomic-placement/v2",
                        "status": "pass",
                        "part": "xcvu19p-test",
                        "provider": "native-openparf",
                        "runtime_validation": "native-openparf",
                        "source": {},
                        "summary": {},
                    },
                }

            with (
                mock.patch(
                    "emuflow.physical.xilinx_physical_backend.emit_xilinx_mapped_index",
                    return_value={"top": "top", "output_sha256": "0" * 64},
                ),
                mock.patch(
                    "emuflow.physical.xilinx_physical_backend.open_xilinx_mapped_index",
                    return_value=mapped_value,
                ),
                mock.patch("emuflow.physical.xilinx_physical_backend.read_json") as read,
                mock.patch(
                    "emuflow.physical.xilinx_physical_backend.build_xilinx_openparf_atomic_source",
                    side_effect=publish_atomic_source,
                ) as build_source,
                mock.patch(
                    "emuflow.physical.xilinx_physical_backend.run_qualification",
                    side_effect=qualify,
                ) as qualify,
                mock.patch(
                    "emuflow.physical.xilinx_physical_backend.open_xilinx_physical_handoff",
                    return_value=({"summary":{"clusters":2}},{"summary":{"clusters":1}}),
                ) as bridge,
                mock.patch(
                    "emuflow.physical.xilinx_physical_backend.run_native_rwroute_stage",
                    side_effect=stop_at_export,
                ) as export,
                mock.patch(
                    "emuflow.physical.xilinx_physical_backend._sha256",
                    return_value="0" * 64,
                ) as digest,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "native-bridge-reached-rwroute"
                ):
                    run_rapidwright_openparf_native_candidate_backend(
                        fpga="fpga0",
                        part="xcvu19p-test",
                        merged_ir_path=root / "input.json",
                        architecture_path=root / "architecture.json",
                        runtime={},
                        original_cells=0,
                        transport_cells=0,
                        output_dir=physical,
                        boundary_identity_path=root / "boundary.json",
                        rapidwright_jar=root / "rapidwright.jar",
                        java=root / "java",
                        java_source=root / "route.java",
                        device_data_root=root / "device-data",
                        timing_data_dir=root / "timing-data",
                        openparf_native_constraints=native_constraints,
                        openparf_provider_manifest=provider_manifest,
                    )
            build_source.assert_called_once()
            qualify.assert_called_once()
            bridge.assert_called_once_with(published_handoff_path,validated_sha256="0"*64,validated_generation=[0]*7)
            self.assertFalse((physical/'packed-sites.json').exists())
            self.assertFalse((physical/'placement.json').exists())
            export.assert_called_once()
            self.assertNotIn(mock.call(published_handoff_path),digest.call_args_list)
            read.assert_not_called()
            self.assertIs(
                build_source.call_args.kwargs["mapped_value"], mapped_value
            )
            self.assertIs(
                qualify.call_args.kwargs["mapped_value"], mapped_value
            )

    def test_physical_clocks_only_include_emuir_clocks(self):
        runtime = {
            "fabric_clock": {"period_ns": 4.0},
            "virtual_dut_clocks": {
                "clk": {"nominal_period_ns": 40.0}
            },
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

    def test_slr_window_uses_complete_device_with_headroom(self):
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
                ("SLR0", "SLR1", "SLR2", "SLR3"),
            )

    def test_sparse_partition_still_uses_complete_device(self):
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
                    {"id": "c0", "kind": "slice", "assignments": []}
                ],
            }), encoding="utf-8")
            self.assertEqual(
                _select_xilinx_slr_window(packed, arch),
                ("SLR0", "SLR1", "SLR2", "SLR3"),
            )

    def test_atomic_slr_window_counts_slice_site_equivalents_not_atoms(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arch = root / "arch.json"
            packed = root / "atomic.json"
            sites = [
                {
                    "name": f"SLICE_X{x}Y{slr}", "type": "SLICEL",
                    "template": "SLICEL", "x": x, "y": slr,
                    "tile": {"grid_col": x, "grid_row": slr * 10},
                    "physical_region": {"slr": f"SLR{slr}"},
                }
                for slr in range(3) for x in range(16)
            ]
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
            clusters = [
                {
                    "id": f"lut-{index}", "kind": "slice",
                    "assignments": [{
                        "instance": f"lut-{index}", "cell_type": "LUT6",
                    }],
                }
                for index in range(64)
            ] + [
                {
                    "id": f"ff-{index}", "kind": "slice",
                    "assignments": [{
                        "instance": f"ff-{index}", "cell_type": "FDRE",
                    }],
                }
                for index in range(64)
            ]
            packed.write_text(__import__("json").dumps({
                "schema": "emuflow.openparf-atomic-source/v1",
                "clusters": clusters,
            }), encoding="utf-8")
            # Capacity admission still counts slice-site equivalents, while
            # the legal placement region remains the complete device.
            self.assertEqual(
                _select_xilinx_slr_window(packed, arch),
                ("SLR0", "SLR1", "SLR2"),
            )



if __name__ == "__main__":
    unittest.main()
