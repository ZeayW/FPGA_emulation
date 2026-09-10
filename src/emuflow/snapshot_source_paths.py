"""Bounded complete source connectivity production, never fabricated STA.

This database owns original net-chain identity and a declared target period.
It contains no estimated delay or slack. Physical bounds and native numerical
STA are supplied downstream. A path budget rejects before enumeration instead
of producing a truncated database or silently sampling endpoint paths.
"""
import math
from collections import defaultdict

from .errors import ValidationError
from .sta import sta_object_index
from .snapshot_timing_population import boundary_id

SCHEMA = 'emuflow.snapshot-source-paths/v1'


def validate_snapshot_source_paths(database, ir):
    if database.get('schema') != SCHEMA or database.get('design') != ir.value['design']['name']:
        raise ValidationError('snapshot source path schema/design mismatch')
    if database.get('source') != {'provider': 'complete-structural-net-chains-v1'}:
        raise ValidationError('snapshot source paths require explicit structural provenance')
    objects = sta_object_index(ir)
    nets = {net['id'] for net in ir.value['nets']}
    seen = set()
    if not isinstance(database.get('paths'), list) or not database['paths']:
        raise ValidationError('snapshot source paths must be nonempty')
    for row in database['paths']:
        if set(row) != {'id','clock_domain','clock_period_ns','startpoint','endpoint','path_nets'}:
            raise ValidationError('snapshot source path must not contain synthetic STA delay/slack')
        name, period = row['id'], row['clock_period_ns']
        if not isinstance(name, str) or not name or name in seen:
            raise ValidationError('invalid/duplicate snapshot source path ID')
        seen.add(name)
        if (not isinstance(row['clock_domain'], str) or not row['clock_domain'] or
                type(period) not in (int, float) or not math.isfinite(period) or period <= 0):
            raise ValidationError('snapshot source path target clock is invalid')
        for key in ('startpoint','endpoint'):
            endpoint = row[key]
            if not isinstance(endpoint, dict) or objects.get(endpoint.get('object')) != endpoint:
                raise ValidationError('snapshot source path endpoint is not an original pin')
        chain = row['path_nets']
        if (not isinstance(chain, list) or not chain or any(not isinstance(n, str) or n not in nets for n in chain)
                or len(set(chain)) != len(chain)):
            raise ValidationError('snapshot source path net identity is invalid')


def build_snapshot_source_paths(ir, *, port_owners, clock_port, target_period_ns, max_paths=200000):
    # Imported here to keep the source schema validator independent of the
    # path-binding consumer used by the separate coverage gate.
    from .snapshot_path_coverage import _source_path_graph
    if type(max_paths) is not int or max_paths <= 0:
        raise ValidationError('snapshot path budget must be a positive integer')
    if type(target_period_ns) not in (int,float) or not math.isfinite(target_period_ns) or target_period_ns <= 0:
        raise ValidationError('snapshot source paths require an explicit positive target period')
    clock_nets = [net for net in ir.value['nets'] if net['cut_class'] == 'clock']
    ffs = {cell['id'] for cell in ir.value['instances'] if cell['type'] == '$_DFF_P_'}
    if (len(clock_nets) != 1 or
            clock_nets[0]['drivers'] != [dict(instance=None, port=clock_port, bit=0)] or
            {(ep['instance'],ep['port'],ep['bit']) for ep in clock_nets[0]['sinks']} != {(name,'C',0) for name in ffs}):
        raise ValidationError('snapshot source paths require one explicit clock driving every mapped FF')
    successors, captures, expected = _source_path_graph(ir, port_owners)
    total = sum(expected.values())
    if total > max_paths:
        raise ValidationError(f'complete snapshot population has {total} paths, above budget {max_paths}; no paths enumerated')
    objects = sta_object_index(ir)
    identities = {(ep['instance'],ep['port'],ep['bit']): ep for ep in objects.values()}
    def endpoint(ep):
        return dict(identities[ep['instance'], ep['port'], ep['bit']])
    capture_endpoints = {}
    roots = []
    cells = {cell['id']:cell for cell in ir.value['instances']}
    for net in ir.value['nets']:
        if net['cut_class'] == 'clock':
            continue
        driver = net['drivers'][0]
        if driver['instance'] is None or cells[driver['instance']]['type'] == '$_DFF_P_':
            roots.append((net['id'],endpoint(driver)))
        for sink in net['sinks']:
            if sink['instance'] is None:
                capture_endpoints[boundary_id('host',sink['port'],bit=sink['bit'])] = endpoint(sink)
            elif cells[sink['instance']]['type'] == '$_DFF_P_':
                capture_endpoints[boundary_id('state',sink['instance'])] = endpoint(sink)
    by_net = defaultdict(list)
    for key, net in captures.items():
        by_net[net].append(capture_endpoints[key])
    # Prune dead logic so a tiny endpoint population cannot hide exponentially
    # many paths that terminate in no original capture.
    predecessors = defaultdict(set)
    for source, targets in successors.items():
        for target in targets:
            predecessors[target].add(source)
    live = set(by_net)
    pending = list(live)
    while pending:
        for source in predecessors[pending.pop()]:
            if source not in live:
                live.add(source)
                pending.append(source)
    paths = []
    for root, start in sorted(roots, key=lambda item:item[0]):
        if root not in live:
            continue
        stack = [(root, [root])]
        while stack:
            net, chain = stack.pop()
            for end in sorted(by_net[net], key=lambda ep:ep['object']):
                paths.append(dict(id=f'source-path-{len(paths):08d}', clock_domain=clock_port,
                    clock_period_ns=float(target_period_ns),startpoint=start,endpoint=end,path_nets=chain))
            for target in sorted(successors[net], reverse=True):
                if target in live:
                    stack.append((target,chain+[target]))
    if len(paths) != total:
        raise ValidationError('snapshot path enumeration differs from source graph count')
    result = dict(schema=SCHEMA, design=ir.value['design']['name'],
        source={'provider':'complete-structural-net-chains-v1'}, paths=paths)
    validate_snapshot_source_paths(result, ir)
    return result
