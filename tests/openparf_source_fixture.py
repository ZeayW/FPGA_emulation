from __future__ import annotations
from emuflow.physical.openparf.xilinx_openparf_source_storage import *
import emuflow.physical.openparf.xilinx_openparf_source_storage as _storage
OPENPARF_ATOMIC_SOURCE_APPLICATION_ID = _storage.OPENPARF_ATOMIC_SOURCE_APPLICATION_ID
OPENPARF_ATOMIC_SOURCE_SCHEMA = _storage.OPENPARF_ATOMIC_SOURCE_SCHEMA
OPENPARF_ATOMIC_SOURCE_STORAGE_SCHEMA = _storage.OPENPARF_ATOMIC_SOURCE_STORAGE_SCHEMA
OPENPARF_ATOMIC_SOURCE_USER_VERSION = _storage.OPENPARF_ATOMIC_SOURCE_USER_VERSION
XilinxOpenparfAtomicSourceView = _storage.XilinxOpenparfAtomicSourceView
_DDL = _storage._DDL
_SINGLETON_CLUSTER_PREFIX = _storage._SINGLETON_CLUSTER_PREFIX
_canonical = _storage._canonical
_ordered_cell_types = _storage._ordered_cell_types
_previous_source_store = _storage._previous_source_store
_store_name = _storage._store_name
_valid_digest = _storage._valid_digest
import hashlib, json, os, sqlite3, tempfile
from pathlib import Path
from typing import Any, Optional
from collections.abc import Iterable, Iterator, Mapping, Sequence
from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256, read_json, write_json
from emuflow.common.native_tools import native_file_generation
from emuflow.storage.store_publication import bind_store_source, publish_sqlite_store, require_store_sources_unchanged, validate_store_destination
from emuflow.physical.xilinx_netlist import is_xilinx_mapped_index
from tests.xilinx_packing_reference import CONSTANT_TYPES
def _validate_macro_cluster(cluster: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cluster_id = cluster.get("id")
    kind = cluster.get("kind")
    templates = cluster.get("site_templates")
    assignments = cluster.get("assignments")
    if (
        not isinstance(cluster_id, str) or not cluster_id
        or cluster_id.startswith(_SINGLETON_CLUSTER_PREFIX)
        or kind not in {"slice", "carry", "hard"}
        or not isinstance(templates, list) or not templates
        or any(not isinstance(item, str) or not item for item in templates)
        or not isinstance(assignments, list) or not assignments
    ):
        raise ValidationError("OpenPARF atomic-source macro cluster is invalid")
    normalized_assignments = []
    for assignment in assignments:
        if not isinstance(assignment, Mapping):
            raise ValidationError("OpenPARF atomic-source macro assignment is invalid")
        instance = assignment.get("instance")
        cell_type = assignment.get("cell_type")
        candidates = assignment.get("bel_candidates")
        bel = assignment.get("bel")
        if (
            not isinstance(instance, str) or not instance
            or not isinstance(cell_type, str) or not cell_type
            or not isinstance(candidates, list) or not candidates
            or any(not isinstance(item, str) or not item for item in candidates)
            or (bel is not None and (not isinstance(bel, str) or not bel))
        ):
            raise ValidationError("OpenPARF atomic-source macro assignment is invalid")
        normalized = {
            "instance": instance, "cell_type": cell_type,
            "bel_candidates": list(candidates),
        }
        if bel is not None:
            normalized["bel"] = bel
        normalized_assignments.append(normalized)
    normalized_cluster = {
        "id": cluster_id, "kind": kind,
        "site_templates": list(templates),
        "control_set": cluster.get("control_set"),
        "assignments": normalized_assignments,
    }
    if cluster.get("site_mode") is not None:
        normalized_cluster["site_mode"] = cluster["site_mode"]
    return normalized_cluster, normalized_assignments


def _validate_cascade_identity(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith(_SINGLETON_CLUSTER_PREFIX)
    ):
        raise ValidationError("OpenPARF atomic-source cascade identity is invalid")
    return value


def write_xilinx_openparf_atomic_source_storage(
    path: Path,
    *,
    mapped_path: Path,
    mapped_value: Mapping[str, Any],
    mapped_sha256: str,
    top: str,
    macro_clusters: Iterable[Mapping[str, Any]],
    cascades: Iterable[Mapping[str, Any]],
    physical_macro_count: int,
) -> XilinxOpenparfAtomicSourceView:
    """Publish one selector plus the non-derivable typed source relations."""

    if path.parent.resolve() != mapped_path.parent.resolve():
        raise ValidationError(
            "OpenPARF atomic-source selector and mapped input must share a directory"
        )
    if not _valid_digest(mapped_sha256):
        raise ValidationError("OpenPARF atomic-source mapped seal is invalid")
    require_mapped = getattr(mapped_value, "require_source_unchanged", None)
    if callable(require_mapped):
        require_mapped()
    mapped_schema = (
        "xilinx-mapped-index-v2"
        if is_xilinx_mapped_index(mapped_path)
        else "xilinx-mapped-json-v1"
    )
    source_bindings = validate_store_destination(
        path, (bind_store_source(mapped_path, mapped_schema),)
    )
    modules = mapped_value.get("modules")
    module = modules.get(top) if isinstance(modules, Mapping) else None
    cells = module.get("cells") if isinstance(module, Mapping) else None
    if not isinstance(cells, Mapping) or len(cells) > 4096:
        raise ValidationError("OpenPARF atomic-source mapped top is invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    # The previous generation belongs to the existing selector.  Discover and
    # seal-check it without opening the newly generated mapped input: a valid
    # mapped replacement necessarily disagrees with the old selector's source
    # seal.  The old store remains live until the new selector commits.
    old_store = _previous_source_store(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}.", suffix=".sqlite.tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    connection: Optional[sqlite3.Connection] = None
    published_store: Optional[Path] = None
    created_store = False
    selector_committed = False
    selector_descriptor, selector_temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".selector.tmp", dir=path.parent
    )
    os.close(selector_descriptor)
    selector_temporary = Path(selector_temporary_name)
    try:
        connection = sqlite3.connect(temporary)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA application_id={OPENPARF_ATOMIC_SOURCE_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={OPENPARF_ATOMIC_SOURCE_USER_VERSION}")
        connection.executescript(_DDL)
        cluster_digest = hashlib.sha256()
        assignment_digest = hashlib.sha256()
        macro_count = 0
        macro_assignment_count = 0
        for cluster_seq, raw_cluster in enumerate(macro_clusters):
            cluster, assignments = _validate_macro_cluster(raw_cluster)
            connection.execute(
                "INSERT INTO clusters VALUES (?,?,?,?,?,?)",
                (
                    cluster_seq, cluster["id"], cluster["kind"],
                    _canonical(cluster["site_templates"]),
                    _canonical(cluster["control_set"]), cluster.get("site_mode"),
                ),
            )
            cluster_digest.update(_canonical(cluster).encode("utf-8"))
            cluster_digest.update(b"\n")
            for assignment_seq, assignment in enumerate(assignments):
                connection.execute(
                    "INSERT INTO assignments VALUES (?,?,?,?,?,?)",
                    (
                        cluster_seq, assignment_seq, assignment["instance"],
                        assignment["cell_type"], assignment.get("bel"),
                        _canonical(assignment["bel_candidates"]),
                    ),
                )
                assignment_digest.update(_canonical(assignment).encode("utf-8"))
                assignment_digest.update(b"\n")
                macro_assignment_count += 1
            macro_count += 1

        cascade_digest = hashlib.sha256()
        cascade_count = 0
        for seq, raw_cascade in enumerate(cascades):
            if not isinstance(raw_cascade, Mapping):
                raise ValidationError("OpenPARF atomic-source cascade is invalid")
            cascade = dict(raw_cascade)
            cascade_id = _validate_cascade_identity(cascade.get("id"))
            if connection.execute(
                "SELECT 1 FROM clusters WHERE cluster_id=?", (cascade_id,)
            ).fetchone() is not None:
                raise ValidationError(
                    "OpenPARF atomic-source cascade identity collides with a macro cluster"
                )
            encoded = _canonical(cascade)
            connection.execute(
                "INSERT INTO cascades VALUES (?,?,?)", (seq, cascade_id, encoded)
            )
            cascade_digest.update(encoded.encode("utf-8"))
            cascade_digest.update(b"\n")
            cascade_count += 1

        owned_rows = iter(connection.execute(
            "SELECT instance,cell_type FROM assignments ORDER BY instance"
        ))
        owned = next(owned_rows, None)
        constant_digest = hashlib.sha256()
        constant_count = 0
        physical_count = 0
        singleton_count = 0
        for _index, (name, cell_type) in enumerate(_ordered_cell_types(cells)):
            if owned is not None and str(owned[0]) < name:
                raise ValidationError(
                    "OpenPARF atomic-source macro cell is absent from mapped input"
                )
            if cell_type in CONSTANT_TYPES:
                connection.execute(
                    "INSERT INTO constants VALUES (?,?)", (constant_count, name)
                )
                constant_digest.update(_canonical(name).encode("utf-8"))
                constant_digest.update(b"\n")
                constant_count += 1
                continue
            physical_count += 1
            if owned is not None and str(owned[0]) == name:
                if str(owned[1]) != cell_type:
                    raise ValidationError(
                        "OpenPARF atomic-source macro cell type disagrees"
                    )
                owned = next(owned_rows, None)
            else:
                singleton_count += 1
        if owned is not None:
            raise ValidationError(
                "OpenPARF atomic-source macro cell is absent from mapped input"
            )

        summary: dict[str, Any] = {
            "physical_atoms": macro_count + singleton_count,
            "constant_cells": constant_count,
        }
        if physical_macro_count:
            summary["physical_macros"] = physical_macro_count
        header = {
            "schema": OPENPARF_ATOMIC_SOURCE_SCHEMA,
            "status": "pass",
            "top": top,
            "source": {"mapped_sha256": mapped_sha256},
            "policy": {
                "provider": "mapped-singleton-atoms-no-site-packing-v1",
                "dedicated_or_relative_constraints": "fail-closed",
            },
            "summary": summary,
        }
        counts = {
            "physical_cells": physical_count,
            "macro_clusters": macro_count,
            "macro_assignments": macro_assignment_count,
            "singleton_clusters": singleton_count,
            "cascade_chains": cascade_count,
            "constant_cells": constant_count,
        }
        mapped_descriptor = {
            "artifact": mapped_path.name,
            "bytes": mapped_path.stat().st_size,
            "sha256": mapped_sha256,
        }
        logical_sha = hashlib.sha256(_canonical({
            "header": header,
            "counts": counts,
            "mapped": mapped_descriptor,
            "cluster_sha256": cluster_digest.hexdigest(),
            "assignment_sha256": assignment_digest.hexdigest(),
            "cascade_sha256": cascade_digest.hexdigest(),
            "constant_sha256": constant_digest.hexdigest(),
        }).encode("utf-8")).hexdigest()
        selector_base = {
            "schema": OPENPARF_ATOMIC_SOURCE_STORAGE_SCHEMA,
            "logical_schema": OPENPARF_ATOMIC_SOURCE_SCHEMA,
            "application_id": OPENPARF_ATOMIC_SOURCE_APPLICATION_ID,
            "user_version": OPENPARF_ATOMIC_SOURCE_USER_VERSION,
            "header": header,
            "counts": counts,
            "mapped": mapped_descriptor,
            "logical_sha256": logical_sha,
        }
        for key, value in (
            ("header", header), ("counts", counts),
            ("mapped", mapped_descriptor), ("logical_sha256", logical_sha),
        ):
            connection.execute(
                "INSERT INTO metadata VALUES (?,?)", (key, _canonical(value))
            )
        connection.commit()
        connection.close()
        connection = None
        store_sha = file_sha256(temporary)
        published_store = path.with_name(_store_name(store_sha))
        require_store_sources_unchanged(source_bindings)
        if callable(require_mapped):
            require_mapped()
        if published_store.is_file():
            if file_sha256(published_store) != store_sha:
                raise ValidationError(
                    "existing OpenPARF atomic-source artifact seal disagrees"
                )
            temporary.unlink()
        else:
            publish_sqlite_store(
                temporary,
                published_store,
                "openparf-atomic-source-store-v1",
                sources=source_bindings,
            )
            created_store = True
        selector = {
            **selector_base,
            "artifact": published_store.name,
            "store_bytes": published_store.stat().st_size,
            "store_sha256": store_sha,
        }
        write_json(selector_temporary, selector, compact=True)
        view = XilinxOpenparfAtomicSourceView(
            selector_temporary, mapped_path=mapped_path, mapped_value=mapped_value,
            mapped_sha256=mapped_sha256,
        )
        view.require_sources_unchanged()
        require_store_sources_unchanged(source_bindings)
        os.replace(selector_temporary, path)
        selector_committed = True
        view.selector_path = path.resolve()
        # Rename may change ctime even though the verified inode/content remain
        # the same. Bind the returned view to its committed selector, while
        # retaining the original mapped/store generations.
        committed_generation = native_file_generation(view.selector_path)
        if committed_generation[:5] != view.selector_generation[:5]:
            raise ValidationError("OpenPARF atomic source selector changed at publication")
        view.selector_generation = committed_generation
        if (
            old_store is not None
            and old_store.resolve() != published_store.resolve()
        ):
            old_store.unlink(missing_ok=True)
        return view
    except BaseException as error:
        if connection is not None:
            connection.close()
        temporary.unlink(missing_ok=True)
        selector_temporary.unlink(missing_ok=True)
        if published_store is not None and created_store and not selector_committed:
            published_store.unlink(missing_ok=True)
        if isinstance(error, sqlite3.IntegrityError):
            raise ValidationError(
                "OpenPARF atomic-source identity or ownership is duplicated"
            ) from error
        raise
