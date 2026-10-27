"""Explicit current typed timing facts, without production publication or validation.

Only schema encoding and canonical record hashes live here. Native readers and
real producer tests independently qualify identity, coverage and publication.
"""
from hashlib import sha256
import importlib
import json
from pathlib import Path
import sqlite3

from emuflow.errors import ValidationError


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _finish(db, path, module, header, counts, logical, source=None):
    metadata={'header':header,'counts':counts,'logical_sha256':logical}
    if source is not None:metadata['source']=source
    db.executemany('INSERT INTO metadata VALUES (?,?)',[(k,_json(v)) for k,v in metadata.items()])
    db.commit();db.close()
    raw=path.with_name(path.name+'.facts.sqlite')
    digest=sha256(raw.read_bytes()).hexdigest();target=path.with_name(path.name+'.store.'+digest+'.sqlite')
    raw.replace(target)
    name=module.__name__.rsplit('.',1)[-1].removesuffix('_storage')
    prefix=name.upper()
    selector=dict(schema=getattr(module,prefix+'_STORAGE_SCHEMA'),logical_schema=getattr(module,prefix+'_SCHEMA'),
        application_id=getattr(module,prefix+'_APPLICATION_ID'),user_version=1,**metadata,
        artifact=target.name,store_bytes=target.stat().st_size,store_sha256=digest)
    path.write_text(_json(selector))
    return getattr(module,'read_'+name+'_storage')(path)


def _database(path, name, application):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    raw=path.with_name(path.name+'.facts.sqlite');raw.unlink(missing_ok=True)
    db=sqlite3.connect(raw);db.row_factory=sqlite3.Row
    schema=Path(__file__).parents[1]/'src/emuflow/data/schema'/name
    db.executescript(f'PRAGMA application_id={application};PRAGMA user_version=1;'+schema.read_text())
    return path,db


def local_timing_facts(path, header, records):
    from emuflow import local_path_timing_storage as m
    path,db=_database(path,'local_path_timing.sql',m.LOCAL_PATH_TIMING_APPLICATION_ID)
    try:
        for seq, raw in enumerate(records):
            assert seq<131072
            row=dict(raw)
            db.execute('INSERT INTO timings VALUES (?,?,?,?,?,?,?,?,?,?,?)',(seq,row['id'],row['kind'],row['fpga'],
                row['clock_domain'],float(row['clock_period_ns']),row.get('required_time_ns'),row['start_pin'],row['end_pin'],
                row.get('measurement'),float(row['delay_ns'])))
            db.executemany('INSERT INTO path_pins VALUES (?,?,?)',[(row['id'],i,v) for i,v in enumerate(row.get('path_pins',()))])
        count=db.execute('SELECT count(*) FROM timings').fetchone()[0]
        counts={'local_paths':count};digest=sha256()
        for row in m._iter_records(db):digest.update((_json(row)+'\n').encode())
        logical=sha256(_json(dict(header=header,counts=counts,path_sha256=digest.hexdigest())).encode()).hexdigest()
        return _finish(db,path,m,dict(header),counts,logical)
    except sqlite3.IntegrityError as error:
        db.close();raise ValidationError('local timing fixture identity is duplicated') from error
    except BaseException:db.close();raise


def logic_timing_facts(path, *, header, segments, unsupported_member_paths=(), unmeasured_segments=()):
    from emuflow import logic_segment_timing_storage as m
    path,db=_database(path,'logic_segment_timing.sql',m.LOGIC_SEGMENT_TIMING_APPLICATION_ID)
    keys=('id','kind','system_path','member_path','cut_index','fpga','replace_tx_endpoint','start_pin','end_pin',
          'delay_ns','static_exact_segment_id','start_object_kind','end_object_kind','cone_anchor_object_kind',
          'cone_anchor_pin','measurement','actual_start_object','actual_end_object')
    try:
        for seq, raw in enumerate(segments):
            assert seq<131072
            row=dict(raw,delay_ns=float(raw['delay_ns']),measurement=raw.get('measurement','endpoint-exact'))
            db.execute('INSERT INTO segments VALUES ('+','.join('?' for _ in range(19))+')',(seq,*(row.get(k) for k in keys)))
        digests=[]
        for table,records,columns in [('unsupported_members',unsupported_member_paths,('path','reason')),
                ('unmeasured_segments',unmeasured_segments,('id','kind','system_path','member_path','reason'))]:
            digest=sha256()
            for seq,row in enumerate(records):
                assert seq<131072
                db.execute('INSERT INTO '+table+' VALUES ('+','.join('?' for _ in range(len(columns)+1))+')',
                           (seq,*(row[k] for k in columns)))
                digest.update((_json(row)+'\n').encode())
            digests.append(digest.hexdigest())
        aggregate=db.execute("SELECT count(*),count(DISTINCT system_path),count(DISTINCT member_path),sum(measurement='endpoint-exact'),sum(measurement='cut-net-cone-upper-bound'),sum(measurement='optimized-endpoint-collection-upper-bound') FROM segments").fetchone()
        counts=dict(zip(('segments','system_paths','member_paths','endpoint_exact_segments','cone_bound_segments','endpoint_collection_bound_segments'),[v or 0 for v in aggregate]))
        counts.update(unsupported_member_paths=db.execute('SELECT count(*) FROM unsupported_members').fetchone()[0],
                      unmeasured_segments=db.execute('SELECT count(*) FROM unmeasured_segments').fetchone()[0])
        header={**header,'coverage':{**header['coverage'],**{k:v for k,v in counts.items() if k!='unmeasured_segments'}}}
        source={k:header.get(k) for k in ('design','platform','fpga','system_routes_sha256','tdm_schedule_sha256','semantic_contract_sha256')}
        digest=sha256()
        for row in db.execute('SELECT * FROM segments ORDER BY system_path,member_path,cut_index,segment_id'):
            digest.update((_json(m._segment_from_row(row))+'\n').encode())
        logical=sha256(_json(dict(header=header,counts=counts,source=source,segment_sha256=digest.hexdigest(),
            unsupported_sha256=digests[0],unmeasured_sha256=digests[1])).encode()).hexdigest()
        return _finish(db,path,m,header,counts,logical,source)
    except sqlite3.IntegrityError as error:
        db.close();raise ValidationError('logic timing fixture identity is duplicated') from error
    except BaseException:db.close();raise


def physical_facts_writer(kind):
    if kind=='boundary_timing':return boundary_timing_facts
    if kind=='logic_segment_timing':return logic_timing_facts
    if kind=='local_path_timing':return local_timing_facts
    module=importlib.import_module('emuflow.'+kind+'_storage')
    return getattr(module,'write_'+kind+'_storage')



def boundary_timing_facts(path, header, records):
    from emuflow import boundary_timing_storage as m
    path,db=_database(path,'boundary_timing.sql',m.BOUNDARY_TIMING_APPLICATION_ID)
    keys=('id','kind','schedule_entry','delay_ns','start_object','end_object','measurement')
    try:
        for seq,raw in enumerate(records):
            assert seq<131072
            row=dict(raw,delay_ns=float(raw['delay_ns']))
            db.execute('INSERT INTO timings VALUES (?,?,?,?,?,?,?)',tuple(row[k] for k in keys))
        kinds=dict(db.execute('SELECT kind,count(*) FROM timings GROUP BY kind'))
        counts=dict(endpoints=sum(kinds.values()),tx=kinds.get('tx',0),rx=kinds.get('rx',0))
        header={**header,'coverage':counts};digest=sha256()
        for row in db.execute('SELECT * FROM timings ORDER BY id'):
            digest.update((_json(m._record_from_row(row))+'\n').encode())
        logical=sha256(_json(dict(header=header,counts=counts,endpoint_sha256=digest.hexdigest())).encode()).hexdigest()
        return _finish(db,path,m,header,counts,logical)
    except sqlite3.IntegrityError as error:
        db.close();raise ValidationError('boundary timing fixture identity is duplicated') from error
    except BaseException:db.close();raise
