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


TRANSPORT_STORAGE_SCHEMA = "emuflow.transport-storage/v1"
LANE_MAP_STORAGE_SCHEMA = "emuflow.logical-lane-map-storage/v1"
ANCHOR_STORAGE_SCHEMA = "emuflow.virtual-io-anchors-storage/v1"
VIRTUAL_BINDING_STORAGE_SCHEMA = "emuflow.virtual-board-binding-storage/v1"
VIRTUAL_BINDING_LOGICAL_SCHEMA = "emuflow.virtual-board-binding/v1"
TRANSPORT_APPLICATION_ID = 0x454D5558  # ASCII "EMUX"
TRANSPORT_USER_VERSION = 1
TRANSPORT_STORE_NAME = "transport.sqlite"


DDL = """
CREATE TABLE metadata(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE parts(
  part_index INTEGER PRIMARY KEY,
  fpga TEXT NOT NULL UNIQUE
);
CREATE TABLE endpoints(
  id TEXT PRIMARY KEY,
  fpga TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('tx','rx')),
  peer TEXT NOT NULL,
  signal TEXT NOT NULL,
  schedule_entry TEXT NOT NULL,
  demand TEXT NOT NULL,
  net TEXT NOT NULL,
  link TEXT NOT NULL,
  slot INTEGER NOT NULL,
  arrival_slot INTEGER NOT NULL,
  lane INTEGER NOT NULL,
  logical_lane INTEGER NOT NULL,
  record_json TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX endpoints_fpga_identity ON endpoints(fpga,id);
CREATE INDEX endpoints_fpga_kind_signal
  ON endpoints(fpga,kind,signal);
CREATE INDEX endpoints_anchor
  ON endpoints(fpga,link,peer,kind,lane,id);
CREATE TABLE signals(
  fpga TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('source','shadow')),
  seq INTEGER NOT NULL,
  signal TEXT NOT NULL,
  PRIMARY KEY(fpga,kind,seq),
  UNIQUE(fpga,kind,signal)
) WITHOUT ROWID;
CREATE TABLE lanes(
  id TEXT PRIMARY KEY,
  schedule_entry TEXT NOT NULL UNIQUE,
  demand TEXT NOT NULL,
  net TEXT NOT NULL,
  link TEXT NOT NULL,
  source_fpga TEXT NOT NULL,
  sink_fpga TEXT NOT NULL,
  slot INTEGER NOT NULL,
  lane INTEGER NOT NULL,
  logical_lane INTEGER NOT NULL,
  tx_endpoint TEXT NOT NULL UNIQUE,
  rx_endpoint TEXT NOT NULL UNIQUE,
  record_json TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX lanes_link_slot ON lanes(link,slot,lane);
CREATE TABLE anchors(
  id TEXT PRIMARY KEY,
  fpga TEXT NOT NULL,
  link TEXT NOT NULL,
  peer TEXT NOT NULL,
  kind TEXT NOT NULL,
  lane INTEGER NOT NULL,
  record_json TEXT NOT NULL,
  UNIQUE(fpga,link,peer,kind,lane)
) WITHOUT ROWID;
CREATE INDEX anchors_fpga_identity ON anchors(fpga,id);
CREATE TABLE binding_entries(
  schedule_entry TEXT PRIMARY KEY,
  link TEXT NOT NULL,
  slot INTEGER NOT NULL,
  wire INTEGER NOT NULL,
  source_fpga TEXT NOT NULL,
  sink_fpga TEXT NOT NULL,
  record_json TEXT NOT NULL,
  UNIQUE(link,slot,wire)
) WITHOUT ROWID;
CREATE INDEX binding_source ON binding_entries(source_fpga,schedule_entry);
CREATE INDEX binding_sink ON binding_entries(sink_fpga,schedule_entry);
CREATE TABLE binding_adapter_ports(
  fpga TEXT NOT NULL,
  p_port TEXT NOT NULL,
  n_port TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('source','sink')),
  logical_port TEXT NOT NULL,
  logical_port_width INTEGER NOT NULL,
  package_pin_p TEXT NOT NULL,
  package_pin_n TEXT NOT NULL,
  iostandard TEXT NOT NULL,
  PRIMARY KEY(fpga,p_port),
  UNIQUE(fpga,n_port)
) WITHOUT ROWID;
"""


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
        or manifest.get("user_version") != TRANSPORT_USER_VERSION
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
        f"file:{path.resolve()}?mode=ro&immutable=1", uri=True
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
    def __init__(self, manifest_path: Path):
        self.manifest_path = manifest_path.resolve()
        self.manifest = _raw_manifest(self.manifest_path)
        self._header: dict[str, Any] = {}
        self._sections: dict[str, Sequence[Mapping[str, Any]]] = {}

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
) -> tuple[dict[str, Any], Path]:
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
    except OSError as error:
        raise ValidationError("transport SQLite store is missing") from error
    if stat.st_size != manifest["store_bytes"]:
        raise ValidationError("transport SQLite store size disagrees")
    if file_sha256(store_path) != manifest["store_sha256"]:
        raise ValidationError("transport SQLite store digest disagrees")
    connection = _connect(store_path)
    try:
        if (
            connection.execute("PRAGMA application_id").fetchone()[0]
            != TRANSPORT_APPLICATION_ID
            or connection.execute("PRAGMA user_version").fetchone()[0]
            != TRANSPORT_USER_VERSION
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
    return manifest, store_path


class TransportView(_BaseView):
    def __init__(self, manifest_path: Path):
        super().__init__(manifest_path)
        manifest, store_path = _open_and_validate(
            self.manifest_path,
            schema=TRANSPORT_STORAGE_SCHEMA,
            logical_schema=TRANSPORT_ENDPOINTS_SCHEMA,
            sections={"source_signals", "shadow_signals", "endpoints"},
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
    def __init__(self, manifest_path: Path):
        super().__init__(manifest_path)
        manifest, store_path = _open_and_validate(
            self.manifest_path,
            schema=LANE_MAP_STORAGE_SCHEMA,
            logical_schema=LOGICAL_LANE_MAP_SCHEMA,
            sections={"entries"},
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
    def __init__(self, manifest_path: Path):
        super().__init__(manifest_path)
        manifest, store_path = _open_and_validate(
            self.manifest_path,
            schema=ANCHOR_STORAGE_SCHEMA,
            logical_schema=VIRTUAL_IO_ANCHORS_SCHEMA,
            sections={"anchors"},
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
    def __init__(self, manifest_path: Path):
        super().__init__(manifest_path)
        manifest, store_path = _open_and_validate(
            self.manifest_path,
            schema=VIRTUAL_BINDING_STORAGE_SCHEMA,
            logical_schema=VIRTUAL_BINDING_LOGICAL_SCHEMA,
            sections={"entries"},
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
                "SELECT 1 FROM sqlite_master WHERE type='table' "
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
                "SELECT 1 FROM sqlite_master WHERE type='table' "
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


def write_transport_systemverilog(
    path: Path,
    transport: Mapping[str, Any],
    platform: Platform,
) -> None:
    """Stream production transport RTL from indexed endpoint cursors."""

    rtl_rows = getattr(transport, "iter_rtl_endpoints", None)
    endpoint_groups = getattr(transport, "endpoint_groups", None)
    if rtl_rows is None or endpoint_groups is None:
        from .netlist import transport_to_systemverilog

        value = transport_to_systemverilog(transport, platform)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            return
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    fpga = transport["fpga"]
    links = {link.id: link for link in platform.links}
    groups = endpoint_groups()
    virtual_clocks = transport.get("virtual_clock_order", ["clk"])
    if (
        not isinstance(virtual_clocks, list)
        or not virtual_clocks
        or any(not isinstance(clock, str) or not clock for clock in virtual_clocks)
        or len(set(virtual_clocks)) != len(virtual_clocks)
    ):
        raise ValidationError("transport virtual clock order is invalid")
    ports = [
        "  input  logic fabric_clk",
        "  input  logic reset",
        "  input  logic links_ready",
        "  input  logic [SOURCE_COUNT-1:0] source_values",
        "  output logic [SHADOW_COUNT-1:0] shadow_values",
        f"  output logic [{len(virtual_clocks) - 1}:0] virtual_clock_enable",
        "  output logic [SLOT_BITS-1:0] slot_debug",
    ]
    bus_name: dict[tuple[str, str, str], str] = {}
    for link_id, peer, kinds in groups:
        width = links[link_id].transport_bits_per_cycle_per_direction
        for kind, direction in (("rx", "input  "), ("tx", "output ")):
            if kind not in kinds:
                continue
            name = transport_endpoint_port(
                {"kind": kind, "link": link_id, "peer": peer}
            )
            bus_name[(link_id, peer, kind)] = name
            ports.append(
                f"  {direction}logic [{width - 1}:0] {name}"
            )

    source_count = max(1, len(transport["source_signals"]))
    shadow_count = max(1, len(transport["shadow_signals"]))
    frame_slots = int(transport["frame_slots"])
    slot_bits = max(1, (frame_slots - 1).bit_length())
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")

    def line(stream: Any, value: str = "") -> None:
        stream.write(value)
        stream.write("\n")

    try:
        with temporary.open("w", encoding="utf-8") as stream:
            line(stream, f"module emuflow_transport_{_sv_name(fpga)} #(")
            line(stream, f"  parameter integer FRAME_SLOTS = {frame_slots},")
            line(stream, f"  parameter integer SLOT_BITS = {slot_bits},")
            line(stream, f"  parameter integer SOURCE_COUNT = {source_count},")
            line(stream, f"  parameter integer SHADOW_COUNT = {shadow_count}")
            line(stream, ") (")
            line(stream, ",\n".join(ports))
            line(stream, ");")
            line(stream)
            line(stream, "  logic [SLOT_BITS-1:0] slot;")
            if transport.get("cut_mode") == "static-exact-combinational":
                line(stream, '  (* KEEP = "yes", DONT_TOUCH = "yes" *)')
            line(stream, "  logic [SHADOW_COUNT-1:0] shadow_regs;")
            line(stream, "  emuflow_virtual_runtime_controller #(")
            line(stream, "    .FRAME_SLOTS(FRAME_SLOTS),")
            line(stream, "    .SLOT_BITS(SLOT_BITS)")
            line(stream, "  ) runtime_controller (")
            line(stream, "    .fabric_clk(fabric_clk),")
            line(stream, "    .reset(reset),")
            line(stream, "    .links_ready(links_ready),")
            line(stream, "    .virtual_clock_enable(virtual_clock_enable),")
            line(stream, "    .event_index_debug(),")
            line(stream, "    .slot(slot)")
            line(stream, "  );")
            line(stream, "  assign slot_debug = slot;")
            line(stream, "  assign shadow_values = shadow_regs;")
            line(stream)

            tx_groups = [
                (link_id, peer)
                for link_id, peer, kinds in groups
                if "tx" in kinds
            ]
            if tx_groups:
                line(stream, "  always @* begin")
                for link_id, peer in tx_groups:
                    line(
                        stream,
                        f"    {bus_name[(link_id, peer, 'tx')]} = '0;",
                    )
                line(stream, "    case (slot)")
                active_slot = None
                for slot, endpoint, signal_index in rtl_rows("tx"):
                    if slot != active_slot:
                        if active_slot is not None:
                            line(stream, "      end")
                        active_slot = slot
                        line(stream, f"      {slot}: begin")
                    expression = (
                        f"source_values[{signal_index}]"
                        if endpoint["signal"].startswith("net:")
                        else f"shadow_regs[{signal_index}]"
                    )
                    name = bus_name[
                        (endpoint["link"], endpoint["peer"], "tx")
                    ]
                    line(
                        stream,
                        f"        {name}[{endpoint['lane']}] = {expression};",
                    )
                if active_slot is not None:
                    line(stream, "      end")
                line(stream, "      default: begin end")
                line(stream, "    endcase")
                line(stream, "  end")
                line(stream)

            line(stream, "  always_ff @(posedge fabric_clk) begin")
            line(stream, "    if (reset) begin")
            line(stream, "      shadow_regs <= '0;")
            line(stream, "    end else begin")
            line(stream, "      case (slot)")
            active_slot = None
            for slot, endpoint, signal_index in rtl_rows("rx"):
                if slot != active_slot:
                    if active_slot is not None:
                        line(stream, "        end")
                    active_slot = slot
                    line(stream, f"        {slot}: begin")
                name = bus_name[(endpoint["link"], endpoint["peer"], "rx")]
                line(
                    stream,
                    f"          shadow_regs[{signal_index}] "
                    f"<= {name}[{endpoint['lane']}];",
                )
            if active_slot is not None:
                line(stream, "        end")
            line(stream, "        default: begin end")
            line(stream, "      endcase")
            line(stream, "    end")
            line(stream, "  end")
            line(stream, "endmodule")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _manifest(
    *,
    schema: str,
    logical_schema: str,
    artifact: str,
    header: Mapping[str, Any],
    sections: Mapping[str, Mapping[str, Any]],
    store_path: Path,
    canonical_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": schema,
        "logical_schema": logical_schema,
        "artifact": artifact,
        "application_id": TRANSPORT_APPLICATION_ID,
        "user_version": TRANSPORT_USER_VERSION,
        "store_bytes": store_path.stat().st_size,
        "store_sha256": file_sha256(store_path),
        "header": dict(header),
        "sections": dict(sections),
        "logical_sha256": _logical_digest(header, sections),
        **(
            {"canonical_sha256": canonical_sha256}
            if canonical_sha256 is not None else {}
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
    output_dir: Path,
    assignment_index_path: Path,
    schedule: Mapping[str, Any],
    platform: Platform,
    *,
    design: Mapping[str, Any],
    clocks: Sequence[Mapping[str, Any]],
    pin_plan: Mapping[str, Any] | None = None,
    virtual_board: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Publish Phase 6 transport relations without hop-sized Python lists."""

    if schedule.get("schema") != TDM_SCHEDULE_SCHEMA:
        raise ValidationError("schedule has an unsupported schema")
    design_name = design.get("name")
    if schedule.get("design") != design_name:
        raise ValidationError("schedule design disagrees with EmuIR")
    if schedule.get("platform") != platform.name:
        raise ValidationError("schedule platform disagrees with BoardDB")

    assignment = open_phase3_assignment_index(assignment_index_path)
    try:
        assignment_metadata = {
            row["key"]: json.loads(row["value"])
            for row in assignment.execute("SELECT key,value FROM metadata")
        }
        parts = list(
            assignment.execute(
                "SELECT part_index,fpga FROM parts ORDER BY part_index"
            )
        )
    finally:
        assignment.close()
    if assignment_metadata.get("design") != design_name:
        raise ValidationError("assignment index design disagrees with EmuIR")
    if assignment_metadata.get("platform") != platform.name:
        raise ValidationError("assignment index platform disagrees with BoardDB")
    fpga_ids = [str(row["fpga"]) for row in parts]
    if set(fpga_ids) != {fpga.id for fpga in platform.fpgas}:
        raise ValidationError("assignment partitions disagree with BoardDB")

    exact_digest = assignment_metadata.get("semantic_contract_sha256")
    exact_mode = exact_digest is not None
    if exact_mode and (
        not is_sampled_virtual_wire_schedule(schedule)
        or schedule.get("semantic_contract_sha256") != exact_digest
    ):
        raise ValidationError(
            "exact transport inputs do not share one semantic contract"
        )
    exact_storage = assignment_metadata.get("semantic_contract_binary")

    physical_lane_by_entry: dict[str, int] = {}
    pin_lane_iterator: Iterator[tuple[str, int, int]] | None = None
    if pin_plan is not None:
        if pin_plan.get("schema") != "emuflow.placement-aware-pin-plan/v1":
            raise ValidationError("pin plan has an unsupported schema")
        lane_records = getattr(pin_plan, "iter_lane_records", None)
        if lane_records is not None:
            pin_lane_iterator = iter(lane_records())
        else:
            physical_lane_by_entry = {
                item["schedule_entry"]: item["physical_lane"]
                for item in pin_plan.get("entries", [])
            }
            if len(physical_lane_by_entry) != len(pin_plan.get("entries", [])):
                raise ValidationError("pin plan repeats a schedule entry")

    binding_wire_by_key: dict[tuple[str, int], Mapping[str, Any]] = {}
    binding_links = {link.id: link for link in platform.links}
    binding_direction_by_wire: dict[str, tuple[str, str]] = {}
    binding_pin_ports: dict[tuple[str, str], str] = {}
    binding_port_pins: dict[tuple[str, str], tuple[str, str]] = {}
    if virtual_board is not None:
        if (
            virtual_board.get("schema") != "emuflow.virtual-board-wiring/v1"
            or virtual_board.get("platform") != platform.name
            or not isinstance(virtual_board.get("wires"), Sequence)
        ):
            raise ValidationError("virtual board wiring is invalid")
        binding_wire_by_key = {
            (wire["link"], wire["wire"]): wire
            for wire in virtual_board["wires"]
        }

    def pin_port_name(value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_]", "_", value)

    output_dir.mkdir(parents=True, exist_ok=True)
    store_path = output_dir / TRANSPORT_STORE_NAME
    temporary = store_path.with_name(
        f".{store_path.name}.{uuid.uuid4().hex}.tmp"
    )
    # The assignment sidecar is attached below through a read-only immutable
    # ``file:`` URI.  URI handling is a per-connection SQLite flag; without it
    # older production builds interpret the complete URI as a literal filename
    # and fail after Phase 5 even though the sidecar exists.
    connection = sqlite3.connect(temporary, uri=True)
    connection.row_factory = sqlite3.Row
    seen_pin_entries: set[str] = set()
    entry_count = 0
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute("PRAGMA cache_size=-131072")
        connection.execute(f"PRAGMA application_id={TRANSPORT_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={TRANSPORT_USER_VERSION}")
        connection.executescript(DDL)
        metadata = {
            "design": design_name,
            "platform": platform.name,
            "frame_slots": schedule["metrics"]["frame_slots"],
            "virtual_clock_order": sorted(item["id"] for item in clocks) or ["clk"],
            "semantic_contract_sha256": exact_digest,
            "semantic_contract_storage": exact_storage,
        }
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES (?,?)",
            ((key, _canonical(value)) for key, value in sorted(metadata.items())),
        )
        connection.executemany(
            "INSERT INTO parts(part_index,fpga) VALUES (?,?)",
            ((int(row["part_index"]), str(row["fpga"])) for row in parts),
        )
        connection.execute(
            "ATTACH DATABASE ? AS assignment",
            (f"file:{assignment_index_path.resolve()}?mode=ro&immutable=1",),
        )

        typed_records = getattr(schedule, "iter_transport_records", None)
        joined_records = getattr(schedule, "iter_entry_route_records", None)
        if typed_records is not None:
            entry_routes = (
                (record, None)
                for record in typed_records(
                    identity_order=pin_lane_iterator is not None
                )
            )
        elif joined_records is None:
            routes = schedule.get("routes")
            entries = schedule.get("entries")
            if not isinstance(routes, Sequence) or not isinstance(entries, Sequence):
                raise ValidationError("Phase 6 schedule lacks route metadata")
            route_by_demand = {route["id"]: route for route in routes}
            entry_routes = (
                (entry, route_by_demand.get(entry["demand"]))
                for entry in sorted(
                    entries,
                    key=lambda item: (
                        len(item["id"]), item["id"]
                    ) if pin_lane_iterator is not None else item["id"],
                )
            )
        else:
            entry_routes = joined_records()

        endpoint_rows: list[tuple[Any, ...]] = []
        lane_rows: list[tuple[Any, ...]] = []
        binding_rows: list[tuple[Any, ...]] = []
        binding_adapter_rows: list[tuple[Any, ...]] = []
        seen_binding_adapter_ports: set[tuple[str, str]] = set()

        def flush() -> None:
            if endpoint_rows:
                connection.executemany(
                    "INSERT INTO endpoints VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    endpoint_rows,
                )
                endpoint_rows.clear()
            if lane_rows:
                connection.executemany(
                    "INSERT INTO lanes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    lane_rows,
                )
                lane_rows.clear()
            if binding_rows:
                connection.executemany(
                    "INSERT INTO binding_entries VALUES (?,?,?,?,?,?,?)",
                    binding_rows,
                )
                binding_rows.clear()
            if binding_adapter_rows:
                connection.executemany(
                    "INSERT INTO binding_adapter_ports VALUES (?,?,?,?,?,?,?,?,?)",
                    binding_adapter_rows,
                )
                binding_adapter_rows.clear()

        for entry, route in entry_routes:
            if typed_records is not None:
                entry_id = entry.entry_id
                demand = entry.demand
                net = entry.net
                link = entry.link
                source = entry.source
                sink = entry.sink
                slot = entry.slot
                arrival_slot = entry.arrival_slot
                logical_lane = entry.lane
                tdm_ratio = entry.tdm_ratio
                route_source = entry.route_source
            elif route is None:
                raise ValidationError(
                    f"schedule entry {entry['id']!r} has no route metadata"
                )
            else:
                entry_id = entry["id"]
                demand = entry["demand"]
                net = entry["net"]
                link = entry["link"]
                source = entry.get("from")
                sink = entry.get("to")
                slot = entry["slot"]
                arrival_slot = entry["arrival_slot"]
                logical_lane = entry["lane"]
                tdm_ratio = entry.get("tdm_ratio")
                route_source = route["source"]
            if source not in fpga_ids or sink not in fpga_ids:
                raise ValidationError("schedule entry references an unknown FPGA")
            if pin_lane_iterator is not None:
                pin_record = next(pin_lane_iterator, None)
                if (
                    pin_record is None
                    or pin_record[0] != entry_id
                    or pin_record[2] != logical_lane
                ):
                    raise ValidationError(
                        "pin plan must cover schedule entries exactly"
                    )
                lane = pin_record[1]
            else:
                lane = physical_lane_by_entry.get(entry_id, logical_lane)
            if pin_plan is not None and pin_lane_iterator is None:
                if entry_id not in physical_lane_by_entry:
                    raise ValidationError(
                        "pin plan must cover every schedule entry exactly"
                    )
                seen_pin_entries.add(entry_id)
            tx_id = _endpoint_id("tx", entry_id)
            rx_id = _endpoint_id("rx", entry_id)
            tx_signal = (
                f"net:{net}"
                if source == route_source
                else f"shadow:{demand}:{source}"
            )
            rx_signal = f"shadow:{demand}:{sink}"
            common = {
                "schedule_entry": entry_id,
                "demand": demand,
                "net": net,
                "link": link,
                "slot": slot,
                "arrival_slot": arrival_slot,
                "lane": lane,
                "logical_lane": logical_lane,
            }
            tx_exact: dict[str, Any] = {}
            rx_exact: dict[str, Any] = {}
            if exact_mode:
                tx_exact = {
                    "boundary_identity": f"static_exact:tx:{entry_id}",
                    "semantic_contract_sha256": exact_digest,
                    "source_role": (
                        "cut_source"
                        if source == route_source
                        else "route_tree_relay"
                    ),
                    "preserve": True,
                }
                rx_exact = {
                    "boundary_identity": f"static_exact:rx:{entry_id}",
                    "semantic_contract_sha256": exact_digest,
                    "preserve": True,
                }
            tx = {
                "id": tx_id,
                "kind": "tx",
                "fpga": source,
                "peer": sink,
                "signal": tx_signal,
                **common,
                **tx_exact,
            }
            rx = {
                "id": rx_id,
                "kind": "rx",
                "fpga": sink,
                "peer": source,
                "signal": rx_signal,
                **common,
                **rx_exact,
            }
            for endpoint in (tx, rx):
                endpoint_rows.append(
                    (
                        endpoint["id"], endpoint["fpga"], endpoint["kind"],
                        endpoint["peer"], endpoint["signal"], entry_id,
                        demand, net, link, slot, arrival_slot, lane,
                        logical_lane, _canonical(endpoint),
                    )
                )
            lane_record = {
                "id": f"lane_{entry_id}",
                "schedule_entry": entry_id,
                "demand": demand,
                "net": net,
                "link": link,
                "from": source,
                "to": sink,
                "slot": slot,
                "lane": lane,
                "logical_lane": logical_lane,
                "tx_endpoint": tx_id,
                "rx_endpoint": rx_id,
            }
            lane_rows.append(
                (
                    lane_record["id"], entry_id, demand, net,
                    link, source, sink, slot, lane,
                    logical_lane, tx_id, rx_id, _canonical(lane_record),
                )
            )
            if virtual_board is not None:
                wire = binding_wire_by_key.get((link, lane))
                if wire is None:
                    raise ValidationError(
                        "scheduled wire has no virtual board connection"
                    )
                direction = (source, sink)
                if (
                    binding_direction_by_wire.setdefault(wire["id"], direction)
                    != direction
                ):
                    raise ValidationError(
                        "one physical wire cannot change direction within "
                        "the schedule"
                    )
                pins = {pin["fpga"]: pin for pin in wire["endpoints"]}
                bound: dict[str, Any] = {}
                for role, endpoint in (("source", tx), ("sink", rx)):
                    kind = endpoint["kind"]
                    endpoint_fpga = endpoint["fpga"]
                    logical_port = (
                        f"{transport_endpoint_port(endpoint)}[{lane}]"
                    )
                    pair = pins.get(endpoint_fpga)
                    if pair is None:
                        raise ValidationError(
                            "virtual board wire omits a scheduled FPGA"
                        )
                    bound_end = {
                        key: pair[key] for key in ("fpga", "bank", "pair_id")
                    }
                    bound_end["logical_port"] = logical_port
                    bound_end["logical_port_width"] = binding_links[
                        link
                    ].transport_bits_per_cycle_per_direction
                    for polarity in ("p", "n"):
                        pin = pair[polarity]
                        port = (
                            f"emuflow_io_{kind}_{pin_port_name(link)}_"
                            f"wire{wire['wire']}_{polarity}"
                        )
                        identity = (endpoint_fpga, pin["package_pin"])
                        if binding_pin_ports.setdefault(identity, port) != port:
                            raise ValidationError(
                                "package pin is assigned to more than one RTL port"
                            )
                        if (
                            binding_port_pins.setdefault(
                                (endpoint_fpga, port), identity
                            )
                            != identity
                        ):
                            raise ValidationError(
                                "physical RTL port name collides after "
                                "identifier normalization"
                            )
                        bound_end[polarity] = {**pin, "port": port}
                    bound[role] = bound_end
                    adapter_identity = (endpoint_fpga, bound_end["p"]["port"])
                    if adapter_identity not in seen_binding_adapter_ports:
                        seen_binding_adapter_ports.add(adapter_identity)
                        binding_adapter_rows.append(
                            (
                                endpoint_fpga,
                                bound_end["p"]["port"],
                                bound_end["n"]["port"],
                                role,
                                logical_port,
                                bound_end["logical_port_width"],
                                bound_end["p"]["package_pin"],
                                bound_end["n"]["package_pin"],
                                virtual_board["assumptions"]["iostandard"],
                            )
                        )
                binding_record = {
                    "schedule_entry": entry_id,
                    "net": net,
                    "slot": slot,
                    "arrival_slot": arrival_slot,
                    "tdm_ratio": tdm_ratio,
                    "wire_id": wire["id"],
                    "wire": wire["wire"],
                    "link": wire["link"],
                    "bank_group": wire["bank_group"],
                    "cable": wire["cable"],
                    "iostandard": virtual_board["assumptions"]["iostandard"],
                    **bound,
                }
                binding_rows.append(
                    (
                        entry_id, link, slot, wire["wire"],
                        source, sink, _canonical(binding_record),
                    )
                )
            entry_count += 1
            if len(lane_rows) >= 4096:
                flush()
        flush()
        if pin_lane_iterator is not None and next(pin_lane_iterator, None) is not None:
            raise ValidationError("pin plan must cover schedule entries exactly")
        if (
            pin_plan is not None
            and pin_lane_iterator is None
            and set(physical_lane_by_entry) != seen_pin_entries
        ):
            raise ValidationError("pin plan must cover every schedule entry exactly")

        if exact_mode:
            missing = connection.execute(
                "SELECT e.net FROM endpoints e "
                "LEFT JOIN assignment.cut_sinks c ON c.net_id=e.net "
                "WHERE c.net_id IS NULL LIMIT 1"
            ).fetchone()
            if missing is not None:
                raise ValidationError("exact schedule references an unknown cut node")

        for fpga in fpga_ids:
            for kind, endpoint_kind, predicate in (
                ("source", "tx", "AND substr(signal,1,4)='net:'"),
                ("shadow", "rx", ""),
            ):
                rows = connection.execute(
                    "SELECT DISTINCT signal FROM endpoints "
                    "WHERE fpga=? AND kind=? " + predicate + " ORDER BY signal",
                    (fpga, endpoint_kind),
                )
                connection.executemany(
                    "INSERT INTO signals(fpga,kind,seq,signal) VALUES (?,?,?,?)",
                    (
                        (fpga, kind, index, str(row[0]))
                        for index, row in enumerate(rows)
                    ),
                )

        links = {link.id: link for link in platform.links}
        for row in connection.execute(
            "SELECT fpga,link,peer,kind,lane FROM endpoints "
            "GROUP BY fpga,link,peer,kind,lane "
            "ORDER BY fpga,link,peer,kind,lane"
        ):
            board_link = links.get(row["link"])
            if board_link is None:
                raise ValidationError("transport endpoint references an unknown link")
            lane = int(row["lane"])
            if board_link.mode == "serial":
                required_fields = [
                    "package_pin_p", "package_pin_n", "connector",
                    "transceiver_site",
                ]
                physical_lane = lane // board_link.payload_bits_per_lane_per_cycle
                bit_within_physical_lane = (
                    lane % board_link.payload_bits_per_lane_per_cycle
                )
            else:
                required_fields = ["package_pin", "bank", "iostandard"]
                physical_lane = lane
                bit_within_physical_lane = 0
            record = {
                "id": f"{row['link']}:{row['fpga']}:{row['kind']}:{lane}",
                "link": row["link"],
                "peer": row["peer"],
                "direction": row["kind"],
                "logical_lane": lane,
                "physical_lane": physical_lane,
                "bit_within_physical_lane": bit_within_physical_lane,
                "placement_class": "virtual_link_io_region",
                "binding_status": "unbound",
                "required_hardware_binding_fields": required_fields,
            }
            connection.execute(
                "INSERT INTO anchors VALUES (?,?,?,?,?,?,?)",
                (
                    record["id"], row["fpga"], row["link"], row["peer"],
                    row["kind"], lane, _canonical(record),
                ),
            )
        connection.commit()

        lane_header = {
            "schema": LOGICAL_LANE_MAP_SCHEMA,
            "design": design_name,
            "platform": platform.name,
            "frame_slots": schedule["metrics"]["frame_slots"],
            "binding_status": "logical_only",
            **(
                {
                    "cut_mode": "static-exact-combinational",
                    "semantic_contract_sha256": exact_digest,
                }
                if exact_mode else {}
            ),
        }
        lane_sections = {
            "entries": _record_section(
                connection, "SELECT record_json FROM lanes ORDER BY id", ()
            )
        }
        lane_canonical_sha256 = _canonical_document_digest(
            lane_header,
            {
                "entries": (
                    str(row[0])
                    for row in connection.execute(
                        "SELECT record_json FROM lanes ORDER BY id"
                    )
                )
            },
        )
        binding_header = None
        binding_sections = None
        binding_canonical_sha256 = None
        if virtual_board is not None:
            binding_header = {
                "schema": VIRTUAL_BINDING_LOGICAL_SCHEMA,
                "platform": platform.name,
                "design": design_name,
                "qualification": "virtual_board_real_device_pins",
                "status": "pass",
                "integration_status": (
                    "differential_data_constraints_and_adapter_generated"
                ),
                "physical_backend_status": "phase7_consumption_required",
                "board_services_status": "clock_reset_and_control_pins_not_bound",
                "io_adapter_status": "phase7_top_level_integration_required",
                "board_sha256": hashlib.sha256(
                    _canonical(virtual_board).encode("utf-8")
                ).hexdigest(),
                "schedule_sha256": canonical_tdm_schedule_sha256(schedule),
                "lane_map_sha256": lane_canonical_sha256,
                "metrics": {
                    "scheduled_hops": entry_count,
                    "used_differential_pairs": len(binding_direction_by_wire),
                    "bound_data_ports": len(binding_pin_ports),
                    "total_differential_pairs": len(virtual_board["wires"]),
                },
            }
            binding_sections = {
                "entries": _record_section(
                    connection,
                    "SELECT record_json FROM binding_entries "
                    "ORDER BY schedule_entry",
                    (),
                )
            }
            binding_canonical_sha256 = _canonical_document_digest(
                binding_header,
                {
                    "entries": (
                        str(row[0])
                        for row in connection.execute(
                            "SELECT record_json FROM binding_entries "
                            "ORDER BY schedule_entry"
                        )
                    )
                },
            )
        transport_headers: dict[str, dict[str, Any]] = {}
        transport_sections: dict[str, dict[str, dict[str, Any]]] = {}
        anchor_headers: dict[str, dict[str, Any]] = {}
        anchor_sections: dict[str, dict[str, dict[str, Any]]] = {}
        clock_order = sorted(item["id"] for item in clocks) or ["clk"]
        parts_by_id = {fpga.id: fpga for fpga in platform.fpgas}
        for fpga in fpga_ids:
            transport_headers[fpga] = {
                "schema": TRANSPORT_ENDPOINTS_SCHEMA,
                "design": design_name,
                "platform": platform.name,
                "fpga": fpga,
                "frame_slots": schedule["metrics"]["frame_slots"],
                "virtual_clock_order": clock_order,
                **(
                    {
                        "cut_mode": "static-exact-combinational",
                        "semantic_contract_sha256": exact_digest,
                        **(
                            {"semantic_contract_storage": exact_storage}
                            if isinstance(exact_storage, Mapping) else {}
                        ),
                        "shadow_reset_value": 0,
                        "startup_policy": (
                            "first-post-reset-frame-current-arrivals"
                        ),
                    }
                    if exact_mode else {}
                ),
            }
            transport_sections[fpga] = {
                "source_signals": _signal_section(connection, fpga, "tx"),
                "shadow_signals": _signal_section(connection, fpga, "rx"),
                "endpoints": _record_section(
                    connection,
                    "SELECT record_json FROM endpoints WHERE fpga=? ORDER BY id",
                    (fpga,),
                ),
            }
            required_fields = sorted(
                {
                    field
                    for record in connection.execute(
                        "SELECT record_json FROM anchors WHERE fpga=? ORDER BY id",
                        (fpga,),
                    )
                    for field in json.loads(record[0])[
                        "required_hardware_binding_fields"
                    ]
                }
            )
            anchor_headers[fpga] = {
                "schema": VIRTUAL_IO_ANCHORS_SCHEMA,
                "platform": platform.name,
                "fpga": fpga,
                "part": parts_by_id[fpga].part,
                "required_hardware_binding_fields": required_fields,
            }
            anchor_sections[fpga] = {
                "anchors": _anchor_relation_section(connection, fpga)
            }
        connection.close()
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, store_path)
    except sqlite3.IntegrityError as error:
        try:
            connection.close()
        finally:
            temporary.unlink(missing_ok=True)
        raise ValidationError(
            "transport storage contains duplicate identities"
        ) from error
    except BaseException:
        try:
            connection.close()
        finally:
            temporary.unlink(missing_ok=True)
        raise

    lane_manifest = _manifest(
        schema=LANE_MAP_STORAGE_SCHEMA,
        logical_schema=LOGICAL_LANE_MAP_SCHEMA,
        artifact=TRANSPORT_STORE_NAME,
        header=lane_header,
        sections=lane_sections,
        store_path=store_path,
        canonical_sha256=lane_canonical_sha256,
    )
    write_json(output_dir / "lane_map.json", lane_manifest, compact=True)
    binding_manifest = None
    if binding_header is not None and binding_sections is not None:
        binding_manifest = _manifest(
            schema=VIRTUAL_BINDING_STORAGE_SCHEMA,
            logical_schema=VIRTUAL_BINDING_LOGICAL_SCHEMA,
            artifact=TRANSPORT_STORE_NAME,
            header=binding_header,
            sections=binding_sections,
            store_path=store_path,
            canonical_sha256=binding_canonical_sha256,
        )
        write_json(
            output_dir / "virtual_board_binding.json",
            binding_manifest,
            compact=True,
        )
    for fpga in fpga_ids:
        root = output_dir / fpga
        write_json(
            root / "transport.json",
            _manifest(
                schema=TRANSPORT_STORAGE_SCHEMA,
                logical_schema=TRANSPORT_ENDPOINTS_SCHEMA,
                artifact=f"../{TRANSPORT_STORE_NAME}",
                header=transport_headers[fpga],
                sections=transport_sections[fpga],
                store_path=store_path,
            ),
            compact=True,
        )
        write_json(
            root / "virtual_anchors.json",
            _manifest(
                schema=ANCHOR_STORAGE_SCHEMA,
                logical_schema=VIRTUAL_IO_ANCHORS_SCHEMA,
                artifact=f"../{TRANSPORT_STORE_NAME}",
                header=anchor_headers[fpga],
                sections=anchor_sections[fpga],
                store_path=store_path,
            ),
            compact=True,
        )

    transports = {
        fpga: TransportView(output_dir / fpga / "transport.json")
        for fpga in fpga_ids
    }
    anchors = {
        fpga: AnchorView(output_dir / fpga / "virtual_anchors.json")
        for fpga in fpga_ids
    }
    return {
        "lane_map": LaneMapView(output_dir / "lane_map.json"),
        "transports": transports,
        "anchors": anchors,
        "binding": (
            VirtualBindingView(output_dir / "virtual_board_binding.json")
            if binding_manifest is not None else None
        ),
        "summary": {
            "schema": TRANSPORT_STORAGE_SCHEMA,
            "artifact": TRANSPORT_STORE_NAME,
            "bytes": store_path.stat().st_size,
            "sha256": file_sha256(store_path),
            "scheduled_hops": entry_count,
            "transport_endpoints": 2 * entry_count,
            "lane_entries": entry_count,
            "virtual_binding_entries": (
                entry_count if virtual_board is not None else 0
            ),
            "anchors": sum(len(value["anchors"]) for value in anchors.values()),
            "dataflow": "indexed-lazy-sqlite-v1",
        },
    }


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
