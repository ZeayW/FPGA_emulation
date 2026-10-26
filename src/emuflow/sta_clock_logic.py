"""INIT-derived arc polarity for the indexed source design's clock cones.

SQLite owns cone membership. Only distinct, at-most-six-input LUT models are
retained in Python; this is not a second netlist or an instance-to-type map.
"""
from copy import deepcopy
import re

from .errors import ValidationError


def lut_clock_model(instance, base):
    cell_type = instance['type']
    match = re.fullmatch(r'LUT([1-6])', cell_type)
    if not match and cell_type != 'LUT6_2':
        raise ValidationError('unsupported INIT-derived clock cell: ' + cell_type)
    width = int(match[1]) if match else 6
    raw = instance.get('parameters', {}).get('INIT')
    if isinstance(raw, str) and raw and set(raw) <= {'0', '1'}:
        init = int(raw, 2)
    elif isinstance(raw, int) and not isinstance(raw, bool):
        init = raw
    else:
        raise ValidationError('clock LUT requires a fully defined binary INIT')
    if not 0 <= init < 1 << (1 << width):
        raise ValidationError('clock LUT INIT exceeds its truth-table width')
    senses = {}
    outputs = {'O': width} if match else {'O5': 5, 'O6': 6}
    for output, size in outputs.items():
        arcs = {}
        for bit in range(size):
            changes = set()
            for index in range(1 << size):
                if not index & (1 << bit):
                    changes.add(((init >> (index | (1 << bit))) & 1) - ((init >> index) & 1))
            changes.discard(0)
            if changes:
                arcs['I' + str(bit)] = ('positive_unate' if changes == {1} else
                    'negative_unate' if changes == {-1} else 'non_unate')
        senses[output] = arcs
    model = deepcopy(base)
    model['timing_sense'] = senses
    return cell_type + '__CLOCK_INIT_' + format(init, 'x'), model


def specialize_clock_logic(ir, model):
    """Enable a native SQLite cone projection, then add its distinct LUT types."""
    if not hasattr(ir, 'enable_clock_logic'):
        raise ValidationError('engine-edge-v1 requires compact indexed EmuIR for clock-cone projection')
    for name in ('FDCE', 'FDPE', 'FDRE', 'FDSE'):
        if name in model['cells']:
            negative = deepcopy(model['cells'][name])
            negative['kind'] = 'falling_edge_ff'
            model['cells'].setdefault(name + '__NEG', negative)
    ir.enable_clock_logic(model)
    variants = set()
    cells = 0
    for instance in ir.clock_logic_instances():
        cell_type = instance['type']
        base = model['cells'][cell_type]
        if re.fullmatch(r'LUT[1-6]', cell_type) or cell_type == 'LUT6_2':
            name, specialized = lut_clock_model(instance, base)
            if name in model['cells'] and model['cells'][name] != specialized:
                raise ValidationError('clock LUT timing model identity collision')
            model['cells'][name] = specialized
            variants.add(name)
            cells += 1
        elif base.get('kind') == 'combinational' and 'timing_sense' not in base:
            # Unknown polarity must not silently introduce two clock edges.
            raise ValidationError('clock-cone cell lacks explicit arc polarity: ' + cell_type)
    return {'policy': 'indexed-init-clock-sense-v1', 'lut_instances': cells,
            'distinct_lut_models': len(variants)}
