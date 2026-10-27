"""Source-bound split ownership and bounded native production adapter."""

from emuflow.common.paths import DATA_ROOT
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import os

from emuflow.common.errors import ValidationError
from emuflow.storage.sqlite_artifacts import immutable_sqlite_uri
from emuflow.storage.store_publication import StoreSource, bind_store_source, bind_view_sources, publish_sqlite_store, require_store_sources_unchanged, validate_store_destination
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.common.tool_runtime import NativeToolProcess

SCHEMA='emuflow.split-netlists-sqlite/v3'
_PROJECTION=(DATA_ROOT / 'schema/phase6_split_projection.sql').read_text()
_TABLES=('metadata','parts','resources','net_segments','source_instances','instance_copies',
    'source_nets','payloads','pin_names','endpoint_segments','ports','cut_sinks',
    'exact_cut_sources','exact_cut_nodes','exact_logic_segments')
_ROLES={'source':'emuir-compact-v2','assignment':'assignment-index-v4',
    'schedule':'tdm-schedule-store-v4','exact':'phase3-exact-data-v2'}


def _metadata(connection,alias='main'):
    rows=dict(connection.execute('SELECT key,value FROM '+alias+'.metadata LIMIT 65'))
    if len(rows)>64:raise ValidationError('split control metadata key catalog exceeds bound')
    if any(len(value.encode())>8*1024*1024 for value in rows.values()):
        raise ValidationError('split control metadata exceeds bound')
    try:
        return {key:json.loads(value) for key,value in rows.items()}
    except (TypeError,ValueError) as error:
        raise ValidationError('invalid split control metadata JSON') from error


def _sources(metadata):
    catalog=metadata.get('source_bindings')
    roles=dict(_ROLES) if metadata.get('semantic_contract_binary') is not None else {key:value for key,value in _ROLES.items() if key!='exact'}
    if (metadata.get('semantic_contract_binary') or {}).get('schema')=='emuflow.phase3-static-exact-binary/v1':roles['exact']='phase3-exact-data-v1'
    if type(catalog) is not dict or set(catalog)!=set(roles):
        raise ValidationError('split source owner coverage disagrees')
    sources=[]
    for role,schema in roles.items():
        record=catalog[role]
        if (type(record) is not dict or set(record)!={'path','schema','generation'}
            or record.get('schema')!=schema or type(record.get('path')) is not str
            or not Path(record['path']).is_absolute() or type(record.get('generation')) is not list
            or len(record['generation'])!=7 or any(type(v) is not int for v in record['generation'])):
            raise ValidationError('invalid split original source binding: '+role)
        sources.append(StoreSource(Path(record['path']),schema,tuple(record['generation'])))
    require_store_sources_unchanged(sources)
    source,assignment,schedule=sources[:3]
    with closing(sqlite3.connect(immutable_sqlite_uri(source.path),uri=True)) as db:
        design=json.loads(db.execute("SELECT value FROM metadata WHERE key='design_json'").fetchone()[0])
        counts=json.loads(db.execute("SELECT value FROM metadata WHERE key='record_counts_json'").fetchone()[0])
    with closing(sqlite3.connect(immutable_sqlite_uri(assignment.path),uri=True)) as db:
        authority={key:json.loads(value) for key,value in db.execute("SELECT key,value FROM metadata WHERE key IN ('design','platform','semantic_contract_sha256','semantic_contract_binary','source_instance_count')")}
    with closing(sqlite3.connect(immutable_sqlite_uri(schedule.path),uri=True)) as db:
        header=json.loads(db.execute("SELECT value FROM metadata WHERE key='header'").fetchone()[0])
    if (design!=metadata.get('design') or authority.get('design')!=design.get('name')
        or header.get('design')!=design.get('name') or authority.get('source_instance_count')!=counts.get('cell_records')
        or any(authority.get(key)!=metadata.get(key) or header.get(key)!=metadata.get(key)
               for key in ('platform','semantic_contract_sha256'))
        or authority.get('semantic_contract_binary')!=metadata.get('semantic_contract_binary')):
        raise ValidationError('split original source namespace disagrees')
    if len(sources)==4:
        artifact=authority['semantic_contract_binary'].get('artifact')
        if type(artifact) is not str or Path(artifact).name!=artifact or assignment.path.with_name(artifact).resolve()!=sources[-1].path.resolve():
            raise ValidationError('split original exact owner namespace disagrees')
    require_store_sources_unchanged(sources)
    return tuple(sources)


class SplitProjection:
    def __init__(self,alias,prefix,sources):
        self.alias=alias;self.prefix=prefix;self.sources=sources
        self.pattern=re.compile(r'\b'+re.escape(alias)+r'\.('+ '|'.join(_TABLES)+r')\b')

    def sql(self,statement):
        if self.alias=='main' or self.pattern.search(statement):
            statement=self.pattern.sub(lambda m:self.prefix+m[1],statement)
            # v3 logical relations are views of the original owners. Physical
            # v2 index hints do not name indexes on these views; original owner
            # indexes remain available to the SQLite query planner.
            statement=re.sub(r'\s+INDEXED\s+BY\s+[A-Za-z0-9_]+','',statement)
        return statement


class SplitProjectionConnection(sqlite3.Connection):
    def _sql(self,statement):
        for projection in getattr(self,'_split_projections',()):
            statement=projection.sql(statement)
        return statement

    def execute(self,sql,parameters=()):
        return super().execute(self._sql(sql),parameters)

    def executemany(self,sql,parameters):
        return super().executemany(self._sql(sql),parameters)

    def executescript(self,sql):
        return super().executescript(self._sql(sql))

    def close(self):
        sources=getattr(self,'_split_sources',())
        self._split_sources=()
        try:
            require_store_sources_unchanged(sources)
        finally:
            super().close()


def attach_split_projection(connection,path,alias='main'):
    if re.fullmatch('[A-Za-z_][A-Za-z0-9_]*',alias) is None:
        raise ValidationError('invalid split SQL namespace')
    path=Path(path)
    if alias!='main':
        connection.execute('ATTACH DATABASE ? AS '+alias,(immutable_sqlite_uri(path),))
    application=connection.execute('PRAGMA '+alias+'.application_id').fetchone()[0]
    version=connection.execute('PRAGMA '+alias+'.user_version').fetchone()[0]
    if application!=0x454D5553 or version not in (2,3):
        raise ValidationError('unsupported split-netlist SQLite artifact')
    if version==2:
        return None
    metadata=_metadata(connection,alias)
    if metadata.get('schema')!=SCHEMA:
        raise ValidationError('source-bound split schema disagrees')
    root=bind_store_source(path,'split-netlists-v3');sources=(root,)+_sources(metadata)
    prefix='' if alias=='main' else '__'+alias+'_projection_'
    stem='_split_owner_' if not prefix else prefix+'owner_'
    fields={'{{SPLIT}}':alias,'{{PREFIX}}':prefix,
        '{{SOURCE}}':stem+'source','{{ASSIGNMENT}}':stem+'assignment','{{SCHEDULE}}':stem+'schedule'}
    for name,source in zip(('source','assignment','schedule'),sources[1:4]):
        connection.execute('ATTACH DATABASE ? AS '+stem+name,(immutable_sqlite_uri(source.path),))
    script=_PROJECTION
    for key,value in fields.items():script=script.replace(key,value)
    # Static schema text only; do not commit a caller's active transaction.
    for statement in script.split(';'):
        if statement.strip():connection.execute(statement)
    if len(sources)==4:
        authority=stem+'assignment'
        count=connection.execute("SELECT count(*) FROM "+authority+".sqlite_master WHERE type='table' AND name IN ('exact_cut_nodes','exact_cut_sources','exact_logic_segments')").fetchone()[0]
        if count==3:
            for name in ('exact_cut_nodes','exact_cut_sources','exact_logic_segments'):
                connection.execute('DROP VIEW temp.'+prefix+name)
                connection.execute('CREATE TEMP VIEW '+prefix+name+' AS SELECT * FROM '+authority+'.'+name)
        elif metadata.get('semantic_contract_sha256') is not None:
            raise ValidationError('split exact semantic authority is missing')
    projection=SplitProjection(alias,prefix,sources)
    if isinstance(connection,SplitProjectionConnection):
        connection._split_projections=(*getattr(connection,'_split_projections',()),projection)
        connection._split_sources=(*getattr(connection,'_split_sources',()),*sources)
    require_store_sources_unchanged(sources)
    return projection


def bind_split_sources(path):
    with closing(sqlite3.connect(immutable_sqlite_uri(path),uri=True)) as connection:
        version=connection.execute('PRAGMA user_version').fetchone()[0]
        if version==2:return (bind_store_source(path,'split-netlists-v2'),)
        if version!=3:raise ValidationError('unsupported split-netlist SQLite artifact')
        metadata=_metadata(connection)
        if metadata.get('schema')!=SCHEMA:raise ValidationError('source-bound split schema disagrees')
        return (bind_store_source(path,'split-netlists-v3'),)+_sources(metadata)


def split_binary_path(path,descriptor):
    path=Path(path)
    if path.is_file():
        with closing(sqlite3.connect(immutable_sqlite_uri(path),uri=True)) as connection:
            app=connection.execute('PRAGMA application_id').fetchone()[0]
            version=connection.execute('PRAGMA user_version').fetchone()[0]
            if app==0x454D5553 and version==3:
                metadata=_metadata(connection);sources=_sources(metadata)
                if metadata.get('semantic_contract_binary')!=dict(descriptor):
                    raise ValidationError('split exact descriptor disagrees with original owner')
                return sources[-1].path
    return path.with_name(descriptor['artifact'])


def build_native_split(output,source,assignment,schedule,*,expected_design,expected_platform):
    require_native_schema('split-netlists-v3')
    schedule_path=getattr(schedule,'store_path',None)
    if schedule_path is None:raise ValidationError('native split requires current typed schedule')
    sources=(bind_store_source(source,'emuir-compact-v2'),bind_store_source(assignment,'assignment-index-v4'))+bind_view_sources((schedule,'tdm-schedule-store-v4'))
    output=Path(output);sources=validate_store_destination(output,sources);output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.split-native-',dir=output.parent) as temporary:
        temporary=Path(temporary);target=temporary/'split.sqlite';operation=temporary/'operation.json'
        require_store_sources_unchanged(sources);tool=resolve_native_executable('emuflow-native')
        argv=[tool,'--report',str(operation),'phase6','split',str(source),str(assignment),str(schedule_path),str(target)]
        with NativeToolProcess(tool,argv,environment=dict(os.environ),scratch_parent=temporary) as process:
            result=process.finish()
        if result.returncode:raise ValidationError('native split failed: '+result.stdout)
        if operation.stat().st_size>256*1024:raise ValidationError('native split result exceeds bound')
        proof=json.loads(operation.read_text());outputs=proof.get('outputs',[])
        if proof.get('stage')!='phase6.split' or proof.get('status')!='pass' or len(outputs)!=1:
            raise ValidationError('native split producer identity disagrees')
        row=outputs[0];g=row.get('generation',{});keys=('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')
        if (row.get('schema')!='split-netlists-v3' or row.get('path')!=str(target.resolve())
                or tuple(g.get(key) for key in keys)!=native_file_generation(target)):
            raise ValidationError('native split original output generation changed')
        with closing(sqlite3.connect(immutable_sqlite_uri(target),uri=True)) as connection:
            header=_metadata(connection)
        if header.get('design',{}).get('name')!=expected_design or header.get('platform')!=expected_platform:
            raise ValidationError('native split design/platform disagrees')
        summary=header.get('validation')
        if type(summary) is not dict or summary.get('status')!='pass':
            raise ValidationError('native split has no independent coverage certificate')
        authority=bind_split_sources(target)[1:]
        require_store_sources_unchanged(sources+authority)
        fd,sibling=tempfile.mkstemp(prefix='.'+output.name+'.',suffix='.tmp',dir=output.parent)
        os.close(fd);sibling=Path(sibling)
        try:
            os.replace(target,sibling)
            publish_sqlite_store(sibling,output,'split-netlists-v3',sources=sources+authority)
        finally:
            sibling.unlink(missing_ok=True)
        return summary


def validate_native_split(path,source,assignment,*,expected_design,expected_platform):
    refs=bind_split_sources(path)
    if refs[1].path.resolve()!=Path(source).resolve() or refs[2].path.resolve()!=Path(assignment).resolve():
        raise ValidationError('split original source/assignment owner disagrees')
    with closing(sqlite3.connect(immutable_sqlite_uri(path),uri=True)) as connection:
        header=_metadata(connection)
    if header.get('design',{}).get('name')!=expected_design or header.get('platform')!=expected_platform:
        raise ValidationError('split original design/platform disagrees')
    with tempfile.TemporaryDirectory(prefix='split-validation-') as directory:
        output=Path(directory)/'summary.json';tool=resolve_native_executable('emuflow-native')
        with NativeToolProcess(tool,[tool,'phase6','validate-split',str(path),str(output)],environment=dict(os.environ),scratch_parent=Path(directory)) as process:
            result=process.finish()
        if result.returncode:raise ValidationError('native split validation failed: '+result.stdout)
        if output.stat().st_size>65536:raise ValidationError('native split validation summary exceeds bound')
        summary=json.loads(output.read_text());require_store_sources_unchanged(refs)
        return summary
