"""Explicit recovery of pre-port-resolution compact EmuIR inputs.

This is an offline source repair, not a physical pin alias or timing fallback.
All downstream timing must be regenerated from its output. New synthesis
already performs the authoritative Yosys hierarchy pass before EmuIR export.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time

from .emuir_storage import EMUIR_COMPACT_STORAGE_SCHEMA, open_emuir_sqlite_read_only
from .errors import ValidationError
from .io import file_sha256

_TYPES = ("RAMB18E2", "RAMB36E2")
_ALIASES = dict(zip(
    ("DOADO", "DOBDO", "DOPADOP", "DOPBDOP", "DIADI", "DIBDI", "DIPADIP", "DIPBDIP"),
    ("DOUTADOUT", "DOUTBDOUT", "DOUTPADOUTP", "DOUTPBDOUTP", "DINADIN", "DINBDIN", "DINPADINP", "DINPBDINP"),
))


def bram_port_declarations(path: Path) -> dict:
    """Read fixed port signatures from the checked-in upstream Yosys library."""
    text = path.read_text(encoding="utf-8")
    result = {}
    for cell_type in _TYPES:
        module = re.search(r"\bmodule\s+" + cell_type + r"\b(.*?)\bendmodule\b", text, re.S)
        if module is None:
            raise ValidationError(f"missing public declaration for {cell_type}")
        ports = {}
        for direction, high, low, port in re.findall(
            r"\b(input|output)\s*(?:\[\s*(\d+)\s*:\s*(\d+)\s*\])?\s*(\w+)\s*;",
            module.group(1),
        ):
            ports[port] = (direction, abs(int(high) - int(low)) + 1 if high else 1)
        expected_wea = 2 if cell_type == "RAMB18E2" else 4
        if ports.get("WEA") != ("input", expected_wea):
            raise ValidationError(f"invalid public {cell_type} WEA declaration")
        result[cell_type] = ports
    return result


def _identity(path: Path) -> tuple:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def repair_compact_bram_ports(source: Path, output: Path, declarations: Path) -> dict:
    """Copy once and apply the public unsigned primitive port-sizing rules.

    Integer endpoint relations stay in SQLite. Python sees only the exceptional
    BRAM declarations and constant payloads, never ordinary cells or full nets.
    Oversized unsigned output connections are zero-extended, exactly as
    Yosys passes/hierarchy/hierarchy.cc does (sig.extend_u0). Their consumers
    become constant inputs; they are not assigned fictitious physical pins.
    """
    start = time.monotonic()
    source, output = source.resolve(), output.absolute()
    if source == output or output.exists():
        raise ValidationError("BRAM recovery requires a new output path")
    ports = bram_port_declarations(declarations)
    before = _identity(source)
    probe = open_emuir_sqlite_read_only(source)
    try:
        metadata = dict(probe.execute("SELECT key,value FROM metadata"))
        if (probe.execute("PRAGMA user_version").fetchone()[0] != 2
                or metadata.get("storage_schema") != EMUIR_COMPACT_STORAGE_SCHEMA):
            raise ValidationError("BRAM recovery requires compact EmuIR v2")
        counts = json.loads(metadata["record_counts_json"])
    finally:
        probe.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".bram-port-recovery-", suffix=".sqlite", dir=output.parent)
    os.close(descriptor)
    temporary = Path(name)
    database = None
    try:
        shutil.copyfile(source, temporary)
        copied_seconds = time.monotonic() - start
        if _identity(source) != before:
            raise ValidationError("BRAM recovery source changed during copy")
        database = sqlite3.connect(temporary)
        database.executescript("""
            PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
            PRAGMA cache_size=-65536; PRAGMA temp_store=FILE;
            CREATE TEMP TABLE brams AS
                SELECT seq,id,type,constants_seq FROM cell_records
                WHERE type IN ('RAMB18E2','RAMB36E2');
            CREATE UNIQUE INDEX brams_seq ON brams(seq);
            CREATE TEMP TABLE limits(type TEXT, port_seq INTEGER, direction TEXT,
                width INTEGER, PRIMARY KEY(type,port_seq)) WITHOUT ROWID;
        """)
        pin_names = dict(database.execute("SELECT name,seq FROM pin_names"))
        for cell_type, definitions in ports.items():
            for port, sequence in pin_names.items():
                definition = definitions.get(_ALIASES.get(port, port))
                if definition is not None:
                    database.execute("INSERT INTO limits VALUES (?,?,?,?)",
                                     (cell_type, sequence, *definition))
        bad = database.execute("""
            SELECT b.id,p.name,e.bit,l.direction,l.width FROM brams b
            JOIN endpoint_records e INDEXED BY endpoint_owner ON e.instance_seq=b.seq
            JOIN pin_names p ON p.seq=e.port_seq
            LEFT JOIN limits l ON l.type=b.type AND l.port_seq=e.port_seq
            WHERE l.width IS NULL OR e.role != CASE l.direction WHEN 'input' THEN 1 ELSE 0 END
            LIMIT 1
        """).fetchone()
        if bad is not None:
            raise ValidationError(f"unsupported BRAM endpoint resizing/direction: {tuple(bad)!r}")
        database.executescript("""
            CREATE TEMP TABLE zero_nets AS
                SELECT DISTINCT e.net_seq FROM brams b
                JOIN endpoint_records e INDEXED BY endpoint_owner ON e.instance_seq=b.seq
                JOIN limits l ON l.type=b.type AND l.port_seq=e.port_seq
                WHERE l.direction='output' AND e.bit>=l.width;
            CREATE UNIQUE INDEX zero_net_seq ON zero_nets(net_seq);
        """)
        # A zero-extended output may not override another real driver.
        conflict = database.execute("""
            SELECT e.net_seq FROM zero_nets z JOIN endpoint_records e USING(net_seq)
            WHERE e.role=0 GROUP BY e.net_seq HAVING count(*)!=1 LIMIT 1
        """).fetchone()
        if conflict is not None:
            raise ValidationError("BRAM zero extension conflicts with another driver")
        database.executescript("""
            CREATE TEMP TABLE zero_sinks AS
                SELECT e.instance_seq,e.port_seq,e.bit FROM zero_nets z
                JOIN endpoint_records e USING(net_seq) WHERE e.role=1;
            CREATE INDEX zero_sink_owner ON zero_sinks(instance_seq);
            CREATE TEMP TABLE removed AS
                SELECT e.net_seq,e.role,e.ordinal FROM brams b
                JOIN endpoint_records e INDEXED BY endpoint_owner ON e.instance_seq=b.seq
                JOIN limits l ON l.type=b.type AND l.port_seq=e.port_seq
                WHERE e.bit>=l.width
                UNION SELECT e.net_seq,e.role,e.ordinal FROM zero_nets z
                JOIN endpoint_records e USING(net_seq);
            CREATE UNIQUE INDEX removed_key ON removed(net_seq,role,ordinal);
            CREATE TEMP TABLE affected_nets AS SELECT DISTINCT net_seq FROM removed;
        """)
        removed_endpoints = database.execute("SELECT count(*) FROM removed").fetchone()[0]
        removed_constants, changed_constants = 0, 0
        # Per-BRAM payloads only; no ordinary cell record is decoded.
        for sequence, cell_type, payload in database.execute("""
            SELECT b.seq,b.type,p.value FROM brams b JOIN payloads p ON p.seq=b.constants_seq
        """):
            constants = json.loads(payload)
            kept = []
            for item in constants:
                definition = ports[cell_type].get(_ALIASES.get(item["port"], item["port"]))
                if definition is None:
                    raise ValidationError("unsupported BRAM constant port resizing")
                if item["bit"] < definition[1]:
                    kept.append(item)
                else:
                    removed_constants += 1
            if kept != constants:
                value = json.dumps(kept, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                database.execute("INSERT OR IGNORE INTO payloads(value) VALUES (?)", (value,))
                payload_seq = database.execute("SELECT seq FROM payloads WHERE value=?", (value,)).fetchone()[0]
                database.execute("UPDATE cell_records SET constants_seq=? WHERE seq=?", (payload_seq, sequence))
                changed_constants += 1
        added_constant_inputs = 0
        # Only sinks of the exceptional zero-extended outputs are touched.
        for sequence, cell_type, payload in database.execute("""
            SELECT c.seq,c.type,p.value FROM cell_records c
            JOIN payloads p ON p.seq=c.constants_seq
            WHERE c.seq IN (SELECT instance_seq FROM zero_sinks WHERE instance_seq IS NOT NULL)
        """):
            constants = json.loads(payload)
            existing = {(x["port"], x["bit"]): x["value"] for x in constants}
            for port, bit in database.execute("""
                SELECT p.name,s.bit FROM zero_sinks s JOIN pin_names p ON p.seq=s.port_seq
                WHERE s.instance_seq=? ORDER BY p.name,s.bit
            """, (sequence,)):
                definition = ports.get(cell_type, {}).get(_ALIASES.get(port, port))
                if definition is not None and bit >= definition[1]:
                    continue
                if (port, bit) in existing and existing[(port, bit)] != "0":
                    raise ValidationError("BRAM zero extension conflicts with a constant input")
                if (port, bit) not in existing:
                    constants.append({"port": port, "bit": bit, "value": "0"})
                    existing[(port, bit)] = "0"
                    added_constant_inputs += 1
            constants.sort(key=lambda x: (x["port"], x["bit"]))
            value = json.dumps(constants, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            database.execute("INSERT OR IGNORE INTO payloads(value) VALUES (?)", (value,))
            payload_seq = database.execute("SELECT seq FROM payloads WHERE value=?", (value,)).fetchone()[0]
            database.execute("UPDATE cell_records SET constants_seq=? WHERE seq=?", (payload_seq, sequence))
        top_constant_bits = 0
        for sequence, payload in database.execute("SELECT seq,record_json FROM port_records"):
            port = json.loads(payload)
            bits = [row[0] for row in database.execute("""
                SELECT DISTINCT s.bit FROM zero_sinks s JOIN pin_names p ON p.seq=s.port_seq
                WHERE s.instance_seq IS NULL AND p.name=? ORDER BY s.bit
            """, (port["id"],))]
            if not bits:
                continue
            if port["direction"] != "output":
                raise ValidationError("BRAM zero extension conflicts with a top input")
            constants = {x["bit"]: x for x in port.get("constant_connections", [])}
            for bit in bits:
                if bit in constants and constants[bit]["value"] != "0":
                    raise ValidationError("BRAM zero extension conflicts with a top constant")
                constants[bit] = {"bit": bit, "value": "0"}
                top_constant_bits += 1
            port["constant_connections"] = [constants[bit] for bit in sorted(constants)]
            database.execute("UPDATE port_records SET record_json=? WHERE seq=?",
                             (json.dumps(port, sort_keys=True, separators=(",", ":")), sequence))
        database.execute("""DELETE FROM endpoint_records WHERE (net_seq,role,ordinal)
            IN (SELECT net_seq,role,ordinal FROM removed)""")
        database.execute("""UPDATE net_records SET fanout=(SELECT count(*)
            FROM endpoint_records e WHERE e.net_seq=net_records.seq AND e.role=1)
            WHERE seq IN (SELECT net_seq FROM affected_nets)""")
        database.execute("UPDATE net_records SET cut_class='undriven' WHERE seq IN (SELECT net_seq FROM zero_nets)")
        counts["endpoint_records"] -= removed_endpoints
        database.execute("UPDATE metadata SET value=? WHERE key='record_counts_json'",
                         (json.dumps(counts, sort_keys=True, separators=(",", ":")),))
        bram_count = database.execute("SELECT count(*) FROM brams").fetchone()[0]
        affected_nets = database.execute("SELECT count(*) FROM affected_nets").fetchone()[0]
        zero_nets = database.execute("SELECT count(*) FROM zero_nets").fetchone()[0]
        database.commit()
        database.close()
        database = None
        if _identity(source) != before:
            raise ValidationError("BRAM recovery source changed during normalization")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        # Atomic, non-overwriting publication; a racing destination is an error.
        os.link(temporary, output)
        return {
            "schema": "emuflow.xilinx-bram-port-recovery/v1", "status": "pass",
            "source": str(source), "output": str(output),
            "declarations_sha256": file_sha256(declarations),
            "bram_cells": bram_count, "removed_endpoints": removed_endpoints,
            "zero_extended_output_nets": zero_nets, "added_constant_inputs": added_constant_inputs,
            "top_constant_bits": top_constant_bits,
            "removed_constant_bits": removed_constants, "changed_constant_payloads": changed_constants,
            "affected_nets": affected_nets, "source_bytes": before[2], "output_bytes": output.stat().st_size,
            "copy_seconds": copied_seconds, "total_seconds": time.monotonic()-start,
            "unchanged": ["instance_and_net_ids", "resources", "parameters", "attributes", "clocks", "top_port_declarations"],
            "downstream_timing": "must-regenerate; old timing/route/schedule reports are not evidence",
        }
    finally:
        if database is not None:
            database.close()
        temporary.unlink(missing_ok=True)
