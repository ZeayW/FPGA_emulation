"""Bounded SQLite planning between native Vivado implementation stages.

Vivado retains its current design; each Python child closes its source indexes
before returning the next small export plan. No placed/routed DCP is required.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from pathlib import Path
import sqlite3
import time

from .errors import ValidationError
from .io import file_sha256, read_json, write_json
from .vivado_constant_frontier import check_constant_sources, prepare_constant_frontier_query
from .vivado_lineage import import_cell_lineage, resolve_timing_query
from .vivado_optimization import build_optimization_proofs
from .vivado_clocks import implementation_clocks, implementation_timing_contract

SCHEMA = "emuflow.vivado-native-session/v2"


def _tcl_word(value):
    return '"'+str(value).replace('\\','\\\\').replace('"','\\"').replace('$','\\$').replace('[','\\[').replace(']','\\]').replace('\n','\\n').replace('\r','\\r')+'"'


def prepare_manifest(output: Path, *, fpga, part, mapped_index, functions, mapped_sha256,
                     queries, runtime, ports, synthesized_checkpoint=None, synthesized_checkpoint_sha256=None,
                     dut_frequency_mhz=None):
    """Prepare small native/child contracts after all source views have closed."""
    output = output.resolve()
    checkpoint = Path(synthesized_checkpoint).resolve() if synthesized_checkpoint is not None else output/"synthesized.dcp"
    reserved = {str((output/(kind+suffix)).resolve()) for kind in queries for suffix in ("-synthesis.tsv","-resolved.tsv",".tsv")}
    if any(str(value.resolve()) in reserved for value in queries.values()):
        raise ValidationError("native timing input overlaps a generated query/measurement output")
    clocks = implementation_clocks(runtime, ports, dut_frequency_mhz)
    manifest = {"schema": SCHEMA, "fpga": fpga, "part": part, "output": str(output),
        "implementation_timing": implementation_timing_contract(runtime, dut_frequency_mhz),
        "source_preflight_stage": "optimized",
        "mapped_index": str(mapped_index.resolve()), "functions": str(functions.resolve()),
        "mapped_index_sha256": mapped_sha256, "queries": {key:str(value.resolve()) for key,value in queries.items()},
        "synthesized_checkpoint": str(checkpoint), "synthesized_checkpoint_sha256": synthesized_checkpoint_sha256}
    write_json(output/"native-session.json",manifest,compact=True)
    with (output/"clock-contract.tsv").open("w") as stream:
        stream.write("clock_hex\tport_hex\tperiod_ns\n")
        for name,port,period in clocks:
            if not isinstance(period,(int,float)) or isinstance(period,bool) or not math.isfinite(period) or period<=0:
                raise ValidationError("native clock contract requires finite positive periods")
            stream.write(f"{name.encode().hex()}\t{port.encode().hex()}\t{period:.17g}\n")
    # Vivado modifies library/Python variables. A child receives the original
    # Python runtime and the exact installed package, rather than vendor Python.
    environment = {"PATH":str(Path(sys.executable).parent)+os.pathsep+"/usr/bin:/bin",
        "PYTHONPATH":str(Path(__file__).resolve().parents[1]), "PYTHONDONTWRITEBYTECODE":"1",
        "TMPDIR":str(output/".tmp"), "SQLITE_TMPDIR":str(output/".tmp")}
    if "LD_LIBRARY_PATH" in os.environ:
        environment["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH"]
    command = ["/usr/bin/env","-i",*(f"{key}={value}" for key,value in environment.items()),
               sys.executable,"-m","emuflow.vivado_session"]
    settings = {"emuflow_session_out":str(output),"emuflow_session_manifest":str(output/"native-session.json"),
        "emuflow_start_checkpoint":str(checkpoint) if synthesized_checkpoint is not None else "",
        "emuflow_preflight_stage":"optimized",
        "emuflow_clock_contract":str(output/"clock-contract.tsv"),"emuflow_io_timing_map":str(output/"fixed-io-timing-map.tsv") if (output/"fixed-io-timing-map.tsv").is_file() else ""}
    lines = [f"set {key} {_tcl_word(value)}" for key,value in settings.items()]
    lines.append("set emuflow_child_command [list "+" ".join(_tcl_word(value) for value in command)+"]")
    config = output/"native-session.tcl"
    config.write_text("\n".join(lines)+"\n")
    return config


def load_session(path: Path):
    value = read_json(path)
    if (value.get("schema") != SCHEMA or not isinstance(value.get("fpga"), str)
            or value.get("source_preflight_stage") not in {"synthesized", "optimized"}
            or not isinstance(value.get("queries"), dict)
            or set(value["queries"])-{"boundary_timing", "logic_segment_timing", "local_path_timing"}):
        raise ValidationError("invalid Vivado native session manifest")
    return value


def _paths(session):
    return Path(session["mapped_index"]), Path(session["functions"]), Path(session["output"])


def _resolve(session, lineage, proof, kind, destination, diagnostic=None, stage="routed"):
    mapped, functions, out = _paths(session)
    return resolve_timing_query(mapped, functions, lineage, Path(session["queries"][kind]), destination,
        boundary=kind == "boundary_timing", optimization_proofs=proof, allow_eliminated=True,
        stage=stage, diagnostic_unresolved_path=diagnostic)


def _preflight_snapshot(session):
    stage = session.get("source_preflight_stage")
    if stage not in {"synthesized", "optimized"}:
        raise ValidationError("native source preflight requires a preplacement snapshot")
    filename = "optimized_mapped_cells.tsv" if stage == "optimized" else "mapped_cells.tsv"
    return stage, Path(session["output"])/filename


def prepare_frontiers(session):
    """Classify every required endpoint in the selected preplacement snapshot."""
    mapped, functions, out = _paths(session)
    stage, inventory = _preflight_snapshot(session)
    lineage, model = out/(stage+"-lineage.sqlite"), out/"source-constant-model.sqlite"
    checkpoint = Path(session["synthesized_checkpoint"])
    checkpoint_sha = file_sha256(checkpoint)
    expected = session.get("synthesized_checkpoint_sha256")
    if expected is not None and checkpoint_sha != expected:
        raise ValidationError("synthesized continuation checkpoint differs")
    coverage = import_cell_lineage(mapped, functions, {stage: inventory}, lineage,
        mapped_sha256=session["mapped_index_sha256"], retain_stage=stage)
    proof = build_optimization_proofs(mapped, lineage, model, mapped_sha256=session["mapped_index_sha256"],
                                      stage=stage, fpga=session["fpga"])
    selected = set()
    resolutions = {}
    for kind in session["queries"]:
        resolved, diagnostic = out/(kind+"-"+stage+".tsv"), out/(kind+"-unassociated.sqlite")
        resolutions[kind] = _resolve(session, lineage, model, kind, resolved, diagnostic, stage=stage)
        db = sqlite3.connect(f"{diagnostic.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            db.execute("ATTACH DATABASE ? AS proofs", (f"{model.resolve().as_uri()}?mode=ro&immutable=1",))
            invalid = db.execute("SELECT s.name FROM unresolved_sources s LEFT JOIN proofs.constant_candidates c ON c.origin=s.source_origin WHERE c.origin IS NULL LIMIT 1").fetchone()
            if invalid:
                raise ValidationError(f"required source has no supported native association or constant candidate: {invalid[0]}")
            for (origin,) in db.execute("SELECT DISTINCT source_origin FROM unresolved_sources"):
                selected.add(f"s{origin}")
                if len(selected)>4096:
                    raise ValidationError("required native constant sources exceed bounded scope")
        finally:
            db.close()
        # This child has consumed these intermediate query rows completely.
        resolved.unlink()
    model_sha = file_sha256(model)
    if selected:
        plan = prepare_constant_frontier_query(mapped, model, inventory,
            mapped_sha256=session["mapped_index_sha256"], candidate_proofs_sha256=model_sha,
            checkpoint_sha256=checkpoint_sha, fpga=session["fpga"], selected_sources=sorted(selected), source_stage=stage)
    else:
        plan = {"status": "not-required", "requested_sources": [], "sources": [], "query_objects": [],
                "source_candidate_proofs_sha256": model_sha, "synthesized_checkpoint_sha256": checkpoint_sha, "source_stage":stage}
    write_json(out/"constant-frontier-plan.json", plan, compact=True)
    with (out/"constant-frontier-query.tsv").open("w") as stream:
        rows = csv.writer(stream, delimiter="\t", lineterminator="\n")
        rows.writerow(["source_origin", "native_cell_hex"])
        for item in plan["query_objects"]:
            rows.writerow([item["source_origin"], item["name"].encode().hex()])
    write_json(out/(stage+"-source-preflight.json"), {"status": "inspection-required" if selected else "pass", "source_stage":stage,
        "coverage": coverage, "proof": proof, "queries": resolutions, "requested_constant_sources": len(selected),
        "closed_constant_sources": len(plan["sources"]), "native_frontiers": len(plan["query_objects"])}, compact=True)
    # All children using this preplacement association have returned and closed
    # their connections. Only the compact source model and plan are needed next.
    lineage.unlink()
    return {"status": "pass", "constant_sources": len(plan["sources"]), "native_frontiers": len(plan["query_objects"])}


def _check_constants(session):
    mapped, functions, out = _paths(session)
    plan = read_json(out/"constant-frontier-plan.json")
    checkpoint_sha = file_sha256(Path(session["synthesized_checkpoint"]))
    if checkpoint_sha != plan["synthesized_checkpoint_sha256"]:
        raise ValidationError("native constant facts belong to a different synthesized checkpoint")
    stage, inventory = _preflight_snapshot(session)
    if plan.get("source_stage") != stage:
        raise ValidationError("native constant facts belong to a different preplacement stage")
    if not plan["requested_sources"]:
        return None, checkpoint_sha
    checked = check_constant_sources(mapped, functions, out/"source-constant-model.sqlite",
        out/"constant-frontiers.tsv", inventory, mapped_sha256=session["mapped_index_sha256"],
        candidate_proofs_sha256=plan["source_candidate_proofs_sha256"], checkpoint_sha256=checkpoint_sha,
        fpga=session["fpga"], selected_sources=plan["requested_sources"], source_stage=stage)
    return checked, checkpoint_sha


def confirm_frontiers(session):
    checked, checkpoint_sha = _check_constants(session)
    out = Path(session["output"])
    if checked is not None:
        write_json(out/"checked-constant-frontiers.json", checked.to_document(), compact=True)
    result = {"status": "pass", "confirmed_sources": len(checked["sources"]) if checked else 0,
              "synthesized_checkpoint_sha256": checkpoint_sha}
    write_json(out/"constant-frontier-validation.json", result, compact=True)
    return result


def prepare_timing(session):
    """Import the routed snapshot and provide only checked timing queries."""
    mapped, functions, out = _paths(session)
    checked, checkpoint_sha = _check_constants(session)
    lineage, proof_path = out/"cell-lineage.sqlite", out/"optimization-proofs.sqlite"
    stage, inventory = _preflight_snapshot(session)
    coverage = import_cell_lineage(mapped, functions, {
        stage: inventory, "routed": out/"routed_mapped_cells.tsv"}, lineage,
        mapped_sha256=session["mapped_index_sha256"])
    proof = build_optimization_proofs(mapped, lineage, proof_path, mapped_sha256=session["mapped_index_sha256"],
        fpga=session["fpga"], checked_constants=checked, synthesized_checkpoint_sha256=checkpoint_sha)
    resolutions = {}
    with (out/"timing-exports.tsv").open("w") as stream:
        rows = csv.writer(stream, delimiter="\t", lineterminator="\n")
        rows.writerow(["kind", "query_hex", "output_hex"])
        for kind in session["queries"]:
            resolved = out/(kind+"-resolved.tsv")
            result = _resolve(session, lineage, proof_path, kind, resolved)
            resolutions[kind] = result
            rows.writerow([kind, str(resolved).encode().hex(), str(out/(kind+".tsv")).encode().hex()])
    result = {"status": "pass", "coverage": coverage, "optimization_proofs": proof,
              "resolutions": resolutions, "synthesized_checkpoint_sha256": checkpoint_sha}
    write_json(out/"routed-query-preparation.json", result, compact=True)
    # Native exporters consume only the sealed resolved queries and proof store.
    lineage.unlink()
    for inventory in ("mapped_cells.tsv", "optimized_mapped_cells.tsv", "routed_mapped_cells.tsv"):
        (out / inventory).unlink(missing_ok=True)
    return {"status": "pass", "query_classes": len(resolutions)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare-frontiers", "confirm-frontiers", "prepare-timing"))
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    started = time.monotonic()
    session = load_session(args.manifest)
    result = {"prepare-frontiers": prepare_frontiers, "confirm-frontiers": confirm_frontiers,
              "prepare-timing": prepare_timing}[args.stage](session)
    print(json.dumps({"stage": args.stage, **result, "runtime_seconds": time.monotonic()-started}), flush=True)


if __name__ == "__main__":
    main()
