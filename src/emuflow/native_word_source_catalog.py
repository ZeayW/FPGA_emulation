"""Source-owned transport roles for native Vivado timing observations.

Only the bounded Phase6 transport interface is visited here. DUT logic stays
in its indexed source and Vivado owns its optimized timing endpoints.
"""
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import tempfile

from .errors import ValidationError
from .store_publication import StoreSource,bind_store_source,require_store_sources_unchanged

SCHEMA='emuflow.native-word-source-catalog/v1'
FILES=('catalog.sqlite','counter-roles.tsv','rx-roles.tsv','targets.tsv')


def _require(ok,message):
    if not ok:raise ValidationError(message)


def _parents(sources):
    result={}
    for parent in sources:
        _require(parent.path not in result or result[parent.path]==parent,'native word source receipts belong to different generations')
        result[parent.path]=parent
    require_store_sources_unchanged(tuple(result.values()))
    return tuple(result.values())


def _hash(path):
    digest=hashlib.sha256()
    with path.open('rb')as stream:
        for block in iter(lambda:stream.read(8*1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def prepare_native_word_sources(fpga,schedule_path,boundary_path,mapped_path,output,*,
                                fixed_latency_words=6,maximum_repeat_words=4096,
                                maximum_contexts=1000000,fabric_clock_port='fabric_clk'):
    """Publish canonical roles and every recurring TX context for one FPGA.

    Receive-only periods participate in the joint counter calendar. Requests
    bind source transport state tags, not original DUT FF names/types. Missing
    native projections remain the subsequent observer's explicit facts.
    """
    from .boundary_identity_storage import read_boundary_identity_storage
    from .tdm_schedule_storage import read_tdm_schedule
    from .xilinx_netlist import open_xilinx_mapped_index
    _require(isinstance(fpga,str)and bool(fpga),'native word partition identity is invalid')
    _require(all(type(v)is int and v>0 for v in(fixed_latency_words,maximum_repeat_words,maximum_contexts)),
             'native word source execution bounds must be positive integers')
    output=Path(output).resolve();mapped_path=Path(mapped_path).resolve()
    _require(not output.exists(),'native word source destination already exists')
    output.parent.mkdir(parents=True,exist_ok=True)
    parents=[bind_store_source(mapped_path,'xilinx-mapped-index-v2')]
    with tempfile.TemporaryDirectory(prefix='.native-word-sources-',dir=output.parent)as temporary:
        root=Path(temporary)/'sources';root.mkdir()
        with closing(sqlite3.connect(root/'catalog.sqlite'))as catalog,catalog:
            catalog.executescript('''PRAGMA application_id=0x454d5743;PRAGMA user_version=1;
              PRAGMA foreign_keys=ON;
              CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
              CREATE TABLE entries(id TEXT PRIMARY KEY,demand TEXT NOT NULL,net TEXT NOT NULL,link TEXT NOT NULL,
                source TEXT NOT NULL,sink TEXT NOT NULL,slot INTEGER NOT NULL,arrival INTEGER NOT NULL,
                lane INTEGER NOT NULL,ratio INTEGER NOT NULL) WITHOUT ROWID;
              CREATE TABLE endpoints(id TEXT PRIMARY KEY,kind TEXT NOT NULL,entry_id TEXT UNIQUE NOT NULL REFERENCES entries(id),
                query TEXT NOT NULL,period INTEGER NOT NULL,phase INTEGER NOT NULL,
                source_class TEXT NOT NULL,source_origin TEXT NOT NULL) WITHOUT ROWID;''')
            with read_tdm_schedule(Path(schedule_path))as schedule:
                parents.extend(schedule.bound_store_sources())
                _require(schedule['word_event_policy']=='periodic-lvds-8bit-word-load-to-rx-visible-v2',
                         'native word source requires the continuous eight-bit calendar')
                for item in schedule.iter_transport_records():
                    if fpga not in(item.source,item.sink):continue
                    ratio=item.tdm_ratio
                    _require(type(ratio)is int and 8<=ratio<=256 and ratio%8==0,
                             'native word ratio must count complete eight-UI words')
                    _require(item.source!=item.sink and item.arrival_slot-item.slot==fixed_latency_words,
                             'native word transfer source or fixed latency differs')
                    catalog.execute('INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (item.entry_id,item.demand,item.net,item.link,item.source,item.sink,item.slot,item.arrival_slot,item.lane,ratio))
            periods=set();origins=set();roles=[];bits={}
            with read_boundary_identity_storage(Path(boundary_path))as boundaries,open_xilinx_mapped_index(mapped_path)as mapped:
                parents.extend(boundaries.bound_store_sources())
                _require(boundaries['fpga']==fpga,'native word boundary belongs to another FPGA')
                clock=mapped.ports.get(fabric_clock_port,{})
                _require(clock.get('direction')=='input'and len(clock.get('bits',[]))==1 and type(clock['bits'][0])is int,
                         'native word transport clock lacks its scalar mapped input')
                clock_bit=clock['bits'][0]
                def state(seq):
                    row=mapped.connection.execute("SELECT c.type,c.record_json,p.net_bit FROM cells c "
                        "LEFT JOIN cell_pins p ON p.cell_seq=c.seq AND p.port='C'AND p.port_bit=0 AND p.value_kind=0 "
                        "WHERE c.seq=?",(seq,)).fetchone()
                    _require(row is not None and row[0]=='FDRE'and row[2]==clock_bit and
                             str(json.loads(row[1]).get('parameters',{}).get('IS_C_INVERTED','0')).strip('0')=='',
                             'native word source state is not a positive-edge transport register')
                for endpoint in boundaries['endpoints']:
                    entry=catalog.execute('SELECT demand,net,link,source,sink,slot,arrival,lane,ratio FROM entries WHERE id=?',
                                          (endpoint['schedule_entry'],)).fetchone()
                    _require(entry is not None,'native word boundary has no canonical scheduled transfer')
                    demand,net,link,source,sink,slot,arrival,lane,ratio=entry
                    _require(endpoint['kind']in('tx','rx')and(demand,net,link,lane)==
                             (endpoint['demand'],endpoint['logical_net'],endpoint['link'],endpoint['lane'])and
                             (source,sink)==((fpga,endpoint['peer'])if endpoint['kind']=='tx'else(endpoint['peer'],fpga)),
                             'native word boundary differs from its canonical transfer')
                    period=ratio//8;periods.add(period);merged=endpoint['merged_ir'];origin=''
                    if endpoint['kind']=='rx':
                        instances=merged['boundary_register_instances']
                        _require(len(instances)==1,'native word RX role lacks its source shadow')
                        row=mapped.connection.execute('SELECT seq FROM cells WHERE id=?',(instances[0],)).fetchone()
                        _require(row is not None,'native word RX shadow is absent from its mapped source')
                        state(row[0]);origin='s'+str(row[0])
                        _require(origin not in origins,'native word RX source aliases distinct canonical arrivals')
                        origins.add(origin)
                    query=merged['external_port']+'['+str(merged['external_port_bit'])+']'
                    catalog.execute('INSERT INTO endpoints VALUES(?,?,?,?,?,?,?,?)',
                        (endpoint['id'],endpoint['kind'],endpoint['schedule_entry'],query,period,slot%period,endpoint['source_class'],origin))
                _require(catalog.execute('SELECT count(*)FROM entries').fetchone()==catalog.execute('SELECT count(*)FROM endpoints').fetchone(),
                         'native word boundary catalog does not cover every local transfer')
                repeat=1
                for period in sorted(periods):
                    repeat=repeat*period//math.gcd(repeat,period)
                    _require(repeat<=maximum_repeat_words,'native word serializer recurrence exceeds its source bound')
                    if period==1:
                        # A one-word calendar has only phase zero. There is
                        # no phase choice to project onto native state. The
                        # RTL producer owns the counter behavior; synthesis
                        # may retain a feedback FF, fold it or remove its name.
                        roles.append((1,0,'constant:0'))
                        continue
                    for bit in range(max(1,(period-1).bit_length())):
                        alias=f'__emuflow_transport__/payload_phase_{period}[{bit}]'
                        row=mapped.connection.execute('SELECT record_json FROM netnames WHERE id=?',(alias,)).fetchone()
                        if row is None and max(1,(period-1).bit_length())==1:
                            # The indexed source can flatten a one-bit vector
                            # into a scalar name. Its source bit is unchanged.
                            alias=f'__emuflow_transport__/payload_phase_{period}'
                            row=mapped.connection.execute('SELECT record_json FROM netnames WHERE id=?',(alias,)).fetchone()
                        _require(row is not None,'native word counter role is absent from its source recipe')
                        values=json.loads(row[0])['bits'];_require(len(values)==1,'native word counter alias is not scalar')
                        _require(type(values[0])is int,'native word counter source bit is not a mapped net')
                        bits.setdefault(values[0],[]).append((period,bit))
                for net,coordinates in bits.items():
                    rows=list(mapped.connection.execute("SELECT cell_seq FROM cell_pins WHERE port='Q'AND direction=1 "
                        "AND value_kind=0 AND net_bit=?",(net,)))
                    _require(len(rows)==1,'native word counter source driver is absent or ambiguous');seq=rows[0][0];state(seq)
                    for phase in range(repeat):
                        _require(len({((phase%period)>>bit)&1 for period,bit in coordinates})==1,
                                 'native word source aliases incompatible counter phases')
                    roles.extend((period,bit,'s'+str(seq))for period,bit in coordinates)
            parents=_parents(parents)
            (root/'counter-roles.tsv').write_text('period\tbit\tsource_origin\n'+''.join('%s\t%s\t%s\n'%r for r in sorted(roles)))
            (root/'rx-roles.tsv').write_text('source_origin\n'+''.join(origin+'\n'for origin in sorted(origins)))
            contexts=catalog.execute("SELECT coalesce(sum(?/period),0)FROM endpoints WHERE kind='tx'",(repeat,)).fetchone()[0]
            _require(contexts<=maximum_contexts,'native word TX context population exceeds its source bound')
            with(root/'targets.tsv').open('w')as stream:
                stream.write('phase_word\tquery_hex\n')
                for phase in range(repeat):
                    duplicate=catalog.execute("SELECT query FROM endpoints WHERE kind='tx'AND(?%period)=phase GROUP BY query HAVING count(*)>1 LIMIT 1",(phase,)).fetchone()
                    _require(duplicate is None,'native word TX bit has multiple simultaneous canonical owners')
                    for(query,)in catalog.execute("SELECT query FROM endpoints WHERE kind='tx'AND(?%period)=phase ORDER BY query",(phase,)):
                        stream.write(str(phase)+'\t'+query.encode().hex()+'\n')
            metadata=dict(schema=SCHEMA,fpga=fpga,calendar_periods=sorted(periods),repeat_words=repeat,tx_contexts=contexts,
                          fixed_latency_words=fixed_latency_words,fabric_clock_port=fabric_clock_port,
                          source_generations=[dict(path=str(p.path),schema=p.schema,generation=p.generation)for p in parents])
            catalog.executemany('INSERT INTO metadata VALUES(?,?)',[(k,json.dumps(v,sort_keys=True))for k,v in metadata.items()])
        result=dict(schema='emuflow.native-word-source-preparation/v1',status='complete',directory=str(output),
                    fpga=fpga,calendar_periods=sorted(periods),repeat_words=repeat,tx_contexts=contexts,
                    files={name:dict(sha256=_hash(root/name),bytes=(root/name).stat().st_size)for name in FILES})
        (root/'source-catalog.json').write_text(json.dumps(result,sort_keys=True,indent=2)+'\n')
        require_store_sources_unchanged(parents);root.rename(output)
    return result


def write_native_word_index_from_sources(source_directories,clocks,clock_windows,output,*,source_context,
                                         word_period_ps=5000,reference_period_ps=100000,
                                         maximum_repeat_words=4096,maximum_contexts=1000000,source_bindings=()):
    """Join both canonical owners and preserve complete caller-owned windows.

    Source/capture clock relationships come from the complete design clock
    context supplied by the caller, never from a selected timing-path sample.
    Existing clock/window control receipts retain their original bindings.
    """
    from .native_word_analysis import write_native_word_index
    _require(word_period_ps==5000,'native Vivado word observations require the five-nanosecond TDM clock')
    _require(source_directories and set(source_directories)==set(clocks),'native word source/clock partition coverage differs')
    parts=[];transfers={};owners={};parents=list(source_bindings)
    for fpga in sorted(source_directories):
        root=Path(source_directories[fpga]).resolve();descriptor=root/'source-catalog.json'
        parents.append(bind_store_source(descriptor,'physical-input-document-v1'));report=json.loads(descriptor.read_text())
        _require(report.get('schema')=='emuflow.native-word-source-preparation/v1'and report.get('status')=='complete'and
                 report.get('fpga')==fpga and report.get('directory')==str(root),'native word source preparation owner differs')
        for name in FILES:
            path=root/name;parents.append(bind_store_source(path,'physical-input-document-v1'))
            _require(report['files'].get(name)==dict(sha256=_hash(path),bytes=path.stat().st_size),'native word source preparation file changed')
        with closing(sqlite3.connect((root/'catalog.sqlite').as_uri()+'?mode=ro&immutable=1',uri=True))as db:
            meta={k:json.loads(v)for k,v in db.execute('SELECT * FROM metadata')}
            _require(meta['schema']==SCHEMA and meta['fpga']==fpga,'native word role catalog owner differs')
            parents.extend(StoreSource(Path(p['path']),p['schema'],tuple(p['generation']))for p in meta['source_generations'])
            for entry,source,sink,slot,arrival,ratio in db.execute('SELECT id,source,sink,slot,arrival,ratio FROM entries ORDER BY id'):
                value=(entry,source,sink,slot%(ratio//8),ratio,arrival-slot)
                _require(source in source_directories and sink in source_directories,'native word source network lacks a transfer owner')
                _require(entry not in transfers or transfers[entry]==value,'native word source transfer copies disagree')
                transfers[entry]=value
            for kind,entry in db.execute('SELECT kind,entry_id FROM endpoints'):
                _require((entry,kind)not in owners,'native word source network repeats a canonical endpoint')
                owners[entry,kind]=fpga
            parts.append(dict(fpga=fpga,clocks=clocks[fpga],calendar_periods=meta['calendar_periods'],
                tx_entries=list(db.execute("SELECT entry_id,query FROM endpoints WHERE kind='tx'ORDER BY entry_id")),
                rx_entries=list(db.execute("SELECT source_origin,entry_id FROM endpoints WHERE kind='rx'ORDER BY source_origin"))))
    for entry,source,sink,*_ in transfers.values():
        _require(owners.get((entry,'tx'))==source and owners.get((entry,'rx'))==sink,'native word source network lacks both canonical endpoint roles')
    parents=_parents(parents)
    return write_native_word_index(output,transfers=(transfers[k]for k in sorted(transfers)),partitions=parts,
        clock_windows=clock_windows,source_context=source_context,source_bindings=parents,
        word_period_ps=word_period_ps,reference_period_ps=reference_period_ps,
        maximum_repeat_words=maximum_repeat_words,maximum_contexts=maximum_contexts)
