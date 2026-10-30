"""Test-only bounded state oracle and readers for native controller fixtures."""
from pathlib import Path
import json, math
from emuflow.common.errors import EmuFlowError
from emuflow.common.native_tools import native_file_generation
_LIMIT=8*1024*1024
STAGES=("preflight","frontend","timing-policy","timing","partition-policy","partition","route-policy","route","tdm","split","physical","signoff","finalize")
class CompileContext:
 def __init__(self,values):self.__dict__.update(values)

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
            from emuflow_tools.stage_clients.schedule import validate_phase5_generation
            from emuflow_tools.flow_validation import _stage_summary

            report = validate_phase5_generation(path, managed_root_sealed=True)["report"]
            if native_file_generation(path) != expected:
                raise EmuFlowError("native phase report generation changed")
            return _stage_summary("tdm", report)
        if kind == "prepared-timing-report":
            if (type(body) is not dict or set(body) != {"schema", "path", "generation"}
                    or body["schema"] != "prepared-timing-summary-v1"
                    or not isinstance(body["path"], str) or not Path(body["path"]).is_absolute()
                    or type(body["generation"]) is not list or len(body["generation"]) != 7
                    or any(type(item) is not int for item in body["generation"])):
                raise EmuFlowError("invalid prepared timing report reference")
            from emuflow.frontend.inputs import read_timing_summary
            return read_timing_summary(body["path"], body["generation"])["summary"]
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
            from emuflow_tools.flow_validation import _stage_summary
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
            from emuflow_tools.flow_validation import _stage_summary
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
            from emuflow_tools.flow_validation import _stage_summary
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
