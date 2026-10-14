import hashlib
import json
import re
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.benchmarks.fetch import fetch, fetch_archive


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "benchmarks/rtl_catalog.json"


class RtlCatalogTest(unittest.TestCase):
    def test_root_install_contains_rtl_benchmark_runtime_data(self) -> None:
        cmake = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
        for runtime_path in (
            "scripts/benchmarks",
            "benchmarks/runs",
            "benchmarks/rtl_catalog.json",
        ):
            self.assertIn(runtime_path, cmake)
        self.assertIn("EMUFLOW_RTL_BENCHMARK_DATA", cmake)
        self.assertNotIn(
            '"${CMAKE_COMMAND}" -E copy_directory\n'
            '    "${CMAKE_SOURCE_DIR}/scripts/benchmarks"',
            cmake,
        )
        self.assertNotIn(
            '"${CMAKE_COMMAND}" -E copy_directory\n'
            '    "${CMAKE_SOURCE_DIR}/src/emuflow"',
            cmake,
        )
        self.assertIn("${EMUFLOW_PYTHON_SOURCES}", cmake)
        self.assertTrue(list((ROOT / "src/emuflow").glob("*.py")))
        self.assertEqual(
            list((ROOT / "src/emuflow").glob("**/*.py")),
            list((ROOT / "src/emuflow").glob("*.py")),
            "the root installer copies a flat, explicit Python source list",
        )
        for runtime_source in (
            ROOT / "scripts/benchmarks/fetch.py",
            ROOT / "benchmarks/rtl_catalog.json",
            *(ROOT / "benchmarks/runs").glob("*.json"),
        ):
            self.assertTrue(runtime_source.is_file(), runtime_source)

    def test_catalog_entries_are_pinned_and_unique(self) -> None:
        catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
        self.assertEqual(catalog["schema"], "emuflow.rtl-catalog/v1")
        identifiers = set()
        priorities = set()
        tiers = set()
        for design in catalog["designs"]:
            self.assertNotIn(design["id"], identifiers)
            self.assertNotIn(design["priority"], priorities)
            identifiers.add(design["id"])
            priorities.add(design["priority"])
            self.assertRegex(design["revision"], re.compile(r"^[0-9a-f]{40}$"))
            self.assertTrue(design["repository"].startswith("https://github.com/"))
            self.assertTrue(design["tops"])
            self.assertTrue(design["sparse_paths"])
            self.assertRegex(design["validation_tier"], re.compile(r"^L[1-7](-L[1-7])?$"))
            self.assertTrue(design["feature_tags"])
            if "archive_url" in design or "archive_sha256" in design:
                self.assertTrue(design["archive_url"].startswith("https://"))
                self.assertRegex(
                    design["archive_sha256"], re.compile(r"^[0-9a-f]{64}$")
                )
            tiers.add(design["validation_tier"])
        self.assertIn("L1", tiers)
        self.assertIn("L7", tiers)

    def test_pinned_archive_fetch_is_hash_checked_and_stamped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source" / "upstream-revision"
            source_root.mkdir(parents=True)
            (source_root / "rtl.v").write_text(
                "module rtl; endmodule\n", encoding="utf-8"
            )
            archive_path = root / "source.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                archive.add(source_root, arcname=source_root.name)
            digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            design = {
                "id": "archive-fixture",
                "revision": "a" * 40,
                "archive_url": archive_path.as_uri(),
                "archive_sha256": digest,
            }

            destination = fetch_archive(design, root / "destination")
            self.assertEqual(
                (destination / "rtl.v").read_text(encoding="utf-8"),
                "module rtl; endmodule\n",
            )
            stamp = json.loads(
                (destination / ".emuflow-source.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(stamp["revision"], "a" * 40)
            self.assertEqual(stamp["archive_sha256"], digest)

    def test_large_generated_collection_has_portable_source_identities(self) -> None:
        catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
        collections = {item["id"]: item for item in catalog["collections"]}
        collection = collections["large-rtl-20260906"]
        self.assertFalse(collection["redistributed"])
        self.assertEqual(collection["source_form"], "external-generated-rtl-package")
        self.assertRegex(collection["archive_sha256"], r"^[0-9a-f]{64}$")
        entries = {
            item["id"]: item
            for item in catalog["designs"]
            if item.get("package", {}).get("collection") == collection["id"]
        }
        self.assertEqual(set(entries), {
            "opdb_chip_10x10", "opdb_chip_15x15", "opdb_chip_20x20",
            "occamy_432core_full", "vortex_64core", "blackparrot_16core",
            "chipyard_sonicboom_megaboom",
            "xiangshan_kunminghu_release_25022801",
        })
        for identifier, design in entries.items():
            package = design["package"]
            self.assertEqual(package["directory"], identifier)
            for field in ("filelist_sha256", "primary_rtl_sha256"):
                self.assertRegex(package[field], r"^[0-9a-f]{64}$")
            for field in ("directory", "filelist", "primary_rtl"):
                self.assertFalse(Path(package[field]).is_absolute())
                self.assertNotIn("..", Path(package[field]).parts)
            self.assertEqual(design["flow_readiness"], "source-candidate-unqualified")
            self.assertEqual(design["qualification"], {
                "native_xilinx_mapping": "not_qualified",
                "full_phase1_7": "not_qualified",
            })
            self.assertEqual(
                design["clock_profile"]["elaborated_clock_audit"], "pending"
            )
            self.assertNotIn("clock_periods_ns", design)
            self.assertNotIn("lut", package)

    def test_large_candidates_do_not_confuse_one_port_with_one_clock(self) -> None:
        catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
        entries = {item["id"]: item for item in catalog["designs"]}
        vortex = entries["vortex_64core"]
        blackparrot = entries["blackparrot_16core"]
        self.assertEqual(vortex["clock_profile"]["classification"], "single-root")
        self.assertEqual(vortex["clock_profile"]["root_ports"], ["clk"])
        self.assertEqual(vortex["package"]["core_count"], 64)
        self.assertEqual(
            blackparrot["clock_profile"]["classification"],
            "single-root-with-generated-clocks",
        )
        self.assertNotIn("single-clock", blackparrot["feature_tags"])
        for size, tiles in (("10x10", 100), ("15x15", 225), ("20x20", 400)):
            opdb = entries[f"opdb_chip_{size}"]
            self.assertEqual(opdb["package"]["tile_count"], tiles)
            self.assertEqual(
                opdb["clock_profile"]["root_ports"],
                ["core_ref_clk", "io_clk", "jtag_clk"],
            )
        release = entries["xiangshan_kunminghu_release_25022801"]
        self.assertIn("OpenXiangShan/XiangShan", release["repository"])
        self.assertIn(
            "XS-MLVP/UnityChipXiangShanRTLs",
            release["package"]["release_repository"],
        )

    def test_git_fetch_uses_legacy_compatible_sparse_checkout(self) -> None:
        design = {
            "id": "legacy-git-fixture",
            "revision": "a" * 40,
            "repository": "https://github.com/example/fixture.git",
            "sparse_paths": ["/LICENSE", "/rtl"],
        }
        commands = []

        def fake_run(command, **kwargs):
            commands.append((list(command), kwargs.get("cwd")))
            if command[:2] == ["git", "init"]:
                (Path(command[2]) / ".git" / "info").mkdir(parents=True)
            return None

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("scripts.benchmarks.fetch.subprocess.run", fake_run):
                destination = fetch(design, root)
            self.assertEqual(destination, root / design["id"])
            self.assertEqual(
                (destination / ".git" / "info" / "sparse-checkout").read_text(
                    encoding="utf-8"
                ),
                "/LICENSE\n/rtl\n",
            )
        flattened = [argument for command, _cwd in commands for argument in command]
        self.assertNotIn("-C", flattened)
        self.assertNotIn("sparse-checkout", flattened)
        self.assertTrue(
            all(cwd == destination for command, cwd in commands[1:])
        )


if __name__ == "__main__":
    unittest.main()
