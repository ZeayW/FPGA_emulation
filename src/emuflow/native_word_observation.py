"""Observe an immutable native Vivado snapshot without replaying implementation.

Collection completion and fixed-clock timing closure are distinct results.
The native network analyzer consumes the observation files; this module never
infers functional elimination or a numerical timing arc from a missing tag.
"""
import csv
import json
import math
from pathlib import Path
import shutil
import sqlite3

from .errors import ValidationError
from .io import file_sha256
from .store_publication import StoreSource,bind_store_source,require_store_sources_unchanged

FILES=('native-costs.tsv','fanin-summary.tsv','native-capture-costs.tsv','missing-rx-roles.tsv',
       'native-dut-hold.tsv','native-rx-infrastructure-hold.tsv','native-control-checks.tsv',
       'native-observation-metrics.tsv')
SCRIPTS=('observe_native_word.tcl','native_word_observation.tcl','native_word_captures.tcl',
         'native_word_timing.tcl','native_word_tx_batch.tcl','native_source_census.tcl',
         'native_source_properties.tcl','word_phase_context.tcl','timing_contract.tcl')


def _checks(path):
    with path.open(newline='')as stream:rows=list(csv.DictReader(stream,delimiter='\t'))
    for row in rows:
        kind=row.get('measurement')
        if kind not in ('native-path','native-min-path','native-no-timed-path'):
            raise ValidationError('native word observation has an unknown physical check')
        if kind=='native-no-timed-path':
            if any(row.get(k)for k in ('slack_ns','data_ns','start_hex','end_hex')):
                raise ValidationError('native word absent physical check contains invented measurements')
            continue
        try:
            values=[float(row[k])for k in ('slack_ns','data_ns')]
            names=[bytes.fromhex(row[k]).decode()for k in ('start_hex','end_hex')]
        except (ValueError,KeyError)as error:
            raise ValidationError('native word physical check is malformed')from error
        if not all(math.isfinite(v)for v in values)or values[1]<0 or not all(names):
            raise ValidationError('native word physical check has invalid measurements')
    return rows


def _source_request(fpga,source_directory,boundaries):
    from .native_word_source_catalog import FILES as SOURCE_FILES,SCHEMA
    descriptor=source_directory/'source-catalog.json'
    parents=[bind_store_source(p,'physical-input-document-v1')for p in(boundaries,descriptor)]
    preparation=json.loads(descriptor.read_text())
    if(preparation.get('schema')!='emuflow.native-word-source-preparation/v1'or preparation.get('status')!='complete'or
       preparation.get('fpga')!=fpga or preparation.get('directory')!=str(source_directory)):
        raise ValidationError('native word observation source owner differs')
    for name in SOURCE_FILES:
        path=source_directory/name;parents.append(bind_store_source(path,'physical-input-document-v1'))
        if preparation['files'].get(name)!={'sha256':file_sha256(path),'bytes':path.stat().st_size}:
            raise ValidationError('native word observation source file changed')
    with sqlite3.connect((source_directory/'catalog.sqlite').as_uri()+'?mode=ro&immutable=1',uri=True)as db:
        metadata={k:json.loads(v)for k,v in db.execute('SELECT * FROM metadata')}
    if metadata.get('schema')!=SCHEMA or metadata.get('fpga')!=fpga:
        raise ValidationError('native word observation catalog owner differs')
    parents.extend(StoreSource(Path(p['path']),p['schema'],tuple(p['generation']))for p in metadata['source_generations'])
    require_store_sources_unchanged(parents)
    return preparation,metadata,parents


def observe_native_word_checkpoint(fpga,checkpoint,source_directory,boundaries,output,*,executable=None,threads=2,
                                   runtime_constraints=None):
    """Collect all native clock families and recurring canonical TX contexts.

    Reuses a completed routed DCP in a fresh process. It never changes that
    DCP, performs placement/routing, preserves original DUT primitives, or
    requires unused RX hierarchy ports to survive synthesis.
    """
    from .vivado_backend import _resolve_vivado
    from .vivado_physical import _vivado_script,execute_vivado
    if type(threads)is not int or threads<1:
        raise ValidationError('native word observation threads must be positive')
    checkpoint=Path(checkpoint).resolve();source_directory=Path(source_directory).resolve()
    boundaries=Path(boundaries).resolve();output=Path(output).resolve()
    if output.exists():raise ValidationError('native word observation requires a fresh output directory')
    preparation,_,parents=_source_request(fpga,source_directory,boundaries)
    parents.append(bind_store_source(checkpoint,'physical-input-document-v1'))
    recipes={name:bind_store_source(_vivado_script(name),'physical-input-document-v1')for name in SCRIPTS}
    require_store_sources_unchanged(parents)
    checkpoint_sha=file_sha256(checkpoint)
    if runtime_constraints is None:
        candidate=checkpoint.parent/'runtime.xdc'
        if candidate.is_file():runtime_constraints=candidate
    if runtime_constraints is not None:
        runtime_constraints=Path(runtime_constraints).resolve()
        parents.append(bind_store_source(runtime_constraints,'physical-input-document-v1'))
    output.mkdir(parents=True)
    recipe_directory=output/'recipe';recipe_directory.mkdir()
    for name,parent in recipes.items():shutil.copyfile(parent.path,recipe_directory/name)
    require_store_sources_unchanged(tuple(recipes.values()))
    for name in SCRIPTS:parents.append(bind_store_source(recipe_directory/name,'physical-input-document-v1'))
    arguments=[str(checkpoint),str(source_directory),str(boundaries),str(output),str(threads)]
    if runtime_constraints is not None:
        constraints=output/'runtime.xdc'
        shutil.copyfile(runtime_constraints,constraints)
        parents.append(bind_store_source(constraints,'physical-input-document-v1'))
        arguments.append(str(constraints))
    execution=execute_vivado(_resolve_vivado(executable),recipe_directory/'observe_native_word.tcl',
        arguments,output,'native-observation.log')
    require_store_sources_unchanged(parents)
    if file_sha256(checkpoint)!=checkpoint_sha:raise ValidationError('native observation changed its routed snapshot')
    context=None
    if runtime_constraints is not None:
        from .native_word_source_evidence import publish_native_source_evidence
        context=publish_native_source_evidence(fpga,checkpoint,constraints,output,
            [recipe_directory/name for name in SCRIPTS],parents)
    return _record_native_word_observation(fpga,checkpoint,checkpoint_sha,source_directory,preparation,
        output,parents,execution,source_context=context)


def reuse_native_word_observation(fpga,checkpoint,source_directory,boundaries,observation_path,output):
    """Rebind closed native measurements to the same canonical source request.

    A fresh role directory is permitted; its upstream sources and complete
    canonical relations must equal the observed request. Streams and the DCP
    remain in place. No vendor process or physical implementation is run.
    """
    checkpoint=Path(checkpoint).resolve();source_directory=Path(source_directory).resolve()
    boundaries=Path(boundaries).resolve();observation_path=Path(observation_path).resolve()
    output=Path(output).resolve()
    if output.exists():raise ValidationError('native word observation requires a fresh output directory')
    receipt=bind_store_source(observation_path,'physical-input-document-v1')
    prior=json.loads(observation_path.read_text())
    if(prior.get('schema')!='emuflow.native-word-observation/v1'or prior.get('status')!='complete'or prior.get('fpga')!=fpga):
        raise ValidationError('retained native word observation owner or completion differs')
    old_parents=[StoreSource(Path(p['path']),p['schema'],tuple(p['generation']))for p in prior['source_generations']]
    require_store_sources_unchanged(old_parents+[receipt])
    if file_sha256(checkpoint)!=prior['checkpoint']['sha256']:
        raise ValidationError('retained native word observation checkpoint differs')
    preparation,metadata,parents=_source_request(fpga,source_directory,boundaries)
    old_directory=Path(prior['source_directory']).resolve()
    old_preparation,old_metadata,request_parents=_source_request(fpga,old_directory,boundaries)
    if(metadata!=old_metadata or any(preparation[k]!=old_preparation[k]
            for k in ('schema','status','fpga','calendar_periods','repeat_words','tx_contexts'))):
        raise ValidationError('retained native word observation source request differs')
    for name in ('counter-roles.tsv','rx-roles.tsv','targets.tsv'):
        if preparation['files'][name]!=old_preparation['files'][name]:
            raise ValidationError('retained native word observation role differs: '+name)
    with sqlite3.connect((source_directory/'catalog.sqlite').as_uri()+'?mode=ro&immutable=1',uri=True)as db:
        db.execute('ATTACH DATABASE ? AS prior',((old_directory/'catalog.sqlite').as_uri()+'?mode=ro&immutable=1',))
        for table in ('entries','endpoints'):
            for left,right in (('main','prior'),('prior','main')):
                if db.execute(f'SELECT * FROM {left}.{table} EXCEPT SELECT * FROM {right}.{table} LIMIT 1').fetchone()is not None:
                    raise ValidationError('retained native word observation canonical relations differ: '+table)
    parents.extend(old_parents+request_parents+[receipt,bind_store_source(checkpoint,'physical-input-document-v1')])
    for name,artifact in prior['files'].items():
        if name not in FILES:raise ValidationError('retained native word observation has an unknown stream')
        parents.append(bind_store_source(Path(artifact['path']),'physical-input-document-v1'))
    context=prior.get('source_context')
    if context is not None:
        from .native_word_source_evidence import bind_native_source_evidence
        bind_native_source_evidence(context,parents)
    require_store_sources_unchanged(parents)
    execution=dict(status='reused',invoked_tool=False,
        source_observation=dict(path=str(observation_path),sha256=file_sha256(observation_path)),
        original_execution=prior['execution'])
    output.mkdir(parents=True)
    return _record_native_word_observation(fpga,checkpoint,prior['checkpoint']['sha256'],source_directory,
        preparation,output,parents,execution,file_references=prior['files'],source_context=context)


def _record_native_word_observation(fpga,checkpoint,checkpoint_sha,source_directory,preparation,output,parents,execution,*,file_references=None,source_context=None):
    """Validate complete streams and publish their observation without tool work."""
    files={}if file_references is None else dict(file_references)
    if file_references is not None and set(files)!=set(FILES):
        raise ValidationError('native word observation streams are incomplete')
    for name in FILES:
        path=output/name if file_references is None else Path(files[name]['path']).resolve()
        if not path.is_file()or path.stat().st_size==0:
            raise ValidationError('native word observation is incomplete: '+name)
        actual=dict(path=str(path),sha256=file_sha256(path),bytes=path.stat().st_size)
        if file_references is not None and files[name]!=actual:
            raise ValidationError('native word observation stream changed: '+name)
        files[name]=actual
    with Path(files['native-observation-metrics.tsv']['path']).open(newline='')as stream:
        rows=list(csv.DictReader(stream,delimiter='\t'))
    metrics={}
    for row in rows:
        try:value=int(row['value'])
        except (KeyError,ValueError)as error:raise ValidationError('invalid native observation counter')from error
        if value<0 or row['metric']in metrics:raise ValidationError('invalid or repeated native observation counter')
        metrics[row['metric']]=value
    if(set(metrics)!={'dut_clocks','infrastructure_clocks','observed_rx_origins','requested_rx_origins',
                      'missing_rx_origins','repeat_words','capture_queries','tx_contexts'}or
       metrics['repeat_words']!=preparation['repeat_words']or
       metrics['tx_contexts']!=preparation['tx_contexts']or
       metrics['observed_rx_origins']+metrics['missing_rx_origins']!=metrics['requested_rx_origins']or
       metrics['capture_queries']!=2*metrics['dut_clocks']*(2*metrics['dut_clocks']+metrics['observed_rx_origins'])):
        raise ValidationError('native word observation population differs from its complete request')
    with Path(files['missing-rx-roles.tsv']['path']).open(newline='')as stream:
        missing=[row['source_origin']for row in csv.DictReader(stream,delimiter='\t')]
    with(source_directory/'rx-roles.tsv').open(newline='')as stream:
        requested={row['source_origin']for row in csv.DictReader(stream,delimiter='\t')}
    if(len(missing)!=len(set(missing))or not set(missing)<=requested or len(missing)!=metrics['missing_rx_origins']or
       len(requested)!=metrics['requested_rx_origins']):
        raise ValidationError('native word missing RX roles differ from their source request')
    checks={name:_checks(Path(files[name]['path']))for name in('native-control-checks.tsv','native-dut-hold.tsv','native-rx-infrastructure-hold.tsv')}
    control=checks['native-control-checks.tsv']
    if len(control)!=2 or {row.get('delay_type')for row in control}!={'max','min'}:
        raise ValidationError('native word observation lacks both fixed-clock control checks')
    for name in ('native-dut-hold.tsv','native-rx-infrastructure-hold.tsv'):
        scope='dut-state-to-state'if name=='native-dut-hold.tsv'else'rx-to-infrastructure'
        if len(checks[name])!=1 or checks[name][0].get('scope')!=scope:
            raise ValidationError('native word observation lacks its minimum check scope')
    timed=[row for row in control if row['measurement']=='native-path']
    control_met=all(float(row['slack_ns'])>=0 for row in timed)if len(timed)==2 else None
    report=dict(schema='emuflow.native-word-observation/v1',status='complete',fpga=fpga,
        checkpoint=dict(path=str(checkpoint),sha256=checkpoint_sha),source_directory=str(source_directory),
        counts=metrics,files=files,execution=execution,physical_checks=checks,
        fixed_clock_control_timing_met=control_met,
        source_generations=[dict(path=str(p.path),schema=p.schema,generation=list(p.generation))for p in parents],
        scope='routed-native-clock-and-interface-observations; global STA and original member accounting remain separate')
    if source_context is not None:report['source_context']=source_context
    require_store_sources_unchanged(parents)
    (output/'native-observation.json').write_text(json.dumps(report,sort_keys=True,indent=2)+'\n')
    return report
