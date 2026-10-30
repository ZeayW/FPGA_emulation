"""Small input facts written with current typed-store fixture writers."""
from __future__ import annotations
from tests.phase3_store_fixture import write_assignment_index_fixture
import copy
from pathlib import Path
from types import SimpleNamespace

from emuflow.ir.emuir_compact import write_compact_emuir
from emuflow.ir.ir import EmuIR
from emuflow_tools.timing.sta import STA_PATH_DATABASE_TSV_HEADER, import_sta_path_database_tsv_streaming
from emuflow_tools.stage_clients.opensta import OPENSTA_PROVIDER
from tests.native_route_seal_fixture import write_route_store_fixture


def ep(instance, port, bit=0):
    return {'instance': instance, 'port': port, 'bit': bit}


def circuit():
    return {'schema': 'emuflow.emuir/v1',
        'design': {'name': 'dut', 'top': 'dut', 'source_format': 'fixture'},
        'ports': [], 'clocks': [], 'warnings': [],
        'instances': [{'id': name, 'type': 'FDRE' if name != 'logic' else 'LUT1',
            'resources': {'ff': 1} if name != 'logic' else {'lut': 1},
            'parameters': {}, 'attributes': {}, 'constant_connections': []}
            for name in ('capture', 'launch', 'logic')],
        'nets': [
            {'id': 'n0', 'cut_class': 'register_output', 'drivers': [ep('launch','Q')], 'sinks': [ep('logic','I0')]},
            {'id': 'n1', 'cut_class': 'combinational', 'drivers': [ep('logic','O')], 'sinks': [ep('capture','D')]},
        ]}


def sources(root, *, value=None, owners=None, paths=None, cross=()):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    value = copy.deepcopy(circuit() if value is None else value)
    value['instances'].sort(key=lambda cell: cell['id'])
    ir = EmuIR(value)
    original = root/'original.sqlite'
    write_compact_emuir(original, value)
    owners = owners or {cell['id']: 'fpga0' for cell in value['instances']}
    parts = sorted(set(owners.values()) | {'fpga0', 'fpga1'})
    assignment = root/'assignment.sqlite'
    write_assignment_index_fixture(assignment, ir, {
        'schema': 'emuflow.partition-assignment/v1', 'design': value['design']['name'],
        'platform': 'fixture', 'provider': 'fixture', 'seed': 0, 'constraints': {},
        'partitions': [{'fpga': part} for part in parts],
        'instance_assignment': owners, 'cut_nets': []})
    paths = paths if paths is not None else [
        ('launch/Q->capture/D#00000000', 'clk', 10., 1., 9., ['n0','n1'])]
    measured = root/'paths.tsv'
    measured.write_text(STA_PATH_DATABASE_TSV_HEADER+'\n'+''.join(
        '\t'.join((identity.encode().hex(), clock.encode().hex(), str(period), str(slack), str(delay),
                  ','.join(name.encode().hex() for name in nets)))+'\n'
        for identity, clock, period, slack, delay, nets in paths))
    path_database = root/'paths.json'
    import_sta_path_database_tsv_streaming(measured, original, path_database, provider=OPENSTA_PROVIDER)
    routes = root/'routes.json'
    write_route_store_fixture(routes, {'schema': 'emuflow.system-routes/v1',
        'design': value['design']['name'], 'platform': 'fixture', 'demands': [], 'routes': [],
        'timing': {'paths': list(cross)}})
    merged = {}
    for fpga in parts:
        physical = copy.deepcopy(value)
        physical['design']['name'] = value['design']['name']+'__'+fpga
        path = root/(fpga+'.sqlite')
        write_compact_emuir(path, physical)
        merged[fpga] = path
    return SimpleNamespace(original=original, assignment=assignment, paths=path_database,
        routes=routes, merged=merged, values=value, owners=owners, path_facts=paths)


def prepare(value):
    from emuflow_tools.timing.local_path_timing import prepare_local_path_query_inputs
    return prepare_local_path_query_inputs(value.original, value.assignment, value.paths, value.routes)


def query(value, root, *, backend='xilinx', fpga='fpga0', prepared=None):
    from emuflow_tools.timing.local_path_query_native import write_native_local_path_query
    return write_native_local_path_query(value.merged[fpga], fpga, Path(root)/'query.tsv',
        Path(root)/'identity.json', prepared or prepare(value), backend=backend)
