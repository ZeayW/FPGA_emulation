from contextlib import closing
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256, read_json
from emuflow.physical.openparf.xilinx_openparf_name_map_storage import (
    OPENPARF_ATOMIC_NAME_MAP_SCHEMA,
    OPENPARF_ATOMIC_NAME_MAP_STORAGE_SCHEMA,
    read_xilinx_openparf_atomic_name_map,
    write_xilinx_openparf_atomic_name_map_storage,
)


_DIGESTS = {
    "mapped_sha256": "1" * 64,
    "packed_sha256": "2" * 64,
    "architecture_sha256": "3" * 64,
}


def _header():
    return {
        "schema": OPENPARF_ATOMIC_NAME_MAP_SCHEMA,
        "top": "top",
        "coordinate_system": {
            "x_axis": [0, 1], "y_axis": [0], "y_axis_order": "ascending",
        },
        "site_database": {
            "schema": "emuflow.openparf-atomic-site-database/v1",
            "file": "site-map.sqlite3", "sites": 2,
        },
    }


def _atoms(suffix=""):
    return [
        {
            "openparf": "a0", "instance": "lut" + suffix,
            "cell_type": "LUT6", "source_cluster": "c0", "resource": "LUT",
        },
        {
            "openparf": "a1", "instance": "ff" + suffix,
            "cell_type": "FDRE", "source_cluster": "c1", "resource": "FF",
        },
    ]


def _carry_atoms():
    return [
        {
            "openparf": "a0", "instance": "carry0",
            "cell_type": "CARRY8", "source_cluster": "c0",
            "resource": "CARRY8",
        },
        {
            "openparf": "a1", "instance": "carry1",
            "cell_type": "CARRY8", "source_cluster": "c1",
            "resource": "CARRY8",
        },
    ]


def _cascade(identifier="carry-chain", instances=None):
    members = ["carry0", "carry1"] if instances is None else instances
    return {
        "id": identifier, "cell_type": "CARRY8", "instances": members,
        "links": [{
            "source": members[0], "target": members[1], "signals": ["carry"],
        }],
        "placement": "dedicated-adjacent-chain",
    }


def _write(path: Path, *, suffix="", cascades=(), atoms=None, digests=None):
    return write_xilinx_openparf_atomic_name_map_storage(
        path, header=_header(), atoms=_atoms(suffix) if atoms is None else atoms,
        carry_cascade_chains=list(cascades), **(_DIGESTS if digests is None else digests),
    )


class XilinxOpenparfNameMapStorageTest(unittest.TestCase):
    def test_typed_round_trip_and_relocation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "name_map.json"
            cascade = _cascade()
            view = _write(path, atoms=_carry_atoms(), cascades=[cascade])
            selector = read_json(path)
            self.assertEqual(
                selector["schema"], OPENPARF_ATOMIC_NAME_MAP_STORAGE_SCHEMA
            )
            self.assertNotIn("atoms", selector)
            self.assertLess(path.stat().st_size, 2048)
            self.assertEqual(list(view["atoms"]), _carry_atoms())
            self.assertEqual(list(view["carry_cascade_chains"]), [cascade])
            self.assertEqual(view.atom_by_ordinal(1)["instance"], "carry1")
            relocated = root / "relocated"
            relocated.mkdir()
            shutil.copy2(path, relocated / path.name)
            shutil.copy2(view.store_path, relocated / view.store_path.name)
            relocated_view = read_xilinx_openparf_atomic_name_map(
                relocated / path.name
            )
            try:
                self.assertEqual(list(relocated_view["atoms"]), _carry_atoms())
            finally:
                relocated_view.close()
            view.close()

    def test_deterministic_store_and_generation_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "name_map.json"
            first = _write(path)
            first_selector = path.read_bytes()
            first_store = first.store_path
            first.close()
            second = _write(path)
            self.assertEqual(path.read_bytes(), first_selector)
            self.assertEqual(second.store_path, first_store)
            second.close()

            replacement = _write(path, suffix="2")
            self.assertFalse(first_store.exists())
            self.assertTrue(replacement.store_path.is_file())
            self.assertEqual(
                [row["instance"] for row in replacement["atoms"]],
                ["lut2", "ff2"],
            )
            replacement.close()

    def test_seals_schema_and_source_identity_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "name_map.json"
            view = _write(path)
            original_store = view.store_path.read_bytes()
            with self.assertRaisesRegex(ValidationError, "mapped source seal"):
                read_xilinx_openparf_atomic_name_map(
                    path, mapped_sha256="f" * 64
                )

            missing_root = root / "missing"
            missing_root.mkdir()
            shutil.copy2(path, missing_root / path.name)
            with self.assertRaisesRegex(ValidationError, "store is missing"):
                read_xilinx_openparf_atomic_name_map(missing_root / path.name)

            with closing(sqlite3.connect(view.store_path)) as database, database:
                database.execute(
                    "UPDATE atoms SET resource='BROKEN' WHERE seq=0"
                )
            with self.assertRaisesRegex(ValidationError, "store seal disagrees"):
                read_xilinx_openparf_atomic_name_map(path)

            view.store_path.write_bytes(original_store)
            view.close()
            repaired = _write(path)
            repaired.close()
            selector = read_json(path)
            selector["artifact"] = "../escape.sqlite"
            path.write_text(json.dumps(selector), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "descriptor is invalid"):
                read_xilinx_openparf_atomic_name_map(path)

    def test_resealed_semantic_corruption_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "name_map.json"
            view = _write(path)
            store_path = view.store_path
            view.close()
            with closing(sqlite3.connect(store_path)) as database, database:
                database.execute("UPDATE atoms SET openparf='a9' WHERE seq=0")
            selector = read_json(path)
            digest = file_sha256(store_path)
            renamed = root / f"name-map.{digest}.sqlite"
            os.replace(store_path, renamed)
            selector.update({
                "artifact": renamed.name,
                "store_bytes": renamed.stat().st_size,
                "store_sha256": digest,
            })
            path.write_text(json.dumps(selector), encoding="utf-8")
            # Keep the rejected reader's traceback alive: cleanup must not wait
            # for exception frames or SQLite objects to be garbage-collected.
            from emuflow.physical.openparf import xilinx_openparf_name_map_storage as storage
            connections = []
            leases = []
            connect = storage._connect
            make_lease = storage._NameMapLease

            def track_connection(*args, **kwargs):
                connection = connect(*args, **kwargs)
                connections.append(connection)
                return connection

            def track_lease(*args, **kwargs):
                lease = make_lease(*args, **kwargs)
                leases.append(lease)
                return lease

            retained_error = None
            with mock.patch.object(storage, "_connect", track_connection), mock.patch.object(
                storage, "_NameMapLease", track_lease
            ):
                try:
                    read_xilinx_openparf_atomic_name_map(path)
                except ValidationError as error:
                    retained_error = error
            self.assertIsNotNone(retained_error)
            self.assertRegex(str(retained_error), "atom sequence is invalid")
            self.assertIsNotNone(retained_error.__traceback__)
            self.assertTrue(connections)
            self.assertTrue(leases)
            self.assertTrue(all(lease.closed for lease in leases))
            for connection in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")

    def test_failed_selector_commit_preserves_previous_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "name_map.json"
            first = _write(path)
            selector_before = path.read_bytes()
            store_before = first.store_path.read_bytes()
            first_store = first.store_path
            first.close()
            real_replace = os.replace

            def fail_selector(source, destination):
                if Path(destination) == path:
                    raise OSError("injected selector failure")
                return real_replace(source, destination)

            with mock.patch(
                "emuflow.physical.openparf.xilinx_openparf_name_map_storage.os.replace",
                side_effect=fail_selector,
            ):
                with self.assertRaisesRegex(OSError, "injected selector failure"):
                    _write(path, suffix="2")
            self.assertEqual(path.read_bytes(), selector_before)
            self.assertEqual(first_store.read_bytes(), store_before)
            self.assertEqual(
                [item.resolve() for item in root.glob("name-map.*.sqlite")],
                [first_store.resolve()],
            )
            current = read_xilinx_openparf_atomic_name_map(path)
            try:
                self.assertEqual(
                    [row["instance"] for row in current["atoms"]], ["lut", "ff"]
                )
            finally:
                current.close()

    def test_duplicate_identity_does_not_replace_valid_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "name_map.json"
            first = _write(path)
            selector_before = path.read_bytes()
            first_store = first.store_path
            first.close()
            duplicate = [*_atoms(), {**_atoms()[0], "openparf": "a2"}]
            with self.assertRaisesRegex(ValidationError, "identity is duplicated"):
                write_xilinx_openparf_atomic_name_map_storage(
                    path, header=_header(), atoms=duplicate,
                    carry_cascade_chains=[], **_DIGESTS,
                )
            self.assertEqual(path.read_bytes(), selector_before)
            self.assertTrue(first_store.is_file())

    def test_old_view_blocks_replacement_and_remains_iterable(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "name_map.json"
            old = _write(path)
            old_store = old.store_path
            complete = threading.Event()
            failure = []

            def replace_generation():
                try:
                    replacement = _write(path, suffix="2")
                    replacement.close()
                except BaseException as error:  # pragma: no cover - assertion path
                    failure.append(error)
                finally:
                    complete.set()

            thread = threading.Thread(target=replace_generation)
            thread.start()
            self.assertFalse(complete.wait(0.1))
            self.assertEqual(
                [row["instance"] for row in old["atoms"]], ["lut", "ff"]
            )
            old.close()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failure, [])
            self.assertFalse(old_store.exists())
            current = read_xilinx_openparf_atomic_name_map(path)
            try:
                self.assertEqual(
                    [row["instance"] for row in current["atoms"]], ["lut2", "ff2"]
                )
            finally:
                current.close()

    def test_postcommit_gc_failure_is_deferred(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "name_map.json"
            first = _write(path)
            old_store = first.store_path
            first.close()
            real_unlink = Path.unlink

            def fail_old(candidate, *args, **kwargs):
                if candidate.resolve() == old_store.resolve():
                    raise OSError("injected stale-store unlink failure")
                return real_unlink(candidate, *args, **kwargs)

            with mock.patch.object(Path, "unlink", autospec=True, side_effect=fail_old):
                replacement = _write(path, suffix="2")
            replacement.close()
            self.assertTrue(old_store.exists())
            current = read_xilinx_openparf_atomic_name_map(path)
            current_store = current.store_path
            current.close()
            retry = _write(path, suffix="2")
            retry.close()
            self.assertFalse(old_store.exists())
            self.assertTrue(current_store.exists())

    def test_selector_and_sidecar_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "name_map.json"
            view = _write(path)
            store = view.store_path
            view.close()
            selector_target = root / "real-selector.json"
            os.replace(path, selector_target)
            path.symlink_to(selector_target.name)
            with self.assertRaisesRegex(ValidationError, "selector is a symbolic link"):
                read_xilinx_openparf_atomic_name_map(path)
            path.unlink()
            os.replace(selector_target, path)
            outside = root / "outside.sqlite"
            os.replace(store, outside)
            store.symlink_to(outside.name)
            with self.assertRaisesRegex(ValidationError, "store is a symbolic link"):
                read_xilinx_openparf_atomic_name_map(path)

    def test_old_whole_map_json_has_no_compatibility_opt_in(self):
        import inspect
        from emuflow.physical.openparf import xilinx_openparf_name_map_storage as module
        self.assertNotIn('allow_legacy', inspect.signature(read_xilinx_openparf_atomic_name_map).parameters)
        self.assertFalse(hasattr(module, 'materialize_xilinx_openparf_atomic_name_map'))
        with tempfile.TemporaryDirectory() as temporary:
            legacy = Path(temporary) / 'legacy.json'
            legacy.write_text(json.dumps({**_header(), 'atoms': _atoms(), 'source': _DIGESTS}))
            with self.assertRaisesRegex(ValidationError, 'selector.*malformed'):
                read_xilinx_openparf_atomic_name_map(legacy)

    def test_malformed_cascades_and_cross_chain_ownership_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "name_map.json"
            malformed = _cascade()
            malformed["cell_type"] = "LUT6"
            with self.assertRaisesRegex(ValidationError, "cascade is invalid"):
                _write(path, atoms=_carry_atoms(), cascades=[malformed])
            duplicated = _cascade(instances=["carry0", "carry0"])
            with self.assertRaisesRegex(ValidationError, "cascade is invalid"):
                _write(path, atoms=_carry_atoms(), cascades=[duplicated])
            bad_link = _cascade()
            bad_link["links"][0]["target"] = "carry0"
            with self.assertRaisesRegex(ValidationError, "cascade is invalid"):
                _write(path, atoms=_carry_atoms(), cascades=[bad_link])
            missing_member = _cascade(instances=["carry0", "missing"])
            with self.assertRaisesRegex(ValidationError, "cascade ownership"):
                _write(path, atoms=_carry_atoms(), cascades=[missing_member])
            wrong_type = [*_carry_atoms()]
            wrong_type[1] = {**wrong_type[1], "cell_type": "LUT6"}
            with self.assertRaisesRegex(ValidationError, "cascade ownership"):
                _write(path, atoms=wrong_type, cascades=[_cascade()])
            extra = [*_carry_atoms(), {
                "openparf": "a2", "instance": "carry2", "cell_type": "CARRY8",
                "source_cluster": "c2", "resource": "CARRY8",
            }]
            second = _cascade("other", ["carry1", "carry2"])
            with self.assertRaisesRegex(ValidationError, "identity is duplicated"):
                _write(path, atoms=extra, cascades=[_cascade(), second])

    def test_reader_independently_rejects_resealed_malformed_cascade(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "name_map.json"
            view = _write(path, atoms=_carry_atoms(), cascades=[_cascade()])
            store = view.store_path
            view.close()
            with closing(sqlite3.connect(store)) as database, database:
                record = _cascade()
                record["instances"] = ["carry0", "carry0"]
                database.execute(
                    "UPDATE carry_cascades SET record_json=? WHERE seq=0",
                    (json.dumps(record),),
                )
            selector = read_json(path)
            digest = file_sha256(store)
            renamed = root / f"name-map.{digest}.sqlite"
            os.replace(store, renamed)
            selector.update({
                "artifact": renamed.name,
                "store_bytes": renamed.stat().st_size,
                "store_sha256": digest,
            })
            path.write_text(json.dumps(selector), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "cascade is invalid"):
                read_xilinx_openparf_atomic_name_map(path)

    def test_malformed_previous_selector_does_not_leak_writer_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "name_map.json"
            first = _write(path)
            first.close()
            path.write_text("{malformed", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "previous.*corrupt"):
                _write(path, suffix="2")
            path.unlink()
            second = _write(path, suffix="2")
            try:
                self.assertEqual(
                    [row["instance"] for row in second["atoms"]],
                    ["lut2", "ff2"],
                )
            finally:
                second.close()

    def test_missing_previous_sidecar_does_not_leak_writer_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "name_map.json"
            first = _write(path)
            store = first.store_path
            first.close()
            store.unlink()
            with self.assertRaisesRegex(ValidationError, "store is missing"):
                _write(path, suffix="2")
            path.unlink()
            second = _write(path, suffix="2")
            second.close()

    def test_mkstemp_failure_cleans_staging_and_releases_writer_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "name_map.json"
            real_mkstemp = tempfile.mkstemp
            calls = 0

            def fail_second_mkstemp(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected mkstemp failure")
                return real_mkstemp(*args, **kwargs)

            with mock.patch(
                "emuflow.physical.openparf.xilinx_openparf_name_map_storage.tempfile.mkstemp",
                side_effect=fail_second_mkstemp,
            ):
                with self.assertRaisesRegex(OSError, "injected mkstemp failure"):
                    _write(path)
            self.assertEqual(list(root.glob("*.tmp")), [])
            second = _write(path)
            second.close()


if __name__ == "__main__":
    unittest.main()
