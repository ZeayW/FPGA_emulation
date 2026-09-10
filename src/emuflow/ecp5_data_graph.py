"""Physical pin graph for binding ECP5 data cones to an external STA engine.

No arrival/slack propagation occurs here. Register boundaries and I/O buffers
are explicit cutpoints; clocks and asynchronous reset pins are separate
obligations, never silently converted into combinational data paths.
"""
from collections import defaultdict, deque
from .errors import ValidationError
from .ecp5_timing_coverage import qualify_ecp5_timing_coverage, qualify_ecp5_comb_arcs


def select_ecp5_data_cones(graph, *, roots, captures):
    """Transient physical subgraph for explicitly selected endpoint queries.

    Preserve every raw arc on any selected root-to-capture path. Excluded
    roots are outside this query population, not assigned guessed arrivals.
    This is structural selection, not propagation or source coverage proof.
    """
    roots, captures = set(roots), set(captures)
    if (not roots or not captures or not roots <= set(graph['roots']) or
            not captures <= set(graph['captures']) or
            not (roots | captures) <= graph['dynamic_nodes']):
        raise ValidationError('cone selection needs bound dynamic roots and captures')
    outgoing, incoming = defaultdict(list), defaultdict(list)
    for a, b, _ in graph['edges']:
        outgoing[a].append(b)
        incoming[b].append(a)

    def reachable(start, edges):
        seen, pending = set(start), list(start)
        while pending:
            for other in edges[pending.pop()]:
                if other not in seen:
                    seen.add(other)
                    pending.append(other)
        return seen

    kept = reachable(roots, outgoing) & reachable(captures, incoming)
    if not (roots | captures) <= kept:
        raise ValidationError('selected physical boundary has no root-to-capture path')
    return dict(roots={r: graph['roots'][r] for r in roots},
                captures={c: graph['captures'][c] for c in captures},
                dynamic_nodes=kept, order=[n for n in graph['order'] if n in kept],
                edges=[edge for edge in graph['edges'] if edge[0] in kept and edge[1] in kept])


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


def project_data_reachability(graph, launches, captures):
    """Project named source/cut boundaries without enumerating timing paths.

    Each name maps to an already bound physical pin. Original IDs remain
    separate even if synthesis merges their physical launch registers.
    Bit masks are transient consumer data; they are not a global timing or
    equivalence certificate. No physical delay arithmetic is performed.
    """
    if not launches or not captures:
        raise ValidationError('data reachability needs launch and capture boundaries')
    if any(not isinstance(name,str) or not name for name in (*launches,*captures)):
        raise ValidationError('invalid data reachability boundary name')
    if any(pin not in graph['roots'] for pin in launches.values()):
        raise ValidationError('data reachability launch is not a physical root')
    if any(pin not in graph['captures'] for pin in captures.values()):
        raise ValidationError('data reachability capture is not a physical endpoint')
    labels=tuple(sorted(launches)); masks={}
    for index,name in enumerate(labels):
        pin=launches[name]; masks[pin]=masks.get(pin,0) | (1<<index)
    outgoing=defaultdict(list)
    for source,sink,_ in graph['edges']:outgoing[source].append(sink)
    positions={node:i for i,node in enumerate(graph['order'])}
    if len(positions)!=len(graph['order']):raise ValidationError('duplicate physical graph node')
    for source,sinks in outgoing.items():
        for sink in sinks:
            if source not in positions or sink not in positions or positions[source]>=positions[sink]:
                raise ValidationError('physical data graph is not topologically ordered')
    for node in graph['order']:
        mask=masks.get(node,0)
        if mask:
            for sink in outgoing[node]:masks[sink]=masks.get(sink,0)|mask
    return {'launch_labels':labels,
            'capture_masks':{name:masks.get(pin,0) for name,pin in captures.items()},
            'scope':'bound_physical_boundary_reachability',
            'original_path_coverage_qualified':False,'global_timing_qualified':False}


def require_data_connections(projection, required):
    """Reject missing requested pairs; callers own the original-path population.

    An optimized-away path is not silently accepted. It needs a separate
    semantic proof/classification before the caller changes its required set.
    """
    indices={name:1<<i for i,name in enumerate(projection['launch_labels'])}
    targets=projection['capture_masks']; count=0
    for launch,capture in required:
        if launch not in indices or capture not in targets:
            raise ValidationError('unbound required source/capture identity')
        if not targets[capture] & indices[launch]:
            raise ValidationError(f'missing required physical data connection: {launch} -> {capture}')
        count+=1
    return {'status':'pass','required_pairs':count,
            'scope':'requested_physical_boundary_connections',
            'original_path_coverage_qualified':False,'global_timing_qualified':False}
