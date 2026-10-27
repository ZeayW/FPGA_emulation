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

SCHEMA = "emuflow.vivado-native-session/v4"


def _tcl_word(value):
    return '"'+str(value).replace('\\','\\\\').replace('"','\\"').replace('$','\\$').replace('[','\\[').replace(']','\\]').replace('\n','\\n').replace('\r','\\r')+'"'


def _child_command(output):
    # Vivado modifies library/Python variables. A child receives the original
    # Python runtime and the exact installed package, rather than vendor Python.
    environment = {"PATH":str(Path(sys.executable).parent)+os.pathsep+"/usr/bin:/bin",
        "PYTHONPATH":str(Path(__file__).resolve().parents[1]), "PYTHONDONTWRITEBYTECODE":"1",
        "TMPDIR":str(output/".tmp"), "SQLITE_TMPDIR":str(output/".tmp")}
    if os.environ.get("EMUFLOW_NATIVE_ROOT"):
        environment["EMUFLOW_NATIVE_ROOT"] = str(Path(os.environ["EMUFLOW_NATIVE_ROOT"]).expanduser().resolve())
    if "LD_LIBRARY_PATH" in os.environ:
        environment["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH"]
    return ["/usr/bin/env","-i",*(f"{key}={value}" for key,value in environment.items()),
               sys.executable,"-m","emuflow.vivado_session"]


def prepare_manifest(output: Path, *, fpga, part, mapped_index, functions, mapped_sha256,
                     queries, runtime, ports, synthesized_checkpoint=None, synthesized_checkpoint_sha256=None,
                     dut_frequency_mhz=None,word_phy_directory=None,retain_routed_checkpoint=False,
                     route_after_proof_failure=False,source_hierarchy_prefix=""):
    """Prepare small native/child contracts after all source views have closed."""
    output = output.resolve()
    if not isinstance(retain_routed_checkpoint,bool):raise ValidationError('routed checkpoint retention must be Boolean')
    if not isinstance(route_after_proof_failure,bool) or (route_after_proof_failure and not retain_routed_checkpoint):
        raise ValidationError('routing after a proof failure requires explicit Boolean checkpoint retention')
    if not isinstance(source_hierarchy_prefix,str) or len(source_hierarchy_prefix)>4096 or any(c in source_hierarchy_prefix for c in '\x00\r\n'):
        raise ValidationError('native source hierarchy is invalid')
    if word_phy_directory is not None and not source_hierarchy_prefix:
        raise ValidationError('native word session requires its explicit mapped source hierarchy')
    checkpoint = Path(synthesized_checkpoint).resolve() if synthesized_checkpoint is not None else output/"synthesized.dcp"
    reserved = {str((output/(kind+suffix)).resolve()) for kind in queries for suffix in ("-synthesis.tsv","-resolved.tsv",".tsv")}
    if any(str(value.resolve()) in reserved for value in queries.values()):
        raise ValidationError("native timing input overlaps a generated query/measurement output")
    clocks = implementation_clocks(runtime, ports, dut_frequency_mhz)
    if word_phy_directory is not None:
        table=Path(word_phy_directory)/'native-word-clocks.tsv'
        if not table.is_file()or table.stat().st_size>65536:raise ValidationError('native word root clock table is missing or oversized')
        with table.open()as stream:
            if next(stream,'').rstrip('\n')!='clock_hex\tport_hex\tperiod_ns':raise ValidationError('invalid native word root clock header')
            for row in csv.reader(stream,delimiter='\t'):
                if len(row)!=3:raise ValidationError('invalid native word root clock row')
                clocks.append((bytes.fromhex(row[0]).decode(),bytes.fromhex(row[1]).decode(),float(row[2])))
        if len({name for name,port,period in clocks})!=len(clocks)or len({port for name,port,period in clocks})!=len(clocks):
            raise ValidationError('native word clock names or source ports overlap')
    manifest = {"schema": SCHEMA, "fpga": fpga, "part": part, "output": str(output),
        "implementation_timing": implementation_timing_contract(runtime, dut_frequency_mhz),
        "source_preflight_stage": "optimized",
        "timing_query_mode": "clock-aware-reference" if word_phy_directory is not None else "datapath-only",
        "mapped_index": str(mapped_index.resolve()), "functions": str(functions.resolve()),
        "mapped_index_sha256": mapped_sha256, "queries": {key:str(value.resolve()) for key,value in queries.items()},
        "synthesized_checkpoint": str(checkpoint), "synthesized_checkpoint_sha256": synthesized_checkpoint_sha256,
        "word_phy_directory":str(Path(word_phy_directory).resolve())if word_phy_directory is not None else None,
        "retain_routed_checkpoint":retain_routed_checkpoint,
        "source_hierarchy_prefix":source_hierarchy_prefix,
        "route_after_proof_failure":route_after_proof_failure}
    write_json(output/"native-session.json",manifest,compact=True)
    with (output/"clock-contract.tsv").open("w") as stream:
        stream.write("clock_hex\tport_hex\tperiod_ns\n")
        for name,port,period in clocks:
            if not isinstance(period,(int,float)) or isinstance(period,bool) or not math.isfinite(period) or period<=0:
                raise ValidationError("native clock contract requires finite positive periods")
            stream.write(f"{name.encode().hex()}\t{port.encode().hex()}\t{period:.17g}\n")
    command = _child_command(output)
    settings = {"emuflow_session_out":str(output),"emuflow_session_manifest":str(output/"native-session.json"),
        "emuflow_word_phy_directory":str(Path(word_phy_directory).resolve())if word_phy_directory is not None else "",
        "emuflow_timing_query_mode":manifest["timing_query_mode"],
        "emuflow_retain_routed_checkpoint":"true"if retain_routed_checkpoint else"false",
        "emuflow_route_after_proof_failure":"true"if route_after_proof_failure else"false",
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
    legacy=value.get('schema')=='emuflow.vivado-native-session/v3'
    if (value.get("schema") not in {SCHEMA,'emuflow.vivado-native-session/v3'} or not isinstance(value.get("fpga"), str)
            or value.get("source_preflight_stage") not in {"synthesized", "optimized"}
            or value.get("timing_query_mode") != ("clock-aware-reference" if value.get("word_phy_directory") is not None else "datapath-only")
            or not isinstance(value.get("queries"), dict)
            or set(value["queries"])-{"boundary_timing", "logic_segment_timing", "local_path_timing"}):
        raise ValidationError("invalid Vivado native session manifest")
    if value.get('word_phy_directory')is not None and(
        not isinstance(value['word_phy_directory'],str)
        or Path(value['word_phy_directory']).resolve()!=Path(value['output']).resolve()):
        raise ValidationError('native word configuration must belong to its physical output')
    if not isinstance(value.get('route_after_proof_failure',False),bool) or (
        value.get('route_after_proof_failure',False) and value.get('retain_routed_checkpoint')is not True):
        raise ValidationError('native proof-failure routing requires retained snapshots')
    prefix=value.get('source_hierarchy_prefix','')
    if not isinstance(prefix,str) or len(prefix)>4096 or any(c in prefix for c in '\x00\r\n'):
        raise ValidationError('native source hierarchy is invalid')
    if (not legacy and 'source_hierarchy_prefix'not in value) or (value.get('word_phy_directory')is not None and (legacy or not prefix)):
        raise ValidationError('native word/source hierarchy requires a current explicit session manifest')
    return value


def prepare_routed_timing_export(manifest_path: Path, output: Path, *, query_overrides=None,
                                query_kinds=None):
    """Prepare read-only timing extraction from a session's retained DCP.

    Keep recovery outputs apart from the original implementation. The original
    constraints stay in the DCP; this operation never regenerates clocks or RTL.
    New source-path libraries can replace selected query classes without
    changing the original implementation session or its other queries.
    An explicit selection permits independent extraction of a complete local
    path library while canonical boundary/segment extraction is still running.
    """
    import shutil
    manifest_path = manifest_path.resolve()
    session = load_session(manifest_path)
    prior = Path(session["output"]).resolve()
    checkpoint = prior/"routed.dcp"
    if not checkpoint.is_file():
        raise ValidationError("routed timing extraction requires the retained routed.dcp")
    output = output.resolve()
    if output == prior:
        raise ValidationError("routed timing extraction requires a fresh output directory")
    replacements = {}
    if query_overrides is not None:
        if not isinstance(query_overrides, dict) or set(query_overrides)-{"boundary_timing", "logic_segment_timing", "local_path_timing"}:
            raise ValidationError("invalid routed timing query overrides")
        for kind, path in query_overrides.items():
            path = Path(path).resolve()
            if not path.is_file():
                raise ValidationError("routed timing query override is missing")
            replacements[kind] = {"path":str(path), "sha256":file_sha256(path)}
    selected = None
    if query_kinds is not None:
        try:
            selected = tuple(query_kinds) if not isinstance(query_kinds, str) else ()
        except TypeError:
            selected = ()
        if (not selected or any(not isinstance(kind, str) for kind in selected) or
                len(set(selected)) != len(selected) or
                set(selected) - (set(session["queries"]) | set(replacements))):
            raise ValidationError("invalid routed timing query selection")
    checkpoint_sha = file_sha256(checkpoint)
    output.mkdir(parents=True)
    (output/".tmp").mkdir()
    for name in ("clock-contract.tsv", "fixed-io-timing-map.tsv"):
        source = prior/name
        if source.is_file():
            shutil.copyfile(source, output/name)
        elif name == "clock-contract.tsv":
            raise ValidationError("routed timing extraction lacks its original clock contract")
    session.update(output=str(output), routed_export={"checkpoint":str(checkpoint),
        "checkpoint_sha256":checkpoint_sha, "source_manifest":str(manifest_path),
        "source_manifest_sha256":file_sha256(manifest_path)})
    if replacements:
        session["queries"].update({kind:item["path"] for kind,item in replacements.items()})
        session["routed_export"]["query_overrides"] = replacements
    if selected is not None:
        session["queries"] = {kind:session["queries"][kind] for kind in selected}
        session["routed_export"]["query_kinds"] = list(selected)
    if session.get("word_phy_directory") is not None:
        session["word_phy_directory"] = str(output)
    write_json(output/"native-session.json", session, compact=True)
    settings = {"emuflow_session_out":str(output), "emuflow_session_manifest":str(output/"native-session.json"),
        "emuflow_timing_query_mode":session["timing_query_mode"],
        "emuflow_routed_checkpoint":str(checkpoint), "emuflow_expected_part":session["part"],
        "emuflow_source_prefix":session.get("source_hierarchy_prefix", ""),
        "emuflow_clock_contract":str(output/"clock-contract.tsv"),
        "emuflow_allow_generated_clocks":"true" if session.get("word_phy_directory") else "false",
        "emuflow_export_word_control_timing":"true" if session.get("word_phy_directory") and "boundary_timing" in session["queries"] else "false",
        "emuflow_io_timing_map":str(output/"fixed-io-timing-map.tsv") if (output/"fixed-io-timing-map.tsv").is_file() else ""}
    lines = [f"set {key} {_tcl_word(value)}" for key,value in settings.items()]
    lines.append("set emuflow_child_command [list "+" ".join(_tcl_word(value) for value in _child_command(output))+"]")
    config = output/"native-session.tcl"
    config.write_text("\n".join(lines)+"\n")
    return config


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
            lineage_path=lineage,source_hierarchy_prefix=session.get('source_hierarchy_prefix',''),
            mapped_sha256=session["mapped_index_sha256"], candidate_proofs_sha256=model_sha,
            checkpoint_sha256=checkpoint_sha, fpga=session["fpga"], selected_sources=sorted(selected), source_stage=stage)
    else:
        plan = {"status": "not-required", "requested_sources": [], "sources": [], "query_objects": [],
                "source_candidate_proofs_sha256": model_sha, "synthesized_checkpoint_sha256": checkpoint_sha, "source_stage":stage}
    write_json(out/"constant-frontier-plan.json", plan, compact=True)
    with (out/"constant-frontier-query.tsv").open("w") as stream:
        rows = csv.writer(stream, delimiter="\t", lineterminator="\n")
        rows.writerow(["source_origin", "native_cell_hex", "object_kind"])
        for item in plan["query_objects"]:
            rows.writerow([item["source_origin"], item["name"].encode().hex(), item.get("object_kind", "cell")])
    write_json(out/(stage+"-source-preflight.json"), {"status": "inspection-required" if selected else "pass", "source_stage":stage,
        "coverage": coverage, "proof": proof, "queries": resolutions, "requested_constant_sources": len(selected),
        "closed_constant_sources": len(plan["sources"]), "native_frontiers": len(plan["query_objects"])}, compact=True)
    # The preplacement index owns retained-frontier membership for repeatable
    # confirmation. Keep it until the routed preparation consumes this stage.
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
        lineage_path=out/(stage+'-lineage.sqlite'),source_hierarchy_prefix=session.get('source_hierarchy_prefix',''),
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


def prepare_checked_timing(session):
    """Optional elimination-proof analysis, separate from implementation/export."""
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
    # The sealed lineage owner also supplies routed placement to Chimew.
    # Retire only consumed tool snapshots, never that canonical indexed owner.
    for inventory in ("mapped_cells.tsv", "optimized_mapped_cells.tsv", "routed_mapped_cells.tsv"):
        (out / inventory).unlink(missing_ok=True)
    (out/(stage+'-lineage.sqlite')).unlink(missing_ok=True)
    return {"status": "pass", "query_classes": len(resolutions)}


def _prepare_measurable_timing(session, destination):
    """Resolve available native endpoints and retain every unassociated request."""
    mapped, functions, out = _paths(session)
    lineage = destination/"cell-lineage.sqlite"
    coverage = import_cell_lineage(mapped, functions, {"routed": out/"routed_mapped_cells.tsv"},
        lineage, mapped_sha256=session["mapped_index_sha256"], retain_stage="routed")
    # These post-route facts explain disconnected source regions. They never
    # impose state/INIT assumptions or prevent the vendor implementation.
    proof_path = destination/"routed-unobservable-sources.sqlite"
    try:
        proof = build_optimization_proofs(mapped, lineage, proof_path,
            mapped_sha256=session["mapped_index_sha256"], fpga=session["fpga"], unobservable_only=True)
        disposition_analysis = {"status":"pass"}
    except (ValidationError, sqlite3.Error) as error:
        proof = None
        disposition_analysis = {"status":"incomplete", "error":str(error)}
    resolutions = {}
    rx_scope = None
    with (destination/"timing-exports.tsv").open("w") as plan:
        exports = csv.writer(plan, delimiter="\t", lineterminator="\n")
        exports.writerow(["kind", "query_hex", "output_hex"])
        for kind, source in session["queries"].items():
            raw = destination/(kind+"-all.tsv")
            result = resolve_timing_query(mapped, functions, lineage, Path(source), raw,
                boundary=kind == "boundary_timing", stage="routed",
                optimization_proofs=proof_path if proof is not None else None,
                allow_eliminated=proof is not None,
                diagnostic_unresolved_path=destination/(kind+"-routed-unassociated.sqlite"))
            query = destination/(kind+"-resolved.tsv")
            unresolved = destination/(kind+"-unresolved.tsv")
            counts = [0, 0]
            unassociated_rows = eliminated_rows = 0
            rx_cells = set()
            rx_requested = rx_unresolved = rx_eliminated = 0
            with raw.open(newline="") as incoming, query.open("w", newline="") as ready, unresolved.open("w", newline="") as missing:
                reader = csv.reader(incoming, delimiter="\t")
                writers = [csv.writer(stream, delimiter="\t", lineterminator="\n") for stream in (ready, missing)]
                header = next(reader)
                for writer in writers:
                    writer.writerow(header)
                hints = 1 if kind == "boundary_timing" else 3
                for row in reader:
                    unassociated = "unresolved-source" in row[-hints:]
                    eliminated = any(value.startswith("optimized-away:") for value in row[-hints:])
                    unassociated_rows += int(unassociated)
                    eliminated_rows += int(eliminated)
                    index = int(unassociated and not eliminated)
                    writers[index].writerow(row)
                    counts[index] += 1
                    if kind == "boundary_timing" and row[1] == "rx":
                        rx_requested += 1
                        rx_unresolved += index
                        rx_eliminated += int(eliminated)
                        if not index and not eliminated:
                            for candidate in row[-1].split(","):
                                rx_cells.add(bytes.fromhex(candidate).decode("utf-8"))
            if sum(counts) != result["queries"] or unassociated_rows != result["unresolved_query_rows"]:
                raise ValidationError("routed timing query coverage differs")
            raw.unlink()
            result.update(output=str(query), measurable_queries=counts[0]-eliminated_rows,
                unresolved_queries=str(unresolved), unresolved_query_rows=counts[1],
                status="pass" if not counts[1] else "incomplete")
            if eliminated_rows:
                result["optimized_away_queries"] = eliminated_rows
            resolutions[kind] = result
            exports.writerow([kind, str(query).encode().hex(), str(destination/(kind+".tsv")).encode().hex()])
            if kind == "boundary_timing":
                scope_path = destination/"native-word-rx-sources.tsv"
                status = "incomplete" if rx_unresolved else "complete"
                with scope_path.open("w") as scope:
                    scope.write("status\trx_queries\tunresolved_rx_queries\tnative_cells\n")
                    scope.write(f"{status}\t{rx_requested}\t{rx_unresolved}\t{len(rx_cells)}\ncell_hex\n")
                    for cell in sorted(rx_cells):
                        scope.write(cell.encode("utf-8").hex()+"\n")
                rx_scope = {"status":status, "requested_queries":rx_requested,
                    "unresolved_queries":rx_unresolved, "native_cells":len(rx_cells),
                    "path":str(scope_path), "sha256":file_sha256(scope_path)}
                if rx_eliminated:
                    rx_scope["optimized_away_queries"] = rx_eliminated
    requested = sum(item["queries"] for item in resolutions.values())
    measurable = sum(item["measurable_queries"] for item in resolutions.values())
    eliminated = sum(item.get("optimized_away_queries",0) for item in resolutions.values())
    return {"status": "pass", "coverage": coverage, "resolutions": resolutions,
        "source_disposition_analysis":disposition_analysis,
        **({"optimization_proofs":proof} if proof is not None else {}),
        **({"rx_shadow_scope":rx_scope} if rx_scope is not None else {}),
        "timing_coverage": {"status": "complete" if requested == measurable+eliminated else "incomplete",
            "requested_queries": requested, "measurable_queries": measurable,
            "unresolved_queries": requested - measurable-eliminated,
            **({"optimized_away_queries":eliminated} if eliminated else {})}}


def prepare_timing(session):
    """Export routed measurements and source-bound unobservable dispositions.

    Missing source associations affect timing coverage, never the validity of
    the vendor implementation. They are neither zero delays nor removal facts.
    """
    out = Path(session["output"])
    recovery = session.get("routed_export")
    if recovery and file_sha256(Path(recovery["checkpoint"])) != recovery["checkpoint_sha256"]:
        raise ValidationError("retained routed checkpoint changed before timing extraction")
    result = _prepare_measurable_timing(session, out)
    write_json(out/"routed-query-preparation.json", result, compact=True)
    return {"status": "pass", "query_classes": len(result["resolutions"]),
        "timing_coverage": result["timing_coverage"]}


def prepare_diagnostic_timing(session):
    """Compatibility export into a separate observation directory."""
    diagnostic = Path(session["output"])/"timing-diagnostic"
    diagnostic.mkdir()
    result = _prepare_measurable_timing(session, diagnostic)
    result.update(status="diagnostic", qualified_physical_result=False)
    write_json(diagnostic/"preparation.json", result, compact=True)
    return {"status": "diagnostic", "query_classes": len(result["resolutions"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare-frontiers", "confirm-frontiers", "prepare-timing", "prepare-timing-diagnostic"))
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    started = time.monotonic()
    session = load_session(args.manifest)
    result = {"prepare-frontiers": prepare_frontiers, "confirm-frontiers": confirm_frontiers,
              "prepare-timing": prepare_timing, "prepare-timing-diagnostic": prepare_diagnostic_timing}[args.stage](session)
    print(json.dumps({"stage": args.stage, **result, "runtime_seconds": time.monotonic()-started}), flush=True)


if __name__ == "__main__":
    main()
