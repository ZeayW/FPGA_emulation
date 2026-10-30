"""Bounded graph facts for independently checking the native initializer protocol."""
from tests.partition_balance_fixture import active_partition_fpgas
from tests.partition_balance_fixture import transported_cut_classes_for_clusters
from emuflow.ir.resources import RESOURCE_FIELDS
from emuflow_tools.partition.partition_limits import strict_balance_limits


def facts(ir, platform, clusters, constraints, weights=None):
    records = list(clusters['clusters'])
    assert len(records) <= 256
    weights = weights or {}
    identities = {cell: cluster['id'] for cluster in records for cell in cluster['instances']}
    vertices = {cluster['id']: i+1 for i, cluster in enumerate(records)}
    edges = []
    legal = transported_cut_classes_for_clusters(clusters)
    for net in sorted(ir.value['nets'], key=lambda item: item['id']):
        if net['cut_class'] not in legal:
            continue
        endpoints = sorted({identities[e['instance']] for role in ('drivers', 'sinks')
                            for e in net[role] if e['instance'] is not None})
        if len(endpoints) > 1:
            edges.append({'net': net['id'], 'clusters': endpoints,
                          'vertices': [vertices[key] for key in endpoints],
                          'weight': weights.get(net['id'], 1.0)})
    dimensions = ['cells', *[field for field in RESOURCE_FIELDS if any(c['resources'].get(field, 0) for c in records)]]
    rows = [[len(c['instances']), *[c['resources'].get(d, 0) for d in dimensions[1:]]] for c in records]
    parts = active_partition_fpgas(platform, constraints)
    totals = {field: sum(row[i] for row in rows) for i, field in enumerate(dimensions)}
    bounds = strict_balance_limits(parts, totals, constraints['balance_tolerance'],
        constraints.get('balance_tolerance_by_dimension', {}), constraints.get('balance_dimensions', ()),
        constraints.get('resource_reservations_by_fpga', {}))
    return dict(fpga_order=[p.id for p in parts], hyperedges=edges, vertex_weights=rows,
                dimensions=dimensions, balance_allowed_loads=bounds)


def cut_score(edges, assignment):
    cut = [edge for edge in edges if len({assignment[key] for key in edge['clusters']}) > 1]
    return sum(edge['weight'] for edge in cut), len(cut)
