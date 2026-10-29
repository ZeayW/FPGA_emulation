"""Compose completed native word observations without architectural pin queries.

The setup model, independent witness checks and original-member observation
ledger have separate owners. Missing original identities never acquire zero
delay, and a clock-family maximum never becomes an endpoint TNS.
"""
from contextlib import closing
import csv
import json
import math
from pathlib import Path
import re
import sqlite3

from .errors import ValidationError
from .io import file_sha256,read_json,write_json
from .store_publication import StoreSource,bind_store_source,require_store_sources_unchanged


def _require(condition,message):
    if not condition:raise ValidationError(message)


def _artifact(reference,parents,schema='physical-input-document-v1'):
    path=Path(reference['path']).resolve()
    parent=bind_store_source(path,schema)
    _require(file_sha256(path)==reference['sha256'],'native Phase7c artifact changed: '+path.name)
    parents.append(parent)
    return path


def _metadata(path):
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True))as db:
        result={}
        for key,value in db.execute('SELECT * FROM metadata'):
            try:result[key]=json.loads(value)
            except json.JSONDecodeError:result[key]=value
        return result


def _source_contexts(evidence,summary,output,parents,inputs):
    """Attach observed source membership without changing numerical arcs."""
    from .native_word_context import qualify_native_word_source_contexts
    inputs={}if inputs is None else inputs
    _require(isinstance(inputs,dict)and not(set(inputs)-set(evidence)),
             'native source context inputs name an unknown partition')
    records={};pending=[]
    for index,(fpga,record)in enumerate(sorted(evidence.items())):
        arguments=inputs.get(fpga)
        if arguments is None and record.get('word_preparation')is not None:
            bundle=record['observation'].get('source_context')
            if bundle is not None:arguments={**bundle,'word_preparation':record['word_preparation']}
        if arguments is None:
            pending.append(fpga);continue
        _require(isinstance(arguments,dict)and {'runtime_constraints','word_preparation'}<=set(arguments)and
                 not(set(arguments)-{'runtime_constraints','word_preparation','capture_census','source_properties'}),
                 'native source context input roles differ')
        path=output/f'native-source-contexts-{index}.json'
        result=qualify_native_word_source_contexts(record,**arguments,
            calendar_policy=summary.get('word_phy_implementation',{}).get('physical_calendar_policy'),output=path)
        parents.append(bind_store_source(path,'physical-input-document-v1'))
        parents.extend(StoreSource(Path(p['path']),p['schema'],tuple(p['generation']))for p in result['sources'])
        records[fpga]={'status':result['status'],'capture_source_coverage':result['capture_source_coverage'],
                       'unresolved_contexts':result['unresolved_contexts'],
                       'artifact':path.name,'sha256':file_sha256(path)}
        if result['status']!='complete':pending.append(fpga)
    return {'status':'incomplete'if pending else'complete','partitions':records,'pending_partitions':pending,
            'scope':'native timing source membership; numerical timing and physical/minimum acceptance remain separate'}


def _clock_context(evidence,parents):
    """Use actual primary-clock contracts and complete capture-family requests.

    Current word wrappers create primary DUT clocks at phase zero with 50%
    duty cycle. A different-period or generated DUT domain requires its full
    source phase relationship; it is never silently aliased to a primary root.
    """
    clocks={};shared=None
    for fpga,record in sorted(evidence.items()):
        contract=record['implementation_timing']
        _require(contract['tdm_clock']['period_ns']==5.,'native Phase7c requires the five-nanosecond word clock')
        periods=contract['dut_clock_periods_ns']
        _require(periods and all(type(v)in(int,float)and math.isfinite(v)and v>0 for v in periods.values()),
                 'native Phase7c DUT target is invalid')
        if shared is None:shared=periods
        _require(periods==shared,'native Phase7c workers use different logical DUT targets')
        path=_artifact(record['clock_contract'],parents)
        with path.open(newline='')as stream:rows=list(csv.DictReader(stream,delimiter='\t'))
        roots={}
        for row in rows:
            name=bytes.fromhex(row['clock_hex']).decode();port=bytes.fromhex(row['port_hex']).decode()
            if re.fullmatch(r'emuflow_dut_clk_[0-9]+',name):
                _require(port in periods and abs(float(row['period_ns'])-periods[port])<1e-6,
                         'native Phase7c primary clock differs from its implementation target')
                _require(name not in roots,'native Phase7c primary clock repeats')
                roots[name]=port
        observed=set()
        path=Path(record['observation']['files']['native-capture-costs.tsv']['path'])
        with path.open(newline='')as stream:
            for row in csv.DictReader(stream,delimiter='\t'):
                if row['source_kind']=='dut-clock':observed.add(bytes.fromhex(row['source_hex']).decode())
        _require(len(observed)==record['observation']['counts']['dut_clocks'],
                 'native Phase7c capture families differ from the complete observation request')
        _require(observed==set(roots),'native Phase7c requires explicit source relationships for generated DUT clocks')
        clocks[fpga]=roots
    _require(shared is not None,'native Phase7c has no observed partitions')
    _require(len(set(shared.values()))==1,'native Phase7c requires complete edge windows for different-period DUT roots')
    reference_ns=next(iter(shared.values()));reference_ps=round(reference_ns*1000)
    _require(abs(reference_ps/1000-reference_ns)<1e-8 and reference_ps%2==0,
             'native Phase7c primary edge windows exceed the one-picosecond provider grid')
    active={logical for aliases in clocks.values()for logical in aliases.values()}
    windows=[(a,ae,b,be,reference_ps if ae==be else reference_ps//2)
             for a in sorted(active)for ae in(1,2)for b in sorted(active)for be in(1,2)]
    return clocks,windows,reference_ps,shared


def _original_members(evidence,output,parents,*,endpoint_index=None,source_ir=None,path_database=None,assignment=None):
    from .original_endpoint_index import index_original_endpoints
    from .original_lineage_index import index_original_lineage
    mapped={name:Path(record['input_identity']['mapped_index']['path'])for name,record in sorted(evidence.items())}
    if endpoint_index is None:
        if source_ir is None or path_database is None or assignment is None:
            return {'status':'pending-source-index','members':None,
                    'scope':'Original member records require the original IR, timing selector and assignment, or their existing endpoint index'}
        from .sta import sta_path_database_payload_path
        assignment=Path(assignment)
        if assignment.suffix!='.sqlite':assignment=assignment.with_name('assignment.sqlite')
        selector=Path(path_database).resolve();payload=sta_path_database_payload_path(selector)
        _require(payload is not None,'native Phase7c original members require the complete indexed timing source')
        endpoint_index=output/'original-endpoints.sqlite'
        index_original_endpoints(selector,payload,source_ir,assignment,mapped,endpoint_index,
            selector_sha256=file_sha256(selector),source_context={'scope':'Complete original members used by native Phase7c'})
    endpoint_index=Path(endpoint_index).resolve()
    parents.append(bind_store_source(endpoint_index,'native-original-endpoint-index-v1'))
    source=_metadata(endpoint_index)
    _require(source.get('schema')=='emuflow.native-original-endpoint-index/v1','native Phase7c original endpoint schema differs')
    pairs={name:(mapped[name],Path(record['lineage']['path']))for name,record in sorted(evidence.items())}
    lineage=output/'original-lineage.sqlite'
    index_original_lineage(endpoint_index,pairs,lineage)
    observed=_metadata(lineage)
    _require(observed.get('members')==source.get('members'),'native Phase7c original member population differs')
    with closing(sqlite3.connect(lineage.as_uri()+'?mode=ro&immutable=1',uri=True))as db:
        member_observations=[dict(start_observation=start,end_observation=end,members=count)
            for start,end,count in db.execute('SELECT a.status,b.status,count(*) FROM members m '
                'JOIN endpoint_dispositions a ON a.endpoint_seq=m.start_endpoint '
                'JOIN endpoint_dispositions b ON b.endpoint_seq=m.end_endpoint '
                'GROUP BY a.status,b.status ORDER BY a.status,b.status')]
    _require(sum(row['members']for row in member_observations)==source['members'],
             'native Phase7c original member observation accounting is incomplete')
    return {'status':'complete-source-lineage-observations','members':source['members'],
            'endpoint_index':{'path':str(endpoint_index),'sha256':file_sha256(endpoint_index)},
            'lineage_index':{'path':str(lineage),'sha256':file_sha256(lineage)},
            'observed_copies':observed['observed_copies'],'changed_type_objects':observed['changed_type_objects'],
            'member_observation_counts':member_observations,
            'observation_policy':'Source identity observations are separate from complete native graph timing; '
                'an unretained identity is not an elimination proof or a numerical timing value',
            'numerical_timing_owner':'native-clock-and-interface-graph',
            'scope':'All original member IDs and native source observations retained; absence of a tag is not a zero-delay path or a removal certificate'}


def run_native_word_phase7c(summary,runtime,platform,reports,schedule_path,output,*,
                           global_sta_executable=None,global_sta_results_dir=None,
                           endpoint_index=None,source_ir=None,path_database=None,assignment=None,
                           source_context_inputs=None,collect_native_endpoints=False,
                           native_endpoint_vivado=None,native_endpoint_results_dir=None):
    """Run the native physical-summary path of Phase7c, including its ledger.

    Source context and external minimum/startup qualification remain explicit
    pending coverage until connected. Numerical setup results are still saved;
    successful observation must not be confused with timing closure.
    """
    from .native_word_source_catalog import write_native_word_index_from_sources
    from .native_word_analysis import analyze_native_word_network,export_native_word_checks,verify_native_word_checks
    from .runtime import aggregate_qor
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    evidence=summary['native_word_observations'];parents=[];observations={};sources={}
    for fpga,record in sorted(evidence.items()):
        observation=record['observation']
        _require(observation.get('status')=='complete'and observation.get('fpga')==fpga,
                 'native Phase7c observation owner is incomplete')
        sources[fpga]=Path(record['sources']['directory'])
        _require(observation['source_directory']==str(sources[fpga]),'native Phase7c source role owner differs')
        scheduled=_artifact(record['input_identity']['word_schedule'],parents,'tdm-schedule-selector-v4')
        _require(file_sha256(scheduled)==file_sha256(Path(schedule_path)),
                 'native Phase7c schedule differs from its physical implementation')
        for reference in observation['files'].values():_artifact(reference,parents)
        parents.extend(StoreSource(Path(p['path']),p['schema'],tuple(p['generation']))for p in observation['source_generations'])
        observations[fpga]={role:Path(observation['files'][name]['path'])for role,name in(
            ('tx_costs','native-costs.tsv'),('fanins','fanin-summary.tsv'),
            ('captures','native-capture-costs.tsv'),('missing_rx','missing-rx-roles.tsv'))}
    clock_parents=[]
    clocks,windows,reference_ps,periods=_clock_context(evidence,clock_parents)
    # The role index owns calendars and clock relationships. Timing streams
    # remain independent analyzer inputs, rather than duplicating every
    # observer recipe/checkpoint ancestor in the index's direct parent list.
    for record in evidence.values():
        clock_parents.append(bind_store_source(Path(record['observation']['files']['native-capture-costs.tsv']['path']),
                                               'physical-input-document-v1'))
    parents.extend(clock_parents)
    index=output/'native-word-index.sqlite';analysis=output/'native-word-analysis.json'
    write_native_word_index_from_sources(sources,clocks,windows,index,reference_period_ps=reference_ps,
        source_context={'clock_policy':'shared-period-primary-roots-zero-phase-50-percent',
                        'dut_clock_periods_ns':periods,'schedule':str(Path(schedule_path).resolve())},source_bindings=clock_parents)
    analyze_native_word_network(index,observations,analysis)
    model=read_json(analysis);independent={'status':'not-applicable-no-numerical-window'}
    if model['maximum_sampling_cost_ps']is not None:
        events=output/'native-word-checks.tsv';export_native_word_checks(index,observations,events)
        independent=verify_native_word_checks(events,output/'global-opensta',executable=global_sta_executable,
                                              results_dir=global_sta_results_dir)
    endpoint_timing=None
    if collect_native_endpoints or native_endpoint_results_dir is not None:
        from .native_word_analysis import export_native_word_arrivals
        from .native_word_budgets import write_native_word_budgets,collect_native_word_endpoint_timing
        arrivals=output/'native-rx-arrivals.tsv';budgets=output/'native-rx-budgets.tsv'
        export_native_word_arrivals(index,observations,arrivals)
        write_native_word_budgets(index,analysis,arrivals,budgets)
        endpoint_timing=collect_native_word_endpoint_timing(evidence,budgets,output/'native-endpoints',
            executable=native_endpoint_vivado,results_dir=native_endpoint_results_dir)
    originals=_original_members(evidence,output,parents,endpoint_index=endpoint_index,
        source_ir=source_ir,path_database=path_database,assignment=assignment)
    source_contexts=_source_contexts(evidence,summary,output,parents,source_context_inputs)
    original_summary=dict(originals)
    for key in ('endpoint_index','lineage_index'):
        if key in original_summary:
            ref=original_summary[key]
            original_summary[key]={'artifact':Path(ref['path']).name,'sha256':ref['sha256']}
    independent_summary={k:v for k,v in independent.items()if k not in('source','execution')}
    if independent.get('status')=='verified':
        independent_summary.update(execution='standalone',authority='native-worst-phase-witnesses',
            source={'artifact':'native-word-checks.tsv','sha256':independent['source']['sha256']})
    minimum_slack=min((w['reference_window_ps']-w['maximum_sampling_cost_ps']for w in model.get('windows',[])
                       if w['maximum_sampling_cost_ps']is not None),default=None)
    timing={'schema':'emuflow.native-word-system-timing/v1','status':'incomplete',
        'timing_scope':'routed-native-clock-and-interface-setup-model',
        'summary':{'maximum_sampling_cost_ns':None if model['maximum_sampling_cost_ps']is None else model['maximum_sampling_cost_ps']/1000.,
                   'required_dut_period_ns':None if model['required_reference_period_ps']is None else model['required_reference_period_ps']/1000.,
                   'inferred_dut_frequency_mhz':model['setup_model_frequency_mhz'],
                   'raw_datapath_maximum_ns':None,'endpoint_tns_ns':None},
        'target_clock':{'period_ns':reference_ps/1000.,'frequency_mhz':1000000./reference_ps,
                        'worst_slack_bound_ns':None if minimum_slack is None else minimum_slack/1000.,
                        'total_negative_slack_bound_ns':None},
        'original_members':original_summary,'native_model':model,'global_opensta':independent_summary,
        'physical_evidence_completeness':{'status':'incomplete','source_membership':source_contexts,
            'scope':'Original check dispositions and external minimum/startup acceptance remain separate from source membership'},
        'source_binding':{'index':index.name,'analysis':analysis.name},
        'qualification':'routed-local-plus-declared-link-model; board-signoff-not-claimed'}
    if endpoint_timing is not None:
        timing['native_endpoints']=endpoint_timing
        if endpoint_timing['status']=='complete':
            timing['summary']['endpoint_tns_ns']=endpoint_timing['summary']['tns_ns']
            timing['target_clock']['total_negative_slack_bound_ns']=endpoint_timing['summary']['tns_ns']
        timing['target_clock']['endpoint_wns_ns']=endpoint_timing['summary']['wns_ns']
        timing['target_clock']['endpoint_minus_model_wns_ns']=(None if minimum_slack is None or
            endpoint_timing['summary']['wns_ns']is None else endpoint_timing['summary']['wns_ns']-minimum_slack/1000.)
    qor=aggregate_qor(runtime,*reports,summary,platform,system_timing_result=timing)
    qor['runtime']={'tdm_period_ns':5.,'tdm_frequency_mhz':200.,
                    'dut_clock_periods_ns':periods,'initial_dut_frequency_mhz':1000000./reference_ps,
                    'frequency_policy':'post-route-native-system-timing; serializer recurrence does not define DUT period'}
    write_json(output/'runtime_contract.json',runtime)
    write_json(output/'implementation_clocks.json',qor['runtime'])
    write_json(output/'qor_report.json',qor)
    report={'schema':'emuflow.phase7c-report/v3','phase':'7C','increment':'native-word-system-timing',
        'status':'incomplete','execution_status':'pass','design':runtime['design'],'platform':platform.name,
        'provider':'native-word-network-plus-independent-opensta-v1','physical':qor['physical'],
        'implementation_clocks':qor['runtime'],
        'system_timing_summary':{k:v for k,v in timing.items()if k not in('native_model','original_members')},
        'original_members':originals,'system_timing_ref':{'artifact':'qor_report','json_pointer':'/timing'},
        'artifacts':{'qor_report':'qor_report.json','report':'phase7c_report.json',
                     'runtime_contract':'runtime_contract.json','implementation_clocks':'implementation_clocks.json',
                     'native_word_index':index.name,'native_word_analysis':analysis.name},
        'scope':'Complete native observation analysis; pending source/check coverage is explicit, without fabricated original-path delays or endpoint TNS'}
    require_store_sources_unchanged(parents)
    write_json(output/'phase7c_report.json',report)
    return report
