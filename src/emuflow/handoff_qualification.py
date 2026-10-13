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

from .emuir_compact import source_metadata, source_instances, compact_nets, write_compact_emuir
from .emuir_storage import _json, open_emuir_sqlite_read_only
from .indexed_timing import IndexedTimingView
from .ir import EmuIR
from .opensta import build_architecture_opensta_timing_model, load_timing_model, DEFAULT_TIMING_MODEL, render_opensta_liberty
from .sta import write_emuir_net_map
from .verilog import write_mapped_verilog


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

    def records(self, name, records):
        self.digest.update(name.encode() + b"\0")
        count = 0
        for record in records:
            raw = _json(record).encode()
            self.digest.update(len(raw).to_bytes(8, "big"))
            self.digest.update(raw)
            count += 1
            yield record
        self.counts[name] = count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("repack", "scan", "export"))
    parser.add_argument("--ir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--reader", choices=("materialized", "indexed"), default="indexed")
    parser.add_argument("--architecture-timing-db", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    start, initial_io = time.perf_counter(), _io()
    result = {"operation": args.operation, "input": str(args.ir), "input_bytes": args.ir.stat().st_size}
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
                write_compact_emuir(args.out, {**metadata, "instances": instances, "nets": nets})
                result["output_bytes"] = args.out.stat().st_size
            else:
                for records in (instances, nets):
                    for _ in records:
                        pass
            result.update(logical_sha256=digest.digest.hexdigest(), records=digest.counts)
        finally:
            connection.close()
    else:
        args.out.mkdir(parents=True, exist_ok=True)
        view = IndexedTimingView(args.ir) if args.reader == "indexed" else None
        try:
            ir = view if view is not None else EmuIR.load(args.ir)
            result["reader"] = args.reader
            model, types = (build_architecture_opensta_timing_model(ir, args.architecture_timing_db)
                            if args.architecture_timing_db is not None else (load_timing_model(DEFAULT_TIMING_MODEL), None))
            write_mapped_verilog(ir, args.out / "mapped.v", timing_only=True, timing_cell_types=types)
            write_emuir_net_map(args.ir, args.out / "net-map.tsv", _ir=ir)
            (args.out / "timing.lib").write_text(render_opensta_liberty(model))
            # Explicit qualification only: one seal of the exact STA inputs.
            result["sta_inputs"] = {name: {"bytes": (args.out / name).stat().st_size,
                                          "sha256": _file_digest(args.out / name)}
                                    for name in ("mapped.v", "net-map.tsv", "timing.lib")}
        finally:
            if view is not None:
                view.close()
    final_io = _io()
    usage = resource.getrusage(resource.RUSAGE_SELF)
    result.update(elapsed_seconds=time.perf_counter() - start,
                  peak_rss_bytes=usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024),
                  cpu_seconds=usage.ru_utime + usage.ru_stime,
                  io_delta={key: value - initial_io.get(key, 0) for key, value in final_io.items()})
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, separators=(",", ":")) + "\n")
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
