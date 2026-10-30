"""Native-control Chimew contract tests; no physical/NVDLA qualification."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.common.errors import ValidationError
from tests.native_compile_state_fixture import (
    _pack,
    _unpack,
)
from emuflow.common.native_tools import native_file_generation
from emuflow.transport.chimew.xilinx_chimew_geometry import XilinxPackageGeometry
from tests.test_xilinx_chimew_lookahead import case, fixed_case


def _roundtrip(value):
    return _unpack(json.loads(json.dumps(_pack(value))))


def test_device_control_roundtrip_does_not_rescan_the_architecture(case):
    platform, _source, architecture, *_ = case
    original = XilinxPackageGeometry.open(architecture, platform.fpgas[0].part)
    state = _roundtrip(original.control_snapshot())
    with patch(
        "emuflow.transport.chimew.xilinx_chimew_geometry._native_package_geometry",
        side_effect=AssertionError("ArchitectureDB was rescanned"),
    ):
        restored = XilinxPackageGeometry.from_control_snapshot(state)
    assert restored == original
    assert len(json.dumps(_pack(state))) < 65536
    with pytest.raises(TypeError):
        restored.pins["forged"] = ("site", 0, 0)


@pytest.mark.parametrize(
    "mutation", ["generation", "digest", "coordinates", "source", "schema", "count"]
)
def test_device_control_rejects_changed_source_and_invalid_device_facts(case, mutation):
    platform, _source, architecture, *_ = case
    state = _roundtrip(
        XilinxPackageGeometry.open(
            architecture, platform.fpgas[0].part
        ).control_snapshot()
    )
    if mutation == "generation":
        state["generation"] = (*state["generation"][:-1], -1)
    elif mutation == "digest":
        state["sha256"] = "not-a-sha"
    elif mutation == "coordinates":
        key = next(iter(state["pins"]))
        state["pins"][key] = ("site", True, 0)
    elif mutation == "source":
        architecture.write_text(architecture.read_text() + "\n")
    elif mutation == "schema":
        state["schema"] += "-unknown"
    else:
        state["pins"] = {str(i): ("site", 0, 0) for i in range(16385)}
    with pytest.raises(ValidationError):
        XilinxPackageGeometry.from_control_snapshot(state)


@pytest.mark.parametrize("fixed_case",[2,4],indirect=True)
def test_native_preflight_reaches_the_same_chimew_checks_for_both_backends(case,tmp_path,request):
    from tests.chimew_admission_fixture import admitted
    from emuflow.flow import multi_fpga_flow as flow
    backend=request.node.callspec.params['case']
    _source,state=admitted(case,tmp_path/'admission',backend)
    value=json.loads(state.read_text())
    restored=_unpack(value['state'])
    config=json.loads(restored['native_chimew_configuration'].read_text())
    assert config['package_geometry']['part']==case[0].fpgas[0].part
    assert len(json.loads(restored['platform_path'].read_text())['fpgas'])==len(case[0].fpgas)
    assert restored['native_phase6_configuration'].is_file()
    assert restored['native_physical_policy'].is_file()
    assert restored['physical_backend']==backend
    assert restored['phase6_provider']=='chimew'
    assert restored['native_phase3_configuration'].is_file()
    assert not (tmp_path/'admission/native/frontend/phase1').exists()



@pytest.mark.parametrize("damage",["configuration-generation","source-generation","unknown-source","escaped-owner","board-namespace","architecture-generation"])
def test_native_chimew_controller_rejects_changed_admission_before_any_arm(case,tmp_path,request,damage):
    from tests.chimew_admission_fixture import admitted
    from tests.native_build import emuflow_native
    import subprocess
    backend=request.node.callspec.params['case']
    _source,previous=admitted(case,tmp_path/'admission',backend)
    value=json.loads(previous.read_text());state=_unpack(value['state']);root=state['output_dir']
    state.update(phase3_root=root/'partition',phase4_root=root/'system-route',phase5_root=root/'tdm',ir_path=root/'frontend/phase1/design.emuir.sqlite')
    policy=root/'.native-phase6-request/phase6-policy.json';policy.parent.mkdir();policy.write_text('{}')
    state.update(native_phase6_policy=policy,native_phase6_generation=tuple(map(str,native_file_generation(policy))))
    config=state['native_chimew_configuration'];original=config.read_bytes();data=json.loads(original)
    if damage=='configuration-generation':config.write_bytes(original+b' ')
    elif damage=='architecture-generation':case[2].write_bytes(case[2].read_bytes()+b' ')
    elif damage=='escaped-owner':
        changed=tmp_path/'escaped.json';changed.write_bytes(original);state['native_chimew_configuration']=changed
        state['native_chimew_configuration_generation']=tuple(map(str,native_file_generation(changed)))
    else:
        if damage=='source-generation':data['sources'][0]['generation'][2]+=1
        elif damage=='unknown-source':data['sources'][0]['role']='unowned'
        else:
            row=next(r for r in data['sources'] if r['role']=='board');other=tmp_path/'other-board.json';other.write_bytes(Path(row['path']).read_bytes())
            row.update(path=str(other),generation=list(native_file_generation(other)))
        config.write_text(json.dumps(data));state['native_chimew_configuration_generation']=tuple(map(str,native_file_generation(config)))
    value.update(stage='tdm',state=_pack(state));previous.write_text(json.dumps(value))
    receipt=tmp_path/'receipt.json'
    result=subprocess.run([str(emuflow_native()),'--report',str(receipt),'phase6','compile-chimew',str(previous),str(root)],capture_output=True,text=True)
    assert result.returncode, result.stdout
    operation=json.loads(receipt.read_text());assert operation['stage']=='phase6.compile-chimew' and operation['status']=='fail'
    assert not (root/'split').exists() and not (root/'phase6-comparison').exists()
    assert not list(root.glob('.chimew-compile-*'))
    # Error receipts may never replace original sources even when admission
    # itself is rejected. Include symlink aliases of the source as well.
    if damage=='configuration-generation':
        targets=[previous,config,case[2]]+[Path(row['path']) for row in data['sources']]
        for target in targets:
            before=target.read_bytes()
            for receipt_target in (target,tmp_path/'receipt-alias'):
                if receipt_target!=target:
                    receipt_target.unlink(missing_ok=True);receipt_target.symlink_to(target)
                rejected=subprocess.run([str(emuflow_native()),'--report',str(receipt_target),'phase6','compile-chimew',str(previous),str(root)],capture_output=True,text=True)
                assert rejected.returncode and 'aliases' in rejected.stderr
                assert target.read_bytes()==before
        assert not (root/'phase6-comparison').exists()



def test_retired_stage_adapter_and_python_chimew_bodies_are_absent():
    from importlib.util import find_spec
    from emuflow.flow import multi_fpga_flow as flow
    assert find_spec('emuflow.flow.native_compile_adapter') is None
    for name in ('_CompileContext','_compile_chimew_admission','_compile_split','_compile_physical','_run_selected_physical'):
        assert not hasattr(flow,name)


@pytest.mark.parametrize("fixed_case",[2,4],indirect=True)
def test_native_chimew_rejects_empty_schedule_before_building_either_arm(case,tmp_path,request):
    from tests.chimew_admission_fixture import admitted
    from tests.native_build import emuflow_native
    from tests.schedule_store_fixture import write_tdm_schedule_storage
    import shutil,subprocess
    _source,previous=admitted(case,tmp_path/'admission',request.node.callspec.params['case'])
    value=json.loads(previous.read_text());state=_unpack(value['state']);root=state['output_dir']
    state.update(phase3_root=root/'partition',phase4_root=root/'system-route',phase5_root=root/'tdm',ir_path=root/'frontend/phase1/design.emuir.sqlite',path_database_path=root/'timing/path-database.json')
    for key in ('phase3_root','phase4_root','phase5_root'):
        state[key].mkdir(parents=True,exist_ok=True)
    state['ir_path'].parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(case[1],state['ir_path'])
    # A current empty assignment/schedule pair is rejected before any split or
    # physical engine. Timing/policy bodies remain unconsumed at this boundary.
    from tests.phase3_store_fixture import write_assignment_index_fixture
    from emuflow.ir.ir import EmuIR
    ir=EmuIR.load(state['ir_path'])
    write_assignment_index_fixture(root/'partition/assignment.sqlite',ir,{
        'design':ir.value['design']['name'],'platform':case[0].name,'constraints':{},
        'instance_assignment':{row['id']:'fpga0' for row in ir.value['instances']},
        'cut_nets':[],'partitions':[{'fpga':fpga.id} for fpga in case[0].fpgas]})
    (root/'system-route/routes.json').write_bytes(case[5].read_bytes())
    state['path_database_path'].parent.mkdir(parents=True,exist_ok=True);state['path_database_path'].write_text('unconsumed timing')
    policy=root/'.native-phase6-request/phase6-policy.json';policy.parent.mkdir();policy.write_text('unconsumed policy')
    state.update(native_phase6_policy=policy,native_phase6_generation=tuple(map(str,native_file_generation(policy))))
    write_tdm_schedule_storage(root/'tdm/schedule.json',{'schema':'emuflow.tdm-schedule/v1','design':'empty','platform':case[0].name,'metrics':{'frame_slots':8},'routes':[],'entries':[],'domain_schedules':[],'demand_completions':[],'bank_allocations':[]})
    value.update(stage='tdm',state=_pack(state));previous.write_text(json.dumps(value))
    result=subprocess.run([str(emuflow_native()),'phase6','compile-chimew',str(previous),str(root)],capture_output=True,text=True)
    assert result.returncode and 'at least one scheduled inter-FPGA signal' in result.stderr
    assert not (root/'split').exists() and not (root/'phase6-comparison').exists()
    assert not list(root.glob('.chimew-compile-*'))
