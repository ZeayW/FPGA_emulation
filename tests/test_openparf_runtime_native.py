"""Native solver transaction tests; the executable is a controlled tool fixture."""
import copy
import json
from pathlib import Path
import shutil
from unittest import mock

import pytest
from emuflow.common.errors import ValidationError
from emuflow.physical.openparf.openparf import validate_openparf_runtime
from tests.openparf_runtime_host_fixture import run_openparf_result
from tests.openparf_runtime_host_fixture import validate_openparf_native_metrics


def metrics():
    return {'schema':'emuflow.openparf-native-convergence/v1','status':'pass',
        'stop_reason':'feasible-hpwl-patience','iterations':42,'restored_best_feasible':True,
        'best_feasible_hpwl':10.0,'final_legal_hpwl':11.0,
        'policy':{'sll_weighting':{'provider':'leaps-fixed-adam-step-v2','step_size':0.002,
            'delta_sll_ema_weight':0.9,'accepted_range':[0.0,1000.0]},
            'density_filler_sampling':{'provider':'density-bin-bounded-fillers-v2','status':'pass',
                'maximum_bin_span':[0.5,0.5],'resources':{'LUT':{'count':4,
                    'size':[0.5,0.5],'bin_size':[1.0,1.0],'bin_span':[0.5,0.5]}}}}}


def executable(root, placement='a0 0 0 1\n'):
    installation=root/'installation';(installation/'openparf').mkdir(parents=True)
    (installation/'openparf.py').write_text('# installed entry\n')
    python=root/'python';count=root/'count';control=root/'control.json'
    control.write_text('{}')
    python.write_text('#!/usr/bin/env python3\n'+f'''
import json,sys
from pathlib import Path
control=json.loads(Path({str(control)!r}).read_text())
if '-c' in sys.argv:
    if control.get('preflight_fail'): print('controlled preflight failure');sys.exit(7)
    sys.exit(0)
count=Path({str(count)!r});count.write_text(str(int(count.read_text())+1 if count.exists() else 1))
config=json.loads(Path(sys.argv[sys.argv.index('--config')+1]).read_text())
if control.get('mutate'): Path(control['mutate']).write_text('changed by tool')
if control.get('fail'): print('controlled solver failure');sys.exit(9)
result=Path(config['result_dir'])/(config['benchmark_name']+'.pl');result.parent.mkdir(parents=True,exist_ok=True)
if not control.get('omit_placement'): result.write_text({placement!r})
if not control.get('omit_metrics'): result.with_suffix('.native-metrics.json').write_text(json.dumps(control.get('metrics',{metrics()!r})))
print('controlled solver completed')
''')
    python.chmod(0o700)
    return {'install_root':installation,'python_executable':python},count,control


def prepared(root):
    root.mkdir()
    (root/'design.aux').write_text('immutable input\n')
    config=root/'openparf.json'
    config.write_text(json.dumps({'benchmark_name':'design','result_dir':str(root/'results'),
        'aux_input':str(root/'design.aux'),'emuflow_stable_global_placement':True}))
    (root/'manifest.json').write_text(json.dumps({'schema':'emuflow.openparf-atomic-manifest/v1','status':'pass','files':['openparf.json','design.aux']}))
    return config


def test_solver_checkpoint_reuses_only_sealed_inputs_and_same_runtime(tmp_path):
    options,count,control=executable(tmp_path)
    config=prepared(tmp_path/'input')
    first=run_openparf_result(config,checkpoint=True,**options)
    assert not first['reused'] and first['runtime']['status']=='pass'
    assert first['metrics']==metrics()
    assert run_openparf_result(config,checkpoint=True,**options)['reused']
    assert count.read_text()=='1'
    old=Path(first['placement']).read_bytes()
    (config.parent/'design.aux').write_text('different input\n')
    second=run_openparf_result(config,checkpoint=True,**options)
    assert not second['reused'] and second['placement']!=first['placement']
    assert Path(first['placement']).read_bytes()==old
    # Executable changes also invalidate the result, independent of input bytes.
    with options['python_executable'].open('a') as out:out.write('\n# new tool\n')
    assert not run_openparf_result(config,checkpoint=True,**options)['reused']
    assert count.read_text()=='3'


def test_checkpoint_survives_directory_relocation(tmp_path):
    options,count,_=executable(tmp_path);config=prepared(tmp_path/'input')
    run_openparf_result(config,checkpoint=True,**options)
    relocated=tmp_path/'relocated';shutil.copytree(config.parent,relocated)
    value=json.loads((relocated/'openparf.json').read_text())
    for key in ('result_dir','aux_input'):
        value[key]=str(relocated/Path(value[key]).relative_to(config.parent))
    (relocated/'openparf.json').write_text(json.dumps(value))
    result=run_openparf_result(relocated/'openparf.json',checkpoint=True,**options)
    assert result['reused'] and Path(result['placement']).is_relative_to(relocated)
    assert count.read_text()=='1'


@pytest.mark.parametrize('damage',['raw','metrics','checkpoint'])
def test_damaged_result_reexecutes_in_fresh_generation(tmp_path,damage):
    options,count,_=executable(tmp_path);config=prepared(tmp_path/'input')
    first=run_openparf_result(config,checkpoint=True,**options)
    Path(first[{'raw':'placement','metrics':'metrics_path','checkpoint':'checkpoint'}[damage]]).write_text('{}')
    second=run_openparf_result(config,checkpoint=True,**options)
    assert not second['reused'] and count.read_text()=='2'


@pytest.mark.parametrize('failure',['fail','omit_metrics','omit_placement','preflight_fail','invalid_metrics','mutate'])
def test_failed_retry_preserves_accepted_result_and_keeps_diagnostics(tmp_path,failure):
    options,count,control=executable(tmp_path);config=prepared(tmp_path/'input')
    first=run_openparf_result(config,checkpoint=True,**options)
    report=config.with_suffix('.runtime.json');accepted=report.read_bytes()
    old={p:Path(p).read_bytes() for p in (first['placement'],first['metrics_path'],first['checkpoint'])}
    (config.parent/'design.aux').write_text('changed input')
    if failure=='invalid_metrics':
        bad=metrics();bad['status']='fail';changes={'metrics':bad}
    elif failure=='mutate': changes={'mutate':str(config.parent/'design.aux')}
    else:changes={failure:True}
    control.write_text(json.dumps(changes))
    with pytest.raises(ValidationError):run_openparf_result(config,checkpoint=True,**options)
    assert report.read_bytes()==accepted
    for path,content in old.items():assert Path(path).read_bytes()==content
    assert list(config.parent.glob('solver.generation.*/preflight.log'))


def test_native_runtime_preflight_accepts_and_rejects_the_configured_tool(tmp_path):
    options,_,control=executable(tmp_path)
    assert validate_openparf_runtime(**options)['python']==str(options['python_executable'])
    control.write_text(json.dumps({'preflight_fail':True}))
    with pytest.raises(ValidationError,match='runtime preflight failed'):validate_openparf_runtime(**options)


@pytest.mark.parametrize('damage',['schema','restored','sll','count','span','iteration','nonfinite'])
def test_native_metrics_rejects_invalid_evidence(tmp_path,damage):
    value=metrics()
    if damage=='schema':value['schema']='unknown'
    elif damage=='restored':value['restored_best_feasible']=False
    elif damage=='sll':value['policy']['sll_weighting']['step_size']=0
    elif damage=='count':value['policy']['density_filler_sampling']['resources']['LUT']['count']=False
    elif damage=='span':value['policy']['density_filler_sampling']['resources']['LUT']['bin_span']=[0.6,0.5]
    elif damage=='iteration':value['iterations']=-1
    else:value['final_legal_hpwl']=float('nan')
    path=tmp_path/'metrics.json';path.write_text(json.dumps(value))
    with pytest.raises(RuntimeError):validate_openparf_native_metrics(path)


@pytest.mark.parametrize("clocks",[False,True])
def test_qualified_import_uses_native_solver_and_preserves_checkpoint_pair(tmp_path,clocks):
    from tests.test_openparf_placement_import_native import case
    from emuflow.physical.openparf.qualification_native import run_qualification
    with case(tmp_path,clocks=clocks) as (source,owner):
        tool_root=tmp_path/'tool';tool_root.mkdir()
        options,count,_=executable(tool_root,source['placement_path'].read_text())
        kwargs=dict(mapped_path=source['mapped_path'],packed_path=source['packed_path'],
            architecture_path=source['architecture_path'],output_dir=source['name_map_path'].parent,
            mapped_value=owner,openparf_install=options['install_root'],openparf_python=options['python_executable'])
        from emuflow.platform.architecture import ArchitectureDB
        from emuflow.physical.openparf import input_native
        with (mock.patch.object(ArchitectureDB,'load',side_effect=AssertionError('Python device graph')),
              mock.patch.object(input_native,'prepare_atomic_input',side_effect=AssertionError('Python stage coordinator'))):
            result=run_qualification(**kwargs)
        assert result['bridge']['status']=='pass'
        assert result['certificate']['runtime_validation']=='native-openparf'
        assert result['certificate']['native_convergence']['metrics']==metrics()
        checkpoint=json.loads(Path(result['openparf_result']['checkpoint']).read_text())
        raw=Path(result['openparf_result']['checkpoint']).parent/checkpoint['placement']
        assert raw.read_text()==source['placement_path'].read_text()
        if clocks: assert Path(result['placement'])!=raw
        again=run_qualification(**kwargs)
        assert again['openparf_result']['reused'] and count.read_text()=='1'


@pytest.mark.parametrize('failure',[None,'part','capacity','missing-region'])
def test_complete_native_region_admission_matches_the_independent_policy(tmp_path,failure):
    from tests.test_openparf_placement_import_native import case
    from tests.openparf_region_reference import _select_xilinx_slr_window
    from emuflow.physical.openparf.qualification_native import run_qualification
    with case(tmp_path) as (source,owner):
        architecture=json.loads(source['architecture_path'].read_text())
        for site in architecture['sites']:
            site['physical_region']={'slr':f"SLR{site['y']//2}",'clock_region':f"X0Y{site['y']//2}"}
        if failure=='capacity':architecture['sites']=architecture['sites'][:4]
        if failure=='missing-region':
            for site in architecture['sites']:site.pop('physical_region')
        source['architecture_path'].write_text(json.dumps(architecture))
        tool=tmp_path/'tool';tool.mkdir()
        options,count,_=executable(tool,source['placement_path'].read_text())
        kwargs=dict(mapped_path=source['mapped_path'],packed_path=source['packed_path'],
            architecture_path=source['architecture_path'],output_dir=tmp_path/'qualified',mapped_value=owner,
            openparf_install=options['install_root'],openparf_python=options['python_executable'],
            expected_part='wrong-part' if failure=='part' else architecture['part'])
        if failure:
            with pytest.raises(ValidationError,match={"part":"architecture part", "capacity":"1.5x placement headroom", "missing-region":"complete physical SLR inventory"}[failure]):run_qualification(**kwargs)
            assert not count.exists()
        else:
            logical={'schema':'emuflow.openparf-atomic-source/v1','clusters':[{'assignments':
                [{'cell_type':cell['type']} for cell in owner['modules'][owner.top]['cells'].values()]}]}
            expected=_select_xilinx_slr_window(source['packed_path'],source['architecture_path'],packed_value=logical)
            first=run_qualification(**kwargs)
            assert tuple(first['allowed_slrs'])==expected
            region=Path(first['region_path']);before=region.stat()
            assert json.loads(region.read_text())['policy']=={'provider':'full-device-routability-v1','resource_headroom':1.5}
            assert run_qualification(**kwargs)['openparf_result']['reused']
            assert region.stat()==before
            assert count.read_text()=='1'


def test_retired_placement_policy_is_rejected_before_tool_launch(tmp_path):
    options,count,_=executable(tmp_path);config=prepared(tmp_path/'input')
    value=json.loads(config.read_text());value['emuflow_continuous_global_guidance']=False
    config.write_text(json.dumps(value))
    with pytest.raises(ValidationError,match='continuous guidance placement is retired'):
        run_openparf_result(config,checkpoint=True,**options)
    assert not count.exists()


def test_complete_stage_failed_retry_preserves_prior_physical_owner(tmp_path):
    from tests.test_openparf_placement_import_native import case
    from emuflow.physical.openparf.qualification_native import run_qualification
    with case(tmp_path) as (source,owner):
        tool=tmp_path/'tool';tool.mkdir()
        options,count,control=executable(tool,source['placement_path'].read_text())
        kwargs=dict(mapped_path=source['mapped_path'],packed_path=source['packed_path'],
            architecture_path=source['architecture_path'],output_dir=tmp_path/'qualified',mapped_value=owner,
            openparf_install=options['install_root'],openparf_python=options['python_executable'])
        result=run_qualification(**kwargs)
        paths=[kwargs['output_dir']/'qualification-report.json',Path(result['certificate_path']),
            Path(result['bridge']['physical_handoff']),Path(result['openparf_result']['checkpoint'])]
        accepted={p:p.read_bytes() for p in paths}
        with options['python_executable'].open('a') as out:out.write('\n# tool revision\n')
        control.write_text(json.dumps({'fail':True}))
        with pytest.raises(ValidationError,match='controlled solver failure'):run_qualification(**kwargs)
        for path,data in accepted.items():assert path.read_bytes()==data


@pytest.mark.parametrize('change',['environment','extension','python-module','new-module'])
def test_checkpoint_binds_the_engine_implementation_and_runtime_settings(tmp_path,monkeypatch,change):
    options,count,_=executable(tmp_path);config=prepared(tmp_path/'input')
    package=options['install_root']/'openparf'
    (package/'core.so').write_bytes(b'implementation-one')
    (package/'flow.py').write_text('# implementation one\n')
    run_openparf_result(config,checkpoint=True,**options)
    if change=='environment':monkeypatch.setenv('EMUFLOW_OPENPARF_LIBRARY_PATH',str(tmp_path/'libraries'))
    elif change=='extension':(package/'core.so').write_bytes(b'implementation-two')
    elif change=='python-module':(package/'flow.py').write_text('# implementation two\n')
    else:(package/'extra.py').write_text('# new module\n')
    result=run_openparf_result(config,checkpoint=True,**options)
    assert not result['reused'] and count.read_text()=='2'
