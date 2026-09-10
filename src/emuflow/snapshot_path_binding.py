"""Bind original TimingPathDB members to source-owned snapshot segments.

No source slack/delay is reused as physical timing. Each database member keeps
its identity, including reconvergent paths with identical endpoints. This
does not certify that an upstream STA extractor enumerated every logic path.
"""
from .errors import ValidationError
from .equivalence import _lut_definition
from .sta import validate_sta_path_database_value, sta_object_index, sta_path_endpoints
from .snapshot_timing_population import boundary_id
from .snapshot_source_paths import SCHEMA as SOURCE_PATH_SCHEMA, validate_snapshot_source_paths


def iter_snapshot_path_bindings(database, ir, assignment, *, port_owners):
    """Yield one ordered segment binding per original database member.

    Net index ranges reference the original path rather than copy its net
    list. Only actual on-path board transitions create transport segments;
    multicast branches elsewhere on the same net do not create fake hops.
    """
    if database.get('schema') == SOURCE_PATH_SCHEMA:
        validate_snapshot_source_paths(database, ir)
    else:
        validate_sta_path_database_value(database, ir)
    objects = sta_object_index(ir)
    cells = {cell['id']: cell for cell in ir.value['instances']}
    nets = {net['id']: net for net in ir.value['nets']}

    def owner(endpoint):
        value = (port_owners.get(endpoint['port']) if endpoint['instance'] is None
                 else assignment.get(endpoint['instance']))
        if value not in ('board0', 'board1'):
            raise ValidationError('original path endpoint lacks a snapshot board owner')
        return value

    def equal(a, b):
        return all(a[field] == b[field] for field in ('instance', 'port', 'bit'))

    def boundary(endpoint, launch):
        name = endpoint['instance']
        if name is None:
            return boundary_id('host', endpoint['port'], bit=endpoint['bit'])
        if (cells[name]['type'] != '$_DFF_P_' or endpoint['bit'] != 0 or
                endpoint['port'] not in (('Q', 'C') if launch else ('D',))):
            raise ValidationError('snapshot original path needs mapped FF/host endpoints')
        return boundary_id('state', name)

    for path in database['paths']:
        start, end = sta_path_endpoints(path, objects)
        source_key, end_key = boundary(start, True), boundary(end, False)
        source_board, end_board = owner(start), owner(end)
        chain = [nets[name] for name in path['path_nets']]
        if any(len(net['drivers']) != 1 or net['cut_class'] in ('clock', 'reset') for net in chain):
            raise ValidationError('original path contains non-data or ambiguous net')
        first = chain[0]['drivers'][0]
        normalized_start = dict(start, port='Q') if start['instance'] is not None else start
        if not equal(first, normalized_start) or not any(equal(sink, end) for sink in chain[-1]['sinks']):
            raise ValidationError('original path net chain does not match its endpoints')
        segments = []
        begin = 0
        for index, net in enumerate(chain):
            driver = net['drivers'][0]
            if owner(driver) != source_board:
                raise ValidationError('original path board continuity is broken')
            if index + 1 == len(chain):
                receiver = end
            else:
                next_driver = chain[index + 1]['drivers'][0]
                name = next_driver['instance']
                if name is None or cells[name]['type'] == '$_DFF_P_':
                    raise ValidationError('original combinational path traverses a state boundary')
                width, input_port, output_port, _ = _lut_definition(cells[name])
                valid_inputs = {(f'I{bit}', 0) if input_port == 'I' else (input_port, bit) for bit in range(width)}
                candidates = [sink for sink in net['sinks'] if sink['instance'] == name and
                              (sink['port'], sink['bit']) in valid_inputs]
                if not candidates or next_driver['port'] != output_port or next_driver['bit'] != 0:
                    raise ValidationError('discontinuous original path net sequence')
                receiver = candidates[0]
            receiver_board = owner(receiver)
            if receiver_board != source_board:
                cut = boundary_id('cut', net['id'])
                segments.append(dict(board=source_board, launch=source_key, capture=cut,
                    net_begin=begin, net_end=index, outgoing_cut=net['id']))
                source_board, source_key, begin = receiver_board, cut, index
        if source_board != end_board:
            raise ValidationError('original path did not reach its destination board')
        segments.append(dict(board=source_board, launch=source_key, capture=end_key,
            net_begin=begin, net_end=len(chain)-1, outgoing_cut=None))
        yield dict(path=path['id'], segments=segments,
            scope='original_database_member_source_binding_not_physical_timing')
