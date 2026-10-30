"""Bounded logical facts for small STA importer differential fixtures.

No indexed-store writer, publication, process launcher, or production fallback.
"""
import math
import re

from emuflow.common.errors import ValidationError
from emuflow.common.json_io import write_json
from emuflow.ir.ir import EmuIR
from emuflow_tools.timing.sta import STA_PATH_DATABASE_SCHEMA, STA_PATH_DATABASE_TSV_HEADER

LIMIT = 4096


def endpoint_facts(ir):
    """Enumerate provider spellings for a small in-memory fixture only."""
    value = ir.value
    budget = len(value['instances']) + len(value['nets'])
    budget += sum(port['width'] for port in value['ports'])
    budget += sum(len(net[side]) for net in value['nets'] for side in ('drivers', 'sinks'))
    budget += sum(len(cell.get('constant_connections', [])) for cell in value['instances'])
    if budget > LIMIT:
        raise AssertionError('STA endpoint oracle is restricted to small fixtures')
    pins = {cell['id']: set() for cell in value['instances']}
    for net in value['nets']:
        for endpoint in (*net['drivers'], *net['sinks']):
            if endpoint['instance'] is not None:
                pins[endpoint['instance']].add((endpoint['port'], endpoint['bit']))
    for cell in value['instances']:
        pins[cell['id']].update((pin['port'], pin['bit']) for pin in cell.get('constant_connections', []))
    facts = {}
    for instance, connections in pins.items():
        for port, bit in connections:
            width = max(index + 1 for name, index in connections if name == port)
            name = f'{instance}/{port}' + (f'__{bit}' if width > 1 else '')
            facts[name] = dict(object=name, instance=instance, port=port, bit=bit)
    for port in value['ports']:
        for bit in range(port['width']):
            names = [port['id']] if port['width'] == 1 else [f"{port['id']}[{bit}]", f"{port['id']}__{bit}"]
            for name in names:
                facts[name] = dict(object=name, instance=None, port=port['id'], bit=bit)
    for name, endpoint in list(facts.items()):
        alias = name.replace('\\', '\\\\')
        if alias in facts and facts[alias] != endpoint:
            raise ValidationError('STA provider object alias is ambiguous')
        facts[alias] = endpoint
    return facts


def path_facts(input_path, ir):
    """Compute records, normalization and exact QoR without writing a store."""
    if input_path.stat().st_size > 1024 * 1024:
        raise AssertionError('STA path oracle is restricted to small fixtures')
    lines = input_path.read_text(encoding='utf-8').splitlines()
    if not lines or lines[0] != STA_PATH_DATABASE_TSV_HEADER:
        raise ValidationError('invalid STA fixture header')
    objects = endpoint_facts(ir)
    nets = {net['id'] for net in ir.value['nets']}
    rows = [line.split('\t') for line in lines[1:] if line]
    if not 0 < len(rows) <= LIMIT:
        raise AssertionError('STA path oracle requires 1..4096 fixture paths')
    records, ids = [], set()
    for row in rows:
        if len(row) != 6:
            raise ValidationError('invalid STA fixture row')
        identity, domain = (bytes.fromhex(item).decode('utf-8') for item in row[:2])
        period, slack, fixed = map(float, row[2:5])
        members = [bytes.fromhex(item).decode('utf-8') for item in row[5].split(',')]
        if (not identity or not domain or identity in ids or period <= 0 or fixed < 0
                or not all(map(math.isfinite, (period, slack, fixed)))
                or len(set(members)) != len(members) or not set(members) <= nets):
            raise ValidationError('invalid STA fixture facts')
        ids.add(identity)
        record = dict(id=identity, clock_domain=domain, clock_period_ns=period,
                      slack_ns=slack, fixed_delay_ns=fixed, path_nets=members)
        match = re.fullmatch(r'(.+)->(.+)#\d{8}', identity)
        if match and all(name in objects for name in match.groups()):
            record.update(startpoint=dict(objects[match[1]]), endpoint=dict(objects[match[2]]))
        records.append(record)
    slacks = [record['slack_ns'] for record in records]
    negative = [slack for slack in slacks if slack < 0]
    positive_scale = max((slack for slack in slacks if slack >= 0), default=1.0) or 1.0
    negative_scale = abs(min(negative, default=-1.0))
    max_period = max(record['clock_period_ns'] for record in records)
    normalization = dict(positive_slack_scale_ns=positive_scale,
                         negative_slack_scale_ns=negative_scale, max_clock_period_ns=max_period)
    for record in records:
        slack, period = record['slack_ns'], record['clock_period_ns']
        record['normalized_slack'] = (slack * period / (positive_scale * max_period)
                                      if slack >= 0 else slack / (negative_scale * period))
    summary = dict(paths=len(records), structured_endpoint_paths=sum('endpoint' in row for row in records),
                   unique_path_nets=len({net for row in records for net in row['path_nets']}),
                   qor=dict(wns_ns=min(slacks), tns_ns=math.fsum(negative),
                            failing_endpoints=len(negative), timed_endpoints=len(records)))
    return dict(paths=records, normalization=normalization, summary=summary)


def import_sta_path_database_tsv(input_path, ir_path, output_path, *, provider,
                                 source=None, _ir=None, _return_value=False):
    """Write a tiny inline test fixture; never a production indexed owner."""
    if provider != 'opensta-fpga-path-database-v1':
        raise ValidationError('STA path database provider is invalid')
    ir = _ir if _ir is not None else EmuIR.load(ir_path)
    facts = path_facts(input_path, ir)
    artifact = dict(schema=STA_PATH_DATABASE_SCHEMA, design=ir.value['design']['name'],
                    source={**(source or {}), 'provider': provider, 'input': str(input_path)},
                    normalization=facts['normalization'], paths=facts['paths'])
    write_json(output_path, artifact)
    summary = facts['summary']
    result = {key: summary[key] for key in ('paths', 'structured_endpoint_paths', 'unique_path_nets')}
    result.update(status='pass', design=artifact['design'], output=str(output_path))
    if _return_value:
        result['_value'] = artifact
    return result
