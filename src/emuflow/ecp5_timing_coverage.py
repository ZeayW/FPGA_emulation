"""Independent connectivity-driven checks of nextpnr delay annotation coverage.

Not an STA engine: the checks supply missing-record gates before native STA.
Primitive combinational-arc completeness and reset recovery/removal remain
separate obligations. No absent physical delay is treated as zero.
"""
from .errors import ValidationError


def qualify_ecp5_comb_arcs(routed, delays, *, top="top"):
    """Check the connected LUT/carry arc population, not path slack.

    Arc families follow nextpnr e47c2589 ecp5/trellis_import.py's split
    timing-cell contract. Values come only from the checked routed SDF.
    This deliberately retains the tool's conservative LUT arcs instead of
    dropping paths by reinterpreting INITVAL. Stateful LUT RAM is unsupported.
    """
    if delays.get('delay_connectivity_checked') is not True:
        raise ValidationError('combinational coverage requires checked SDF')
    module = routed.get('modules', {}).get(top)
    if not isinstance(module, dict):
        raise ValidationError('missing combinational timing top')
    counts = {'LOGIC': 0, 'CCU2': 0}; arc_count = 0
    for name, cell in module.get('cells', {}).items():
        if cell.get('type') != 'TRELLIS_COMB':
            continue
        mode = cell.get('parameters', {}).get('MODE', 'LOGIC')
        connections = cell.get('connections', {})
        if mode not in counts:
            raise ValidationError(f'unsupported combinational timing mode: {name}/{mode}')
        if any(connections.get(pin) for pin in ('WD', 'WAD0', 'WAD1', 'WAD2', 'WAD3', 'WRE', 'WCK')):
            raise ValidationError('stateful LUT ports in combinational timing model')
        if mode == 'LOGIC':
            if connections.get('FCI') or connections.get('FCO'):
                raise ValidationError('carry port in LOGIC timing model')
            families = {'F': ('A', 'B', 'C', 'D'),
                        'OFX': ('A', 'B', 'C', 'D', 'M', 'F1', 'FXA', 'FXB')}
        else:
            if any(connections.get(pin) for pin in ('OFX', 'M', 'F1', 'FXA', 'FXB')):
                raise ValidationError('wide mux in carry timing model')
            families = {pin: ('A', 'B', 'C', 'D', 'FCI') for pin in ('F', 'FCO')}
        expected = {(source, sink) for sink, sources in families.items()
                    if connections.get(sink) for source in sources if connections.get(source)}
        for source, sink in expected:
            if (len(connections[source]) != 1 or len(connections[sink]) != 1
                    or cell.get('port_directions', {}).get(source) != 'input'
                    or cell.get('port_directions', {}).get(sink) != 'output'):
                raise ValidationError('invalid combinational timing port')
        actual = set(delays.get('cells', {}).get(name, {}).get('iopaths', {}))
        if actual != expected:
            raise ValidationError(f'combinational arc coverage mismatch: {name}: '
                                  f'missing={len(expected-actual)}, extra={len(actual-expected)}')
        counts[mode] += 1; arc_count += len(expected)
    return {'status': 'pass', 'scope': 'trellis_logic_carry_iopath_coverage',
            'cells_by_mode': counts, 'checked_arcs': arc_count,
            'original_path_coverage_qualified': False, 'global_timing_qualified': False}


def qualify_ecp5_timing_coverage(routed, delays, *, top="top"):
    if delays.get('delay_connectivity_checked') is not True:
        raise ValidationError('timing coverage requires checked SDF connectivity')
    module=routed.get('modules',{}).get(top)
    if not isinstance(module,dict): raise ValidationError('missing timing coverage top')
    cells=module.get('cells',{}); drivers={}; loads={}
    external={bit for port in module.get('ports',{}).values()
              for bit in port.get('bits',[]) if type(bit) is int}
    for name,cell in cells.items():
        for pin,bits in cell.get('connections',{}).items():
            if not bits: continue
            if len(bits)!=1: raise ValidationError('packed timing ports must be scalar')
            bit=bits[0]; direction=cell.get('port_directions',{}).get(pin)
            if direction not in {'input','output','inout'}:
                raise ValidationError('missing physical port direction')
            if bit in ('0','1'):
                if direction!='input': raise ValidationError('constant driven by physical output')
                continue
            if type(bit) is not int or bit<0: raise ValidationError('unknown timing wire')
            if direction=='inout':
                if cell.get('type')!='TRELLIS_IO' or pin!='B' or bit not in external:
                    raise ValidationError('unsupported internal bidirectional timing net')
                continue
            (drivers if direction=='output' else loads).setdefault(bit,set()).add((name,pin))
    expected=set()
    for bit,sinks in loads.items():
        sources=drivers.get(bit,set())
        if len(sources)!=1:
            raise ValidationError('physical timing load has no unique internal driver')
        source=next(iter(sources))
        expected.update((source,sink) for sink in sinks)
    actual=set(delays.get('interconnect',{}))
    if actual!=expected:
        raise ValidationError(f'routed delay coverage mismatch: missing={len(expected-actual)}, extra={len(actual-expected)}')
    ff_count=0; checked_pins=0; async_assertions=[]
    for name,cell in cells.items():
        if cell.get('type')!='TRELLIS_FF': continue
        ff_count+=1
        p=cell.get('parameters',{}); c=cell.get('connections',{})
        mode=str(p.get('SD','')).strip()
        if mode not in {'0','1'} or p.get('CLKMUX')!='CLK':
            raise ValidationError('unsupported FF timing mode')
        pins=['DI' if mode=='1' else 'M']
        ce=str(p.get('CEMUX','')).strip()
        if ce in {'CE','INV'}: pins.append('CE')
        elif ce!='1': raise ValidationError('unsupported FF clock enable')
        sr=p.get('SRMODE')
        if sr is None and not c.get('LSR'):
            # Yosys/nextpnr omit SRMODE on reset-free FFs. No reset timing
            # endpoint exists in this case; data/enable checks still apply.
            pass
        elif sr=='LSR_OVER_CE':
            if c.get('LSR'): pins.append('LSR')
        elif sr=='ASYNC': async_assertions.append((name,'LSR'))
        else: raise ValidationError('unsupported FF set/reset mode')
        annotation=delays.get('cells',{}).get(name,{})
        if ('CLK','Q') not in annotation.get('iopaths',{}):
            raise ValidationError(f'missing FF clock-to-Q delay: {name}')
        for pin in pins:
            if len(c.get(pin,[]))!=1: raise ValidationError(f'unconnected FF timing input: {name}/{pin}')
            for edge in ('posedge','negedge'):
                key=((edge,pin),('posedge','CLK'))
                if key not in annotation.get('setuphold',{}):
                    raise ValidationError(f'missing FF setup/hold annotation: {name}/{pin}/{edge}')
            checked_pins+=1
    if not ff_count: raise ValidationError('timing coverage has no physical FFs')
    return {'status':'pass','scope':'internal_route_and_ff_annotation_coverage',
        'routed_connections':len(expected),'ff_count':ff_count,'ff_checked_inputs':checked_pins,
        'asynchronous_assertion_pins':async_assertions,
        'primitive_combinational_arc_coverage_qualified':False,
        'recovery_removal_qualified':False,'original_path_coverage_qualified':False,
        'global_timing_qualified':False}
