"""Endpoint replay recomputes raw numerical evidence without a vendor session."""
import csv
import json
import shutil

import pytest

from emuflow.errors import ValidationError
from emuflow.io import file_sha256
from emuflow.native_word_budgets import (_summarize_native_endpoints, read_native_word_budgets,
                                       collect_native_word_endpoint_timing)
from emuflow.store_publication import bind_store_source


@pytest.fixture
def observation(tmp_path):
    directory=tmp_path/'measured';directory.mkdir()
    header=('endpoint_hex','capture_clock_hex','slack_ns','data_on_worst_slack_path_ns',
            'start_hex','launch_clock_hex','requirement_ns','launch_edge_ns','capture_edge_ns')
    with (directory/'native-endpoint-slacks.tsv').open('w',newline='')as stream:
        writer=csv.writer(stream,delimiter='\t',lineterminator='\n');writer.writerow(header)
        for endpoint,slack in [('renamed/D',-3.125),('macro/RSTA',.5)]:
            writer.writerow((endpoint.encode().hex(),b'core'.hex(),slack,.25,b'rx/C'.hex(),b'tdm'.hex(),10,0,100))
    (directory/'native-endpoint-metrics.tsv').write_text('metric\tvalue\n'+
        ''.join(f'{key}\t{value}\n'for key,value in dict(target_pin_upper_bound=3,endpoint_checks=2,
            capture_clocks=1,applied_rx_budgets=2,missing_native_rx_budgets=0,reset_ceiling_ps=40001).items()))
    (directory/'missing-budget-rx.tsv').write_text('source_origin\tcapture_clock_hex\tcapture_edge\n')
    metrics,summary,files=_summarize_native_endpoints(directory)
    budgets=tmp_path/'budgets.tsv';budgets.write_text('source-owned fixture budgets\n')
    receipt=tmp_path/'budgets.tsv.sources.json';receipt.write_text(json.dumps(dict(
        schema='emuflow.native-word-rx-budgets/v1',budget=dict(path=str(budgets),sha256=file_sha256(budgets)))))
    parent=bind_store_source(receipt,'physical-input-document-v1')
    record={'observation':{'status':'complete','fpga':'fpga0','checkpoint':{'path':'fixture.dcp','sha256':'c'*64}}}
    result=dict(schema='emuflow.native-word-endpoint-timing/v1',fpga='fpga0',status='complete',metrics=metrics,
                summary=summary,checkpoint=dict(record['observation']['checkpoint']),files=files,
                sources=[dict(path=str(parent.path),schema=parent.schema,generation=list(parent.generation))])
    (directory/'native-endpoint-observation.json').write_text(json.dumps(result))
    return record,directory,result


def test_replay_recomputes_endpoint_tns_without_launching_vivado(observation,monkeypatch):
    record,directory,expected=observation
    monkeypatch.setattr('emuflow.vivado_physical.execute_vivado',lambda *a,**kw:pytest.fail('replay launched Vivado'))
    actual=read_native_word_budgets(record,directory)
    assert actual==expected
    assert actual['summary']==dict(endpoint_clock_checks=2,failing_checks=1,wns_ns=-3.125,tns_ns=-3.125)


def test_verified_raw_files_can_be_relocated_without_rewriting_their_measurements(observation,tmp_path):
    record,directory,expected=observation
    copied=tmp_path/'retained';shutil.copytree(directory,copied);shutil.rmtree(directory)
    assert read_native_word_budgets(record,copied)==expected


def test_cached_collection_keeps_verified_measurements_in_the_new_runtime(observation,tmp_path,monkeypatch):
    record,directory,expected=observation
    retained=tmp_path/'retained';retained.mkdir();shutil.move(str(directory),retained/'fpga0')
    monkeypatch.setattr('emuflow.native_word_budgets.observe_native_word_budgets',
                        lambda *a,**kw:pytest.fail('cached collection launched Vivado'))
    output=tmp_path/'runtime/native-endpoints'
    result=collect_native_word_endpoint_timing({'fpga0':record},tmp_path/'budgets.tsv',output,results_dir=retained)
    assert result['summary']==expected['summary'] and result['status']=='complete'
    shutil.rmtree(retained)
    assert read_native_word_budgets(record,output/'fpga0',expected_budgets=tmp_path/'budgets.tsv')==expected


@pytest.mark.parametrize('source,removed',[('dead',True),('dynamic',False)])
def test_missing_rx_requires_closed_source_elimination_and_never_gets_zero_delay(
        observation,tmp_path,monkeypatch,source,removed):
    from tests.native_build import emuflow_native
    from tests.test_vivado_optimization import VivadoOptimizationTests
    if not __import__('os').environ.get('EMUFLOW_NATIVE_ROOT'):
        native=tmp_path/'native';(native/'bin').mkdir(parents=True)
        (native/'bin/emuflow-native').symlink_to(emuflow_native())
        monkeypatch.setenv('EMUFLOW_NATIVE_ROOT',str(native))
    prepared=tmp_path/'prepared';prepared.mkdir()
    mapped,lineage,digest,origins=VivadoOptimizationTests().prepare(prepared,dynamic_dead=True)
    record,directory,value=observation
    record['input_identity']={'mapped_index':dict(path=str(mapped),sha256=digest)}
    record['lineage']=dict(path=str(lineage),sha256=file_sha256(lineage))
    origin='s'+str(origins[source])
    (directory/'missing-budget-rx.tsv').write_text('source_origin\tcapture_clock_hex\tcapture_edge\n'+
        f'{origin}\t{b"core".hex()}\t1\n')
    path=directory/'native-endpoint-metrics.tsv'
    path.write_text(path.read_text().replace('missing_native_rx_budgets\t0','missing_native_rx_budgets\t1'))
    metrics,summary,files=_summarize_native_endpoints(directory)
    value.update(status='incomplete',metrics=metrics,summary=summary,files=files)
    (directory/'native-endpoint-observation.json').write_text(json.dumps(value))
    retained=tmp_path/'retained';retained.mkdir();shutil.move(str(directory),retained/'fpga0')
    output=tmp_path/'runtime/native-endpoints'
    result=collect_native_word_endpoint_timing({'fpga0':record},tmp_path/'budgets.tsv',output,results_dir=retained)
    part=result['partitions']['fpga0'];coverage=part['rx_coverage']
    assert result['status']==('complete' if removed else 'incomplete')
    assert part['raw_observation_status']=='incomplete' and result['summary']==summary
    assert coverage['optimized_away_origins']==int(removed)
    assert coverage['dispositions'][0]['delay_ns'] is None
    assert coverage['dispositions'][0]['status']==('optimized-away' if removed else 'unresolved')
    replay=collect_native_word_endpoint_timing({'fpga0':record},tmp_path/'budgets.tsv',
        tmp_path/'replayed',results_dir=output)
    assert replay==result
    # Raw data and its missing-role receipt remain intact after qualification.
    assert read_native_word_budgets(record,output/'fpga0')==value
    record['lineage']['sha256']='0'*64
    with pytest.raises(ValidationError,match='different physical source'):
        collect_native_word_endpoint_timing({'fpga0':record},tmp_path/'budgets.tsv',
            tmp_path/'wrong-owner',results_dir=output)


@pytest.mark.parametrize('damage',['raw','summary','count','duplicate','missing','snapshot'])
def test_saved_endpoint_integrity_and_complete_census_are_checked(observation,damage):
    record,directory,result=observation
    if damage=='raw':
        with (directory/'native-endpoint-slacks.tsv').open('a')as stream:stream.write('corrupt\n')
    elif damage=='summary':result['summary']['tns_ns']=-1.
    elif damage=='count':result['metrics']['endpoint_checks']=3
    elif damage=='duplicate':
        path=directory/'native-endpoint-slacks.tsv';lines=path.read_text().splitlines()
        lines[-1]=lines[1];path.write_text('\n'.join(lines)+'\n');result['files'][path.name]['sha256']=file_sha256(path)
    elif damage=='missing':result['metrics']['missing_native_rx_budgets']=1
    else:record['observation']['checkpoint']['sha256']='d'*64
    (directory/'native-endpoint-observation.json').write_text(json.dumps(result))
    with pytest.raises(ValidationError):read_native_word_budgets(record,directory)
