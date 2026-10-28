"""Observe an immutable native Vivado snapshot without replaying implementation.

Collection completion and fixed-clock timing closure are distinct results.
The native network analyzer consumes the observation files; this module never
infers functional elimination or a numerical timing arc from a missing tag.
"""
import csv
import json
import math
from pathlib import Path
import sqlite3

from .errors import ValidationError
from .io import file_sha256
from .store_publication import StoreSource,bind_store_source,require_store_sources_unchanged

FILES=('native-costs.tsv','fanin-summary.tsv','native-capture-costs.tsv','missing-rx-roles.tsv',
       'native-dut-hold.tsv','native-rx-infrastructure-hold.tsv','native-control-checks.tsv',
       'native-observation-metrics.tsv')
SCRIPTS=('observe_native_word.tcl','native_word_observation.tcl','native_word_captures.tcl',
         'native_word_timing.tcl','word_phase_context.tcl','timing_contract.tcl')


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


def observe_native_word_checkpoint(fpga,checkpoint,source_directory,boundaries,output,*,executable=None,threads=2):
    """Collect all native clock families and recurring canonical TX contexts.

    Reuses a completed routed DCP in a fresh process. It never changes that
    DCP, performs placement/routing, preserves original DUT primitives, or
    requires unused RX hierarchy ports to survive synthesis.
    """
    from .native_word_source_catalog import FILES as SOURCE_FILES,SCHEMA
    from .vivado_backend import _resolve_vivado
    from .vivado_physical import _vivado_script,execute_vivado
    if type(threads)is not int or threads<1:
        raise ValidationError('native word observation threads must be positive')
    checkpoint=Path(checkpoint).resolve();source_directory=Path(source_directory).resolve()
    boundaries=Path(boundaries).resolve();output=Path(output).resolve()
    if output.exists():raise ValidationError('native word observation requires a fresh output directory')
    descriptor=source_directory/'source-catalog.json'
    parents=[bind_store_source(p,'physical-input-document-v1')for p in(checkpoint,boundaries,descriptor)]
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
    for name in SCRIPTS:parents.append(bind_store_source(_vivado_script(name),'physical-input-document-v1'))
    require_store_sources_unchanged(parents)
    checkpoint_sha=file_sha256(checkpoint)
    output.mkdir(parents=True)
    execution=execute_vivado(_resolve_vivado(executable),_vivado_script('observe_native_word.tcl'),
        [str(checkpoint),str(source_directory),str(boundaries),str(output),str(threads)],output,'native-observation.log')
    require_store_sources_unchanged(parents)
    if file_sha256(checkpoint)!=checkpoint_sha:raise ValidationError('native observation changed its routed snapshot')
    files={}
    for name in FILES:
        path=output/name
        if not path.is_file()or path.stat().st_size==0:
            raise ValidationError('native word observation is incomplete: '+name)
        files[name]=dict(path=str(path),sha256=file_sha256(path),bytes=path.stat().st_size)
    with(output/'native-observation-metrics.tsv').open(newline='')as stream:
        rows=list(csv.DictReader(stream,delimiter='\t'))
    metrics={}
    for row in rows:
        try:value=int(row['value'])
        except (KeyError,ValueError)as error:raise ValidationError('invalid native observation counter')from error
        if value<0 or row['metric']in metrics:raise ValidationError('invalid or repeated native observation counter')
        metrics[row['metric']]=value
    if(set(metrics)!={'dut_clocks','infrastructure_clocks','observed_rx_origins','requested_rx_origins',
                      'missing_rx_origins','repeat_words','capture_queries','tx_contexts'}or
       metrics['dut_clocks']<1 or metrics['repeat_words']!=preparation['repeat_words']or
       metrics['tx_contexts']!=preparation['tx_contexts']or
       metrics['observed_rx_origins']+metrics['missing_rx_origins']!=metrics['requested_rx_origins']or
       metrics['capture_queries']!=2*metrics['dut_clocks']*(2*metrics['dut_clocks']+metrics['observed_rx_origins'])):
        raise ValidationError('native word observation population differs from its complete request')
    with(output/'missing-rx-roles.tsv').open(newline='')as stream:
        missing=[row['source_origin']for row in csv.DictReader(stream,delimiter='\t')]
    with(source_directory/'rx-roles.tsv').open(newline='')as stream:
        requested={row['source_origin']for row in csv.DictReader(stream,delimiter='\t')}
    if(len(missing)!=len(set(missing))or not set(missing)<=requested or len(missing)!=metrics['missing_rx_origins']or
       len(requested)!=metrics['requested_rx_origins']):
        raise ValidationError('native word missing RX roles differ from their source request')
    checks={name:_checks(output/name)for name in('native-control-checks.tsv','native-dut-hold.tsv','native-rx-infrastructure-hold.tsv')}
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
    require_store_sources_unchanged(parents)
    (output/'native-observation.json').write_text(json.dumps(report,sort_keys=True,indent=2)+'\n')
    return report
