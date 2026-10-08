import json
import tempfile
import unittest
from pathlib import Path

from emuflow.benchmark import BenchmarkRun
from emuflow.ppro_blackbox_application import benchmark_rtl_identity
from scripts.benchmarks.nvdla_release_inventory import (
    collect_nvdla_source_files,
)
from scripts.benchmarks.nvdla_ram_stubs import (
    PHYSICAL_MEMORY_POLICY,
    generate,
)
from scripts.benchmarks.prepare_nvdla_holdout import prepare_nvdla_holdout


class NvdlaRamStubTest(unittest.TestCase):
    def _prepared_fixture(self, root: Path) -> tuple[Path, Path, Path]:
        source = root / "nvdla"
        rtl = source / "vmod" / "nvdla" / "top"
        vlibs = source / "vmod" / "vlibs"
        include = source / "vmod" / "include"
        rams = source / "vmod" / "rams" / "synth"
        for path in (rtl, vlibs, include, rams):
            path.mkdir(parents=True, exist_ok=True)
        catalog = root / "catalog.json"
        catalog.write_text(
            json.dumps(
                {
                    "schema": "emuflow.rtl-catalog/v1",
                    "designs": [
                        {
                            "id": "nvdla",
                            "revision": "revision-1",
                            "archive_sha256": "a" * 64,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (source / ".emuflow-source.json").write_text(
            json.dumps(
                {
                    "schema": "emuflow.source-archive/v1",
                    "design_id": "nvdla",
                    "revision": "revision-1",
                    "archive_sha256": "a" * 64,
                }
            ),
            encoding="utf-8",
        )
        for index in range(249):
            (rtl / f"rtl_{index}.v").write_text(
                f"module rtl_{index}; endmodule\n", encoding="utf-8"
            )
        (rtl / "NV_nvdla.v").write_text(
            "module NV_nvdla(input dla_core_clk, input dla_csb_clk); endmodule\n",
            encoding="utf-8",
        )
        (rtl / "NV_NVDLA_partition_o.v").write_text(
            "#ifdef NVDLA_CDP_ENABLE\nmodule partition_o; endmodule\n#endif\n",
            encoding="utf-8",
        )
        (vlibs / "NV_DW_lsd.v").write_text(
            "module NV_DW_lsd; endmodule\n", encoding="utf-8"
        )
        (vlibs / "cell.v").write_text("module cell; endmodule\n", encoding="utf-8")
        (include / "config.vh").write_text("`define CONFIG 1\n", encoding="utf-8")
        (rams / "nv_ram_rws_32x64.v").write_text(
            """
module nv_ram_rws_32x64 (clk, ra, re, dout, wa, we, di, pwrbus_ram_pd);
parameter FORCE_CONTENTION_ASSERTION_RESET_ACTIVE=1'b0;
input clk;
input [4:0] ra;
input re;
output [63:0] dout;
input [4:0] wa;
input we;
input [63:0] di;
input [31:0] pwrbus_ram_pd;
endmodule
""",
            encoding="utf-8",
        )
        compat = root / "compat.v"
        compat.write_text("module NV_DW_lsd; endmodule\n", encoding="utf-8")
        return source, catalog, compat

    def test_release_inventory_covers_frontend_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            source = repo / "third_party" / "rtl" / "nvdla"
            (source / "vmod" / "nvdla" / "top").mkdir(parents=True)
            (source / "vmod" / "vlibs").mkdir(parents=True)
            (source / "vmod" / "include").mkdir(parents=True)
            (source / "vmod" / "rams" / "synth").mkdir(parents=True)
            (source / ".emuflow-source.json").write_text(
                "{}\n", encoding="utf-8"
            )
            for index in range(250):
                (source / "vmod" / "nvdla" / "top" / f"rtl_{index}.v").write_text(
                    "module rtl; endmodule\n", encoding="utf-8"
                )
            expected = [
                source / "vmod" / "vlibs" / "cell.v",
                source / "vmod" / "include" / "config.vh",
                source / "vmod" / "rams" / "synth" / "nv_ram_demo.v",
            ]
            for path in expected:
                path.write_text("// dependency\n", encoding="utf-8")
            inventory = collect_nvdla_source_files(repo)
            self.assertEqual(len(inventory), 254)
            self.assertTrue(
                all(path.resolve() in inventory for path in expected)
            )

    def test_parameter_and_port_order_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "nv_ram_demo.v").write_text(
                """
module nv_ram_demo (
  clk,
  addr,
  dout
);
parameter FORCE_CONTENTION_ASSERTION_RESET_ACTIVE=1'b0;
input clk;
input [7:0] addr;
output [31:0] dout;
endmodule
""",
                encoding="utf-8",
            )
            output = root / "stubs.v"
            self.assertEqual(generate(root, output), (1, 0))
            text = output.read_text(encoding="utf-8")
            self.assertIn('(* black_box = "yes" *)', text)
            self.assertIn(
                "module nv_ram_demo #(parameter "
                "FORCE_CONTENTION_ASSERTION_RESET_ACTIVE=1'b0)",
                text,
            )
            self.assertLess(text.index("input clk"), text.index("input [7:0] addr"))
            self.assertLess(
                text.index("input [7:0] addr"), text.index("output [31:0] dout")
            )

    def test_logic_companion_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "nv_ram_demo_logic.v").write_text(
                "module nv_ram_demo_logic; endmodule\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "no NVDLA SRAM wrappers"):
                generate(root, root / "stubs.v")

    def test_physical_policy_models_all_supported_wrapper_families(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wrappers = {
                "nv_ram_rws_32x64": """
module nv_ram_rws_32x64 (clk, ra, re, dout, wa, we, di, pwrbus_ram_pd);
parameter FORCE_CONTENTION_ASSERTION_RESET_ACTIVE=1'b0;
input clk;
input [4:0] ra;
input re;
output [63:0] dout;
input [4:0] wa;
input we;
input [63:0] di;
input [31:0] pwrbus_ram_pd;
endmodule
""",
                "nv_ram_rwsp_32x64": """
module nv_ram_rwsp_32x64 (clk, ra, re, ore, dout, wa, we, di, pwrbus_ram_pd);
parameter FORCE_CONTENTION_ASSERTION_RESET_ACTIVE=1'b0;
input clk;
input [4:0] ra;
input re;
input ore;
output [63:0] dout;
input [4:0] wa;
input we;
input [63:0] di;
input [31:0] pwrbus_ram_pd;
endmodule
""",
                "nv_ram_rwst_32x64": """
module nv_ram_rwst_32x64 (clk, ra, re, dout, wa, we, di, pwrbus_ram_pd);
parameter FORCE_CONTENTION_ASSERTION_RESET_ACTIVE=1'b0;
input clk;
input [4:0] ra;
input re;
output [63:0] dout;
input [4:0] wa;
input we;
input [63:0] di;
input [31:0] pwrbus_ram_pd;
endmodule
""",
                "nv_ram_rwsthp_32x64": """
module nv_ram_rwsthp_32x64 (clk, ra, re, ore, dout, wa, we, di, byp_sel, dbyp, pwrbus_ram_pd);
parameter FORCE_CONTENTION_ASSERTION_RESET_ACTIVE=1'b0;
input clk;
input [4:0] ra;
input re;
input ore;
output [63:0] dout;
input [4:0] wa;
input we;
input [63:0] di;
input byp_sel;
input [63:0] dbyp;
input [31:0] pwrbus_ram_pd;
endmodule
""",
            }
            for name, body in wrappers.items():
                (root / f"{name}.v").write_text(body, encoding="utf-8")
            output = root / "models.v"
            self.assertEqual(
                generate(root, output, PHYSICAL_MEMORY_POLICY),
                (4, 4),
            )
            text = output.read_text(encoding="utf-8")
            self.assertNotIn('black_box = "yes"', text)
            self.assertEqual(text.count('ram_style = "block"'), 4)
            self.assertIn("reg [63:0] mem [0:31];", text)
            self.assertIn("always @(posedge clk)", text)
            self.assertIn("read_data <= mem[ra];", text)
            self.assertIn("read_data <= (we && (wa == ra)) ? di : mem[ra];", text)
            self.assertIn("output_data <= read_data;", text)
            self.assertIn("output_data <= byp_sel ? dbyp : read_data;", text)

    def test_physical_policy_rejects_unknown_memory_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "nv_ram_unknown_32x64.v").write_text(
                "module nv_ram_unknown_32x64 (clk);\n"
                "parameter FORCE_CONTENTION_ASSERTION_RESET_ACTIVE=1'b0;\n"
                "input clk;\nendmodule\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError, "unsupported NVDLA SRAM wrapper name"
            ):
                generate(root, root / "models.v", PHYSICAL_MEMORY_POLICY)

    def test_shared_holdout_preparation_seals_one_frontend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, catalog, compat = self._prepared_fixture(root)
            generated = source / ".prepared"
            benchmark_path = root / "nvdla.json"
            report = prepare_nvdla_holdout(
                source_root=source,
                generated_dir=generated,
                benchmark_path=benchmark_path,
                platform="platforms/calibrated/nominal/boarddb.json",
                catalog_path=catalog,
                compat_path=compat,
            )
            self.assertEqual(report["status"], "pass")
            spec = BenchmarkRun.load(benchmark_path)
            self.assertEqual(
                spec.value["calibration_holdout_class"], "nvdla"
            )
            self.assertEqual(
                spec.value["physical_mapping_profile"],
                "xilinx-ultrascaleplus-open-v1",
            )
            self.assertEqual(
                {
                    group["clock"]
                    for group in spec.value["timing_io"]["input_groups"]
                },
                {"dla_core_clk", "dla_csb_clk"},
            )
            self.assertEqual(
                {
                    group["clock"]
                    for group in spec.value["timing_io"]["output_groups"]
                },
                {"dla_core_clk", "dla_csb_clk"},
            )
            constrained_ports = {
                port
                for direction in ("input_groups", "output_groups")
                for group in spec.value["timing_io"][direction]
                for port in group["ports"]
            }
            self.assertNotIn("dla_reset_rstn", constrained_ports)
            self.assertNotIn("test_mode", constrained_ports)
            self.assertIn("nvdla_core2dbb_r_rdata", constrained_ports)
            self.assertIn("nvdla2csb_data", constrained_ports)
            sources = spec.resolve_sources(source)
            self.assertNotIn(
                (source / "vmod" / "vlibs" / "NV_DW_lsd.v").resolve(), sources
            )
            self.assertNotIn(
                (
                    source
                    / "vmod"
                    / "nvdla"
                    / "top"
                    / "NV_NVDLA_partition_o.v"
                ).resolve(),
                sources,
            )
            self.assertIn((generated / "nvdla_compat.v").resolve(), sources)
            normalized = (generated / "NV_NVDLA_partition_o.v").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("#ifdef", normalized)
            self.assertIn("`ifdef", normalized)
            identity = benchmark_rtl_identity(benchmark_path, source)
            self.assertEqual(
                identity["defines"], report["benchmark"]["synthesis"]["defines"]
            )
            self.assertGreater(len(identity["include_file_records"]), 0)

    def test_shared_holdout_can_require_complete_physical_memory_models(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, catalog, compat = self._prepared_fixture(root)
            report = prepare_nvdla_holdout(
                source_root=source,
                generated_dir=source / ".prepared",
                benchmark_path=root / "nvdla.json",
                platform="platforms/calibrated/nominal/boarddb.json",
                memory_policy=PHYSICAL_MEMORY_POLICY,
                catalog_path=catalog,
                compat_path=compat,
            )
            self.assertEqual(report["memory_policy"], PHYSICAL_MEMORY_POLICY)
            self.assertTrue(report["benchmark"]["id"].endswith("_physical"))
            self.assertEqual(
                report["benchmark"]["calibration_holdout_class"], "nvdla"
            )
            prepared = json.loads(
                (source / ".prepared" / "preparation-manifest.json").read_text()
            )
            self.assertEqual(prepared["ram_wrapper_count"], 1)
            self.assertEqual(prepared["ram_modeled_count"], 1)
            wrappers = (source / ".prepared" / "nvdla_ram_wrappers.v").read_text()
            self.assertNotIn('black_box = "yes"', wrappers)
            self.assertIn('ram_style = "block"', wrappers)

    def test_shared_holdout_rejects_unpinned_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, catalog, compat = self._prepared_fixture(root)
            stamp = json.loads(
                (source / ".emuflow-source.json").read_text(encoding="utf-8")
            )
            stamp["revision"] = "wrong"
            (source / ".emuflow-source.json").write_text(
                json.dumps(stamp), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "pinned RTL catalog"):
                prepare_nvdla_holdout(
                    source_root=source,
                    generated_dir=source / ".prepared",
                    benchmark_path=root / "nvdla.json",
                    platform="platform.json",
                    catalog_path=catalog,
                    compat_path=compat,
                )


if __name__ == "__main__":
    unittest.main()
