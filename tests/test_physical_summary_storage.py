import hashlib
import gc
import json
import sqlite3
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.common.errors import ValidationError
from emuflow_tools.document_io import read_json
from emuflow.common.json_io import write_json
import emuflow_tools.physical.physical_summary_storage as physical_summary_storage
from emuflow_tools.physical.physical_summary_storage import PHYSICAL_EVIDENCE_REFERENCE_SCHEMA, PHYSICAL_INDEXED_EVIDENCE_REFERENCE_SCHEMA, canonical_physical_summary_sha256, physical_evidence_reference, publish_logic_segment_timing_store, read_physical_summary
from emuflow_tools.timing.logic_segment_timing import validate_logic_segment_timing


class PhysicalSummaryStorageTest(unittest.TestCase):
    @staticmethod
    def _logic_timing(fpga: str, count: int, *, platform="board"):
        segments = [
            {
                "id": f"{fpga}-segment-{index:08d}",
                "kind": "capture",
                "system_path": f"system-{index:08d}",
                "member_path": f"member-{index:08d}",
                "cut_index": 0,
                "fpga": fpga,
                "replace_tx_endpoint": None,
                "start_pin": f"start-{index}",
                "end_pin": f"end-{index}",
                "delay_ns": float(index % 17) / 10.0,
            }
            for index in range(count)
        ]
        return {
            "schema": "emuflow.logic-segment-timing/v1",
            "status": "pass",
            "design": "dut",
            "platform": platform,
            "fpga": fpga,
            "provider": "test",
            "qualification": "endpoint-chain",
            "system_routes_sha256": "a" * 64,
            "tdm_schedule_sha256": "b" * 64,
            "coverage": {
                "segments": count,
                "system_paths": count,
                "member_paths": count,
                "unsupported_member_paths": 0,
            },
            "unsupported_member_paths": [],
            "segments": segments,
        }


    def test_evidence_is_resolved_lazily_and_sealed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "fpga0/boundary.json"
            write_json(
                evidence,
                {
                    "schema": "emuflow.boundary-identity/v1",
                    "status": "pass",
                    "endpoints": [{"id": "endpoint0"}],
                },
            )
            summary_path = root / "physical-summary.json"
            reference = physical_evidence_reference(
                evidence,
                "emuflow.boundary-identity/v1",
                relative_to=root,
            )
            write_json(
                summary_path,
                {
                    "schema": "emuflow.physical-summary/v1",
                    "boundary_identities": {"fpga0": reference},
                },
            )

            raw = read_json(summary_path)
            self.assertEqual(
                raw["boundary_identities"]["fpga0"]["schema"],
                PHYSICAL_EVIDENCE_REFERENCE_SCHEMA,
            )
            self.assertNotIn(
                "endpoints", raw["boundary_identities"]["fpga0"]
            )
            lazy = read_physical_summary(summary_path)
            self.assertEqual(
                lazy["boundary_identities"]["fpga0"]["endpoints"],
                [{"id": "endpoint0"}],
            )

            evidence.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "byte count|digest"):
                lazy["boundary_identities"]["fpga0"]

    def test_one_fpga_is_resolved_without_reading_other_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            references = {}
            inline = {}
            for fpga in ("fpga0", "fpga1"):
                document = {
                    "schema": "emuflow.boundary-identity/v1",
                    "fpga": fpga,
                    "endpoints": [{"id": f"{fpga}-endpoint"}],
                }
                evidence = root / fpga / "boundary.json"
                write_json(evidence, document)
                references[fpga] = physical_evidence_reference(
                    evidence,
                    document["schema"],
                    relative_to=root,
                )
                inline[fpga] = document
            summary_path = root / "physical-summary.json"
            bounded = {
                "schema": "emuflow.physical-summary/v1",
                "boundary_identities": references,
            }
            write_json(summary_path, bounded)
            lazy = read_physical_summary(summary_path)
            (root / "fpga1/boundary.json").write_text(
                "{}\n", encoding="utf-8"
            )

            self.assertEqual(
                lazy["boundary_identities"]["fpga0"]["fpga"], "fpga0"
            )
            with self.assertRaisesRegex(ValidationError, "byte count|digest"):
                lazy["boundary_identities"]["fpga1"]

            (root / "fpga1/boundary.json").unlink()
            write_json(
                root / "fpga1/boundary.json",
                inline["fpga1"],
            )
            expected = {
                "schema": "emuflow.physical-summary/v1",
                "boundary_identities": inline,
            }
            expected_digest = hashlib.sha256(
                json.dumps(
                    expected,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            self.assertEqual(
                canonical_physical_summary_sha256(lazy), expected_digest
            )

    def test_unsafe_reference_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(
                root / "physical-summary.json",
                {
                    "boundary_identities": {
                        "fpga0": {
                            "schema": PHYSICAL_EVIDENCE_REFERENCE_SCHEMA,
                            "logical_schema": "emuflow.boundary-identity/v1",
                            "path": "../outside.json",
                            "bytes": 1,
                            "sha256": "0" * 64,
                        }
                    }
                },
            )
            with self.assertRaisesRegex(ValidationError, "unsafe"):
                read_physical_summary(root / "physical-summary.json")

    def test_unknown_reference_schema_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(
                root / "physical-summary.json",
                {
                    "boundary_identities": {
                        "fpga0": {
                            "schema": "emuflow.physical-evidence-reference/v3",
                            "logical_schema": "emuflow.boundary-identity/v1",
                            "path": "fpga0/boundary.json",
                            "bytes": 1,
                            "sha256": "0" * 64,
                        }
                    }
                },
            )
            with self.assertRaisesRegex(ValidationError, "unsupported"):
                read_physical_summary(root / "physical-summary.json")

    def test_mixed_inline_and_referenced_evidence_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "identity.json"
            write_json(
                evidence,
                {"schema": "emuflow.boundary-identity/v1"},
            )
            write_json(
                root / "physical-summary.json",
                {
                    "boundary_identities": {
                        "fpga0": physical_evidence_reference(
                            evidence,
                            "emuflow.boundary-identity/v1",
                            relative_to=root,
                        ),
                        "fpga1": {
                            "schema": "emuflow.boundary-identity/v1"
                        },
                    }
                },
            )
            with self.assertRaisesRegex(ValidationError, "mixes"):
                read_physical_summary(root / "physical-summary.json")

    @staticmethod
    def _write_logic_store(root, fpga, count, *, platform="board", semantic=None):
        from emuflow_tools.timing.logic_segment_timing_storage import logic_segment_timing_reference
        from tests.timing_facts import logic_timing_facts

        document = PhysicalSummaryStorageTest._logic_timing(
            fpga, count, platform=platform
        )
        if semantic is not None:
            document["semantic_contract_sha256"] = semantic
            for segment in document["segments"]:
                segment["static_exact_segment_id"] = f"exact:{segment['id']}"
        selector = root / fpga / "logic-segment-timing.json"
        header = {
            key: value for key, value in document.items()
            if key not in {"segments", "unsupported_member_paths", "unmeasured_segments"}
        }
        view = logic_timing_facts(
            selector,
            header=header,
            segments=document["segments"],
            unsupported_member_paths=document["unsupported_member_paths"],
        )
        view.close()
        return document, selector, logic_segment_timing_reference(
            selector, relative_to=root
        )

    def test_logic_segment_store_is_exact_indexed_and_sealed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            semantic = "1" * 64
            document, selector, reference = self._write_logic_store(
                root, "fpga0", 7, semantic=semantic
            )
            summary_path = root / "physical-summary.json"
            write_json(summary_path, {
                "schema": "emuflow.physical-summary/v1",
                "design": "dut",
                "platform": "board",
                "logic_segment_system_routes_sha256": "a" * 64,
                "logic_segment_tdm_schedule_sha256": "b" * 64,
                "logic_segment_semantic_contract_sha256": semantic,
                "logic_segment_timing": {"fpga0": reference},
            })
            with read_physical_summary(summary_path) as summary:
                database = summary["logic_segment_timing"]["fpga0"]
                self.assertEqual(len(database["segments"]), 7)
                self.assertEqual(validate_logic_segment_timing(database)["segments"], 7)
                self.assertEqual(list(database['segments']), [dict(v, measurement='endpoint-exact') for v in document['segments']])
            generation = selector.parent / read_json(selector)["artifact"]
            generation.write_bytes(generation.read_bytes() + b"x")
            with self.assertRaisesRegex(ValidationError, "seal"):
                read_physical_summary(summary_path)["logic_segment_timing"]["fpga0"]

    def test_logic_segment_store_requires_explicit_legacy_and_summary_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy_path = root / "legacy.json"
            write_json(legacy_path, self._logic_timing("fpga0", 1))
            legacy = physical_evidence_reference(
                legacy_path, "emuflow.logic-segment-timing/v1", relative_to=root
            )
            write_json(root / "legacy-summary.json", {
                "logic_segment_timing": {"fpga0": legacy}
            })
            with self.assertRaisesRegex(ValidationError, "current indexed evidence"):
                read_physical_summary(root / "legacy-summary.json")
            semantic = "2" * 64
            _, _, reference = self._write_logic_store(
                root, "fpga0", 1, semantic=semantic
            )
            write_json(root / "unbound.json", {
                "schema": "emuflow.physical-summary/v1",
                "design": "dut", "platform": "board",
                "logic_segment_system_routes_sha256": "a" * 64,
                "logic_segment_tdm_schedule_sha256": "b" * 64,
                "logic_segment_timing": {"fpga0": reference},
            })
            with self.assertRaisesRegex(ValidationError, "authoritative semantic"):
                read_physical_summary(root / "unbound.json")["logic_segment_timing"]["fpga0"]
            write_json(root / "wrong-design.json", {
                "schema": "emuflow.physical-summary/v1",
                "design": "other", "platform": "board",
                "logic_segment_system_routes_sha256": "a" * 64,
                "logic_segment_tdm_schedule_sha256": "b" * 64,
                "logic_segment_semantic_contract_sha256": semantic,
                "logic_segment_timing": {"fpga0": reference},
            })
            with self.assertRaisesRegex(ValidationError, "design source seal"):
                read_physical_summary(root / "wrong-design.json")["logic_segment_timing"]["fpga0"]
            write_json(root / "wrong-routes.json", {
                "schema": "emuflow.physical-summary/v1",
                "design": "dut", "platform": "board",
                "logic_segment_system_routes_sha256": "c" * 64,
                "logic_segment_tdm_schedule_sha256": "b" * 64,
                "logic_segment_semantic_contract_sha256": semantic,
                "logic_segment_timing": {"fpga0": reference},
            })
            with self.assertRaisesRegex(
                ValidationError, "system_routes_sha256 source seal"
            ):
                read_physical_summary(
                    root / "wrong-routes.json"
                )["logic_segment_timing"]["fpga0"]

    def test_logic_segment_store_rejects_manifest_envelope_and_truncation_attacks(self) -> None:
        from emuflow_tools.timing.logic_segment_timing_storage import LOGIC_SEGMENT_TIMING_APPLICATION_ID, read_logic_segment_timing_storage

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, selector_path, _ = self._write_logic_store(root, "fpga0", 2)
            selector = read_json(selector_path)
            selector["counts"]["segments"] = "+1"
            write_json(selector_path, selector, compact=True)
            with self.assertRaisesRegex(ValidationError, "metadata"):
                read_logic_segment_timing_storage(selector_path)

            _, selector_path, _ = self._write_logic_store(root, "fpga0", 2)
            selector = read_json(selector_path)
            generation = selector_path.parent / selector["artifact"]
            connection = sqlite3.connect(generation)
            connection.execute("CREATE TABLE injected(value TEXT)")
            connection.commit()
            connection.close()
            digest = physical_summary_storage.file_sha256(generation)
            replacement = generation.with_name(
                f"{selector_path.name}.store.{digest}.sqlite"
            )
            generation.rename(replacement)
            selector.update({
                "artifact": replacement.name,
                "store_sha256": digest,
                "store_bytes": replacement.stat().st_size,
            })
            write_json(selector_path, selector, compact=True)
            with self.assertRaisesRegex(ValidationError, "objects"):
                read_logic_segment_timing_storage(selector_path)

            _, selector_path, _ = self._write_logic_store(root, "fpga0", 2)
            selector = read_json(selector_path)
            generation = selector_path.parent / selector["artifact"]
            connection = sqlite3.connect(generation)
            connection.execute("PRAGMA application_id=1")
            connection.commit()
            connection.close()
            digest = physical_summary_storage.file_sha256(generation)
            replacement = generation.with_name(
                f"{selector_path.name}.store.{digest}.sqlite"
            )
            generation.rename(replacement)
            selector.update({
                "artifact": replacement.name,
                "store_sha256": digest,
                "store_bytes": replacement.stat().st_size,
            })
            write_json(selector_path, selector, compact=True)
            with self.assertRaisesRegex(ValidationError, '"code":"invalid-artifact"'):
                read_logic_segment_timing_storage(selector_path)

            _, selector_path, _ = self._write_logic_store(root, "fpga0", 2)
            selector = read_json(selector_path)
            generation = selector_path.parent / selector["artifact"]
            generation.write_bytes(generation.read_bytes()[:-64])
            with self.assertRaisesRegex(ValidationError, "seal"):
                read_logic_segment_timing_storage(selector_path)

    def test_logic_segment_store_reader_close_is_deterministic(self) -> None:
        import emuflow_tools.timing.logic_segment_timing_storage as storage

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, selector_path, _ = self._write_logic_store(root, "fpga0", 1)
            view = storage.read_logic_segment_timing_storage(selector_path)
            sequence = view["segments"]
            view.close()
            with self.assertRaisesRegex(ValidationError, "closed"):
                list(sequence)


    def test_logic_segment_store_large_validation_has_bounded_memory(self) -> None:
        from emuflow_tools.timing.logic_segment_timing_storage import read_logic_segment_timing_storage

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, selector, _ = self._write_logic_store(root, "fpga0", 12000)
            gc.collect()

            tracemalloc.start()
            indexed_start = time.perf_counter()
            view = read_logic_segment_timing_storage(selector)
            validation = view.validate_timing()
            indexed_seconds = time.perf_counter() - indexed_start
            _, indexed_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            view.close()
            self.assertEqual(validation["segments"], 12000)
            self.assertLess(indexed_peak, 16 * 1024 * 1024)
            self.assertGreater(indexed_seconds, 0.0)


    def test_logic_store_gc_is_isolated_by_selector_owner(self) -> None:
        from emuflow_tools.timing.logic_segment_timing_storage import read_logic_segment_timing_storage
        from tests.timing_facts import logic_timing_facts

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stores = {}
            for owner, fpga in (("a.json", "fpga0"), ("b.json", "fpga1")):
                document = self._logic_timing(fpga, 1)
                header = {
                    key: value for key, value in document.items()
                    if key not in {"segments", "unsupported_member_paths"}
                }
                view = logic_timing_facts(
                    root / owner, header=header, segments=document["segments"]
                )
                stores[owner] = view.store_path
                view.close()
            document = self._logic_timing("fpga0", 2)
            header = {
                key: value for key, value in document.items()
                if key not in {"segments", "unsupported_member_paths"}
            }
            view = logic_timing_facts(
                root / "a.json", header=header, segments=document["segments"]
            )
            view.close()
            self.assertTrue(stores["b.json"].is_file())
            with read_logic_segment_timing_storage(root / "b.json") as other:
                self.assertEqual(len(other["segments"]), 1)
            self.assertTrue(all(
                path.name.startswith(f"{owner}.store.")
                for owner, path in stores.items()
            ))

    def test_measured_and_unmeasured_segment_ids_are_disjoint(self) -> None:
        from tests.timing_facts import logic_timing_facts

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = self._logic_timing("fpga0", 1)
            header = {
                key: value for key, value in document.items()
                if key not in {"segments", "unsupported_member_paths"}
            }
            segment = document["segments"][0]
            with self.assertRaisesRegex(ValidationError, "measured and unmeasured"):
                logic_timing_facts(
                    root / "timing.json",
                    header=header,
                    segments=(segment,),
                    unmeasured_segments=({
                        "id": segment["id"], "kind": segment["kind"],
                        "system_path": segment["system_path"],
                        "member_path": segment["member_path"],
                        "reason": "missing",
                    },),
                )
            view = logic_timing_facts(
                root / "reader-attack.json",
                header=header,
                segments=(segment,),
                unmeasured_segments=({
                    "id": "unmeasured", "kind": segment["kind"],
                    "system_path": segment["system_path"],
                    "member_path": segment["member_path"],
                    "reason": "missing",
                },),
            )
            generation = view.store_path
            view.close()
            connection = sqlite3.connect(generation)
            connection.execute(
                "UPDATE unmeasured_segments SET segment_id=? WHERE seq=0",
                (segment["id"],),
            )
            connection.commit()
            connection.close()
            selector_path = root / "reader-attack.json"
            selector = read_json(selector_path)
            digest = physical_summary_storage.file_sha256(generation)
            replacement = generation.with_name(
                f"{selector_path.name}.store.{digest}.sqlite"
            )
            generation.rename(replacement)
            selector.update({
                "artifact": replacement.name,
                "store_sha256": digest,
                "store_bytes": replacement.stat().st_size,
            })
            write_json(selector_path, selector, compact=True)
            from emuflow_tools.timing.logic_segment_timing_storage import read_logic_segment_timing_storage
            with self.assertRaisesRegex(ValidationError, "measured and unmeasured"):
                read_logic_segment_timing_storage(selector_path)

    def test_lazy_summary_supports_twelve_fpgas_and_closes_owned_leases(self) -> None:
        from emuflow_tools.timing.logic_segment_timing_storage import logic_segment_timing_reference
        from tests.timing_facts import logic_timing_facts

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            references = {}
            keys = []
            for index in range(12):
                fpga = f"fpga{index:02d}"
                document = self._logic_timing(fpga, 1)
                segment = document["segments"][0]
                segment["system_path"] = f"system-{index:02d}"
                segment["member_path"] = f"member-{index:02d}"
                selector = root / fpga / "logic-segment-timing.json"
                header = {
                    key: value for key, value in document.items()
                    if key not in {"segments", "unsupported_member_paths"}
                }
                view = logic_timing_facts(
                    selector, header=header, segments=(segment,)
                )
                view.close()
                references[fpga] = logic_segment_timing_reference(
                    selector, relative_to=root
                )
                keys.append((segment["system_path"], segment["member_path"]))
            summary_path = root / "physical-summary.json"
            write_json(summary_path, {
                "schema": "emuflow.physical-summary/v1",
                "design": "dut", "platform": "board",
                "logic_segment_system_routes_sha256": "a" * 64,
                "logic_segment_tdm_schedule_sha256": "b" * 64,
                "logic_segment_timing": references,
            })
            summary = read_physical_summary(summary_path)
            views = list(summary['logic_segment_timing'].values())
            self.assertEqual(len(views), 12)
            self.assertEqual([view['segments'][0]['system_path'] for view in views], [key[0] for key in keys])
            summary.close()
            for view in views:
                with self.assertRaisesRegex(ValidationError, 'closed'):
                    len(view['segments'])



if __name__ == "__main__":
    unittest.main()
