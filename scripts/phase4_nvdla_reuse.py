"""Seal and bind completed NVDLA frontend/STA inputs across a Phase3-only fix."""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import subprocess

from emuflow.prepared_inputs import recover_published_timing_report
from phase4_nvdla_compare import ROOT, CLOCKS, digest, read, require, stat_identity, write

# This exact downstream block is the only permitted change in the shared
# orchestration module. A filename allowlist alone would also allow a changed
# synthesis or STA call to reuse its old outputs.
_FRAME_BLOCK = '''if frame_slots is not None:
    raw_constraints = (
        read_json(effective_route_constraints.resolve())
        if effective_route_constraints is not None
        else {"schema": SYSTEM_ROUTE_CONSTRAINTS_SCHEMA}
    )
    if not isinstance(raw_constraints, dict):
        raise ValidationError("route constraints must be an object")
    raw_constraints = {**raw_constraints, "frame_slots": frame_slots}
    timing_root.mkdir(parents=True, exist_ok=True)
    if effective_route_constraints_path is None:
        effective_route_constraints_path = timing_root / "route-constraints.json"
    write_json(effective_route_constraints_path, raw_constraints)
    effective_route_constraints = effective_route_constraints_path
'''


def check_frame_only_change(old, new):
    old_tree, new_tree = ast.parse(old), ast.parse(new)
    block = ast.dump(ast.parse(_FRAME_BLOCK).body[0], include_attributes=False)
    removed = 0
    for node in new_tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_multi_fpga_flow":
            kept = []
            for statement in node.body:
                if ast.dump(statement, include_attributes=False) == block:
                    removed += 1
                else:
                    kept.append(statement)
            node.body = kept
    require(removed == 1 and ast.dump(old_tree, include_attributes=False) ==
            ast.dump(new_tree, include_attributes=False),
            "shared flow changed outside the audited Phase3 frame propagation block")



# These functions consume partition/routes/schedules, never original RTL or
# the unpartitioned STA publication. Keep module-level execution unchanged.
_DOWNSTREAM_FUNCTIONS = {
    "src/emuflow/phase5.py": {"run_phase5"},
    "src/emuflow/tdm.py": {"reconstruct_tdm_schedule_timing"},
    "src/emuflow/timing_routing.py": {
        "compress_sta_paths", "validate_member_constraints", "validate_native_system_routes",
        "route_system_native", "load_sta_paths", "reconstruct_system_route_timing"},
}


def check_downstream_only_change(name, old, new):
    def unchanged(source):
        tree = ast.parse(source)
        constant = ast.dump(ast.parse('MEMBER_CONSTRAINTS_FORMAT = "fixed-delay-shared-required/v1"').body[0])
        tree.body = [node for node in tree.body
                     if not (isinstance(node, ast.FunctionDef) and node.name in _DOWNSTREAM_FUNCTIONS[name])
                     and not (name.endswith("/timing_routing.py") and ast.dump(node) == constant)]
        return ast.dump(tree, include_attributes=False)
    require(unchanged(old) == unchanged(new), f"module changed outside downstream functions: {name}")


def _function(source, name):
    functions = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == name]
    require(len(functions) == 1, f"missing producer function {name}")
    return ast.dump(functions[0], include_attributes=False)


def commit_sources(commit, names):
    require(len(commit) == 40 and all(c in "0123456789abcdef" for c in commit),
            "reuse producer must be an exact 40-character Git commit")
    objects = subprocess.run(["git", "-C", str(ROOT), "cat-file", "--batch"],
        input="".join(f"{commit}:{name}\n" for name in names).encode(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    result, position = {}, 0
    for name in names:
        end = objects.index(b"\n", position)
        header = objects[position:end].split()
        require(len(header) == 3 and header[1] == b"blob", f"producer commit lacks {name}")
        size = int(header[2])
        result[name] = objects[end + 1:end + 1 + size]
        position = end + size + 2
    return result


def check_source_scope(old_provenance, current_provenance, producer_commit):
    old = old_provenance["source_sha256"]
    new = current_provenance["source_sha256"]
    require(set(old) == set(new), "producer source inventory changed")
    originals = commit_sources(producer_commit, list(old))
    require(all(hashlib.sha256(originals[name]).hexdigest() == value for name, value in old.items()),
            "recorded original producer does not match its Git commit")
    changed = sorted(name for name in old if old[name] != new[name])
    permitted = {"src/emuflow/multi_fpga_flow.py", "src/emuflow/prepared_inputs.py",
                 "scripts/phase4_nvdla_prefix.py", "scripts/phase4_nvdla_compare.py",
                 *_DOWNSTREAM_FUNCTIONS}
    require(set(changed) <= permitted, "frontend/STA producer dependency changed: " + ", ".join(set(changed) - permitted))
    if "src/emuflow/multi_fpga_flow.py" in changed:
        check_frame_only_change(originals["src/emuflow/multi_fpga_flow.py"],
                                (ROOT / "src/emuflow/multi_fpga_flow.py").read_text())
    for name in set(changed) & _DOWNSTREAM_FUNCTIONS.keys():
        check_downstream_only_change(name, originals[name], (ROOT / name).read_text())
    prefix = "scripts/phase4_nvdla_prefix.py"
    require(_function(originals[prefix], "flow_options") == _function((ROOT / prefix).read_text(), "flow_options"),
            "frontend/STA invocation options changed")
    exporter = "scripts/opensta/export_timing_path_database.tcl"
    original_exporter = commit_sources(producer_commit, [exporter])[exporter]
    require(hashlib.sha256(original_exporter).hexdigest() == digest(ROOT / exporter),
            "STA exporter changed")
    return {"producer_commit": producer_commit, "source_sha256": old,
            "runtime": old_provenance["runtime"], "allowed_changed_files": changed,
            "scope": "unchanged Phase1/STA implementation; audited downstream frame propagation, route/schedule observation and binding only",
            "sta_exporter_sha256": hashlib.sha256(original_exporter).hexdigest()}


def prepare_reuse(args, current_provenance, argument_parser):
    """Validate once, seal completed files, and create a read-only timing view."""
    previous = args.reuse_prefix
    old_provenance = read(previous / "provenance.json")
    require(old_provenance.get("schema") == "emuflow.nvdla-prefix-provenance/v1", "invalid original provenance")
    require(old_provenance.get("runtime") == current_provenance["runtime"], "prepared Python runtime differs")
    require(old_provenance.get("files"), "missing original input seals")
    for name, expected in old_provenance["files"].items():
        require(digest(name) == expected, f"original producer input changed: {name}")
    producer = check_source_scope(old_provenance, current_provenance, args.reuse_producer_commit)
    process = read(previous / "process.json")
    require(process.get("status") in {"failed", "pass"} and process.get("exit_code") is not None,
            "original prefix has no terminal process record")
    invocation = read(previous / "invocation.json")
    command = invocation.get("command", [])
    require(len(command) > 2 and Path(command[1]).name == "phase4_nvdla_prefix.py", "original prefix invocation missing")
    original_args = argument_parser.parse_args(command[2:])
    require(not original_args.reuse_prefix, "chained prepared-prefix recovery requires a new audited contract")
    for key in ("benchmark", "source_root", "platform", "architecture_timing_db", "yosys", "sta", "sta_max_paths"):
        old_value, new_value = getattr(original_args, key), getattr(args, key)
        if isinstance(old_value, Path):
            old_value, new_value = old_value.resolve(), new_value.resolve()
            require(str(old_value) in old_provenance["files"] or key in {"source_root", "platform"}, f"unsealed original {key}")
        if key != "platform":  # Compared structurally with the published platform below.
            require(old_value == new_value, f"prepared scientific option changed: {key}")
    require(invocation.get("sta_max_paths") == original_args.sta_max_paths, "original STA bounds disagree")
    require(Path(original_args.output).resolve() == previous.resolve(), "original output directory differs")
    old_root = Path(command[1]).resolve().parents[1]
    require(digest(old_root / "scripts/opensta/export_timing_path_database.tcl") == producer["sta_exporter_sha256"],
            "original executed STA exporter changed")
    for name, expected in old_provenance["source_sha256"].items():
        require(digest(old_root / name) == expected, f"original executed source changed: {name}")
    common = previous / "common"
    phase1, timing = common / "frontend/phase1", common / "timing"
    from emuflow.platform import Platform
    require(Platform.load(phase1 / "platform.normalized.json").to_dict() ==
            Platform.load(args.platform).to_dict(), "prepared hardware platform changed")
    timing_report = recover_published_timing_report(timing, CLOCKS, max_paths=original_args.sta_max_paths)
    paths = {previous / "provenance.json", previous / "invocation.json", previous / "process.json",
             phase1 / "design.emuir.sqlite", phase1 / "platform.normalized.json", timing / "opensta.log"}
    phase1_report = phase1 / "phase1_report.json"
    if not phase1_report.is_file():
        phase1_report = phase1 / "phase1_report.source.json"
    paths.add(phase1_report)
    manifests = {}
    for name in ("path-database.json", "partition-net-weights.json"):
        manifest = read(timing / name)
        manifests[name] = manifest
        paths.add(timing / name)
        for descriptor in manifest["payloads"].values():
            relative = Path(descriptor["path"])
            require(relative.name == str(relative), "prepared payload escapes timing directory")
            paths.add(timing / relative)
    before = stat_identity(str(path.resolve()) for path in paths)
    files = {str(path.resolve()): digest(path) for path in sorted(paths)}
    require(before == stat_identity(files), "prepared publication changed while being sealed")
    for manifest in manifests.values():
        for descriptor in manifest["payloads"].values():
            expected = descriptor.get("sha256")
            if expected is not None:
                require(files[str((timing / descriptor["path"]).resolve())] == expected, "prepared payload differs from published seal")
    producer["original_provenance_sha256"] = files[str((previous / "provenance.json").resolve())]
    timing_report["source_producer"] = producer
    prepared = args.output / "prepared-timing"
    prepared.mkdir()
    for name, manifest in manifests.items():
        write(prepared / name, manifest)
        for descriptor in manifest["payloads"].values():
            target = prepared / descriptor["path"]
            if not target.exists():
                target.symlink_to((timing / descriptor["path"]).resolve())
    write(prepared / "experiment-timing-report.json", timing_report)
    return {"schema": "emuflow.nvdla-prepared-prefix/v1", "source_producer": producer,
            "source_run": str(previous), "prepared_frontend": str(phase1),
            "prepared_timing": str(prepared), "files": files,
            "stat_identity": stat_identity(files), "sta": timing_report["sta"],
            "binding_files": {str(p): digest(p) for p in prepared.iterdir() if not p.is_symlink()},
            "binding_symlinks": {str(p): str(p.resolve()) for p in prepared.iterdir() if p.is_symlink()}}


def verify_reuse_binding(contract):
    require(contract.get("schema") == "emuflow.nvdla-prepared-prefix/v1", "invalid prepared prefix contract")
    # Payloads were hashed once immediately before launch. Detect mutation in
    # that same active run without rescanning multi-GB completed databases.
    observed = {name: list(value) for name, value in stat_identity(contract["files"]).items()}
    expected = {name: list(value) for name, value in contract["stat_identity"].items()}
    require(observed == expected, "sealed prepared payload changed before consumption")
    for name, expected in contract["binding_files"].items():
        require(digest(name) == expected, "prepared binding metadata changed")

    for name, target in contract["binding_symlinks"].items():
        require(Path(name).is_symlink() and str(Path(name).resolve()) == target,
                "prepared payload binding changed")
