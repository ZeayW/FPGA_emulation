import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from emuflow.errors import ValidationError
from emuflow.openparf_continuous_driver import (
    build_convergence_certificate,
    guidance_stop_condition,
    skip_diagnostic_plot,
    skip_internal_site_legalization,
    write_continuous_placement,
)
from emuflow.xilinx_openparf import (
    export_xilinx_cluster_bookshelf,
    import_xilinx_openparf_guidance,
)


class XilinxOpenparfTest(unittest.TestCase):
    def test_cluster_export_and_guidance_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, arch = root / "mapped.json", root / "packed.json", root / "arch.json"
            out = root / "openparf"
            mapped.write_text(json.dumps({"modules": {"top": {
                "attributes": {"top": "1"}, "cells": {
                    "a": {"type": "LUT6", "port_directions": {"O": "output"}, "connections": {"O": [1]}},
                    "b": {"type": "LUT6", "port_directions": {"I": "input"}, "connections": {"I": [1]}},
                }}}}), encoding="utf-8")
            packed.write_text(json.dumps({
                "schema": "emuflow.packed-site-netlist/v1", "clusters": [
                    {"id": "ca", "kind": "slice", "assignments": [{"instance": "a", "cell_type": "LUT6"}]},
                    {"id": "cb", "kind": "slice", "assignments": [{"instance": "b", "cell_type": "LUT6"}]},
                ]}), encoding="utf-8")
            arch.write_text(json.dumps({
                "schema": "emuflow.archdb/v1", "part": "test",
                "source": {"format": "test/v1"}, "policy": {"name": "test"},
                "site_templates": {"SLICEL": {"bels": [
                    {"name": "A6LUT", "type": "LUT6", "z": 0, "compatible_cells": ["LUT6"]}
                ], "alternative_templates": []}},
                "sites": [
                    {"name": "SLICE_X0Y0", "type": "SLICEL", "template": "SLICEL", "x": 100, "y": 100},
                    {"name": "SLICE_X0Y1", "type": "SLICEL", "template": "SLICEL", "x": 300, "y": 500},
                ]}), encoding="utf-8")
            report = export_xilinx_cluster_bookshelf(mapped, packed, arch, out)
            placement = out / "result.pl"
            placement.write_text("c0 0.25 0.5 0\nc1 0.75 1.0 0\n", encoding="utf-8")
            guidance = out / "guidance.json"
            imported = import_xilinx_openparf_guidance(
                placement, out / "name_map.json", guidance
            )
            value = json.loads(guidance.read_text(encoding="utf-8"))
            config = json.loads(
                (out / "openparf.json").read_text(encoding="utf-8")
            )
            nets_text = (out / "design.nets").read_text(encoding="utf-8")
            sites_text = (out / "design.scl").read_text(encoding="utf-8")
        self.assertEqual(report["clusters"], 2)
        self.assertEqual(report["nets"], 1)
        self.assertEqual(imported["clusters"], 2)
        self.assertEqual(
            value["provider"], "openparf-global-guidance-v2-dense-grid"
        )
        self.assertEqual(
            [(item["x"], item["y"]) for item in value["clusters"]],
            [(150.0, 300.0), (250.0, 500.0)],
        )
        self.assertIn("net n0 2", nets_text)
        self.assertIn("SITEMAP 2 2", sites_text)
        self.assertIn("0 0 SLICEL", sites_text)
        self.assertIn("1 1 SLICEL", sites_text)
        self.assertEqual(config["global_place_flag"], 1)
        self.assertEqual(config["legalize_flag"], 0)
        self.assertEqual(config["detailed_place_flag"], 0)
        self.assertTrue(config["emuflow_continuous_global_guidance"])
        self.assertEqual(config["max_global_place_iters"], 1000)
        self.assertEqual(config["logic_area_type_names"], ["X_SLICE"])
        self.assertEqual(config["target_density"], 0.75)

    def test_cluster_export_can_limit_guidance_to_one_physical_slr(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped = root / "mapped.json"
            packed = root / "packed.json"
            arch = root / "arch.json"
            out = root / "openparf"
            mapped.write_text(json.dumps({"modules": {"top": {
                "attributes": {"top": "1"},
                "cells": {"a": {
                    "type": "LUT6", "port_directions": {"O": "output"},
                    "connections": {"O": [1]},
                }},
            }}}), encoding="utf-8")
            packed.write_text(json.dumps({
                "schema": "emuflow.packed-site-netlist/v1",
                "clusters": [{
                    "id": "ca", "kind": "slice",
                    "assignments": [{"instance": "a", "cell_type": "LUT6"}],
                }],
            }), encoding="utf-8")
            arch.write_text(json.dumps({
                "schema": "emuflow.archdb/v1", "part": "test",
                "source": {"format": "test/v1"},
                "policy": {"name": "test"},
                "site_templates": {"SLICEL": {
                    "bels": [{
                        "name": "A6LUT", "type": "LUT6", "z": 0,
                        "compatible_cells": ["LUT6"],
                    }],
                    "alternative_templates": [],
                }},
                "sites": [
                    {
                        "name": "SLICE_X0Y0", "type": "SLICEL",
                        "template": "SLICEL", "x": 10, "y": 20,
                        "physical_region": {"slr": "SLR0"},
                    },
                    {
                        "name": "SLICE_X0Y1", "type": "SLICEL",
                        "template": "SLICEL", "x": 30, "y": 40,
                        "physical_region": {"slr": "SLR1"},
                    },
                ],
            }), encoding="utf-8")
            report = export_xilinx_cluster_bookshelf(
                mapped, packed, arch, out, slr="SLR1"
            )
            sites_text = (out / "design.scl").read_text(encoding="utf-8")
            name_map = json.loads(
                (out / "name_map.json").read_text(encoding="utf-8")
            )
            with self.assertRaisesRegex(ValidationError, "no physical SLR"):
                export_xilinx_cluster_bookshelf(
                    mapped, packed, arch, root / "bad", slr="SLR9"
                )
        self.assertEqual(report["placement_region"], {"slr": "SLR1"})
        self.assertIn("SITEMAP 1 1", sites_text)
        self.assertEqual(
            name_map["coordinate_system"]["x_axis"], [30]
        )
        self.assertEqual(
            name_map["coordinate_system"]["y_axis"], [40]
        )

    def test_continuous_writer_does_not_require_discrete_sites(self):
        class Tensor:
            shape = (2, 2)

            def __getitem__(self, _key):
                return self

            def detach(self):
                return self

            def cpu(self):
                return self

            def tolist(self):
                return [[0.25, 1.5], [12.75, 8.125]]

        class PlaceDB:
            @staticmethod
            def instName(index):
                return f"c{index}"

        engine = SimpleNamespace(
            data_cls=SimpleNamespace(
                pos=[Tensor()], inst_locs_xyz=SimpleNamespace(shape=(2, 3))
            ),
            placedb=PlaceDB(),
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "global.pl"
            write_continuous_placement(engine, output)
            self.assertEqual(
                output.read_text(encoding="utf-8").splitlines()[1:],
                ["c0 0.25 1.5 0", "c1 12.75 8.125 0"],
            )

    def test_continuous_driver_disables_generic_site_legalization(self):
        engine = SimpleNamespace(
            params=SimpleNamespace(ssr_legalize_lock_iters=100),
            last_ssr_legalize_iter=0,
        )
        self.assertFalse(skip_internal_site_legalization(engine, object()))
        self.assertEqual(engine.last_ssr_legalize_iter, -101)
        self.assertIsNone(skip_diagnostic_plot(object(), filename="unused.bmp"))

    def test_convergence_certificate_fails_closed_on_overflow(self):
        class Tensor:
            def __init__(self, value):
                self.value = value

            def __getitem__(self, _key):
                return self

            def detach(self):
                return self

            def cpu(self):
                return self

            def tolist(self):
                return self.value

        engine = SimpleNamespace(
            data_cls=SimpleNamespace(
                pos=[Tensor([[0.0, 1.0], [2.0, 3.0]])],
                inst_locs_xyz=SimpleNamespace(shape=(2, 3)),
                area_type_inst_groups=[list(range(11)), list(range(4))],
            ),
            op_cls=SimpleNamespace(
                normalized_overflow_op=lambda _pos: Tensor([0.25, 0.9]),
                hpwl_op=lambda _pos: Tensor([4.0, 5.0]),
            ),
            placedb=SimpleNamespace(getAreaTypeIndexFromName=lambda _name: 0),
            params=SimpleNamespace(
                io_at_names=[], logic_area_type_names=["X_SLICE"],
                stop_overflow=0.1,
            ),
            cur_metric_record=SimpleNamespace(
                opt_iter=SimpleNamespace(iteration=123)
            ),
        )
        certificate = build_convergence_certificate(engine)
        self.assertEqual(certificate["status"], "fail")
        self.assertEqual(certificate["checked_area_types"], [0])
        self.assertEqual(certificate["maximum_checked_overflow"], 0.25)
        self.assertEqual(certificate["overflow_limits"], [0.1])
        self.assertEqual(certificate["maximum_limit_ratio"], 2.5)
        self.assertEqual(certificate["iterations"], 123)

    def test_hard_resource_guidance_uses_openparf_two_x_limit(self):
        class Tensor:
            def __init__(self, value):
                self.value = value

            def __getitem__(self, _key):
                return self

            def detach(self):
                return self

            def cpu(self):
                return self

            def tolist(self):
                return self.value

        names = {"X_DSP": 0, "X_BRAM": 1, "X_SLICE": 2}
        overflow = Tensor([0.35, 0.3, 0.19])
        engine = SimpleNamespace(
            data_cls=SimpleNamespace(
                pos=[Tensor([[0.0, 0.0]])],
                inst_locs_xyz=SimpleNamespace(shape=(1, 3)),
                area_type_inst_groups=[list(range(11)) for _ in range(3)],
            ),
            op_cls=SimpleNamespace(
                normalized_overflow_op=lambda _pos: overflow,
                hpwl_op=lambda _pos: Tensor([1.0, 2.0]),
            ),
            placedb=SimpleNamespace(
                getAreaTypeIndexFromName=lambda name: names[name]
            ),
            params=SimpleNamespace(
                io_at_names=[], logic_area_type_names=["X_SLICE"],
                stop_overflow=0.2, max_global_place_iters=1000,
            ),
            cur_metric_record=SimpleNamespace(
                opt_iter=SimpleNamespace(iteration=600)
            ),
        )
        certificate = build_convergence_certificate(engine)
        self.assertEqual(certificate["status"], "pass")
        self.assertEqual(certificate["overflow_limits"], [0.4, 0.4, 0.2])
        metric = SimpleNamespace(
            opt_iter=SimpleNamespace(iteration=600), overflow=overflow
        )
        self.assertTrue(guidance_stop_condition(engine, [metric]))
