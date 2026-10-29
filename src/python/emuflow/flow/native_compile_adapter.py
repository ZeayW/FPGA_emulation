"""Transitional phase adapters for the native one-shot compiler.

The C++ controller selects and executes the phase order. This module executes
one shared phase body, never the Python compiler, and transports only bounded
configuration/report state. Circuit populations remain in phase-owned stores.
"""
from __future__ import annotations

import argparse
import inspect
import json
import math
from pathlib import Path
import signal
import sys

from emuflow.common.errors import EmuFlowError
from emuflow.common.io import write_json
from emuflow.common.native_tools import native_file_generation


STAGES = ("preflight", "frontend", "timing-policy", "timing", "partition-policy", "partition", "route-policy", "route",
          "tdm", "split", "physical", "signoff", "finalize")
_LIMIT = 8 * 1024 * 1024


class _Cancelled(BaseException):
    pass


def _cancel(signum, _frame):
    # Unwind stream-consumer/tool contexts before exiting. The native parent
    # gives this cleanup time before escalation; nested supervisors then reap
    # their separately grouped Yosys/OpenSTA children as well.
    raise _Cancelled(f"native compile adapter cancelled by signal {signum}")


def _pack(value, depth=0):
    if depth > 64:
        raise EmuFlowError("native compiler control state is too deep")
    if isinstance(value, Path):
        return ["path", str(value)]
    if type(value) in {dict, list, tuple}:
        if len(value) > 16384:
            raise EmuFlowError("graph-sized collection in native compiler control state")
        if type(value) is dict:
            if not all(isinstance(key, str) for key in value):
                raise EmuFlowError("invalid native compiler control key")
            return ["map", {key: _pack(item, depth + 1) for key, item in value.items()}]
        return ["tuple" if type(value) is tuple else "list", [_pack(item, depth + 1) for item in value]]
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise EmuFlowError(f"unsupported object in native compiler control state: {type(value).__name__}")


def _unpack(value, depth=0):
    if depth > 64:
        raise EmuFlowError("native compiler control state is too deep")
    if type(value) is list:
        if len(value) != 2:
            raise EmuFlowError("invalid native compiler control envelope")
        kind, body = value
        if kind == "phase-report":
            if (type(body) is not dict or set(body) != {"schema", "path", "report_schema", "generation"}
                    or body.get("schema") != "emuflow.phase-report-reference/v1"
                    or body.get("report_schema") != "emuflow.phase5-report/v1"
                    or type(body.get("path")) is not str
                    or not Path(body["path"]).is_absolute()
                    or type(body.get("generation")) is not list
                    or len(body["generation"]) != 7
                    or any(type(item) is not int for item in body["generation"])):
                raise EmuFlowError("invalid native phase report reference")
            path = Path(body["path"])
            expected = tuple(body["generation"])
            if (path.is_symlink() or native_file_generation(path) != expected
                    or path.stat().st_size > 256 * 1024):
                raise EmuFlowError("native phase report generation changed")
            from emuflow.scheduling.phase5 import validate_phase5_generation
            from emuflow.flow.multi_fpga_flow import _stage_summary

            report = validate_phase5_generation(path, managed_root_sealed=True)["report"]
            if native_file_generation(path) != expected:
                raise EmuFlowError("native phase report generation changed")
            return _stage_summary("tdm", report)
        if kind == "timing-report":
            if (type(body) is not dict or set(body)!={"schema","path","generation"}
                    or body.get("schema")!="phase2-report-v1" or type(body.get("path")) is not str
                    or not Path(body["path"]).is_absolute() or type(body.get("generation")) is not list
                    or len(body["generation"])!=7 or any(type(item) is not int for item in body["generation"])):
                raise EmuFlowError("invalid native timing report reference")
            path=Path(body['path']);generation=tuple(body['generation'])
            if path.is_symlink() or native_file_generation(path)!=generation or path.stat().st_size>262144:
                raise EmuFlowError('native timing report generation changed')
            report=json.loads(path.read_text())
            if (report.get('schema')!='emuflow.phase2-report/v1' or report.get('status')!='pass'
                    or report.get('stage_implementation_id')!='native-phase2-v1'):
                raise EmuFlowError('native timing report identity disagrees')
            if native_file_generation(path)!=generation:
                raise EmuFlowError('native timing report generation changed')
            return report
        if kind == "partition-report":
            if (type(body) is not dict or set(body) != {"schema", "path", "generation"}
                    or body.get("schema") != "phase3-report-v1" or type(body.get("path")) is not str
                    or not Path(body['path']).is_absolute() or type(body.get('generation')) is not list
                    or len(body['generation']) != 7 or any(type(item) is not int for item in body['generation'])):
                raise EmuFlowError("invalid native partition report reference")
            path = Path(body['path']);generation = tuple(body['generation'])
            if path.is_symlink() or native_file_generation(path) != generation or path.stat().st_size > _LIMIT:
                raise EmuFlowError("native partition report generation changed")
            report = json.loads(path.read_text())
            if (report.get('schema') != 'emuflow.phase3-report/v1' or report.get('status') != 'pass'
                    or report.get('stage_implementation_id') != 'native-phase3-v1'):
                raise EmuFlowError("native partition report identity disagrees")
            if native_file_generation(path) != generation:
                raise EmuFlowError("native partition report generation changed")
            from emuflow.flow.multi_fpga_flow import _stage_summary
            return _stage_summary('partition', report)
        if kind == "route-report":
            if (type(body) is not dict or set(body)!={"schema","path","generation"}
                    or body.get("schema")!="phase4-report-v1" or type(body.get("path")) is not str
                    or not Path(body["path"]).is_absolute() or type(body.get("generation")) is not list
                    or len(body["generation"])!=7 or any(type(item) is not int for item in body["generation"])):
                raise EmuFlowError("invalid native route report reference")
            path=Path(body['path']);generation=tuple(body['generation'])
            if path.is_symlink() or native_file_generation(path)!=generation or path.stat().st_size>_LIMIT:
                raise EmuFlowError('native route report generation changed')
            report=json.loads(path.read_text())
            if (report.get('schema')!='emuflow.phase4-report/v1' or report.get('status')!='pass'
                    or report.get('stage_implementation_id')!='native-phase4-v1'):
                raise EmuFlowError('native route report identity disagrees')
            if native_file_generation(path)!=generation:
                raise EmuFlowError('native route report generation changed')
            from emuflow.flow.multi_fpga_flow import _stage_summary
            return _stage_summary('system_route',report)
        if kind == "split-report":
            if (type(body) is not dict or set(body)!={"schema","path","generation"}
                    or body.get("schema")!="phase6-report-v1" or type(body.get("path")) is not str
                    or not Path(body["path"]).is_absolute() or type(body.get("generation")) is not list
                    or len(body["generation"])!=7 or any(type(item) is not int for item in body["generation"])):
                raise EmuFlowError("invalid native split report reference")
            path=Path(body['path']);generation=tuple(body['generation'])
            if path.is_symlink() or native_file_generation(path)!=generation or path.stat().st_size>_LIMIT:
                raise EmuFlowError('native split report generation changed')
            report=json.loads(path.read_text())
            if (report.get('schema')!='emuflow.phase6-report/v1' or report.get('status')!='pass'
                    or report.get('stage_implementation_id')!='native-baseline-phase6-v1'):
                raise EmuFlowError('native split report identity disagrees')
            if native_file_generation(path)!=generation:
                raise EmuFlowError('native split report generation changed')
            from emuflow.flow.multi_fpga_flow import _stage_summary
            return _stage_summary('split',report)
        if kind == "frontend-report":
            if (type(body) is not dict or set(body) != {"schema", "path", "generation"}
                    or body.get("schema") != "phase1-summary-v1" or not isinstance(body.get("path"), str)
                    or not Path(body["path"]).is_absolute() or type(body.get("generation")) is not list or len(body["generation"]) != 7):
                raise EmuFlowError("invalid native frontend report reference")
            from emuflow.frontend.frontend_native import read_frontend_summary
            return read_frontend_summary(body["path"], body["generation"])
        if kind == "path" and isinstance(body, str):
            return Path(body)
        if kind == "map" and type(body) is dict and len(body) <= 16384:
            return {key: _unpack(item, depth + 1) for key, item in body.items()}
        if kind in {"tuple", "list"} and type(body) is list and len(body) <= 16384:
            result = [_unpack(item, depth + 1) for item in body]
            return tuple(result) if kind == "tuple" else result
        raise EmuFlowError("invalid native compiler control type")
    if value is None or type(value) in {str, bool, int} or (type(value) is float and math.isfinite(value)):
        return value
    raise EmuFlowError("invalid native compiler control scalar")


def _read(path):
    if path.stat().st_size > _LIMIT:
        raise EmuFlowError("native compiler control file exceeds bound")
    return json.loads(path.read_text(encoding="utf-8"))


def _plain(value):
    if isinstance(value, Path):
        return str(value)
    if type(value) is dict:
        return {key: _plain(item) for key, item in value.items()}
    if type(value) in {tuple, list}:
        return [_plain(item) for item in value]
    return value


def _initial_context(request):
    from emuflow.flow.multi_fpga_flow import _CompileContext, run_multi_fpga_flow

    options = request["options"]
    if type(options) is not dict:
        raise EmuFlowError("native compile options must be an object")
    signature = inspect.signature(run_multi_fpga_flow)
    # Signature binding is the single current-default contract during this
    # transition; no duplicated or silently inferred native defaults.
    bound = signature.bind(**options)
    bound.apply_defaults()
    for name, value in list(bound.arguments.items()):
        annotation = str(signature.parameters[name].annotation)
        if value is not None and "Path" in annotation:
            def path(item):
                if not isinstance(item, (str, Path)) or not Path(item).is_absolute():
                    raise EmuFlowError(f"native compile {name} requires absolute paths")
                return Path(item)
            bound.arguments[name] = tuple(path(item) for item in value) if "Iterable" in annotation else path(value)
    return _CompileContext(dict(bound.arguments))


def _control_snapshot(ctx, stage, flow):
    state = {key: item for key, item in vars(ctx).items() if not key.startswith("_")}
    catalog = state.get("chimew_package_geometry")
    if catalog is not None:
        from emuflow.transport.chimew.xilinx_chimew_geometry import XilinxPackageGeometry

        if type(catalog) is not XilinxPackageGeometry:
            raise EmuFlowError("invalid native Chimew package catalog owner")
        state["chimew_package_geometry"] = catalog.control_snapshot()
    comparison = state.get("phase6_comparison")
    if comparison is not None:
        # Each baseline report already belongs to the comparison. Carry it
        # once, then restore aliases in the next adapter, not JSON copies.
        baseline = comparison["baseline"]
        for field, key in (
            ("baseline_physical_report", "physical"),
            ("baseline_runtime_report", "runtime"),
        ):
            if state.get(field) is not baseline[key]:
                raise EmuFlowError("native Chimew baseline report owners disagree")
            state.pop(field)
    for field, kind in (("frontend_report", "frontend"), ("phase3_report", "partition"),
                        ("phase4_report", "system_route"), ("phase5_report", "tdm"),
                        ("phase6_report", "split")):
        if state.get(field) is not None:
            state[field] = flow._stage_summary(kind, state[field])
    if state.get("physical_report") is not None:
        state["physical_report"] = flow._physical_report_summary(state["physical_report"])
    return state


def _restore_control_state(state):
    if type(state) is not dict:
        raise EmuFlowError("invalid native compiler context")
    catalog = state.get("chimew_package_geometry")
    if catalog is not None:
        from emuflow.transport.chimew.xilinx_chimew_geometry import XilinxPackageGeometry

        state["chimew_package_geometry"] = XilinxPackageGeometry.from_control_snapshot(
            catalog
        )
    comparison = state.get("phase6_comparison")
    if comparison is not None:
        if (
            type(comparison) is not dict
            or type(comparison.get("baseline")) is not dict
            or not all(
                type(comparison["baseline"].get(key)) is dict
                for key in ("physical", "runtime")
            )
        ):
            raise EmuFlowError("invalid native Chimew baseline reports")
        for field, key in (
            ("baseline_physical_report", "physical"),
            ("baseline_runtime_report", "runtime"),
        ):
            if field in state:
                raise EmuFlowError("duplicate native Chimew baseline report owner")
            state[field] = comparison["baseline"][key]
    return state


def execute(stage, request_path, previous_path, output_path, run_id):
    if stage == "signoff":
        raise EmuFlowError("native compiler signoff is owned by the C++ Phase 7C stage")
    if stage == "finalize":
        raise EmuFlowError("native compiler finalize is owned by the C++ controller")
    if stage == "route":
        raise EmuFlowError("native compiler route is owned by the C++ Phase 4 stage")
    if stage == "tdm":
        raise EmuFlowError("native compiler TDM is owned by the C++ Phase 5 stage")
    if stage == "partition":
        raise EmuFlowError("native compiler partition is owned by the C++ Phase 3 stage")
    from emuflow.flow import multi_fpga_flow as flow

    index = STAGES.index(stage)
    request = _read(request_path)
    generation = [str(value) for value in native_file_generation(request_path)]
    if index == 0:
        if previous_path is not None:
            raise EmuFlowError("native preflight cannot consume prior state")
        ctx = _initial_context(request)
    else:
        previous = _read(previous_path)
        if (previous.get("schema") != "emuflow.native-compile-state/v1" or
                previous.get("run_id") != run_id or previous.get("status") != "pass" or
                previous.get("stage") != STAGES[index - 1] or
                previous.get("request_generation") != generation):
            raise EmuFlowError("mixed or out-of-order native compiler state")
        ctx = flow._CompileContext(
            _restore_control_state(_unpack(previous["state"]))
        )
    function = getattr(flow, "_compile_" + {"tdm": "schedule"}.get(stage, stage.replace("-", "_")))
    if stage=="split" and ctx.phase6_provider=="baseline":
        raise EmuFlowError('native compiler baseline Phase 6 is owned by the C++ stage')
    if stage=="physical" and ctx.phase6_provider=="baseline":
        raise EmuFlowError('native compiler physical boundary is owned by the C++ controller')
    function(ctx)
    value = {"schema": "emuflow.native-compile-state/v1", "run_id": run_id,
             "stage": stage, "status": "pass", "request_generation": generation,
             "state": _pack(_control_snapshot(ctx, stage, flow))}
    if index == 0:
        names = inspect.signature(flow.run_multi_fpga_flow).parameters
        value["effective_options"] = _plain({name: getattr(ctx, name) for name in names})
    if len(json.dumps(value, separators=(",", ":"), allow_nan=False).encode()) > _LIMIT:
        raise EmuFlowError("native compiler control state exceeds bound")
    write_json(output_path, value, compact=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=[stage for stage in STAGES if stage not in {"tdm", "partition", "route", "signoff", "finalize"}], required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _cancel)
    try:
        execute(args.stage, args.request, args.previous, args.output, args.run_id)
    except BaseException as error:
        # No partially populated state is reusable. The parent retains a
        # bounded failure message and does not advance or retry a phase.
        print(f"native compile {args.stage}: {type(error).__name__}: {str(error)[:16384]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
