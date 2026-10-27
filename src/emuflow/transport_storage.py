"""Indexed Phase 6 transport, lane, and virtual-anchor relations.

The public JSON paths remain stable, but they contain small selector manifests.
Hop-sized records live once in ``transport.sqlite`` and are exposed through
read-only sequence views.  Materialization is retained only as an explicit
compatibility operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from .sqlite_artifacts import immutable_sqlite_uri
from .errors import ValidationError
from .io import file_sha256, write_json
from .netlist import (
    LOGICAL_LANE_MAP_SCHEMA,
    TRANSPORT_ENDPOINTS_SCHEMA,
    VIRTUAL_IO_ANCHORS_SCHEMA,
    _endpoint_id,
    _sv_name,
    transport_endpoint_port,
)
from .phase3_assignment_index import open_phase3_assignment_index
from .platform import Platform
from .tdm import TDM_SCHEDULE_SCHEMA, is_sampled_virtual_wire_schedule
from .tdm_schedule_storage import canonical_tdm_schedule_sha256
from .store_publication import (
    StoreSource,
    VerifiedStoreFile,
    bind_view_sources,
    bind_store_source,
    require_store_sources_unchanged,
    publish_sqlite_store,
)
from .native_tools import native_file_generation


TRANSPORT_STORAGE_SCHEMA = "emuflow.transport-storage/v1"
LANE_MAP_STORAGE_SCHEMA = "emuflow.logical-lane-map-storage/v1"
ANCHOR_STORAGE_SCHEMA = "emuflow.virtual-io-anchors-storage/v1"
VIRTUAL_BINDING_STORAGE_SCHEMA = "emuflow.virtual-board-binding-storage/v1"
VIRTUAL_BINDING_LOGICAL_SCHEMA = "emuflow.virtual-board-binding/v1"
TRANSPORT_APPLICATION_ID = 0x454D5558  # ASCII "EMUX"
TRANSPORT_USER_VERSION = 2
TRANSPORT_STORE_NAME = "transport.sqlite"


DDL = (Path(__file__).parent / 'data/schema/phase6_transport.sql').read_text()


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )


def _digest_records(records: Iterator[Mapping[str, Any]]) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for record in records:
        digest.update(_canonical(record).encode("utf-8"))
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def _logical_digest(
    header: Mapping[str, Any], sections: Mapping[str, Mapping[str, Any]]
) -> str:
    return hashlib.sha256(
        _canonical({"header": header, "sections": sections}).encode("utf-8")
    ).hexdigest()


def _canonical_document_digest(
    header: Mapping[str, Any],
    sections: Mapping[str, Iterator[str]],
) -> str:
    """Hash the historical compact, sort-key JSON without building it."""

    digest = hashlib.sha256()
    digest.update(b"{")
    first_key = True
    for key in sorted(set(header) | set(sections)):
        if not first_key:
            digest.update(b",")
        first_key = False
        digest.update(_canonical(key).encode("utf-8"))
        digest.update(b":")
        if key in sections:
            digest.update(b"[")
            first_record = True
            for encoded in sections[key]:
                if not first_record:
                    digest.update(b",")
                first_record = False
                digest.update(encoded.encode("utf-8"))
            digest.update(b"]")
        else:
            digest.update(_canonical(header[key]).encode("utf-8"))
    digest.update(b"}")
    return digest.hexdigest()


def _raw_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValidationError(f"{path}: transport manifest is unreadable") from error
    if not isinstance(value, dict):
        raise ValidationError(f"{path}: transport manifest is not an object")
    return value


def _store_path(path: Path, manifest: Mapping[str, Any]) -> Path:
    artifact = manifest.get("artifact")
    if artifact == TRANSPORT_STORE_NAME:
        result = path.with_name(artifact)
    elif artifact == f"../{TRANSPORT_STORE_NAME}":
        result = path.parent.parent / TRANSPORT_STORE_NAME
    else:
        raise ValidationError("transport storage artifact path is invalid")
    return result.resolve()


def _validate_manifest(
    manifest: Mapping[str, Any],
    path: Path,
    *,
    schema: str,
    logical_schema: str,
    sections: set[str],
) -> Path:
    if (
        manifest.get("schema") != schema
        or manifest.get("logical_schema") != logical_schema
        or manifest.get("application_id") != TRANSPORT_APPLICATION_ID
        or manifest.get("user_version") not in (1,TRANSPORT_USER_VERSION)
        or not isinstance(manifest.get("header"), dict)
        or not isinstance(manifest.get("logical_sha256"), str)
        or len(manifest["logical_sha256"]) != 64
        or isinstance(manifest.get("store_bytes"), bool)
        or not isinstance(manifest.get("store_bytes"), int)
        or manifest["store_bytes"] < 0
        or not isinstance(manifest.get("store_sha256"), str)
        or len(manifest["store_sha256"]) != 64
        or not isinstance(manifest.get("sections"), dict)
        or set(manifest["sections"]) != sections
    ):
        raise ValidationError(f"{path}: transport storage manifest is malformed")
    canonical_sha256 = manifest.get("canonical_sha256")
    if canonical_sha256 is not None and (
        not isinstance(canonical_sha256, str) or len(canonical_sha256) != 64
    ):
        raise ValidationError(f"{path}: transport canonical seal is malformed")
    for name in sections:
        section = manifest["sections"][name]
        if (
            not isinstance(section, dict)
            or isinstance(section.get("records"), bool)
            or not isinstance(section.get("records"), int)
            or section["records"] < 0
            or not isinstance(section.get("sha256"), str)
            or len(section["sha256"]) != 64
        ):
            raise ValidationError(
                f"{path}: transport section {name!r} is malformed"
            )
    if _logical_digest(manifest["header"], manifest["sections"]) != manifest[
        "logical_sha256"
    ]:
        raise ValidationError(f"{path}: transport logical seal disagrees")
    return _store_path(path, manifest)


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        immutable_sqlite_uri(path), uri=True
    )
    connection.row_factory = sqlite3.Row
    return connection


class _RecordSequence(Sequence[Mapping[str, Any]]):
    def __init__(
        self,
        store_path: Path,
        query: str,
        parameters: tuple[Any, ...],
        count: int,
        digest: str,
        label: str,
    ):
        self.store_path = store_path
        self.query = query
        self.parameters = parameters
        self._count = count
        self.expected_sha256 = digest
        self.label = label

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        connection = _connect(self.store_path)
        try:
            observed = 0
            for row in connection.execute(self.query, self.parameters):
                try:
                    value = json.loads(row[0])
                except json.JSONDecodeError as error:
                    raise ValidationError(
                        f"transport {self.label} record is corrupt"
                    ) from error
                if not isinstance(value, dict):
                    raise ValidationError(
                        f"transport {self.label} record is not an object"
                    )
                observed += 1
                yield value
            if observed != self._count:
                raise ValidationError(
                    f"transport {self.label} row count disagrees"
                )
        finally:
            connection.close()

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return list(self)[index]
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError(index)
        connection = _connect(self.store_path)
        try:
            row = connection.execute(
                f"SELECT record_json FROM ({self.query}) LIMIT 1 OFFSET ?",
                self.parameters + (index,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValidationError(
                f"transport {self.label} sequence is not contiguous"
            )
        try:
            value = json.loads(row[0])
        except json.JSONDecodeError as error:
            raise ValidationError(
                f"transport {self.label} record is corrupt"
            ) from error
        if not isinstance(value, dict):
            raise ValidationError(
                f"transport {self.label} record is not an object"
            )
        return value


class _SignalSequence(Sequence[Mapping[str, Any]]):
    def __init__(
        self,
        store_path: Path,
        fpga: str,
        kind: str,
        count: int,
        digest: str,
    ):
        self.store_path = store_path
        self.fpga = fpga
        self.kind = kind
        self._count = count
        self.expected_sha256 = digest

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        connection = _connect(self.store_path)
        try:
            logical_kind = "source" if self.kind == "tx" else "shadow"
            rows = connection.execute(
                "SELECT seq,signal FROM signals "
                "WHERE fpga=? AND kind=? ORDER BY seq",
                (self.fpga, logical_kind),
            )
            observed = 0
            for row in rows:
                observed += 1
                yield {"index": int(row[0]), "signal": row[1]}
            if observed != self._count:
                raise ValidationError("transport signal row count disagrees")
        finally:
            connection.close()

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return list(self)[index]
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError(index)
        for current, value in enumerate(self):
            if current == index:
                return value
        raise ValidationError("transport signal sequence is not contiguous")


class _AnchorSequence(Sequence[Mapping[str, Any]]):
    def __init__(
        self, store_path: Path, fpga: str, count: int, digest: str
    ):
        self.store_path = store_path
        self.fpga = fpga
        self._count = count
        self.expected_sha256 = digest

    def __len__(self) -> int:
        return self._count

    def iter_headers(self) -> Iterator[Mapping[str, Any]]:
        connection = _connect(self.store_path)
        try:
            observed = 0
            for row in connection.execute(
                "SELECT record_json FROM anchors WHERE fpga=? ORDER BY id",
                (self.fpga,),
            ):
                value = json.loads(row[0])
                if not isinstance(value, dict):
                    raise ValidationError("transport anchor record is malformed")
                observed += 1
                yield value
            if observed != self._count:
                raise ValidationError("transport anchor row count disagrees")
        except json.JSONDecodeError as error:
            raise ValidationError("transport anchor record is corrupt") from error
        finally:
            connection.close()

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        connection = _connect(self.store_path)
        try:
            observed = 0
            rows = connection.execute(
                "SELECT id,link,peer,kind,lane,record_json FROM anchors "
                "WHERE fpga=? ORDER BY id",
                (self.fpga,),
            )
            for row in rows:
                value = json.loads(row["record_json"])
                members = list(
                    connection.execute(
                        "SELECT id,slot FROM endpoints WHERE fpga=? "
                        "AND link=? AND peer=? AND kind=? AND lane=? ORDER BY id",
                        (
                            self.fpga,
                            row["link"],
                            row["peer"],
                            row["kind"],
                            row["lane"],
                        ),
                    )
                )
                value["endpoint_ids"] = [member["id"] for member in members]
                value["slots"] = sorted(member["slot"] for member in members)
                observed += 1
                yield value
            if observed != self._count:
                raise ValidationError("transport anchor row count disagrees")
        except json.JSONDecodeError as error:
            raise ValidationError("transport anchor record is corrupt") from error
        finally:
            connection.close()

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return list(self)[index]
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError(index)
        for current, value in enumerate(self):
            if current == index:
                return value
        raise ValidationError("transport anchor sequence is not contiguous")


class _BaseView(Mapping[str, Any]):
    @property
    def store_schema(self):
        return "transport-store-v"+str(self.manifest["user_version"])

    def __init__(self, manifest_path: Path):
        self.manifest_path = manifest_path.resolve()
        self.manifest = _raw_manifest(self.manifest_path)
        self._header: dict[str, Any] = {}
        self._sections: dict[str, Sequence[Mapping[str, Any]]] = {}

    def bound_store_sources(self):
        require_store_sources_unchanged(self._source_bindings)
        return self._source_bindings

    def __getitem__(self, key: str) -> Any:
        if key in self._sections:
            return self._sections[key]
        return self._header[key]

    def __iter__(self) -> Iterator[str]:
        yield from self._header
        yield from self._sections

    def __len__(self) -> int:
        return len(self._header) + len(self._sections)

    @property
    def logical_sha256(self) -> str:
        return self.manifest["logical_sha256"]

    @property
    def canonical_sha256(self) -> str:
        return self.manifest.get(
            "canonical_sha256", self.manifest["logical_sha256"]
        )

    def materialize(self, *, verify_records: bool = True) -> dict[str, Any]:
        result = dict(self._header)
        for name, records in self._sections.items():
            digest = hashlib.sha256()
            values = []
            for record in records:
                value = dict(record)
                digest.update(_canonical(value).encode("utf-8"))
                digest.update(b"\n")
                values.append(value)
            if verify_records and digest.hexdigest() != records.expected_sha256:
                raise ValidationError(
                    f"transport {name} logical digest disagrees"
                )
            result[name] = values
        return result


def _open_and_validate(
    path: Path,
    *,
    schema: str,
    logical_schema: str,
    sections: set[str],
    store_owner: VerifiedStoreFile | None = None,
) -> tuple[dict[str, Any], Path, tuple[StoreSource, StoreSource]]:
    selector_source = bind_store_source(path, "transport-selector-v1")
    manifest = _raw_manifest(path)
    store_path = _validate_manifest(
        manifest,
        path,
        schema=schema,
        logical_schema=logical_schema,
        sections=sections,
    )
    try:
        stat = store_path.stat()
        store_schema = "transport-store-v"+str(manifest["user_version"])
        store_source = bind_store_source(store_path, store_schema)
    except OSError as error:
        raise ValidationError("transport SQLite store is missing") from error
    if stat.st_size != manifest["store_bytes"]:
        raise ValidationError("transport SQLite store size disagrees")
    if store_owner is None:
        if file_sha256(store_path) != manifest["store_sha256"]:
            raise ValidationError("transport SQLite store digest disagrees")
    else:
        store_owner.require(
            store_path,
            store_schema,
            manifest["store_bytes"],
            manifest["store_sha256"],
        )
    connection = _connect(store_path)
    try:
        if (
            connection.execute("PRAGMA application_id").fetchone()[0]
            != TRANSPORT_APPLICATION_ID
            or connection.execute("PRAGMA user_version").fetchone()[0]
            != manifest["user_version"]
        ):
            raise ValidationError("unsupported transport SQLite artifact")
        metadata = {
            row[0]: json.loads(row[1])
            for row in connection.execute("SELECT key,value FROM metadata")
        }
        header = manifest["header"]
        if (
            ("design" in header and metadata.get("design") != header["design"])
            or metadata.get("platform") != header.get("platform")
        ):
            raise ValidationError("transport SQLite metadata disagrees")
    except json.JSONDecodeError as error:
        raise ValidationError("transport SQLite metadata is corrupt") from error
    finally:
        connection.close()
    sources = (selector_source, store_source)
    if manifest['user_version']==2:
        from .transport_native import original_sources
        sources+=original_sources(metadata)
    require_store_sources_unchanged(sources)
    return manifest, store_path, sources


class TransportView(_BaseView):

    def __init__(
        self, manifest_path: Path, *, store_owner: VerifiedStoreFile | None = None
    ):
        super().__init__(manifest_path)
        manifest, store_path, self._source_bindings = _open_and_validate(
            self.manifest_path,
            schema=TRANSPORT_STORAGE_SCHEMA,
            logical_schema=TRANSPORT_ENDPOINTS_SCHEMA,
            sections={"source_signals", "shadow_signals", "endpoints"},
            store_owner=store_owner,
        )
        self.manifest = manifest
        self.store_path = store_path
        self._header = dict(manifest["header"])
        fpga = self._header.get("fpga")
        if not isinstance(fpga, str):
            raise ValidationError("transport selector is invalid")
        connection = _connect(store_path)
        try:
            actual = connection.execute(
                "SELECT count(*) FROM endpoints WHERE fpga=?", (fpga,)
            ).fetchone()[0]
        finally:
            connection.close()
        if actual != manifest["sections"]["endpoints"]["records"]:
            raise ValidationError("transport endpoint row count disagrees")
        self._sections = {
            "source_signals": _SignalSequence(
                store_path,
                fpga,
                "tx",
                manifest["sections"]["source_signals"]["records"],
                manifest["sections"]["source_signals"]["sha256"],
            ),
            "shadow_signals": _SignalSequence(
                store_path,
                fpga,
                "rx",
                manifest["sections"]["shadow_signals"]["records"],
                manifest["sections"]["shadow_signals"]["sha256"],
            ),
            "endpoints": _RecordSequence(
                store_path,
                "SELECT record_json FROM endpoints WHERE fpga=? ORDER BY id",
                (fpga,),
                manifest["sections"]["endpoints"]["records"],
                manifest["sections"]["endpoints"]["sha256"],
                f"{fpga} endpoints",
            ),
        }

    def iter_rx_signal_records(self) -> Iterator[tuple[str, str]]:
        """Yield each RX shadow signal and its logical net in signal order."""

        connection = _connect(self.store_path)
        try:
            rows = connection.execute(
                "SELECT signal,net FROM endpoints WHERE fpga=? AND kind='rx' "
                "GROUP BY signal,net ORDER BY signal,net",
                (self._header["fpga"],),
            )
            previous = None
            for row in rows:
                signal = str(row["signal"])
                if signal == previous:
                    raise ValidationError(
                        "one transport shadow signal maps to multiple nets"
                    )
                previous = signal
                yield signal, str(row["net"])
        finally:
            connection.close()

    def iter_expected_rx_bindings(self) -> Iterator[tuple[str, str]]:
        """Yield logical-net/RX-placeholder identities in endpoint order."""

        connection = _connect(self.store_path)
        try:
            for row in connection.execute(
                "SELECT net,id FROM endpoints WHERE fpga=? AND kind='rx' "
                "ORDER BY net,id",
                (self._header["fpga"],),
            ):
                yield str(row["net"]), str(row["id"])
        finally:
            connection.close()

    def endpoint_groups(self) -> list[tuple[str, str, set[str]]]:
        connection = _connect(self.store_path)
        try:
            grouped: dict[tuple[str, str], set[str]] = {}
            for row in connection.execute(
                "SELECT link,peer,kind FROM endpoints WHERE fpga=? "
                "GROUP BY link,peer,kind ORDER BY link,peer,kind",
                (self._header["fpga"],),
            ):
                grouped.setdefault((row["link"], row["peer"]), set()).add(
                    row["kind"]
                )
            return [
                (link, peer, kinds)
                for (link, peer), kinds in sorted(grouped.items())
            ]
        finally:
            connection.close()

    def iter_rtl_endpoints(
        self, kind: str
    ) -> Iterator[tuple[int, Mapping[str, Any], int]]:
        if kind not in {"tx", "rx"}:
            raise ValueError("RTL endpoint kind must be tx or rx")
        slot_column = "slot" if kind == "tx" else "arrival_slot"
        connection = _connect(self.store_path)
        try:
            rows = connection.execute(
                f"""
                SELECT e.{slot_column},e.record_json,s.seq
                FROM endpoints e
                JOIN signals s ON s.fpga=e.fpga AND s.signal=e.signal
                 AND s.kind=CASE
                   WHEN e.kind='tx' AND substr(e.signal,1,4)='net:'
                     THEN 'source' ELSE 'shadow' END
                WHERE e.fpga=? AND e.kind=?
                ORDER BY e.{slot_column},e.link,e.lane,e.id
                """,
                (self._header["fpga"], kind),
            )
            for row in rows:
                value = json.loads(row["record_json"])
                if not isinstance(value, dict):
                    raise ValidationError("transport RTL endpoint is malformed")
                yield int(row[0]), value, int(row[2])
        except json.JSONDecodeError as error:
            raise ValidationError("transport RTL endpoint is corrupt") from error
        finally:
            connection.close()


class LaneMapView(_BaseView):

    def __init__(
        self, manifest_path: Path, *, store_owner: VerifiedStoreFile | None = None
    ):
        super().__init__(manifest_path)
        manifest, store_path, self._source_bindings = _open_and_validate(
            self.manifest_path,
            schema=LANE_MAP_STORAGE_SCHEMA,
            logical_schema=LOGICAL_LANE_MAP_SCHEMA,
            sections={"entries"},
            store_owner=store_owner,
        )
        self.manifest = manifest
        self.store_path = store_path
        self._header = dict(manifest["header"])
        self._sections = {
            "entries": _RecordSequence(
                store_path,
                "SELECT record_json FROM lanes ORDER BY id",
                (),
                manifest["sections"]["entries"]["records"],
                manifest["sections"]["entries"]["sha256"],
                "lane entries",
            )
        }


class AnchorView(_BaseView):

    def __init__(
        self, manifest_path: Path, *, store_owner: VerifiedStoreFile | None = None
    ):
        super().__init__(manifest_path)
        manifest, store_path, self._source_bindings = _open_and_validate(
            self.manifest_path,
            schema=ANCHOR_STORAGE_SCHEMA,
            logical_schema=VIRTUAL_IO_ANCHORS_SCHEMA,
            sections={"anchors"},
            store_owner=store_owner,
        )
        self.manifest = manifest
        self.store_path = store_path
        self._header = dict(manifest["header"])
        fpga = self._header.get("fpga")
        if not isinstance(fpga, str):
            raise ValidationError("virtual-anchor selector is invalid")
        self._sections = {
            "anchors": _AnchorSequence(
                store_path,
                fpga,
                manifest["sections"]["anchors"]["records"],
                manifest["sections"]["anchors"]["sha256"],
            )
        }

    def materialize(self, *, verify_records: bool = True) -> dict[str, Any]:
        if verify_records:
            connection = _connect(self.store_path)
            try:
                summary = _anchor_relation_section(connection, self._header["fpga"])
            finally:
                connection.close()
            if summary["sha256"] != self.manifest["sections"]["anchors"]["sha256"]:
                raise ValidationError("transport anchors logical digest disagrees")
        result = dict(self._header)
        result["anchors"] = [dict(record) for record in self._sections["anchors"]]
        return result


class VirtualBindingView(_BaseView):

    def __init__(
        self, manifest_path: Path, *, store_owner: VerifiedStoreFile | None = None
    ):
        super().__init__(manifest_path)
        manifest, store_path, self._source_bindings = _open_and_validate(
            self.manifest_path,
            schema=VIRTUAL_BINDING_STORAGE_SCHEMA,
            logical_schema=VIRTUAL_BINDING_LOGICAL_SCHEMA,
            sections={"entries"},
            store_owner=store_owner,
        )
        self.manifest = manifest
        self.store_path = store_path
        self._header = dict(manifest["header"])
        self._sections = {
            "entries": _RecordSequence(
                store_path,
                "SELECT record_json FROM binding_entries "
                "ORDER BY schedule_entry",
                (),
                manifest["sections"]["entries"]["records"],
                manifest["sections"]["entries"]["sha256"],
                "virtual binding entries",
            )
        }

    def validate_transport_relations(self) -> None:
        connection = _connect(self.store_path)
        try:
            mismatch = connection.execute(
                """
                SELECT b.schedule_entry
                FROM binding_entries b
                LEFT JOIN lanes l ON l.schedule_entry=b.schedule_entry
                LEFT JOIN endpoints tx ON tx.id=l.tx_endpoint
                LEFT JOIN endpoints rx ON rx.id=l.rx_endpoint
                WHERE l.schedule_entry IS NULL OR tx.id IS NULL OR rx.id IS NULL
                   OR tx.kind!='tx' OR rx.kind!='rx'
                   OR tx.fpga!=l.source_fpga OR rx.fpga!=l.sink_fpga
                   OR tx.peer!=l.sink_fpga OR rx.peer!=l.source_fpga
                   OR tx.link!=l.link OR rx.link!=l.link
                   OR tx.slot!=l.slot OR rx.slot!=l.slot
                   OR tx.lane!=l.lane OR rx.lane!=l.lane
                   OR b.link!=l.link OR b.slot!=l.slot OR b.wire!=l.lane
                   OR b.source_fpga!=l.source_fpga
                   OR b.sink_fpga!=l.sink_fpga
                LIMIT 1
                """
            ).fetchone()
            counts = connection.execute(
                "SELECT (SELECT count(*) FROM binding_entries),"
                "(SELECT count(*) FROM lanes)"
            ).fetchone()
            if mismatch is not None or counts[0] != counts[1]:
                raise ValidationError(
                    "virtual binding disagrees with transport relations"
                )
        finally:
            connection.close()

    def iter_adapter_ports(self, fpga: str) -> Iterator[Mapping[str, Any]]:
        """Yield unique adapter pin pairs without scanning hop records.

        The virtual binding keeps one entry per scheduled hop for compatibility.
        RTL and XDC emission only depend on the much smaller set of physical
        differential pairs.  New stores normalize that set here so every FPGA
        exporter does not decode the complete hop relation again.
        """

        connection = _connect(self.store_path)
        try:
            has_relation = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') "
                "AND name='binding_adapter_ports'"
            ).fetchone()
            if has_relation is not None:
                for row in connection.execute(
                    "SELECT role,logical_port,logical_port_width,p_port,n_port,"
                    "package_pin_p,package_pin_n,iostandard "
                    "FROM binding_adapter_ports WHERE fpga=? ORDER BY p_port",
                    (fpga,),
                ):
                    yield {
                        "role": str(row["role"]),
                        "logical_port": str(row["logical_port"]),
                        "logical_port_width": int(row["logical_port_width"]),
                        "iostandard": str(row["iostandard"]),
                        "p": {
                            "port": str(row["p_port"]),
                            "package_pin": str(row["package_pin_p"]),
                        },
                        "n": {
                            "port": str(row["n_port"]),
                            "package_pin": str(row["package_pin_n"]),
                        },
                    }
                return

            # Compatibility for v1 stores written before the normalized
            # adapter relation existed.  This path is intentionally bounded by
            # physical port count, although it must decode historical entries.
            records: dict[str, Mapping[str, Any]] = {}
            for row in connection.execute(
                "SELECT record_json FROM binding_entries "
                "WHERE source_fpga=? OR sink_fpga=? ORDER BY schedule_entry",
                (fpga, fpga),
            ):
                value = json.loads(row[0])
                if not isinstance(value, dict):
                    raise ValidationError("virtual binding entry is malformed")
                for role in ("source", "sink"):
                    end = value.get(role)
                    if isinstance(end, Mapping) and end.get("fpga") == fpga:
                        record = {"role": role, **end, "iostandard": value.get("iostandard")}
                        records[str(end["p"]["port"])] = record
            yield from (records[key] for key in sorted(records))
        except json.JSONDecodeError as error:
            raise ValidationError("virtual binding entry is corrupt") from error
        finally:
            connection.close()

    def iter_adapter_port_fpgas(self) -> Iterator[str]:
        """Yield the complete FPGA population represented by adapter ports."""

        connection = _connect(self.store_path)
        try:
            has_relation = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') "
                "AND name='binding_adapter_ports'"
            ).fetchone()
            if has_relation is not None:
                for row in connection.execute(
                    "SELECT DISTINCT fpga FROM binding_adapter_ports "
                    "ORDER BY fpga"
                ):
                    yield str(row[0])
                return
            for row in connection.execute(
                "SELECT source_fpga AS fpga FROM binding_entries "
                "UNION SELECT sink_fpga AS fpga FROM binding_entries "
                "ORDER BY fpga"
            ):
                yield str(row[0])
        finally:
            connection.close()

    def iter_physical_contract_entries(
        self, fpga: str
    ) -> Iterator[Mapping[str, Any]]:
        """Yield independently verified compact fixed-I/O records.

        Phase 7 only consumes the physical pair and logical bus identity.  The
        complete hop relation remains authoritative in ``binding_entries``;
        each compact record carries only a cached count and canonical digest of
        its schedule-entry membership.  Recompute those values from the
        authoritative hop rows and merge them with the compact relation in
        physical-port order.  This keeps Python state bounded to one port while
        still rejecting a compact relation that has been tampered with and
        resealed at the file level.
        """

        if self.manifest['user_version']==2:
            from .transport_native import audit_transport
            self.bound_store_sources()
            if not hasattr(self,'_native_physical_audit'):
                self._native_physical_audit=audit_transport(self)
            for entry in self._native_physical_audit:
                if entry['fpga']==fpga:
                    yield entry['record']
            return

        connection = _connect(self.store_path)
        try:
            has_relation = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') "
                "AND name='binding_physical_ports'"
            ).fetchone()
            if has_relation is None:
                # Historical stores predate the compact relation.  Reconstruct
                # their contract explicitly for read compatibility.
                records: dict[str, dict[str, Any]] = {}
                for row in connection.execute(
                    "SELECT record_json FROM binding_entries "
                    "WHERE source_fpga=? OR sink_fpga=? ORDER BY schedule_entry",
                    (fpga, fpga),
                ):
                    value = json.loads(row[0])
                    if not isinstance(value, dict):
                        raise ValidationError("virtual binding entry is malformed")
                    for role, direction in (("source", "tx"), ("sink", "rx")):
                        end = value.get(role)
                        if not isinstance(end, Mapping) or end.get("fpga") != fpga:
                            continue
                        logical = re.fullmatch(
                            r"([a-zA-Z_][a-zA-Z0-9_]*)\[(\d+)\]",
                            str(end.get("logical_port", "")),
                        )
                        if logical is None:
                            raise ValidationError(
                                "virtual binding logical port is invalid"
                            )
                        identity = f"{direction}:{end['p']['port']}"
                        record = {
                            "id": identity,
                            "direction": direction,
                            "link": value["link"],
                            "wire": value["wire"],
                            "wire_id": value["wire_id"],
                            "bank": end["bank"],
                            "pair_id": end["pair_id"],
                            "iostandard": value["iostandard"],
                            "logical_port": logical.group(1),
                            "logical_bit": int(logical.group(2)),
                            "logical_port_width": end["logical_port_width"],
                            "p": dict(end["p"]),
                            "n": dict(end["n"]),
                            "schedule_entries": [],
                        }
                        previous = records.setdefault(identity, record)
                        comparable = {
                            key: item
                            for key, item in record.items()
                            if key != "schedule_entries"
                        }
                        if {
                            key: item
                            for key, item in previous.items()
                            if key != "schedule_entries"
                        } != comparable:
                            raise ValidationError(
                                "one physical differential pair has conflicting bindings"
                            )
                        previous["schedule_entries"].append(
                            str(value["schedule_entry"])
                        )
                for identity in sorted(records):
                    record = records[identity]
                    members = sorted(record.pop("schedule_entries"))
                    digest = hashlib.sha256()
                    for member in members:
                        digest.update(_canonical(member).encode("utf-8"))
                        digest.update(b"\n")
                    record["schedule_entry_count"] = len(members)
                    record["schedule_entries_sha256"] = digest.hexdigest()
                    yield record
                return

            compact_rows = iter(connection.execute(
                "SELECT record_json,schedule_entry_count,"
                "schedule_entries_sha256 FROM binding_physical_ports "
                "WHERE fpga=? ORDER BY p_port",
                (fpga,),
            ))
            # Expand both endpoint roles in SQL, then sort once by physical
            # port and schedule identity.  The cursor may contain H endpoint
            # memberships, but Python retains only the digest for the current
            # pair.  json_extract reads the authoritative binding entry rather
            # than any producer-provided field in binding_physical_ports.
            membership_rows = iter(connection.execute(
                """
                SELECT p_port,schedule_entry FROM (
                  SELECT json_extract(record_json,'$.source.p.port') AS p_port,
                         schedule_entry
                  FROM binding_entries WHERE source_fpga=?
                  UNION ALL
                  SELECT json_extract(record_json,'$.sink.p.port') AS p_port,
                         schedule_entry
                  FROM binding_entries WHERE sink_fpga=?
                )
                ORDER BY p_port,schedule_entry
                """,
                (fpga, fpga),
            ))
            member = next(membership_rows, None)
            for row in compact_rows:
                value = json.loads(row["record_json"])
                if not isinstance(value, dict):
                    raise ValidationError(
                        "virtual binding physical-port record is malformed"
                    )
                p = value.get("p")
                p_port = p.get("port") if isinstance(p, Mapping) else None
                if not isinstance(p_port, str) or not p_port:
                    raise ValidationError(
                        "virtual binding physical-port record is malformed"
                    )
                if member is not None and (
                    not isinstance(member["p_port"], str)
                    or not isinstance(member["schedule_entry"], str)
                ):
                    raise ValidationError(
                        "virtual binding physical-port membership is malformed"
                    )
                if member is not None and member["p_port"] < p_port:
                    raise ValidationError(
                        "virtual binding physical-port relation is incomplete"
                    )
                digest = hashlib.sha256()
                count = 0
                while member is not None and member["p_port"] == p_port:
                    digest.update(
                        _canonical(str(member["schedule_entry"])).encode("utf-8")
                    )
                    digest.update(b"\n")
                    count += 1
                    member = next(membership_rows, None)
                    if member is not None and (
                        not isinstance(member["p_port"], str)
                        or not isinstance(member["schedule_entry"], str)
                    ):
                        raise ValidationError(
                            "virtual binding physical-port membership is malformed"
                        )
                observed_digest = digest.hexdigest()
                cached_count = row["schedule_entry_count"]
                cached_digest = row["schedule_entries_sha256"]
                if (
                    count <= 0
                    or isinstance(cached_count, bool)
                    or not isinstance(cached_count, int)
                    or cached_count != count
                    or not isinstance(cached_digest, str)
                    or cached_digest != observed_digest
                ):
                    raise ValidationError(
                        "virtual binding physical-port membership disagrees "
                        "with binding entries"
                    )
                value["schedule_entry_count"] = count
                value["schedule_entries_sha256"] = observed_digest
                yield value
            if member is not None:
                raise ValidationError(
                    "virtual binding physical-port relation is incomplete"
                )
        except json.JSONDecodeError as error:
            raise ValidationError(
                "virtual binding physical-port record is corrupt"
            ) from error
        except sqlite3.OperationalError as error:
            raise ValidationError(
                "virtual binding physical-port membership is corrupt"
            ) from error
        finally:
            connection.close()


def read_transport(path: Path, *, materialize: bool = False) -> Mapping[str, Any]:
    value = _raw_manifest(path)
    if value.get("schema") == TRANSPORT_STORAGE_SCHEMA:
        view = TransportView(path)
        return view.materialize() if materialize else view
    from .io import read_json

    return read_json(path)


def read_virtual_binding(
    path: Path, *, materialize: bool = False
) -> Mapping[str, Any]:
    value = _raw_manifest(path)
    if value.get("schema") == VIRTUAL_BINDING_STORAGE_SCHEMA:
        view = VirtualBindingView(path)
        return view.materialize() if materialize else view
    from .io import read_json

    return read_json(path)


def read_lane_map(path: Path, *, materialize: bool = False) -> Mapping[str, Any]:
    value = _raw_manifest(path)
    if value.get("schema") == LANE_MAP_STORAGE_SCHEMA:
        view = LaneMapView(path)
        return view.materialize() if materialize else view
    from .io import read_json

    return read_json(path)




def _manifest(
    *,
    schema: str,
    logical_schema: str,
    artifact: str,
    header: Mapping[str, Any],
    sections: Mapping[str, Mapping[str, Any]],
    store_owner: VerifiedStoreFile,
    canonical_sha256: str | None = None,
) -> dict[str, Any]:
    store_owner.require_unchanged()
    return {
        "schema": schema,
        "logical_schema": logical_schema,
        "artifact": artifact,
        "application_id": TRANSPORT_APPLICATION_ID,
        "user_version": int(store_owner.source.schema.rsplit("v",1)[1]),
        "store_bytes": store_owner.bytes,
        "store_sha256": store_owner.sha256,
        "header": dict(header),
        "sections": dict(sections),
        "logical_sha256": _logical_digest(header, sections),
        **(
            {"canonical_sha256": canonical_sha256}
            if canonical_sha256 is not None
            else {}
        ),
    }


def _record_section(
    connection: sqlite3.Connection, query: str, parameters: tuple[Any, ...]
) -> dict[str, Any]:
    def records() -> Iterator[Mapping[str, Any]]:
        for row in connection.execute(query, parameters):
            value = json.loads(row[0])
            if not isinstance(value, dict):
                raise ValidationError("transport store contains a malformed record")
            yield value

    count, digest = _digest_records(records())
    return {"records": count, "sha256": digest}


def _signal_section(
    connection: sqlite3.Connection, fpga: str, kind: str
) -> dict[str, Any]:
    logical_kind = "source" if kind == "tx" else "shadow"

    def records() -> Iterator[Mapping[str, Any]]:
        for row in connection.execute(
            "SELECT seq,signal FROM signals WHERE fpga=? AND kind=? ORDER BY seq",
            (fpga, logical_kind),
        ):
            yield {"index": int(row[0]), "signal": row[1]}

    count, digest = _digest_records(records())
    return {"records": count, "sha256": digest}


def _anchor_relation_section(
    connection: sqlite3.Connection, fpga: str
) -> dict[str, Any]:
    """Seal normalized anchor headers and memberships with bounded memory."""

    digest = hashlib.sha256()
    count = 0
    for row in connection.execute(
        "SELECT id,record_json FROM anchors WHERE fpga=? ORDER BY id",
        (fpga,),
    ):
        digest.update(b"anchor\0")
        digest.update(row["record_json"].encode("utf-8"))
        digest.update(b"\n")
        count += 1
        for member in connection.execute(
            "SELECT id,slot FROM endpoints WHERE fpga=? AND id IN ("
            "SELECT e.id FROM endpoints e JOIN anchors a "
            "ON a.fpga=e.fpga AND a.link=e.link AND a.peer=e.peer "
            "AND a.kind=e.kind AND a.lane=e.lane WHERE a.id=?"
            ") ORDER BY id",
            (fpga, row["id"]),
        ):
            digest.update(b"member\0")
            digest.update(str(member["id"]).encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(member["slot"]).encode("ascii"))
            digest.update(b"\n")
    return {"records": count, "sha256": digest.hexdigest()}


def write_transport_storage(
    output_dir, assignment_index_path, schedule, platform, *, design, clocks,
    pin_plan=None, virtual_board=None, runtime=None, emit_rtl=False,
):
    from .transport_native import build_transport
    return build_transport(output_dir,assignment_index_path,schedule,platform,
        design=design,clocks=clocks,pin_plan=pin_plan,virtual_board=virtual_board,
        runtime=runtime,emit_rtl=emit_rtl)


def expand_transport_storage(
    value: Mapping[str, Any], path: Path
) -> dict[str, Any]:
    schema = value.get("schema")
    if schema == TRANSPORT_STORAGE_SCHEMA:
        return TransportView(path).materialize()
    if schema == LANE_MAP_STORAGE_SCHEMA:
        return LaneMapView(path).materialize()
    if schema == ANCHOR_STORAGE_SCHEMA:
        return AnchorView(path).materialize()
    if schema == VIRTUAL_BINDING_STORAGE_SCHEMA:
        return VirtualBindingView(path).materialize()
    raise ValidationError("unsupported transport storage manifest")
