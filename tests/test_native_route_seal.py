"""Route sealing contracts; real NVDLA remains the large-design acceptance."""
from contextlib import closing
import copy
import json
import math
from pathlib import Path
import sqlite3
import subprocess
from unittest.mock import patch

import pytest
from emuflow.errors import ValidationError
from emuflow.route_seal_native import seal_native_system_routes, write_route_seal_request
from emuflow.system_route_storage import write_system_route_storage, _demand_from_route
from tests.native_build import emuflow_native
from tests.test_phase4 import _storage_routes


@pytest.fixture
def native_root(tmp_path, monkeypatch):
    root = tmp_path/'native'; (root/'bin').mkdir(parents=True)
    (root/'bin/emuflow-native').symlink_to(emuflow_native())
    monkeypatch.setenv('EMUFLOW_NATIVE_ROOT', str(root))
    return emuflow_native()


def fixture(count=1, rich=False):
    logical = _storage_routes(0)
    routes = []
    floats = [0.0, 2e-300, 1e-5, 1e-4, 1e15, 1e16, math.nextafter(1.0, 2.0), 0.1 + 0.2]
    for i in range(count):
        row = copy.deepcopy(logical['routes'][0])
        row.update(id=f'demand-{i:05}', net=f'net-{i:05}')
        if rich:
            row.update(source='a中文', sinks=['b🙂', 'c\nquote"'],
                       tree_edges=[{'link':'abα', 'from':'a中文', 'to':'b🙂'},
                                   {'link':'bc', 'from':'b🙂', 'to':'c\nquote"'}],
                       width_bits=i+1, transport_round=i%3, cut_class='combinational',
                       dependency_level=i, combinational_dependency_depth=i,
                       predecessor_cut_nets=[f'net-{i-1:05}'] if i else [],
                       max_latency_cycles=0, predicted_max_delay_ns=floats[i%len(floats)])
        routes.append(row)
    logical['routes'] = routes
    logical['demands'] = [_demand_from_route(row) for row in routes]
    if rich:
        logical.update(metadata={'unicode':'α中🙂', 'escaped':'"\\\n\t', 'nested':[{}, [], None, True, 1<<80, 1e-120, -0.0]})
        logical['timing'] = {'kind':'fixture', 'paths':[{'path':f'path{i}', 'delay':i/10, 'nets':['α', 'β']} for i in range(3)]}
        logical['joint_optimization'] = {
            'candidate_generation': {'master_selection':[{'demand':row['id'], 'generator':'unicode-α'} for row in routes]},
            'refinement_batches': {'batch_count':2, 'maximum_parallel_batch':2, 'batches':[[0,2],[1]]}}
    return logical


@pytest.mark.parametrize('count,rich', [(0,False), (1,False), (0,True), (8,True), (4096,True)])
def test_native_matches_independent_python_logical_and_all_section_hashes(native_root, tmp_path, count, rich):
    logical = fixture(count, rich)
    manifest = write_system_route_storage(tmp_path/'routes.json', logical)
    store = tmp_path/manifest['artifact']
    before = store.read_bytes()
    result = seal_native_system_routes(store, logical)
    assert result['logical_sha256'] == manifest['logical_sha256']
    assert result['sections'] == manifest['sections']
    assert store.read_bytes() == before


class NeverReadRows(list):
    def __len__(self): return 1000000
    def __iter__(self): raise AssertionError('route population expanded into request')
    def __getitem__(self, index): raise AssertionError('route population indexed by request')


def test_request_carries_shape_only_without_expanding_large_populations(tmp_path):
    logical = fixture(1, True)
    logical['routes'] = logical['demands'] = NeverReadRows()
    logical['timing']['paths'] = NeverReadRows()
    logical['joint_optimization']['candidate_generation']['master_selection'] = NeverReadRows()
    logical['joint_optimization']['refinement_batches']['batches'] = NeverReadRows()
    request = tmp_path/'request'
    write_route_seal_request(request, logical)
    assert request.stat().st_size < 4096


@pytest.mark.parametrize('corruption', ['truncated', 'crc', 'header', 'orphan-demand', 'orphan-sink', 'edge-order', 'timing', 'selection', 'batch', 'count', 'shape-count', 'version', 'numeric-type', 'source-seal'])
def test_native_rejects_corruption_without_replacing_prior_seal(native_root, tmp_path, corruption):
    logical = fixture(8, True)
    manifest = write_system_route_storage(tmp_path/'routes.json', logical)
    store = tmp_path/manifest['artifact']; request = tmp_path/'request'; output = tmp_path/'seal'
    write_route_seal_request(request, logical)
    output.write_text('previous')
    if corruption in {'truncated','crc','version'}:
        data = bytearray(request.read_bytes())
        if corruption == 'truncated': data = data[:-1]
        elif corruption == 'crc': data[-1] ^= 1
        else:
            import struct, zlib
            struct.pack_into('<I',data,8,99)
            struct.pack_into('<I',data,len(data)-4,zlib.crc32(data[:-4]))
        request.write_bytes(data)
    elif corruption == 'shape-count':
        other=copy.deepcopy(logical);other['routes']=[]
        write_route_seal_request(request,other)
    else:
        with closing(sqlite3.connect(store)) as db, db:
            if corruption == 'header': db.execute("UPDATE metadata SET value=replace(value,'route-store-lifecycle','different') WHERE key='header'")
            if corruption == 'orphan-demand': db.execute("INSERT INTO demands VALUES (999,'orphan',999)")
            if corruption == 'orphan-sink': db.execute("INSERT INTO route_sinks VALUES(999,0,'b')")
            if corruption == 'edge-order': db.execute('UPDATE route_edges SET seq=9 WHERE route_seq=0 AND seq=0')
            if corruption == 'numeric-type':
                db.execute('PRAGMA ignore_check_constraints=ON')
                db.execute("UPDATE routes SET predicted_max_delay_ns='not-a-number' WHERE seq=0")
            if corruption == 'timing': db.execute("UPDATE timing_paths SET record_json=replace(record_json,'path0','bad') WHERE seq=0")
            if corruption == 'selection': db.execute("UPDATE route_master_selections SET generator='wrong' WHERE seq=0")
            if corruption == 'batch': db.execute('UPDATE refinement_batch_paths SET position=4 WHERE seq=0')
            if corruption == 'source-seal':
                sections=json.loads(db.execute("SELECT value FROM metadata WHERE key='sections'").fetchone()[0]);sections['timing_paths']['sha256']=None
                db.execute("UPDATE metadata SET value=? WHERE key='sections'",(json.dumps(sections),))
            if corruption == 'count':
                sections=json.loads(db.execute("SELECT value FROM metadata WHERE key='sections'").fetchone()[0]); sections['routes']['records']+=1
                db.execute("UPDATE metadata SET value=? WHERE key='sections'",(json.dumps(sections),))
    result = subprocess.run([str(native_root),'phase4','seal-routes',str(store),str(request),str(output)],capture_output=True)
    assert result.returncode
    assert output.read_text() == 'previous'
    assert not list(tmp_path.glob('seal.tmp.*'))




def test_post_storage_float_owner_and_empty_batches_match_reference(native_root,tmp_path):
    logical=fixture(1,True)
    logical['joint_optimization']['refinement_batches']={'batch_count':3,'maximum_parallel_batch':2,'batches':[[0,2],[],[1]]}
    manifest=write_system_route_storage(tmp_path/'routes.json',logical)
    store=tmp_path/manifest['artifact']
    # SQLite's numeric owner normalizes -0.0 to +0.0. Both native and Python
    # typed readers seal the stored value; header literals retain their spelling.
    with closing(sqlite3.connect(store)) as db,db:
        db.execute('UPDATE routes SET predicted_max_delay_ns=? WHERE seq=0',(-0.0,))
    result=seal_native_system_routes(store,logical)
    assert result['logical_sha256']==manifest['logical_sha256']
    assert result['sections']==manifest['sections']






def test_binary64_and_int64_records_match_python_oracle(native_root,tmp_path):
    import random, struct
    rng=random.Random(423)
    values=[0.0,math.ulp(0.0),math.nextafter(1e-4,0.0),math.nextafter(1e16,0.0),float.fromhex('0x1.fffffffffffffp+1023')]
    while len(values)<1200:
        value=struct.unpack('<d',struct.pack('<Q',rng.getrandbits(63)))[0]
        if math.isfinite(value):values.append(value)
    logical=fixture(len(values),True)
    for row,value in zip(logical['routes'],values):row['predicted_max_delay_ns']=value
    logical['routes'][0]['width_bits']=(1<<63)-1
    logical['demands']=[_demand_from_route(row) for row in logical['routes']]
    manifest=write_system_route_storage(tmp_path/'routes.json',logical)
    result=seal_native_system_routes(tmp_path/manifest['artifact'],logical)
    assert result['logical_sha256']==manifest['logical_sha256']
    assert result['sections']==manifest['sections']


def test_large_unexpected_header_stops_at_request_bound(tmp_path):
    logical=fixture(1)
    logical['unexpected_header']=['bounded-value']*1000000
    path=tmp_path/'request'
    with pytest.raises(ValidationError,match='exceeds bound'):
        write_route_seal_request(path,logical)
    assert path.stat().st_size<4096


def test_source_output_alias_rejected_before_mutation(native_root,tmp_path):
    logical=fixture(1)
    manifest=write_system_route_storage(tmp_path/'routes.json',logical)
    store=tmp_path/manifest['artifact'];request=tmp_path/'request'
    write_route_seal_request(request,logical)
    original=store.read_bytes()
    result=subprocess.run([str(native_root),'phase4','seal-routes',str(store),str(request),str(store)],capture_output=True)
    assert result.returncode
    assert store.read_bytes()==original
