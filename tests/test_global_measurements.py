"""Native numerical import, full-original-path reduction and direct QoR ownership."""
from contextlib import ExitStack
from pathlib import Path
import copy
import gc
import json
import math
import os
import sqlite3
import weakref
from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from tests.event_facts import EventFacts as EventBuilder, MetadataFacts as MetadataBuilder
from emuflow.global_measurements import NativeMeasurements, NativeTimingPaths, import_measurements, read_native_measurements
from emuflow.global_sta import EventCheck, build_opensta_timing
from emuflow.qor_storage import write_qor_report, read_qor_report
from tests.test_global_sta import example


@pytest.fixture(autouse=True)
def runtime(tmp_path,monkeypatch):
    if not os.environ.get('EMUFLOW_NATIVE_ROOT'):
        from tests.test_native_schedule_generation import install_runtime
        install_runtime(tmp_path,monkeypatch)


def _case(tmp_path,count=24,*,large=False):
    builder=EventBuilder(bound=True);metadata=MetadataBuilder(builder);all_rows=[]
    for i in reversed(range(count)):
        path=f'路径-{i:05d}-😀'
        local=i%3==0
        metadata.append(path,representative_path='代表',path_scope='same-fpga-local' if local else 'cross-fpga',
                        clock_domain='时钟',target_period_ns=18.0,physical_logic_segments_cone_bound=bool(i%2))
        values=example()[2:4] if local else example()
        for row in values:
            arcs=(1e16,1.0,1.0) if large else ((0.25+i%4,) if local else row.arcs_ns)
            if not large and not local and i%4==0 and row.role=='tx':
                arcs=(5.0,)
            value=EventCheck(path,row.role,row.event,row.launch_ns,arcs,row.required_ns)
            builder.append(value);all_rows.append(value)
    rows,paths=builder.finish(),metadata.finish()
    representatives,_=rows.compressed()
    file=tmp_path/'engine.tsv';_tsv(file,representatives)
    with patch.object(type(rows),'__iter__',side_effect=AssertionError('Python event population')):
        result=import_measurements(file,representatives,directory=tmp_path/'native')
    reference=[{'path':r.path,'role':r.role,'event':r.event,
                'arrival_ns':r.launch_ns+math.fsum(r.arcs_ns),
                'required_ns':r.required_ns,
                'slack_ns':r.required_ns-r.launch_ns-math.fsum(r.arcs_ns)}
               for r in all_rows]
    info={'paths':paths,'source_binding':None,'compressed_representative_paths':4}
    runtime={'virtual_dut_clocks':{'时钟':{'nominal_period_ns':100.0}},'virtual_dut_clock':{'nominal_period_ns':100.0}}
    return rows,info,runtime,result,reference


def _tsv(path,rows):
    with path.open('w') as f:
        f.write('endpoint\tarrival_ns\trequired_ns\tslack_ns\n')
        for i,row in enumerate(rows):
            a=math.fsum(row.arcs_ns);r=row.required_ns-row.launch_ns
            f.write(f'o{i}\t{a:.17g}\t{r:.17g}\t{r-a:.17g}\n')


@pytest.mark.parametrize('count,large',[(1,False),(24,False),(73,True)])
def test_native_full_population_metrics_paths_and_qor_match_reference(tmp_path,count,large):
    rows,info,runtime,native,reference=_case(tmp_path,count,large=large)
    try:
        with patch.object(NativeMeasurements,'__iter__',side_effect=AssertionError('Python H scan')):
            actual=build_opensta_timing(runtime,info,native)
        by_path = {}
        for row in reference:
            by_path.setdefault(row['path'], {})[row['role']] = row
        for row in actual['paths']:
            values = by_path[row['path']]
            assert {key:row[key] for key in info['paths'][row['path']]} == info['paths'][row['path']]
            assert row['system_delay_bound_ns'] == values['target']['arrival_ns']
            for role in ('target', 'runtime'):
                assert row[role+'_required_time_ns'] == values[role]['required_ns']
                assert row[role+'_clock_slack_bound_ns'] == values[role]['slack_ns']
        for role in ('target', 'runtime'):
            scalars = [(values[role]['slack_ns'], path) for path, values in by_path.items()]
            worst, path = min(scalars)
            assert actual[role+'_clock']['worst_slack_bound_ns'] == worst
            assert actual[role+'_clock']['worst_path'] == path
            assert actual[role+'_clock']['total_negative_slack_bound_ns'] == math.fsum(v for v,_ in scalars if v < 0)
            assert actual[role+'_clock']['negative_slack_paths'] == sum(v < 0 for v,_ in scalars)
        assert actual['global_opensta']['event_failures'] == sum(
            row['role'] in ('tx','commit') and row['slack_ns'] < -1e-3 for row in reference)
        assert actual['global_opensta']['implementation_id'] == 'native-global-measurements-v1'
        expected_summary = {
            'timing_paths':len(by_path), 'original_paths':None,
            'original_local_paths':sum(row['path_scope']=='same-fpga-local' for row in info['paths'].values()),
            'compressed_representative_paths':4, 'original_path_coverage':None,
            'original_path_ids_sha256':None,
            'maximum_system_delay_bound_ns':max(values['target']['arrival_ns'] for values in by_path.values()),
        }
        expected_summary['original_cross_fpga_paths'] = len(by_path)-expected_summary['original_local_paths']
        assert actual['summary'] == expected_summary
        logical={'schema':'emuflow.qor-report/v4','status':'pass','timing':actual}
        with patch.object(NativeTimingPaths,'__iter__',side_effect=AssertionError('Python P publication')):
            owner=write_qor_report(tmp_path/'qor.json',logical)
        native_sha=owner.selector['logical_sha256'];owner.close()
        materialized={**logical,'timing':{**actual,'paths':list(actual['paths'])}}
        reference_owner=write_qor_report(tmp_path/'reference.json',materialized)
        assert reference_owner.selector['logical_sha256']==native_sha
        reference_owner.close()
        with patch('emuflow.qor_storage._record_from_row',side_effect=AssertionError('Python P validation')):
            with read_qor_report(tmp_path/'qor.json') as report:
                assert report['timing']['summary']==expected_summary
        replay=read_native_measurements(tmp_path/'native/measurements.json',rows)
        try:
            assert list(replay)==list(native)==list(reference)
        finally:
            replay.close()
        assert not (tmp_path/'native/measurements.tsv').exists()
    finally:
        native.close();rows.close()


def test_native_import_rejects_bad_endpoint_numbers_and_coverage(tmp_path):
    rows,info,runtime,native,reference=_case(tmp_path,3)
    representatives,_=rows.compressed()
    original=(tmp_path/'engine.tsv').read_text()
    try:
        for body in [original.replace('o0\t','o1\t',1),original.replace('\t3\t','\tnan\t',1),
                     '\n'.join(original.splitlines()[:-1])+'\n',original+'o999\t0\t0\t0\n',
                     original.replace('o0\t','o0\t0x1p0\t',1)]:
            path=tmp_path/'bad.tsv';path.write_text(body)
            with pytest.raises(ValidationError):
                import_measurements(path,representatives)
    finally:
        native.close();rows.close()


def test_saved_native_measurements_require_matching_reconstructed_event_namespace(tmp_path):
    rows,info,runtime,native,reference=_case(tmp_path,4)
    builder=EventBuilder(bound=True);metadata=MetadataBuilder(builder)
    for p in info['paths']:
        data=info['paths'][p]
        metadata.append(p,**data)
        for row in rows:
            if row.path==p:
                builder.append(row)
    altered=builder.finish();metadata.finish()
    try:
        with pytest.raises(ValidationError,match='namespace'):
            read_native_measurements(tmp_path/'native/measurements.json',altered)
    finally:
        native.close();altered.close();rows.close()


def test_native_qor_publication_rejects_wrong_bounded_metrics(tmp_path):
    rows,info,runtime,native,reference=_case(tmp_path,3)
    try:
        timing=build_opensta_timing(runtime,info,native)
        timing['target_clock']['total_negative_slack_bound_ns']-=1
        with pytest.raises(ValidationError,match='metrics disagree'):
            write_qor_report(tmp_path/'qor.json',{'schema':'emuflow.qor-report/v4','timing':timing})
        assert not (tmp_path/'qor.json').exists()
    finally:
        native.close();rows.close()


def test_event_cycle_closes_sqlite_before_directory_retirement(monkeypatch):
    import emuflow.global_event_store as module
    actual_connect=sqlite3.connect
    connections=[]
    class ObservedConnection:
        def __init__(self,*a,**kw):
            self.inner=actual_connect(*a,**kw);self.closed=False;connections.append(self)
        def __getattr__(self,name):return getattr(self.inner,name)
        def close(self):self.closed=True;self.inner.close()
    monkeypatch.setattr(module.sqlite3,'connect',ObservedConnection)
    builder=EventBuilder()
    for row in example():builder.append(row)
    rows=builder.finish();root=rows._owner.root;owner=weakref.ref(rows._owner)
    rows._owner.cycle=rows
    actual_remove=module.shutil.rmtree
    def remove(path,*a,**kw):
        if Path(path)==root:
            assert all(c.closed for c in connections)
        return actual_remove(path,*a,**kw)
    monkeypatch.setattr(module.shutil,'rmtree',remove)
    del builder,rows
    gc.collect()
    assert owner() is None and not root.exists()


def test_saved_validation_checks_all_raw_arc_equations(tmp_path):
    rows,info,runtime,native,reference=_case(tmp_path,2)
    try:
        representatives,_=rows.compressed()
        original=(tmp_path/'engine.tsv').read_text().splitlines()
        fields=original[1].split('\t');fields[1]=str(float(fields[1])+.01);fields[3]=str(float(fields[2])-float(fields[1]))
        original[1]='\t'.join(fields)
        bad=tmp_path/'engine-wrong.tsv';bad.write_text('\n'.join(original)+'\n')
        unverified=import_measurements(bad,representatives,verify_arcs=False,directory=tmp_path/'unverified')
        unverified.close()
        with pytest.raises(ValidationError,match='binding disagrees'):
            read_native_measurements(tmp_path/'unverified/measurements.json',rows)
    finally:
        native.close();rows.close()


def test_measurement_close_retains_live_cursor_then_releases(tmp_path):
    rows,info,runtime,native,reference=_case(tmp_path,3)
    iterator=iter(native)
    assert next(iterator)['path']
    native.close()
    with pytest.raises(ValidationError,match='closed'):
        next(iterator)
    assert native.connection is None
    rows.close()


def test_saved_measurements_survive_owner_retirement_and_exact_reconstruction(tmp_path):
    rows,info,runtime,native,reference=_case(tmp_path,12)
    facts=list(rows);details=dict(info['paths']);namespace=rows._owner.header['logical_sha256']
    native.close();rows.close()
    (tmp_path/'engine.tsv').unlink()
    builder=EventBuilder(bound=True);metadata=MetadataBuilder(builder);current=None
    for fact in facts:
        if fact.path!=current:
            current=fact.path;metadata.append(current,**details[current])
        builder.append(fact)
    reconstructed=builder.finish();metadata.finish()
    try:
        assert reconstructed._owner.header['logical_sha256']==namespace
        measured=read_native_measurements(tmp_path/'native/measurements.json',reconstructed)
        try:
            assert list(measured)==list(reference)
        finally:
            measured.close()
    finally:
        reconstructed.close()


def _reseal(path,store_key,artifact_key):
    import hashlib
    selector=json.loads(path.read_text());old=path.parent/selector[artifact_key]
    digest=hashlib.sha256(old.read_bytes()).hexdigest()
    prefix='measurements' if artifact_key=='artifact' and path.name=='measurements.json' else path.stem
    target=path.with_name(f'{prefix}.{digest}.sqlite');old.rename(target)
    selector[artifact_key]=target.name;selector[store_key]=digest;selector['store_bytes']=target.stat().st_size
    path.write_text(json.dumps(selector))


def test_native_measurement_semantics_reject_resealed_store_corruption(tmp_path):
    import struct,shutil
    rows,info,runtime,native,reference=_case(tmp_path,2)
    try:
        bad=tmp_path/'bad';bad.mkdir();manifest=bad/'measurements.json'
        original=json.loads((tmp_path/'native/measurements.json').read_text())
        store=bad/original['artifact'];shutil.copyfile(native.store_path,store)
        conn=sqlite3.connect(store)
        try:
            old=struct.unpack('<3d',conn.execute('SELECT numbers FROM measurements WHERE seq=0').fetchone()[0])
            conn.execute('UPDATE measurements SET numbers=? WHERE seq=0',(struct.pack('<3d',old[0]+.125,old[1],old[2]-.125),));conn.commit()
        finally:
            conn.close()
        manifest.write_text(json.dumps(original));_reseal(manifest,'store_sha256','artifact')
        with pytest.raises(ValidationError,match='reduction/digest disagrees'):
            read_native_measurements(manifest,rows,verify_arcs=False)
    finally:
        native.close();rows.close()


def test_native_qor_validator_rejects_resealed_record_corruption_without_python_fallback(tmp_path):
    rows,info,runtime,native,reference=_case(tmp_path,2)
    try:
        timing=build_opensta_timing(runtime,info,native)
        path=tmp_path/'qor.json';owner=write_qor_report(path,{'schema':'emuflow.qor-report/v4','timing':timing})
        store=owner.store_path;owner.close()
        conn=sqlite3.connect(store)
        try:
            conn.execute('UPDATE timing_paths SET target_clock_slack_bound_ns=target_clock_slack_bound_ns+1 WHERE seq=0');conn.commit()
        finally:
            conn.close()
        _reseal(path,'store_sha256','artifact')
        with patch('emuflow.qor_storage._record_from_row',side_effect=AssertionError('Python fallback')):
            with pytest.raises(ValidationError,match='logical records disagree'):
                read_qor_report(path)
    finally:
        native.close();rows.close()
