#!/usr/bin/env python3
"""Explicit one-shot storage/timing A/B measurement on an existing mapped IR.

This tool never invokes synthesis. Each operation runs in its own process so
peak RSS is attributable to that operation, not a prior in-process workload.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import resource
import sys
import time
from pathlib import Path

from emuflow.emuir_compact import source_metadata, source_instances, compact_nets, write_compact_emuir
from emuflow.emuir_storage import _json, open_emuir_sqlite_read_only, _write_emuir_sqlite_v1
from emuflow.indexed_timing import IndexedTimingView
from emuflow.ir import EmuIR
from emuflow.opensta import build_architecture_opensta_timing_model, load_timing_model, DEFAULT_TIMING_MODEL, render_opensta_liberty, run_opensta_path_database
from emuflow.sta import write_emuir_net_map, iter_sta_path_database_paths
from emuflow.verilog import write_mapped_verilog


def _io():
    path = Path("/proc/self/io")
    return {name: int(value) for name, value in (
        line.split(":", 1) for line in path.read_text().splitlines()
    )} if path.is_file() else {}


def _file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class _GraphDigest:
    def __init__(self, metadata):
        self.digest = hashlib.sha256(_json(metadata).encode())
        self.counts = {}
        self.seal_seconds = 0.0

    def records(self, name, records):
        self.digest.update(name.encode() + b"\0")
        count = 0
        for record in records:
            start = time.perf_counter()
            raw = _json(record).encode()
            self.digest.update(len(raw).to_bytes(8, "big"))
            self.digest.update(raw)
            count += 1
            self.seal_seconds += time.perf_counter() - start
            yield record
        self.counts[name] = count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("repack", "scan", "export", "timing"))
    parser.add_argument("--ir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--reader", choices=("materialized", "indexed"), default="indexed")
    parser.add_argument("--storage-version", type=int, choices=(1, 2), default=2)
    parser.add_argument("--architecture-timing-db", type=Path)
    parser.add_argument("--clock", action="append", default=[], help="name=period_ns; repeat for multiple clocks")
    parser.add_argument("--timing-io", type=Path)
    parser.add_argument("--sta-executable")
    parser.add_argument("--max-paths", type=int, default=200000)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    start, initial_io = time.perf_counter(), _io()
    result = {"operation": args.operation, "input": str(args.ir), "input_bytes": args.ir.stat().st_size}
    seal_seconds = 0.0
    if args.operation in {"repack", "scan"}:
        connection = open_emuir_sqlite_read_only(args.ir)
        try:
            metadata = source_metadata(connection)
            digest = _GraphDigest(metadata)
            instances = digest.records("instances", source_instances(connection))
            nets = digest.records("nets", compact_nets(connection))
            if args.operation == "repack":
                if args.out.resolve() == args.ir.resolve():
                    raise ValueError("output must not overwrite source")
                writer = write_compact_emuir if args.storage_version == 2 else _write_emuir_sqlite_v1
                writer(args.out, {**metadata, "instances": instances, "nets": nets})
                result["output_bytes"] = args.out.stat().st_size
                result["storage_version"] = args.storage_version
            else:
                for records in (instances, nets):
                    for _ in records:
                        pass
            result.update(logical_sha256=digest.digest.hexdigest(), records=digest.counts)
            seal_seconds = digest.seal_seconds
        finally:
            connection.close()
    elif args.operation == "export":
        args.out.mkdir(parents=True, exist_ok=True)
        view = IndexedTimingView(args.ir) if args.reader == "indexed" else None
        try:
            stage_start = time.perf_counter()
            ir = view if view is not None else EmuIR.load(args.ir)
            result["load_seconds"] = time.perf_counter() - stage_start
            result["reader"] = args.reader
            stage_start = time.perf_counter()
            model, types = (build_architecture_opensta_timing_model(ir, args.architecture_timing_db)
                            if args.architecture_timing_db is not None else (load_timing_model(DEFAULT_TIMING_MODEL), None))
            result["model_seconds"] = time.perf_counter() - stage_start
            stage_start = time.perf_counter()
            write_mapped_verilog(ir, args.out / "mapped.v", timing_only=True, timing_cell_types=types)
            result["verilog_write_seconds"] = time.perf_counter() - stage_start
            stage_start = time.perf_counter()
            write_emuir_net_map(args.ir, args.out / "net-map.tsv", _ir=ir)
            result["net_map_write_seconds"] = time.perf_counter() - stage_start
            (args.out / "timing.lib").write_text(render_opensta_liberty(model))
            # Explicit qualification only: one seal of the exact STA inputs.
            stage_start = time.perf_counter()
            result["sta_inputs"] = {name: {"bytes": (args.out / name).stat().st_size,
                                          "sha256": _file_digest(args.out / name)}
                                    for name in ("mapped.v", "net-map.tsv", "timing.lib")}
            seal_seconds = time.perf_counter() - stage_start
        finally:
            if view is not None:
                view.close()
    else:
        clocks = dict((name, float(period)) for name, period in (c.split("=", 1) for c in args.clock))
        timing = run_opensta_path_database(
            args.ir, args.out, clocks=clocks or None,
            timing_io=json.loads(args.timing_io.read_text()) if args.timing_io else None,
            architecture_timing_db_path=args.architecture_timing_db,
            executable=args.sta_executable, max_paths=args.max_paths,
            log_path=args.out.with_suffix(".sta.log"),
        )
        result["timing"] = timing
        # Explicit paired qualification: seal canonical path records, not run-
        # specific source paths. Identical scalars alone are insufficient.
        stage_start = time.perf_counter()
        digest = _GraphDigest({})
        manifest = json.loads(args.out.read_text())
        for _ in digest.records("paths", iter_sta_path_database_paths(args.out, manifest)):
            pass
        result.update(path_records_sha256=digest.digest.hexdigest(), records=digest.counts)
        seal_seconds = time.perf_counter() - stage_start
    final_io = _io()
    usage = resource.getrusage(resource.RUSAGE_SELF)
    elapsed = time.perf_counter() - start
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    result.update(elapsed_seconds=elapsed,
                  qualification_seal_seconds=seal_seconds,
                  operation_seconds=elapsed - seal_seconds,
                  peak_rss_bytes=usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024),
                  child_peak_rss_bytes=children.ru_maxrss * (1 if sys.platform == "darwin" else 1024),
                  cpu_seconds=usage.ru_utime + usage.ru_stime,
                  io_delta={key: value - initial_io.get(key, 0) for key, value in final_io.items()})
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, separators=(",", ":")) + "\n")
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
