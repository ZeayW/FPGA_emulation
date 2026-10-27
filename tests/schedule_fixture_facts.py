"""Explicit independent root cuts for small schedule semantic fixtures."""
import hashlib
import json


def exact_root_routes(routes):
    rows = routes['routes']
    if len(rows) > 4096:
        raise AssertionError('exact root fixture exceeds its small-graph bound')
    for row in rows:
        row.update(cut_class='register_output', dependency_level=0,
                   combinational_dependency_depth=0, predecessor_cut_nets=[])
    schema = 'emuflow.static-exact-combinational-cut/v3'
    routes.update(semantic_contract_schema=schema,
                  semantic_contract=dict(schema=schema, indexed=True,
                      mode='static-exact-combinational', qualification='structural-partition-legality'),
                  semantic_contract_sha256=hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest())
    return routes
