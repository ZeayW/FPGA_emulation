"""Physical pin graph for binding ECP5 data cones to an external STA engine.

No arrival/slack propagation occurs here. Register boundaries and I/O buffers
are explicit cutpoints; clocks and asynchronous reset pins are separate
obligations, never silently converted into combinational data paths.
"""
from collections import defaultdict, deque
from .errors import ValidationError
from .ecp5_timing_coverage import qualify_ecp5_timing_coverage, qualify_ecp5_comb_arcs


def build_ecp5_data_graph(routed, delays, *, top='top'):
    """Return a shared DAG with raw rise/fall delay triples and physical IDs.

    The caller owns the routed/SDF inputs. The returned in-memory graph is a
    consumer interface, not another persisted copy of the physical netlist.
    Pin reachability is structural and conservatively retains LUT timing arcs.
    It does not establish original-design endpoint or path correspondence.
    """
    qualify_ecp5_timing_coverage(routed, delays, top=top)
    qualify_ecp5_comb_arcs(routed, delays, top=top)
    cells = routed['modules'][top]['cells']
    roots = {}; captures = {}; excluded = {}; constants = set(); edges = []
    for name, cell in cells.items():
        kind = cell['type']; c = cell['connections']; p = cell.get('parameters', {})
        if kind == 'TRELLIS_FF':
            roots[name, 'Q'] = {'kind': 'ff', 'clock': (name, 'CLK'),
                               'clock_to_q': delays['cells'][name]['iopaths']['CLK', 'Q']}
            excluded[name, 'CLK'] = 'clock_network'
            pins = ['DI' if str(p['SD']).strip() == '1' else 'M']
            if str(p['CEMUX']).strip() in ('CE', 'INV'): pins.append('CE')
            if c.get('LSR'):
                if p.get('SRMODE') == 'ASYNC': excluded[name, 'LSR'] = 'asynchronous_reset'
                else: pins.append('LSR')
            for pin in pins:
                captures[name, pin] = {'kind': 'ff', 'clock': (name, 'CLK'),
                    'setuphold': {edge: delays['cells'][name]['setuphold'][(edge, pin), ('posedge', 'CLK')]
                                  for edge in ('posedge', 'negedge')}}
        elif kind == 'TRELLIS_COMB':
            for (source, sink), values in delays['cells'][name]['iopaths'].items():
                edges.append(((name, source), (name, sink), values))
            # A zero-input LOGIC F is a physical constant generator. No fake
            # launch path or zero-delay replacement is inserted for it.
            if (p.get('MODE', 'LOGIC') == 'LOGIC' and c.get('F')
                    and not any(c.get(pin) for pin in ('A', 'B', 'C', 'D'))):
                constants.add((name, 'F'))
        elif kind == 'DCCA':
            for pin, bits in c.items():
                if bits: excluded[name, pin] = 'clock_network'
        elif kind == 'TRELLIS_IO':
            direction = p.get('DIR')
            if direction == 'INPUT' and c.get('O'):
                roots[name, 'O'] = {'kind': 'external_input', 'external_delay_qualified': False}
            elif direction == 'OUTPUT' and c.get('I'):
                captures[name, 'I'] = {'kind': 'external_output', 'external_delay_qualified': False}
            else:
                raise ValidationError(f'unsupported physical I/O data boundary: {name}')
            if c.get('T'):
                if c['T'] != ['0']:
                    raise ValidationError('dynamic output-enable needs explicit timing binding')
                constants.add((name, 'T'))
        else:
            raise ValidationError(f'unsupported physical data-graph primitive: {kind}')
        for pin, bits in c.items():
            if bits in (['0'], ['1']) and cell.get('port_directions', {}).get(pin) == 'input':
                constants.add((name, pin))
    for (source, sink), values in delays['interconnect'].items():
        if source in excluded or sink in excluded:
            # Only clock/reset boundary arcs may leave the data graph.
            if source in excluded and sink not in excluded:
                raise ValidationError('clock-network output used as unmodelled data')
            continue
        edges.append((source, sink, values))
    incoming = defaultdict(list); outgoing = defaultdict(list)
    nodes = set(roots) | set(captures) | constants
    for source, sink, values in edges:
        if sink in roots: raise ValidationError('data arc enters a launch boundary')
        nodes.update((source, sink)); incoming[sink].append(source); outgoing[source].append(sink)
    # Connectivity, including dead cones, must not hide undriven nodes or loops.
    for node in nodes:
        if not incoming[node] and node not in roots and node not in constants:
            raise ValidationError(f'physical data cone has unclassified root: {node}')
    indegree = {n: len(incoming[n]) for n in nodes}
    queue = deque(sorted(n for n in nodes if not indegree[n])); order = []
    dynamic = set(roots)
    while queue:
        node = queue.popleft(); order.append(node)
        for sink in outgoing[node]:
            if node in dynamic: dynamic.add(sink)
            indegree[sink] -= 1
            if not indegree[sink]: queue.append(sink)
    if len(order) != len(nodes): raise ValidationError('combinational cycle in physical data graph')
    return {'roots': roots, 'captures': captures, 'constants': constants,
            'edges': edges, 'order': order, 'dynamic_nodes': dynamic,
            'excluded_boundaries': excluded,
            'scope': 'physical_data_pin_graph', 'original_path_coverage_qualified': False,
            'global_timing_qualified': False}
