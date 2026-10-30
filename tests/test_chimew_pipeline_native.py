"""Whole native pipeline ownership, relocation and protocol rejection."""
import copy
import json
from pathlib import Path
import subprocess
from unittest.mock import patch
import pytest

from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256
from emuflow.transport.chimew.chimew_pipeline import run_chimew_phase6_pipeline,validate_chimew_phase6_pipeline
from tests.chimew_pipeline_fixture import pipeline_inputs
from tests.native_build import emuflow_native
from tests.test_xilinx_chimew_lookahead import case
from tests.test_chimew_fixed_wiring import fixed_case


def run(paths,output,sources=None):
    return run_chimew_phase6_pipeline(*(paths[name] for name in ("schedule","platform","crossings","positions","rudy_input","bank_input","electrical")),output,source_paths=sources)


@pytest.mark.parametrize("bound",[False,True])
def test_native_generation_does_not_construct_python_record_views(tmp_path,bound):
    paths,sources=pipeline_inputs(tmp_path,byte_bound=bound,opaque_prefix=b"{opaque bytes\x00\xff ")
    with patch("emuflow.scheduling.tdm_schedule_storage.read_tdm_schedule",side_effect=AssertionError("Python schedule")), \
         patch("emuflow.transport.chimew.chimew_bank_channel_storage.read_chimew_bank_channel_input",side_effect=AssertionError("Python bank input")), \
         patch("emuflow.transport.chimew.chimew_bank_channel_report_storage.read_chimew_bank_channel_report",side_effect=AssertionError("Python assignment")), \
         patch("emuflow.transport.pin_plan_storage.read_pin_plan",side_effect=AssertionError("Python pins")):
        report=run(paths,tmp_path/"pipeline",sources)
        checked=validate_chimew_phase6_pipeline(tmp_path/"pipeline")
    assert report["implementation_id"]=="native-chimew-pipeline-v1"
    assert checked["signals"]==2 and checked["groups"]==2
    assert checked["artifact_hashes_verified"]==len(report["artifacts"])
    assert len(list((tmp_path/"pipeline/generations").iterdir()))==1


def test_pipeline_receipt_and_audit_cannot_overwrite_owner_files(tmp_path):
    paths,sources=pipeline_inputs(tmp_path)
    output=tmp_path/"pipeline";report=run(paths,output,sources)
    request=tmp_path/"request.json"
    roles={name:{"path":str(paths[key]),"schema":schema} for name,key,schema in (
        ("schedule","schedule","physical-input-document-v1"),("board","platform","boarddb-v1"),
        ("crossings","crossings","physical-input-document-v1"),("positions","positions","physical-input-document-v1"),
        ("rudy_input","rudy_input","physical-input-document-v1"),("bank_input","bank_input","chimew-bank-input-selector-v1"),
        ("electrical","electrical","physical-input-document-v1"))}
    roles.update({"source_"+name:{"path":str(path),"schema":"physical-input-document-v1"} for name,path in sources.items()})
    request.write_text(json.dumps({"schema":"emuflow.chimew-pipeline-input/v1","region_count":31,"sources":roles}))
    schedule=json.loads(paths["schedule"].read_text());bank=json.loads(paths["bank_input"].read_text())
    original_files=[request,*paths.values(),*sources.values(),paths["schedule"].parent/schedule["artifact"],paths["bank_input"].parent/bank["artifact"]]
    owned=[output/record["path"] for record in report["artifacts"].values()]
    lock=output.parent/("."+output.name+".pipeline.lock")
    original={path:path.read_bytes() for path in (*original_files,*owned,output/"pipeline_report.json",lock)}
    for receipt in (*original_files,*owned,output/"pipeline_report.json",lock):
        result=subprocess.run([str(emuflow_native()),"--report",str(receipt),"phase6","run-chimew-pipeline",str(request),str(output)],capture_output=True,text=True)
        assert result.returncode and "alias" in result.stderr+result.stdout or result.returncode and "namespace" in result.stderr+result.stdout
        assert original=={path:path.read_bytes() for path in original}
    audit=tmp_path/"audit.json";audit.write_text(json.dumps({"schema":"emuflow.chimew-pipeline-validation-input/v1","sources":{}}))
    for summary in (audit,*owned,output/"pipeline_report.json",lock):
        result=subprocess.run([str(emuflow_native()),"phase6","audit-chimew-pipeline",str(audit),str(output),str(summary)],capture_output=True,text=True)
        assert result.returncode
        assert original=={path:path.read_bytes() for path in original}
    summary=tmp_path/"same-receipt.json";summary.write_bytes(b"previous summary")
    result=subprocess.run([str(emuflow_native()),"--report",str(summary),"phase6","audit-chimew-pipeline",str(audit),str(output),str(summary)],capture_output=True,text=True)
    assert result.returncode and summary.read_bytes()==b"previous summary"


@pytest.mark.parametrize("defect",["schema","role","region","opaque-role"])
def test_bad_native_request_preserves_the_complete_commit(tmp_path,defect):
    paths,sources=pipeline_inputs(tmp_path)
    output=tmp_path/"pipeline";run(paths,output,sources)
    previous={path.relative_to(output):path.read_bytes() for path in output.rglob("*") if path.is_file()}
    request=tmp_path/"request.json"
    values={"schema":"emuflow.chimew-pipeline-input/v1","region_count":31,"sources":{}}
    if defect=="schema":values["schema"]="retired"
    elif defect=="role":values["sources"]={"unknown":{"path":str(paths["platform"]),"schema":"boarddb-v1"}}
    elif defect=="region":values["region_count"]=True
    else:values["sources"]={"source_unknown":{"path":str(paths["platform"]),"schema":"physical-input-document-v1"}}
    request.write_text(json.dumps(values))
    result=subprocess.run([str(emuflow_native()),"phase6","run-chimew-pipeline",str(request),str(output)],capture_output=True,text=True)
    assert result.returncode
    assert previous=={path.relative_to(output):path.read_bytes() for path in output.rglob("*") if path.is_file()}


@pytest.mark.parametrize("fixed_case",[2,4],indirect=True)
def test_native_pipeline_joins_real_fixed_wiring_and_selected_backend_geometry(case,tmp_path):
    from emuflow.transport.chimew.chimew_lookahead import materialize_xilinx_chimew_inputs
    from emuflow.platform.default_platform import research_platform_path
    platform,source,architecture,physical,schedule,routes=case
    prepared=materialize_xilinx_chimew_inputs(ir_path=source,schedule_path=schedule,routes_path=routes,
        platform_path=research_platform_path(len(platform.fpgas)),architecture_path=architecture,physical_report=physical,output_dir=tmp_path/"lookahead")
    artifacts=prepared["artifacts"]
    output=tmp_path/"pipeline"
    report=run_chimew_phase6_pipeline(Path(artifacts["schedule"]["path"]),research_platform_path(len(platform.fpgas)),
        *(Path(artifacts[label]["path"]) for label in ("crossings","positions","rudy_input","bank_channel_input","electrical_map")),output,
        source_paths={name:Path(path) for name,path in prepared["sources"].items()})
    assert validate_chimew_phase6_pipeline(output)["status"]=="pass"
    assert report["implementation_id"]=="native-chimew-pipeline-v1"
