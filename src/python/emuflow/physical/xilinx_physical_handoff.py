"""Single-file typed handoff for Xilinx packing and placement.

The native OpenPARF path produces one row-oriented SQLite artifact.  Legacy
PackedSiteNetlist and XilinxPlacement mappings are exposed as lazy views so
existing independently written validators and RapidWright exporters keep
checking the same contracts without serializing two graph-sized JSON files.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Dict, Optional

from emuflow.common.errors import ValidationError
from emuflow.storage.store_publication import (
    StoreSource,
    bind_store_source,
    require_store_sources_unchanged,
)
from emuflow.common.io import file_sha256
from emuflow.common.native_tools import native_file_generation
from emuflow.storage.sqlite_artifacts import immutable_sqlite_uri


XILINX_PHYSICAL_HANDOFF_SCHEMA = "emuflow.xilinx-physical-handoff-sqlite/v1"
_APPLICATION_ID = 0x454D5550  # EMUP
_USER_VERSION = 1
_CANONICAL_CLUSTER_ORDER_FEATURE = "canonical-cluster-id-order-v1"
_TYPED_SITE_PIN_REPAIRS_FEATURE = "typed-site-pin-repairs-v1"


def _decode(value: Optional[str]) -> Any:
    return None if value is None else json.loads(value)


def _connect(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(immutable_sqlite_uri(path), uri=True)
    except sqlite3.Error as error:
        raise ValidationError("Xilinx physical handoff cannot be opened") from error
    connection.row_factory = sqlite3.Row
    return connection


class _RecordSequence(Sequence):
    """Generation-bound ordered records without a Python population cache."""

    def __init__(self, source, table, column, count, *, decode=False):
        if (table, column) not in {("constant", "name"), ("cascade", "value_json")}:
            raise ValueError("unsupported handoff record relation")
        self._source, self._table, self._column = source, table, column
        self._count, self._decode = count, decode

    def __len__(self):
        return self._count

    def __iter__(self):
        require_store_sources_unchanged((self._source,))
        connection = _connect(self._source.path)
        try:
            count = 0
            for ordinal, value in connection.execute(
                f"SELECT ordinal,{self._column} FROM {self._table} ORDER BY ordinal"
            ):
                if ordinal != count:
                    raise ValidationError("physical handoff record order disagrees")
                count += 1
                yield json.loads(value) if self._decode else value
            if count != self._count:
                raise ValidationError("physical handoff record count disagrees")
        finally:
            connection.close()
            require_store_sources_unchanged((self._source,))

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[item] for item in range(*index.indices(self._count))]
        if not isinstance(index, int):
            raise TypeError("physical handoff record index must be an integer")
        if index < 0:
            index += self._count
        if not 0 <= index < self._count:
            raise IndexError(index)
        require_store_sources_unchanged((self._source,))
        connection = _connect(self._source.path)
        try:
            row = connection.execute(
                f"SELECT {self._column} FROM {self._table} WHERE ordinal=?", (index,)
            ).fetchone()
            if row is None:
                raise ValidationError("physical handoff record is missing")
            return json.loads(row[0]) if self._decode else row[0]
        finally:
            connection.close()
            require_store_sources_unchanged((self._source,))

    def __eq__(self, other):
        if not isinstance(other, Sequence) or isinstance(other, (str, bytes)):
            return NotImplemented
        return len(self) == len(other) and all(a == b for a, b in zip(self, other))


class _ClusterSequence(Sequence[Mapping[str, Any]]):
    def __init__(
        self, path: Path, *, placement: bool, count: int,
        canonical_order: bool = False,
        source_binding: Optional[StoreSource] = None,
    ) -> None:
        self._path = path
        self._placement = placement
        self._count = count
        self._canonical_order = canonical_order
        self._source_binding = source_binding or bind_store_source(path, "xilinx-physical-handoff-v1")

    def bound_store_sources(self):
        require_store_sources_unchanged((self._source_binding,))
        return (self._source_binding,)

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        self.bound_store_sources()
        connection = _connect(self._path)
        try:
            if self._canonical_order:
                cluster_rows = connection.execute(
                    "SELECT * FROM cluster ORDER BY cluster_id"
                )
                assignment_rows = iter(connection.execute(
                    "SELECT a.* FROM assignment a JOIN cluster c "
                    "ON c.ordinal=a.cluster_ordinal ORDER BY c.cluster_id, "
                    + (
                        "a.placement_ordinal"
                        if self._placement else "a.packed_ordinal"
                    )
                ))
            else:
                cluster_rows = connection.execute(
                    "SELECT * FROM cluster ORDER BY ordinal"
                )
                assignment_rows = iter(connection.execute(
                    "SELECT * FROM assignment ORDER BY cluster_ordinal, "
                    + (
                        "placement_ordinal"
                        if self._placement else "packed_ordinal"
                    )
                ))
            assignment = next(assignment_rows, None)
            for row in cluster_rows:
                values = []
                while (
                    assignment is not None
                    and assignment["cluster_ordinal"] == row["ordinal"]
                ):
                    if self._placement:
                        values.append({
                            "instance": assignment["instance"],
                            "cell_type": assignment["cell_type"],
                            "bel": assignment["bel"],
                            "placement_mode": assignment["placement_mode"],
                            "site": assignment["physical_site"],
                        })
                    else:
                        values.append({
                            "instance": assignment["instance"],
                            "cell_type": assignment["cell_type"],
                            "bel": assignment["bel"],
                        })
                    assignment = next(assignment_rows, None)
                if self._placement:
                    yield {
                        "cluster": row["cluster_id"], "site": row["site"],
                        "site_type": row["site_type"], "x": row["x"],
                        "y": row["y"], "fixed": bool(row["fixed"]),
                        "physical_region": _decode(row["physical_region_json"]),
                        "assignments": values,
                    }
                else:
                    cluster: Dict[str, Any] = {
                        "id": row["cluster_id"], "kind": row["kind"],
                        "site_templates": _decode(row["site_templates_json"]),
                        "control_set": _decode(row["control_set_json"]),
                        "assignments": values,
                    }
                    control_sets = _decode(row["control_sets_json"])
                    site_mode = row["site_mode"]
                    if control_sets is not None:
                        cluster["control_sets"] = control_sets
                    if site_mode is not None:
                        cluster["site_mode"] = site_mode
                    yield cluster
            if assignment is not None:
                raise ValidationError(
                    "Xilinx physical handoff contains orphan assignments"
                )
        except sqlite3.Error as error:
            raise ValidationError("Xilinx physical handoff rows are invalid") from error
        finally:
            connection.close()
            self.bound_store_sources()

    def __getitem__(self, index: int | slice) -> Any:
        values = list(self)
        return values[index]


class _SitePinRepairSequence(Sequence[Mapping[str, str]]):
    """Ordered projections of repair facts; the native gate audits their seal."""

    def __init__(self, source: StoreSource, count: int) -> None:
        self._source = source
        self._count = count

    def bound_store_sources(self):
        require_store_sources_unchanged((self._source,))
        return (self._source,)

    def __len__(self) -> int:
        return self._count

    @staticmethod
    def _record(row):
        return {"cluster": row["cluster_id"], "instance": row["instance"],
                "from_bel": row["from_bel"], "to_bel": row["to_bel"]}

    def __iter__(self):
        self.bound_store_sources()
        connection = _connect(self._source.path)
        count = 0
        try:
            for row in connection.execute("SELECT * FROM site_pin_repair ORDER BY seq"):
                if row["seq"] != count or count >= self._count:
                    raise ValidationError("Xilinx physical handoff repair sequence disagrees")
                count += 1
                yield self._record(row)
            if count != self._count:
                raise ValidationError("Xilinx physical handoff repair count disagrees")
        except sqlite3.Error as error:
            raise ValidationError("Xilinx physical handoff repair rows are invalid") from error
        finally:
            connection.close()
            self.bound_store_sources()

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(self._count))]
        if not isinstance(index, int):
            raise TypeError("repair index must be an integer or slice")
        if index < 0:
            index += self._count
        if not 0 <= index < self._count:
            raise IndexError(index)
        self.bound_store_sources()
        connection = _connect(self._source.path)
        try:
            row = connection.execute("SELECT * FROM site_pin_repair WHERE seq=?", (index,)).fetchone()
            if row is None:
                raise ValidationError("Xilinx physical handoff repair sequence disagrees")
            return self._record(row)
        except sqlite3.Error as error:
            raise ValidationError("Xilinx physical handoff repair rows are invalid") from error
        finally:
            connection.close()
            self.bound_store_sources()


def iter_xilinx_physical_handoff_cluster_pairs(
    path: Path,
) -> Iterator[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """Read packing and placement together with one assignment-table scan."""

    connection = _connect(path)
    try:
        cluster_rows = connection.execute(
            "SELECT * FROM cluster ORDER BY cluster_id"
        )
        assignment_rows = iter(connection.execute(
            "SELECT a.* FROM assignment a JOIN cluster c "
            "ON c.ordinal=a.cluster_ordinal "
            "ORDER BY c.cluster_id, a.packed_ordinal"
        ))
        assignment = next(assignment_rows, None)
        for row in cluster_rows:
            rows = []
            while (
                assignment is not None
                and assignment["cluster_ordinal"] == row["ordinal"]
            ):
                rows.append(assignment)
                assignment = next(assignment_rows, None)
            packed_assignments = [
                {
                    "instance": item["instance"],
                    "cell_type": item["cell_type"],
                    "bel": item["bel"],
                }
                for item in rows
            ]
            placed_assignments = [
                {
                    "instance": item["instance"],
                    "cell_type": item["cell_type"],
                    "bel": item["bel"],
                    "placement_mode": item["placement_mode"],
                    "site": item["physical_site"],
                }
                for item in sorted(rows, key=lambda item: item["placement_ordinal"])
            ]
            packed: Dict[str, Any] = {
                "id": row["cluster_id"], "kind": row["kind"],
                "site_templates": _decode(row["site_templates_json"]),
                "control_set": _decode(row["control_set_json"]),
                "assignments": packed_assignments,
            }
            control_sets = _decode(row["control_sets_json"])
            if control_sets is not None:
                packed["control_sets"] = control_sets
            if row["site_mode"] is not None:
                packed["site_mode"] = row["site_mode"]
            yield packed, {
                "cluster": row["cluster_id"], "site": row["site"],
                "site_type": row["site_type"], "x": row["x"],
                "y": row["y"], "fixed": bool(row["fixed"]),
                "physical_region": _decode(row["physical_region_json"]),
                "assignments": placed_assignments,
            }
        if assignment is not None:
            raise ValidationError(
                "Xilinx physical handoff contains orphan assignments"
            )
    except sqlite3.Error as error:
        raise ValidationError("Xilinx physical handoff rows are invalid") from error
    finally:
        connection.close()


def validate_xilinx_physical_handoff(path: Path, *, integrity_check: bool = False) -> Dict[str, Any]:
    """Ask the source-bound native owner to audit current rows and accounting."""
    from emuflow.common.native_protocol import CheckedRequestWriter
    from emuflow.common.native_tools import resolve_native_executable
    from emuflow.common.tool_runtime import NativeToolProcess

    if type(integrity_check) is not bool:
        raise ValidationError('physical handoff integrity_check must be a boolean')
    source = bind_store_source(Path(path), 'xilinx-physical-handoff-v1')
    with tempfile.TemporaryDirectory(prefix='emuflow-handoff-structure-') as temporary:
        root = Path(temporary)
        request, summary, operation = root/'request.bin', root/'summary.json', root/'operation.json'
        writer = CheckedRequestWriter(request)
        try:
            writer.put('QI', 0x31534850464D5545, 1)
            writer.put('3Q4q', *source.generation)
            writer.put('B', int(integrity_check))
            writer.finish()
        finally:
            writer.close()
        executable = resolve_native_executable('emuflow-native')
        command = [executable, '--report', str(operation), 'physical', 'validate-handoff-structure',
                   str(source.path), str(request), str(summary)]
        with NativeToolProcess(executable, command, environment=dict(os.environ), scratch_parent=root) as process:
            result = process.finish()
        if result.returncode:
            raise ValidationError('native physical handoff validation failed: '+result.stdout)
        require_store_sources_unchanged((source,))
        if summary.stat().st_size > 262144 or operation.stat().st_size > 262144:
            raise ValidationError('physical handoff validation result exceeds bound')
        proof = json.loads(operation.read_text())
        outputs = proof.get('outputs', [])
        if (proof.get('stage') != 'physical.validate-handoff-structure' or proof.get('status') != 'pass' or
                len(outputs) != 1 or outputs[0].get('path') != str(summary.resolve()) or
                outputs[0].get('schema') != 'physical-handoff-structure-report-v1'):
            raise ValidationError('physical handoff validation producer identity disagrees')
        fields = ('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')
        generation = tuple(outputs[0].get('generation', {}).get(field) for field in fields)
        if any(type(value) is not int for value in generation) or generation != native_file_generation(summary):
            raise ValidationError('physical handoff validation output generation changed')
        value = json.loads(summary.read_text())
        if (value.get('schema') != 'emuflow.xilinx-physical-handoff-validation/v1' or value.get('status') != 'pass' or
                value.get('integrity_check') is not integrity_check or any(
                    type(value.get(key)) is not int or value[key] < 0
                    for key in ('packed_clusters','assignments','cascade_chains','constants')) or
                not isinstance(value.get('physical_handoff_sha256'), str) or
                len(value['physical_handoff_sha256']) != 64):
            raise ValidationError('physical handoff validation summary disagrees')
        require_store_sources_unchanged((source,))
        return value


def open_xilinx_physical_handoff(
    path: Path, *, validated_sha256: Optional[str] = None, validated_generation=None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Open relocatable lazy compatibility views over one physical handoff."""

    if validated_sha256 is not None and (
        len(validated_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in validated_sha256
        )
    ):
        raise ValidationError("Xilinx physical handoff digest is invalid")

    source_binding = bind_store_source(path, "xilinx-physical-handoff-v1")
    if validated_generation is not None and (
        not isinstance(validated_generation,(list,tuple)) or len(validated_generation)!=7
        or any(type(value) is not int for value in validated_generation)
        or tuple(validated_generation)!=source_binding.generation
    ):
        raise ValidationError("Xilinx physical handoff differs from its validated generation")
    connection = _connect(path)
    try:
        manifest = connection.execute("SELECT * FROM manifest WHERE singleton=1").fetchone()
        if manifest is None or manifest["schema"] != XILINX_PHYSICAL_HANDOFF_SCHEMA:
            raise ValidationError("Xilinx physical handoff schema is invalid")
        documents = {
            row["name"]: json.loads(row["value_json"])
            for row in connection.execute("SELECT * FROM document")
        }
        cascades = _RecordSequence(source_binding, "cascade", "value_json", manifest["cascade_chains"], decode=True)
        constants = _RecordSequence(source_binding, "constant", "name", manifest["constants"])
        feature_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='feature'"
        ).fetchone()
        features = set()
        if feature_table is not None:
            features = {
                row[0] for row in connection.execute("SELECT name FROM feature")
            }
            if (not features or _CANONICAL_CLUSTER_ORDER_FEATURE not in features or
                    not features <= {_CANONICAL_CLUSTER_ORDER_FEATURE, _TYPED_SITE_PIN_REPAIRS_FEATURE}):
                raise ValidationError(
                    "Xilinx physical handoff features are invalid"
                )
        canonical_order = _CANONICAL_CLUSTER_ORDER_FEATURE in features
        typed_repairs = _TYPED_SITE_PIN_REPAIRS_FEATURE in features
        repair_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='site_pin_repair'"
        ).fetchone() is not None
        if repair_table != typed_repairs:
            raise ValidationError("Xilinx physical handoff repair feature disagrees")
    except (sqlite3.Error, json.JSONDecodeError) as error:
        raise ValidationError("Xilinx physical handoff metadata is invalid") from error
    finally:
        connection.close()
    if set(documents) != {"packed", "placement"}:
        raise ValidationError("Xilinx physical handoff documents are incomplete")
    # The production caller has just validated and hashed the immutable,
    # atomically published artifact.  Reusing that identity avoids a second
    # complete read of the same large database.  Standalone callers retain
    # the self-contained hash calculation.
    identity = validated_sha256 or file_sha256(path)
    require_store_sources_unchanged((source_binding,))
    packed = dict(documents["packed"])
    placement = dict(documents["placement"])
    policy = packed.get("policy")
    legalization = policy.get("site_pin_legalization") if isinstance(policy, dict) else None
    descriptor = legalization.get("repairs") if isinstance(legalization, dict) else None
    if isinstance(descriptor, dict) and not typed_repairs:
        raise ValidationError("Xilinx physical handoff repair feature disagrees")
    if typed_repairs:
        if (not isinstance(descriptor, dict) or set(descriptor) != {"schema", "records", "sha256"} or
                descriptor.get("schema") != "emuflow.xilinx-site-pin-repairs/v1" or
                type(descriptor.get("records")) is not int or descriptor["records"] < 0 or
                not isinstance(descriptor.get("sha256"), str) or len(descriptor["sha256"]) != 64 or
                any(character not in "0123456789abcdef" for character in descriptor["sha256"])):
            raise ValidationError("Xilinx physical handoff repair descriptor is invalid")
        packed["policy"] = dict(policy, site_pin_legalization=dict(
            legalization, repairs=_SitePinRepairSequence(source_binding, descriptor["records"])))
    packed_source = dict(packed.get("source", {}))
    packed_source["physical_handoff_sha256"] = identity
    packed["source"] = packed_source
    packed["clusters"] = _ClusterSequence(
        path, placement=False, count=manifest["packed_clusters"],
        canonical_order=canonical_order,
        source_binding=source_binding,
    )
    packed["cascade_chains"] = cascades
    packed["unplaced_constants"] = constants
    placement_source = dict(placement.get("source", {}))
    placement_source["packed_sha256"] = identity
    placement_source["physical_handoff_sha256"] = identity
    placement["source"] = placement_source
    placement["clusters"] = _ClusterSequence(
        path, placement=True, count=manifest["packed_clusters"],
        canonical_order=canonical_order,
        source_binding=source_binding,
    )
    return packed, placement


def validate_xilinx_physical_handoff_pair(
    packed: Mapping[str, Any], placement: Mapping[str, Any]
) -> None:
    """Reject mixing lazy views from different physical handoff runs."""

    packed_source = packed.get("source")
    placement_source = placement.get("source")
    packed_identity = (
        packed_source.get("physical_handoff_sha256")
        if isinstance(packed_source, Mapping) else None
    )
    placement_identity = (
        placement_source.get("physical_handoff_sha256")
        if isinstance(placement_source, Mapping) else None
    )
    if packed_identity is None and placement_identity is None:
        return
    if (
        not isinstance(packed_identity, str)
        or not isinstance(placement_identity, str)
        or len(packed_identity) != 64
        or any(character not in "0123456789abcdef" for character in packed_identity)
        or packed_identity != placement_identity
    ):
        raise ValidationError(
            "packing and placement physical handoff runs disagree"
        )
