"""Source-bound native captures and independent ordered validation."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
from unittest.mock import patch

import pytest

from emuflow_tools.ir.emuir_fixture import write_compact_emuir
from tests.native_semantic_facts import read_captures as read_native_static_exact_capture_stream
from tests import test_combinational_cut as reference
from tests.native_build import emuflow_native


@pytest.fixture(params=['base', 'gaps', 'unicode', 'duplicate', 'top', 'zero', 'relay'])
def captures(tmp_path, monkeypatch, request):
    fixture = reference.StaticExactCombinationalCutPartitionTest(); fixture.setUp()
    _, assignment = fixture._exact_artifacts(dependent_return=request.param != 'relay')
    root = tmp_path/'native'; (root/'bin').mkdir(parents=True)
    binary = emuflow_native(); (root/'bin/emuflow-native').symlink_to(binary)
    monkeypatch.setenv('EMUFLOW_NATIVE_ROOT', str(root))
    source, inputs, topology, parts_file = [tmp_path/name for name in ('source.sqlite', 'request.bin', 'topology.bin', 'parts.bin')]
    write_compact_emuir(source, fixture.ir.value)
    cuts = sorted(assignment['cut_nets'], key=lambda row: row['net'])
    parts = sorted(set(assignment['instance_assignment'].values()))
    with closing(sqlite3.connect(source)) as db, db:
        if request.param == 'gaps': db.execute('UPDATE endpoint_records SET ordinal=ordinal*7+3')
        if request.param == 'unicode': db.execute("UPDATE pin_names SET name=name||'端口α'")
        if request.param in ('duplicate', 'top'):
            net, ordinal = db.execute("SELECT e.net_seq,e.ordinal FROM endpoint_records e JOIN cell_records c ON c.seq=e.instance_seq JOIN payloads p ON p.seq=c.resources_seq WHERE e.role=1 AND json_extract(p.value,'$.ff')>0 LIMIT 1").fetchone()
            target = db.execute('SELECT max(ordinal)+1 FROM endpoint_records WHERE net_seq=? AND role=1', (net,)).fetchone()[0]
            db.execute('INSERT INTO endpoint_records SELECT net_seq,role,?,instance_seq,port_seq,bit,extras_seq FROM endpoint_records WHERE net_seq=? AND role=1 AND ordinal=?', (target, net, ordinal))
            if request.param == 'top': db.execute('UPDATE endpoint_records SET instance_seq=NULL WHERE net_seq=? AND role=1 AND ordinal=?', (net, target))
            db.execute('UPDATE endpoint_records SET bit=?', (2**63-1,))
            counts = json.loads(db.execute("SELECT value FROM metadata WHERE key='record_counts_json'").fetchone()[0]); counts['endpoint_records'] += 1
            db.execute("UPDATE metadata SET value=? WHERE key='record_counts_json'", (json.dumps(counts),))
        cells = list(db.execute('SELECT seq,id FROM cell_records ORDER BY seq'))
        nets = dict(db.execute('SELECT id,seq FROM net_records'))
        sinks = db.execute('SELECT count(*) FROM endpoint_records WHERE role=1').fetchone()[0]
    data = bytearray(struct.pack('<Q6I', 0x31584553464d5545, 4, len(cells), len(nets), len(cuts), len(parts), sinks))
    for _seq, name in cells: data.extend(struct.pack('<I', parts.index(assignment['instance_assignment'][name])))
    for cut in cuts:
        destinations = sorted(set(cut['sink_fpgas']))
        data.extend(struct.pack('<3I', nets[cut['net']], parts.index(cut['source_fpgas'][0]), len(destinations)))
        data.extend(b''.join(struct.pack('<I', parts.index(part)) for part in destinations))
    inputs.write_bytes(data)
    parts_file.write_bytes(struct.pack('<QII', 0x31504553464d5545, 1, len(parts))+b''.join(struct.pack('<I', len(part.encode()))+part.encode() for part in parts))
    subprocess.run([str(binary), 'phase3', 'analyze', '--indexed-input', str(source), str(inputs), str(topology)], check=True)
    raw = topology.read_bytes(); offset = 24
    for _ in cuts:
        _, edges = struct.unpack_from('<BI', raw, offset); offset += 5+4*edges
    if request.param == 'zero':
        raw = bytearray(raw[:offset]); struct.pack_into('<Q', raw, 16, 0); topology.write_bytes(raw)
    coordinates = list(struct.iter_unpack('<4I', raw[offset:]))
    return dict(binary=binary, source=source, request=inputs, topology=topology, parts_file=parts_file,
                cuts=cuts, parts=parts, assignment=assignment, coordinates=coordinates, mode=request.param)


def command(case, operation, output, captures=None):
    result = [str(case['binary']), 'phase3', operation, *(str(case[key]) for key in ('source', 'request', 'topology', 'parts_file'))]
    if captures is not None: result.append(str(captures))
    return result+[str(output)]


def pack_records(rows):
    offsets, payload = [0], bytearray()
    for net, part, kind, endpoint, port, bit in rows:
        fields = (net, part, endpoint, port, bit)
        payload.extend(struct.pack('<B5I', kind, *(len(value) for value in fields)))
        payload.extend(b''.join(fields)); offsets.append(len(payload))
    return struct.pack('<QIIQ', 0x31434553464d5545, 1, len(rows), len(payload))+b''.join(struct.pack('<Q', value) for value in offsets)+payload


def test_native_build_matches_explicit_reference_and_retires_private_index(captures, tmp_path):
    output, proof = [tmp_path/name for name in ('captures.bin', 'proof.json')]
    subprocess.run(command(captures, 'build-captures', output), check=True)
    # Independent explicit endpoints; no old Python ordered-merge publisher.
    assert len(captures['coordinates']) <= 65536
    facts=set()
    with closing(sqlite3.connect(captures['source'])) as db:
        for cut,part,net,sink in captures['coordinates']:
            row=db.execute("SELECT c.id,p.name,e.bit FROM endpoint_records e LEFT JOIN cell_records c ON c.seq=e.instance_seq JOIN pin_names p ON p.seq=e.port_seq WHERE e.net_seq=? AND e.role=1 ORDER BY e.ordinal LIMIT 1 OFFSET ?",(net,sink)).fetchone()
            assert row is not None
            instance,port,bit=row
            facts.add((captures['cuts'][cut]['net'],captures['parts'][part],
                'top-output' if instance is None else 'architectural-state',
                f'top:{port}[{bit}]' if instance is None else instance,
                '' if instance is None else port,-1 if instance is None else bit))
    expected=[]
    for i,(net,part,kind,endpoint,port,bit) in enumerate(sorted(facts)):
        row=dict(id=f'capture{i:06d}',cut_net=net,fpga=part,kind=kind,endpoint=endpoint)
        if kind=='architectural-state':row.update(port=port,bit=bit)
        expected.append(row)
    assert list(read_native_static_exact_capture_stream(output)) == list(expected)
    subprocess.run(command(captures, 'validate-captures', proof, output), check=True)
    summary = json.loads(proof.read_text())
    assert summary['status'] == 'pass' and summary['captures'] == len(expected)
    assert summary['coordinates'] == len(captures['coordinates'])
    assert not Path(str(output)+'.sqlite').exists()
    assert not list(tmp_path.glob('*.stage.*'))


@pytest.mark.parametrize('corruption', ['missing', 'extra', 'order', 'bit', 'endpoint', 'truncated', 'offset'])
def test_independent_validator_rejects_mutated_stream_before_replacing_proof(captures, tmp_path, corruption):
    stream, proof = tmp_path/'captures.bin', tmp_path/'proof.json'
    subprocess.run(command(captures, 'build-captures', stream), check=True)
    values = []
    for row in read_native_static_exact_capture_stream(stream):
        kind = int(row['kind'] == 'architectural-state')
        bit = row.get('bit', 0)
        values.append((row['cut_net'].encode(), row['fpga'].encode(), kind, row['endpoint'].encode(),
            row.get('port', '').encode(), bit.to_bytes(max(1, (bit.bit_length()+7)//8), 'big') if kind else b''))
    empty = not values
    rows = list(values) or [(b'unknown', b'fpga0', 0, b'top:missing[0]', b'', b'')]
    if corruption == 'missing': rows.pop()
    elif corruption == 'extra': rows.append(rows[-1])
    elif corruption == 'order':
        if len(rows) < 2: rows.append(rows[0])
        rows.reverse()
    elif corruption == 'bit':
        row = list(rows[0]); row[5] = b'\x7f'; rows[0] = tuple(row)
    elif corruption == 'endpoint':
        row = list(rows[0]); row[3] += b'changed'; rows[0] = tuple(row)
    payload = pack_records(rows)
    if empty and corruption == 'missing': payload = payload[:-1]
    if corruption == 'truncated': payload = payload[:-1]
    elif corruption == 'offset': payload = payload[:24]+struct.pack('<Q', 1)+payload[32:]
    stream.write_bytes(payload); proof.write_bytes(b'previous-proof')
    result = subprocess.run(command(captures, 'validate-captures', proof, stream), capture_output=True, text=True)
    assert result.returncode != 0
    assert proof.read_bytes() == b'previous-proof'
    assert not list(tmp_path.glob('*.stage.*'))


def test_build_independent_source_validation_failure_preserves_previous_generation(captures, tmp_path):
    # The old producer only inspects selected resource flags. The independent
    # ResourceView must reject an unknown field after production, before publish.
    with closing(sqlite3.connect(captures['source'])) as db, db:
        if captures['coordinates']:
            db.execute("UPDATE payloads SET value=json_set(value,'$.mystery',1) WHERE seq IN (SELECT resources_seq FROM cell_records WHERE json_extract((SELECT value FROM payloads WHERE seq=resources_seq),'$.ff')>0)")
        else:
            db.execute('PRAGMA user_version=99')
    output = tmp_path/'captures.bin'; output.write_bytes(b'previous-generation')
    result = subprocess.run(command(captures, 'build-captures', output), capture_output=True, text=True)
    assert result.returncode != 0
    assert output.read_bytes() == b'previous-generation'
    assert not list(tmp_path.glob('*.stage.*'))




def test_retired_capture_index_entry_and_schema_are_rejected(tmp_path):
    binary=emuflow_native()
    output=tmp_path/'old-result'
    output.write_text('previous')
    result=subprocess.run([str(binary),'phase3','resolve-captures',
                           *[str(tmp_path/'missing')]*4,str(output)],capture_output=True)
    assert result.returncode and output.read_text()=='previous'
    result=subprocess.run([str(binary),'check-runtime','exact-capture-index-private-v1'],capture_output=True)
    assert result.returncode
    assert not list(tmp_path.glob('*.sqlite'))
