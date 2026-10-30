"""Bounded file-reference host for native Chimew pipeline publication/audit."""
from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile
from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation, resolve_native_executable
from emuflow.common.tool_runtime import NativeToolProcess

_FIELDS=("device","inode","bytes","mtime_seconds","mtime_nanoseconds","ctime_seconds","ctime_nanoseconds")


def _invoke(command, sources, request_schema, directory, summary=None, *, region_count=None, executables=()):
    from emuflow.transport.pin_artifacts_native import _row
    native=resolve_native_executable("emuflow-native")
    for executable in executables:
        if executable is not None:
            try:
                same=Path(executable).samefile(native)
            except OSError:
                same=False
            if not same:
                raise ValidationError("Chimew pipeline requires the unified native executable")
    target=Path(directory).resolve()
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".chimew-pipeline-host-",dir=target.parent) as temporary:
        root=Path(temporary).resolve();request=root/"request.json";receipt=root/"operation.json"
        document={"schema":request_schema,"sources":sources}
        if region_count is not None:
            document["region_count"]=region_count
        request.write_text(json.dumps(document,sort_keys=True,allow_nan=False))
        schema="chimew-pipeline-input-v1" if region_count is not None else "chimew-pipeline-validation-input-v1"
        expected={(v["path"],v["schema"]):native_file_generation(v["path"]) for v in sources.values()}
        expected[(str(request),schema)]=native_file_generation(request)
        output=(target/"pipeline_report.json") if summary is None else root/"validation.json"
        argv=[native,"--report",str(receipt),"phase6",command,str(request),str(target)]
        if summary is not None:
            argv.append(str(output))
        with NativeToolProcess(native,argv,environment=dict(os.environ),scratch_parent=root) as process:
            completed=process.finish()
        if not receipt.is_file() or receipt.stat().st_size>2097152:
            if completed.returncode:
                raise ValidationError("native Chimew pipeline failed before receipt: "+completed.stdout[-4096:])
            raise ValidationError("native Chimew pipeline receipt is missing or exceeds bound")
        operation=json.loads(receipt.read_text())
        if completed.returncode:
            raise ValidationError("native Chimew pipeline failed: "+operation.get("error",{}).get("message",completed.stdout[-4096:]))
        if (operation.get("schema")!="emuflow.native-stage-result/v1" or operation.get("stage")!="phase6."+command
                or operation.get("status")!="pass" or len(operation.get("outputs",[]))!=1):
            raise ValidationError("native Chimew pipeline receipt identity differs")
        observed={}
        for row in operation.get("inputs",[]):
            key=(row["path"],row["schema"]);generation=tuple(row["generation"][f] for f in _FIELDS)
            if native_file_generation(row["path"])!=generation or (key in observed and observed[key]!=generation):
                raise ValidationError("native Chimew pipeline source generation changed")
            observed[key]=generation
        if any(observed.get(key)!=generation for key,generation in expected.items()):
            raise ValidationError("native Chimew pipeline source coverage differs")
        row=operation["outputs"][0]
        if row["path"]!=str(output) or row["schema"]!=("chimew-pipeline-report-v1" if summary is None else "chimew-pipeline-validation-v1") or native_file_generation(output)!=tuple(row["generation"][f] for f in _FIELDS):
            raise ValidationError("native Chimew pipeline output generation differs")
        if output.stat().st_size>262144:
            raise ValidationError("native Chimew pipeline control exceeds bound")
        value=json.loads(output.read_text())
        expected_schema="emuflow.chimew-phase6-pipeline-report/v1" if summary is None else "emuflow.chimew-pipeline-validation/v1"
        if value.get("schema")!=expected_schema or value.get("status")!="pass":
            raise ValidationError("native Chimew pipeline control identity differs")
        if summary is None and value.get("implementation_id")!="native-chimew-pipeline-v1":
            raise ValidationError("native Chimew pipeline implementation differs")
        return value


def run_native_chimew_pipeline(schedule,board,crossings,positions,rudy_input,bank_input,electrical,directory,
                              *,source_paths=None,region_count=31,executables=()):
    from emuflow.platform.platform import Platform
    from emuflow.transport.pin_artifacts_native import _board_sources,_row
    with tempfile.TemporaryDirectory(prefix=".chimew-board-host-") as temporary:
        sources=_board_sources(Platform.load(board),Path(temporary),Path(board).resolve())
    sources.update({role:_row(path,schema) for role,path,schema in (
        ("schedule",schedule,"physical-input-document-v1"),("crossings",crossings,"physical-input-document-v1"),
        ("positions",positions,"physical-input-document-v1"),("rudy_input",rudy_input,"physical-input-document-v1"),
        ("bank_input",bank_input,"chimew-bank-input-selector-v1"),("electrical",electrical,"physical-input-document-v1"))})
    if source_paths is not None:
        required={"routing","placement","netlist","architecture","package_pins"}
        if not required<=set(source_paths) or set(source_paths)-(required|{"timing_paths"}):
            raise ValidationError("Chimew source bundle must provide routing, placement, netlist, architecture, package pins, and only recognized optional sources")
        sources.update({"source_"+key:_row(path,"physical-input-document-v1") for key,path in source_paths.items()})
    return _invoke("run-chimew-pipeline",sources,"emuflow.chimew-pipeline-input/v1",directory,
                   region_count=region_count,executables=executables)


def audit_native_chimew_pipeline(directory):
    from emuflow.platform.platform import Platform
    from emuflow.transport.pin_artifacts_native import _board_sources
    from emuflow.transport.chimew.chimew_pipeline import _artifact_path
    target=Path(directory).resolve();report_path=target/"pipeline_report.json"
    if report_path.stat().st_size>262144:
        raise ValidationError("Chimew pipeline report exceeds bound")
    report=json.loads(report_path.read_text())
    board=_artifact_path(target,"platform",report.get("artifacts",{}).get("platform"))
    if board.stat().st_size>8388608:
        raise ValidationError("Chimew board control exceeds bound")
    with tempfile.TemporaryDirectory(prefix=".chimew-board-host-") as temporary:
        sources=_board_sources(Platform.load(board),Path(temporary),board)
    sources.pop("board")
    value=_invoke("audit-chimew-pipeline",sources,"emuflow.chimew-pipeline-validation-input/v1",target,summary=True)
    value.pop("schema")
    return value
