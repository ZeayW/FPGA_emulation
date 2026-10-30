import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.common.errors import ValidationError
from emuflow.common.json_io import file_sha256, write_json
from emuflow_tools.physical.openparf.xilinx_openparf_atomic import build_xilinx_openparf_atomic_source
from tests.openparf_placement_reference import (materialize_xilinx_openparf_atomic_source, read_xilinx_openparf_atomic_source)
from emuflow_tools.physical.xilinx_netlist import emit_xilinx_mapped_index, open_xilinx_mapped_index
from tests.openparf_source_fixture import write_xilinx_openparf_atomic_source_storage
from emuflow_tools.physical.openparf.xilinx_openparf_source_storage import OPENPARF_ATOMIC_SOURCE_STORAGE_SCHEMA
from tests.yosys_reference import import_yosys_json
from tests.openparf_runtime_fixture import write_openparf_runtime_fixture


def _write_mapped(path: Path, cells: int) -> dict:
    value = {
        "modules": {
            "top": {
                "attributes": {"top": "1"},
                "ports": {},
                "netnames": {},
                "cells": {
                    f"ff_{index:06d}": {
                        "type": "FDRE",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {},
                        "connections": {},
                    }
                    for index in range(cells)
                },
            },
        },
    }
    write_json(path, value, compact=True)
    return value


def _macro(cluster_id: str, instance: str = "ff_000000") -> dict:
    return {
        "id": cluster_id,
        "kind": "slice",
        "site_templates": ["SLICEL", "SLICEM"],
        "control_set": None,
        "assignments": [{
            "instance": instance,
            "cell_type": "FDRE",
            "bel_candidates": ["AFF", "AFF2"],
        }],
    }


class XilinxOpenparfSourceStorageTest(unittest.TestCase):
    def test_production_mapped_index_is_the_singleton_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped_json, _packed, _architecture = write_openparf_runtime_fixture(
                root, include_hard=True
            )
            mapped_ir = root / "partition.mapped.emuir.sqlite"
            mapped_index = root / "partition.mapped.sqlite"
            import_yosys_json(mapped_json).save(mapped_ir)
            report = emit_xilinx_mapped_index(mapped_ir, mapped_index)
            source_path = root / "openparf-atomic-source.json"
            with open_xilinx_mapped_index(mapped_index) as indexed:
                source = build_xilinx_openparf_atomic_source(
                    mapped_index,
                    source_path,
                    top=report["top"],
                    mapped_value=indexed,
                    mapped_sha256=report["output_sha256"],
                )
                first = list(source["clusters"])
                second = list(source["clusters"])
                assignments = list(source.iter_source_assignments())
            selector = json.loads(source_path.read_text(encoding="utf-8"))
            self.assertEqual(first, second)
            self.assertEqual(len(first), source["summary"]["physical_atoms"])
            self.assertEqual(len(assignments), source["summary"]["physical_atoms"])
            self.assertEqual(
                {assignment[0] for assignment in assignments},
                {
                    cluster_assignment["instance"]
                    for cluster in first
                    for cluster_assignment in cluster["assignments"]
                },
            )
            self.assertEqual(selector["mapped"]["artifact"], mapped_index.name)
            self.assertEqual(
                selector["mapped"]["sha256"], report["output_sha256"]
            )

    def test_singletons_remain_derived_and_compatibility_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped = root / "partition.mapped.json"
            _write_mapped(mapped, 2_000)
            source_path = root / "openparf-atomic-source.json"
            source = build_xilinx_openparf_atomic_source(
                mapped, source_path, top="top"
            )
            selector_bytes = source_path.read_bytes()
            selector = json.loads(selector_bytes)
            store_path = root / selector["artifact"]
            connection = sqlite3.connect(store_path)
            try:
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM clusters").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM assignments").fetchone()[0],
                    0,
                )
            finally:
                connection.close()
            self.assertEqual(
                selector["schema"], OPENPARF_ATOMIC_SOURCE_STORAGE_SCHEMA
            )
            self.assertNotIn("clusters", selector)
            self.assertEqual(len(source["clusters"]), 2_000)
            self.assertEqual(
                source["clusters"][0]["assignments"][0]["instance"],
                "ff_000000",
            )
            self.assertEqual(
                source["clusters"][-1]["assignments"][0]["instance"],
                "ff_001999",
            )

            compatibility = root / "atomic-source.compatibility.json"
            materialize_xilinx_openparf_atomic_source(source_path, compatibility)
            self.assertGreater(compatibility.stat().st_size, len(selector_bytes) * 100)
            first_artifact = selector["artifact"]
            first_store_sha = file_sha256(store_path)
            build_xilinx_openparf_atomic_source(mapped, source_path, top="top")
            repeated = json.loads(source_path.read_text(encoding="utf-8"))
            self.assertEqual(repeated["artifact"], first_artifact)
            self.assertEqual(file_sha256(root / first_artifact), first_store_sha)
            relocated = root / "relocated"
            relocated.mkdir()
            for artifact in (mapped.name, source_path.name, first_artifact):
                shutil.copy2(root / artifact, relocated / artifact)
            relocated_source = read_xilinx_openparf_atomic_source(
                relocated / source_path.name
            )
            self.assertEqual(len(relocated_source["clusters"]), 2_000)

    def test_store_and_mapped_seals_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped = root / "partition.mapped.json"
            _write_mapped(mapped, 8)
            source_path = root / "openparf-atomic-source.json"
            build_xilinx_openparf_atomic_source(mapped, source_path, top="top")
            selector = json.loads(source_path.read_text(encoding="utf-8"))
            store_path = root / selector["artifact"]
            store_bytes = store_path.read_bytes()
            store_path.write_bytes(store_bytes + b"tamper")
            with self.assertRaisesRegex(ValidationError, "store seal"):
                read_xilinx_openparf_atomic_source(source_path)
            store_path.write_bytes(store_bytes)
            mapped_bytes = mapped.read_bytes()
            mapped.write_bytes(mapped_bytes + b"\n")
            with self.assertRaisesRegex(ValidationError, "mapped size"):
                read_xilinx_openparf_atomic_source(source_path)
            mapped.write_bytes(mapped_bytes)
            self.assertEqual(
                read_xilinx_openparf_atomic_source(source_path)["summary"][
                    "physical_atoms"
                ],
                8,
            )

    def test_failed_replacement_preserves_previous_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped = root / "partition.mapped.json"
            mapped_value = _write_mapped(mapped, 4)
            source_path = root / "openparf-atomic-source.json"
            build_xilinx_openparf_atomic_source(mapped, source_path, top="top")
            selector_before = source_path.read_bytes()
            mapped_before = mapped.read_bytes()
            selector = json.loads(selector_before)
            store_path = root / selector["artifact"]
            store_before = store_path.read_bytes()
            mapped_value = _write_mapped(mapped, 6)
            with self.assertRaisesRegex(ValidationError, "macro cluster"):
                write_xilinx_openparf_atomic_source_storage(
                    source_path,
                    mapped_path=mapped,
                    mapped_value=mapped_value,
                    mapped_sha256=file_sha256(mapped),
                    top="top",
                    macro_clusters=[{"id": "broken"}],
                    cascades=[],
                    physical_macro_count=1,
                )
            self.assertEqual(source_path.read_bytes(), selector_before)
            self.assertEqual(store_path.read_bytes(), store_before)
            self.assertEqual(
                list(root.glob("openparf-atomic-source.*.sqlite")), [store_path]
            )
            mapped.write_bytes(mapped_before)
            self.assertEqual(
                len(read_xilinx_openparf_atomic_source(source_path)["clusters"]),
                4,
            )

    def test_changed_mapped_generation_replaces_previous_sidecar(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped = root / "partition.mapped.json"
            _write_mapped(mapped, 4)
            source_path = root / "openparf-atomic-source.json"
            build_xilinx_openparf_atomic_source(mapped, source_path, top="top")
            previous = json.loads(source_path.read_text(encoding="utf-8"))
            previous_store = root / previous["artifact"]

            _write_mapped(mapped, 6)
            replacement = build_xilinx_openparf_atomic_source(
                mapped, source_path, top="top"
            )
            current = json.loads(source_path.read_text(encoding="utf-8"))
            self.assertNotEqual(current["artifact"], previous["artifact"])
            self.assertFalse(previous_store.exists())
            self.assertTrue((root / current["artifact"]).is_file())
            self.assertEqual(len(replacement["clusters"]), 6)
            self.assertEqual(
                len(read_xilinx_openparf_atomic_source(source_path)["clusters"]),
                6,
            )

    def test_selector_commit_failure_preserves_previous_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped = root / "partition.mapped.json"
            _write_mapped(mapped, 4)
            source_path = root / "openparf-atomic-source.json"
            build_xilinx_openparf_atomic_source(mapped, source_path, top="top")
            selector_before = source_path.read_bytes()
            mapped_before = mapped.read_bytes()
            previous = json.loads(selector_before)
            previous_store = root / previous["artifact"]
            store_before = previous_store.read_bytes()

            _write_mapped(mapped, 6)
            with mock.patch.dict(os.environ, {"EMUFLOW_TEST_OPENPARF_SOURCE_PUBLICATION": "before-selector"}), self.assertRaisesRegex(ValidationError, "injected selector commit"):
                build_xilinx_openparf_atomic_source(
                    mapped, source_path, top="top"
                )

            self.assertEqual(source_path.read_bytes(), selector_before)
            self.assertEqual(previous_store.read_bytes(), store_before)
            self.assertEqual(
                list(root.glob("openparf-atomic-source.*.sqlite")),
                [previous_store],
            )
            mapped.write_bytes(mapped_before)
            self.assertEqual(
                len(read_xilinx_openparf_atomic_source(source_path)["clusters"]),
                4,
            )

    def test_cluster_identity_namespace_and_uniqueness_fail_closed(self):
        cases = (
            ([_macro("atomic-source-000001")], [], "macro cluster"),
            ([], [{"id": "atomic-source-000001"}], "cascade identity"),
            ([_macro("macro-a"), _macro("macro-a", "ff_000001")], [], "duplicated"),
            ([_macro("shared-id")], [{"id": "shared-id"}], "collides"),
        )
        for macros, cascades, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                mapped = root / "partition.mapped.json"
                mapped_value = _write_mapped(mapped, 4)
                with self.assertRaisesRegex(ValidationError, message):
                    write_xilinx_openparf_atomic_source_storage(
                        root / "openparf-atomic-source.json",
                        mapped_path=mapped,
                        mapped_value=mapped_value,
                        mapped_sha256=file_sha256(mapped),
                        top="top",
                        macro_clusters=macros,
                        cascades=cascades,
                        physical_macro_count=len(macros),
                    )

    def test_selector_envelopes_reject_unknown_keys_but_ignore_json_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped = root / "partition.mapped.json"
            _write_mapped(mapped, 4)
            source_path = root / "openparf-atomic-source.json"
            build_xilinx_openparf_atomic_source(mapped, source_path, top="top")
            selector = json.loads(source_path.read_text(encoding="utf-8"))

            for location in (
                (), ("header",), ("counts",), ("mapped",),
                ("header", "source"), ("header", "policy"),
                ("header", "summary"),
            ):
                malformed = json.loads(json.dumps(selector))
                target = malformed
                for component in location:
                    target = target[component]
                target["unexpected"] = True
                source_path.write_text(json.dumps(malformed), encoding="utf-8")
                with self.subTest(location=location), self.assertRaisesRegex(
                    ValidationError, "malformed|descriptor|contract|summary"
                ):
                    read_xilinx_openparf_atomic_source(source_path)

            reordered = dict(reversed(list(selector.items())))
            reordered["header"] = dict(
                reversed(list(reordered["header"].items()))
            )
            reordered["mapped"] = dict(
                reversed(list(reordered["mapped"].items()))
            )
            source_path.write_text(
                json.dumps(reordered, ensure_ascii=False, indent=4) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                len(read_xilinx_openparf_atomic_source(source_path)["clusters"]),
                4,
            )


if __name__ == "__main__":
    unittest.main()
