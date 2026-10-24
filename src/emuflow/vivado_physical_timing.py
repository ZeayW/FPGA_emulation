"""Indexed, source-bound timing handoffs for the current Vivado backend."""
from __future__ import annotations

import json
import math
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .boundary_identity_storage import read_boundary_identity_storage
from .boundary_timing import boundary_timing_header, boundary_timing_record, validate_boundary_timing_database
from .boundary_timing_storage import write_boundary_timing_storage
from .errors import ValidationError
from .timing_disposition import OPTIMIZED_AWAY, validate_optimization_record, require_optimization_proof
from .indexed_physical_ir import IndexedPhysicalIR
from .local_path_timing import LOCAL_PATH_IDENTITY_SCHEMA_V2, local_path_timing_header, validate_local_path_timing
from .local_path_timing_storage import write_local_path_timing_storage
from .logic_segment_timing import VIVADO_LOGIC_SEGMENT_QUERY_HEADER, VIVADO_LOGIC_SEGMENT_TIMING_HEADER, _vivado_object


def _hex(text: str) -> str:
    return text.encode("utf-8").hex()


def write_vivado_local_query(
    merged_ir: Path, fpga: str, query_path: Path, identity_path: Path, prepared: Any,
) -> dict[str, Any]:
    """Project the original same-FPGA path population into one scratch owner."""
    connection = sqlite3.connect(identity_path)
    connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    connection.execute("CREATE TABLE paths(id TEXT PRIMARY KEY,record_json TEXT NOT NULL)")
    count = 0
    try:
        with IndexedPhysicalIR(merged_ir) as ir, query_path.open("w", encoding="utf-8") as stream:
            stream.write(VIVADO_LOGIC_SEGMENT_QUERY_HEADER + "\n")
            for source in prepared.local_paths_by_fpga.get(fpga, ()):
                start_kind, start = _vivado_object(ir, source["startpoint"], ir.pins, ir.instances)
                end_kind, end = _vivado_object(ir, source["endpoint"], ir.pins, ir.instances)
                record = {
                    "id": source["id"], "kind": "local", "fpga": fpga,
                    "clock_domain": source["clock_domain"],
                    "clock_period_ns": float(source["clock_period_ns"]),
                    "required_time_ns": float(source["fixed_delay_ns"]) + float(source["slack_ns"]),
                    "start_pin": start, "end_pin": end,
                    "measurement": "endpoint-longest-path-fallback", "path_pins": [],
                }
                connection.execute("INSERT INTO paths VALUES (?,?)", (record["id"], json.dumps(record, separators=(",", ":"))))
                stream.write("\t".join((_hex(record["id"]), "local", start_kind, _hex(start), end_kind, _hex(end), "", "")) + "\n")
                count += 1
        header = {
            "schema": LOCAL_PATH_IDENTITY_SCHEMA_V2, "status": "pass",
            "design": prepared.assignment["design"], "fpga": fpga,
            "source": dict(prepared.source_manifest), "coverage": {"local_paths": count},
        }
        connection.execute("INSERT INTO metadata VALUES ('header',?)", (json.dumps(header),))
        connection.commit()
    finally:
        connection.close()
    return {"status": "pass", "fpga": fpga, "local_paths": count,
            "query": str(query_path), "identity": str(identity_path)}


def import_vivado_local_timing(input_path: Path, identity_path: Path, output_path: Path, *, optimization_proofs=None) -> dict[str, Any]:
    """Require every original local path exactly once, without a sampled PathDB."""
    with tempfile.TemporaryDirectory(prefix=".vivado-local-", dir=output_path.parent) as temporary:
        connection = sqlite3.connect(Path(temporary) / "measurements.sqlite", uri=True)
        try:
            connection.execute("ATTACH DATABASE ? AS identity", (f"file:{identity_path.resolve()}?mode=ro",))
            header = json.loads(connection.execute("SELECT value FROM identity.metadata WHERE key='header'").fetchone()[0])
            connection.execute("CREATE TABLE measurements(id TEXT PRIMARY KEY,delay REAL,optimization_source TEXT)")
            with input_path.open(encoding="utf-8") as stream:
                if next(stream, "").rstrip("\n") != VIVADO_LOGIC_SEGMENT_TIMING_HEADER:
                    raise ValidationError("Vivado local timing header is invalid")
                for line in stream:
                    if not line.strip():
                        continue
                    fields = line.rstrip("\n").split("\t")
                    try:
                        identity = bytes.fromhex(fields[0]).decode("utf-8")
                        eliminated = fields[5].startswith(OPTIMIZED_AWAY+":")
                        proof_source = fields[5].split(":",1)[1] if eliminated else None
                        delay = None if eliminated else float(fields[2])
                        row = connection.execute("SELECT record_json FROM identity.paths WHERE id=?", (identity,)).fetchone()
                        record = json.loads(row[0]) if row else None
                        valid = (len(fields) == 8 and fields[1] == "local" and record is not None
                                 and bytes.fromhex(fields[3]).decode("utf-8") == record["start_pin"]
                                 and bytes.fromhex(fields[4]).decode("utf-8") == record["end_pin"]
                                 and ((eliminated and not fields[2] and not fields[6] and not fields[7]) or
                                      (not eliminated and fields[5] in {"endpoint-exact", "optimized-endpoint-collection-upper-bound"}
                                       and bool(fields[6] and fields[7]) and math.isfinite(delay) and delay >= 0)))
                    except (IndexError, ValueError, UnicodeDecodeError) as error:
                        raise ValidationError("Vivado local timing row is invalid") from error
                    if not valid:
                        raise ValidationError("Vivado local timing identity or measurement disagrees")
                    if eliminated:
                        disposition = {**record,"delay_ns":None,"measurement":OPTIMIZED_AWAY,"optimization_source":proof_source}
                        validate_optimization_record(disposition)
                        require_optimization_proof({"optimization_proofs":{header["fpga"]:optimization_proofs}},header["fpga"],proof_source,record)
                    try:
                        connection.execute("INSERT INTO measurements VALUES (?,?,?)", (identity, delay, proof_source))
                    except sqlite3.IntegrityError as error:
                        raise ValidationError("Vivado local timing contains a duplicate path") from error
            count = connection.execute("SELECT count(*) FROM measurements").fetchone()[0]
            if count != header["coverage"]["local_paths"]:
                raise ValidationError("Vivado local timing coverage is incomplete")
            def records():
                for row in connection.execute("SELECT p.record_json,m.delay,m.optimization_source FROM identity.paths p JOIN measurements m ON m.id=p.id ORDER BY p.id"):
                    record = {**json.loads(row[0]), "delay_ns": None if row[1] is None else float(row[1])}
                    if row[2] is not None:
                        record.update(measurement=OPTIMIZED_AWAY,optimization_source=str(row[2]),path_pins=[])
                    yield record
            timing_header = local_path_timing_header(header, provider="vivado-source-bound-local-endpoint-delay-v1",
                                                     qualification="routed-vendor-device-local-endpoint-longest-path")
            with write_local_path_timing_storage(output_path, timing_header, records()) as view:
                validation = validate_local_path_timing(view)
            return {**validation, "output": str(output_path)}
        finally:
            connection.close()


def import_indexed_vivado_boundary(input_path: Path, identity_path: Path, output_path: Path, *, measurement_scope: str = "partition-boundary") -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=".vivado-boundary-", dir=output_path.parent) as temporary:
        connection = sqlite3.connect(Path(temporary) / "measurements.sqlite")
        identities = None
        try:
            identities = read_boundary_identity_storage(identity_path)
            connection.execute("CREATE TABLE measurements(id TEXT PRIMARY KEY,kind TEXT,delay REAL,start TEXT,end TEXT)")
            with input_path.open(encoding="utf-8") as stream:
                if next(stream, "").rstrip("\n") != "endpoint_hex\tkind\tdelay_ns\tstart_object_hex\tend_object_hex":
                    raise ValidationError("Vivado boundary timing header is invalid")
                for line in stream:
                    if not line.strip():
                        continue
                    fields = line.rstrip("\n").split("\t")
                    try:
                        if len(fields) != 5:
                            raise ValueError("invalid field count")
                        row = (bytes.fromhex(fields[0]).decode("utf-8"), fields[1], float(fields[2]),
                               bytes.fromhex(fields[3]).decode("utf-8"), bytes.fromhex(fields[4]).decode("utf-8"))
                        connection.execute("INSERT INTO measurements VALUES (?,?,?,?,?)", row)
                    except (ValueError, UnicodeDecodeError, sqlite3.IntegrityError) as error:
                        raise ValidationError("Vivado boundary timing row is invalid or duplicated") from error
            def records():
                count = 0
                for identity in identities["endpoints"]:
                    row = connection.execute("SELECT kind,delay,start,end FROM measurements WHERE id=?", (identity["id"],)).fetchone()
                    if row is None or row[0] != identity["kind"]:
                        raise ValidationError("Vivado boundary timing coverage disagrees")
                    yield boundary_timing_record(identity, {"delay_ns": row[1], "start_object": row[2], "end_object": row[3]}, measurement_scope=measurement_scope)
                    count += 1
                if count != connection.execute("SELECT count(*) FROM measurements").fetchone()[0]:
                    raise ValidationError("Vivado boundary timing has unexpected endpoints")
            header = boundary_timing_header(identities, provider="vivado-indexed-routed-boundary-v1", qualification="routed-vendor-device-boundary", measurement_scope=measurement_scope)
            with write_boundary_timing_storage(output_path, header, records()) as view:
                validation = validate_boundary_timing_database(view, identities)
            return {**validation, "output": str(output_path)}
        finally:
            close = getattr(identities, "close", None)
            if callable(close):
                close()
            connection.close()
