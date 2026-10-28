"""Bounded control and source indexes for native word-network analysis.

The index contains transport roles and clock windows, not the DUT graph.
Native code consumes timing streams and computes continuous calendar events.
Projection output remains separate from full physical/global-STA acceptance.
"""
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import tempfile

from .errors import EmuFlowError, ValidationError
from .native_protocol import CheckedRequestWriter
from .native_tools import native_file_generation, resolve_native_executable
from .store_publication import (
    bind_store_source, publish_sqlite_store, require_store_sources_unchanged,
    validate_store_destination,
)

INDEX_SCHEMA='native-word-observation-index-v1'
ROW_SCHEMA='native-word-observation-rows-v1'
REQUEST_MAGIC=0x315150574e4d5545
ROLES=('tx_costs','fanins','captures','missing_rx')


def write_native_word_index(output, *, transfers, partitions, clock_windows,
                            source_context, source_bindings=(), word_period_ps=5000,
                            reference_period_ps=100000, maximum_repeat_words=4096,
                            maximum_contexts=1000000):
    """Write canonical role cursors supplied by the validated Phase6 producer.

    Transfer tuples are (id, source, sink, word_phase, serial_ratio, latency_words).
    Each partition supplies fpga, clocks (native -> logical), complete local
    counter periods, TX (entry, query) and RX (source_origin, entry) cursors.
    clock_windows are explicit (launch_clock, edge, capture_clock, edge, ps)
    tuples from the complete source clock/constraint context. A selected source
    path population must not be used to infer which clock pairs can be timed.
    Existing parent seals must keep their original generation bindings.
    """
    output=Path(output).resolve();output.parent.mkdir(parents=True,exist_ok=True)
    parents=validate_store_destination(output,source_bindings)
    values=(word_period_ps,reference_period_ps,maximum_repeat_words,maximum_contexts)
    if any(type(value)is not int or value<=0 for value in values):
        raise ValidationError('native word index periods and execution bounds must be positive integers')
    descriptor,name=tempfile.mkstemp(prefix='.native-word-index-',suffix='.sqlite',dir=output.parent)
    os.close(descriptor)
    temporary=Path(name)
    try:
        with sqlite3.connect(temporary)as db:
            db.execute('PRAGMA application_id=0x454d574e');db.execute('PRAGMA user_version=1')
            db.execute('PRAGMA foreign_keys=ON')
            db.executescript('''
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
                CREATE TABLE partitions(seq INTEGER PRIMARY KEY,fpga TEXT UNIQUE NOT NULL);
                CREATE TABLE transfers(id TEXT PRIMARY KEY,source_partition TEXT NOT NULL REFERENCES partitions(fpga),
                  sink_partition TEXT NOT NULL REFERENCES partitions(fpga),phase_word INTEGER NOT NULL,
                  ratio_serial_uis INTEGER NOT NULL,fixed_latency_words INTEGER NOT NULL) WITHOUT ROWID;
                CREATE TABLE clocks(fpga TEXT REFERENCES partitions(fpga),native_clock TEXT,logical_clock TEXT NOT NULL,
                  PRIMARY KEY(fpga,native_clock)) WITHOUT ROWID;
                CREATE TABLE calendar_periods(fpga TEXT REFERENCES partitions(fpga),period_words INTEGER,
                  PRIMARY KEY(fpga,period_words)) WITHOUT ROWID;
                CREATE TABLE tx_entries(fpga TEXT REFERENCES partitions(fpga),entry_id TEXT UNIQUE REFERENCES transfers(id),
                  query TEXT NOT NULL,PRIMARY KEY(fpga,entry_id)) WITHOUT ROWID;
                CREATE TABLE rx_entries(fpga TEXT REFERENCES partitions(fpga),origin TEXT,entry_id TEXT UNIQUE REFERENCES transfers(id),
                  PRIMARY KEY(fpga,origin)) WITHOUT ROWID;
                CREATE TABLE windows(launch_clock TEXT,launch_edge INTEGER,capture_clock TEXT,capture_edge INTEGER,
                  reference_window_ps INTEGER NOT NULL,PRIMARY KEY(launch_clock,launch_edge,capture_clock,capture_edge)) WITHOUT ROWID;
            ''')
            parts=list(partitions)
            if not 0<len(parts)<=64:raise ValidationError('native word index partition population is invalid')
            db.executemany('INSERT INTO partitions VALUES(?,?)',((i,p['fpga'])for i,p in enumerate(parts)))
            db.executemany('INSERT INTO transfers VALUES(?,?,?,?,?,?)',transfers)
            for part in parts:
                name=part['fpga']
                db.executemany('INSERT INTO clocks VALUES(?,?,?)',((name,native,logical)for native,logical in part['clocks'].items()))
                db.executemany('INSERT INTO calendar_periods VALUES(?,?)',((name,period)for period in part['calendar_periods']))
                db.executemany('INSERT INTO tx_entries VALUES(?,?,?)',((name,entry,query)for entry,query in part['tx_entries']))
                db.executemany('INSERT INTO rx_entries VALUES(?,?,?)',((name,origin,entry)for origin,entry in part['rx_entries']))
            db.executemany('INSERT INTO windows VALUES(?,?,?,?,?)',clock_windows)
            metadata=dict(zip(('word_period_ps','reference_period_ps','maximum_repeat_words','maximum_contexts'),values))
            metadata.update(word_width_bits=8,source_context=source_context,
                source_generations=[dict(path=str(p.path),schema=p.schema,generation=p.generation)for p in parents])
            db.executemany('INSERT INTO metadata VALUES(?,?)',[(k,json.dumps(v,sort_keys=True))for k,v in metadata.items()])
            db.execute('INSERT INTO metadata VALUES(?,?)',('schema','emuflow.native-word-observation-index/v1'))
        db.close()
        require_store_sources_unchanged(parents)
        publish_sqlite_store(temporary,output,INDEX_SCHEMA,sources=parents)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def _write_projection_request(path, *, sources, covered, project):
    writer=CheckedRequestWriter(Path(path))
    try:
        writer.put('QIII',REQUEST_MAGIC,1,len(covered),int(project))
        for source in sources:writer.put('3Q4q',*source.generation)
        for seq,ports in covered:
            writer.put('II',seq,len(ports))
            for port in ports:writer.text(port)
        writer.finish()
    finally:writer.close()


def analyze_native_word_network(index, observations, output, *, project=True,
                                source_bindings=None, executable=None):
    """Analyze frozen native exports; report pending partitions explicitly.

    Each observed partition supplies the four paths named by ROLES, plus an
    optional declared_inputs sequence for separately queried I/O witnesses.
    An empty observations mapping binds source calendars without inventing
    timing files. Existing exporter seals use their original source_bindings;
    a fresh invocation without content seals may bind current file generations.
    """
    if type(project)is not bool:raise ValidationError('native word projection mode must be Boolean')
    index=Path(index).resolve();output=Path(output).resolve()
    with sqlite3.connect(index.as_uri()+'?mode=ro&immutable=1',uri=True)as db:
        names={name:seq for seq,name in db.execute('SELECT seq,fpga FROM partitions ORDER BY seq')}
    db.close()
    if set(observations)-set(names):raise ValidationError('native word observations name an unknown partition')
    files=[];covered=[]
    for name in sorted(observations,key=names.__getitem__):
        record=observations[name]
        if set(record)-{'declared_inputs'}!=set(ROLES):raise ValidationError('native word observation stream roles differ')
        ports=tuple(record.get('declared_inputs',()))
        if any(not isinstance(p,str)or not p for p in ports)or len(set(ports))!=len(ports):
            raise ValidationError('native word separately queried input identities are invalid')
        files.extend(Path(record[role]).resolve()for role in ROLES);covered.append((names[name],ports))
    ordered=[index,*files]
    parents=tuple(source_bindings)if source_bindings is not None else tuple(
        bind_store_source(path,INDEX_SCHEMA if i==0 else ROW_SCHEMA)for i,path in enumerate(ordered))
    by_path={p.path:p for p in parents}
    if len(by_path)!=len(parents)or set(by_path)!=set(ordered):
        raise ValidationError('native word input generation bindings are incomplete or repeated')
    if by_path[index].schema!=INDEX_SCHEMA or any(by_path[p].schema!=ROW_SCHEMA for p in files):
        raise ValidationError('native word input source schemas differ')
    validate_store_destination(output,parents)
    output.parent.mkdir(parents=True,exist_ok=True)
    native=resolve_native_executable('emuflow-native',executable)
    with tempfile.TemporaryDirectory(prefix='.native-word-analysis-',dir=output.parent)as directory:
        request=Path(directory)/'request.bin'
        _write_projection_request(request,sources=[by_path[p]for p in ordered],covered=covered,project=project)
        result=subprocess.run([native,'phase7','analyze-word-network',str(index),str(request),str(output),
            *(str(path)for path in files)],capture_output=True,text=True)
        if result.returncode:raise EmuFlowError('native word-network analysis failed: '+result.stderr[-16384:])
    require_store_sources_unchanged(parents)
    return output
