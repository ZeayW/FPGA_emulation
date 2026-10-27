"""Exact bounded LUT functions for source STA constant propagation."""
from copy import deepcopy
import re

from .errors import EmuFlowError, ValidationError
from .native_tools import resolve_native_executable
from .store_publication import bind_store_source, require_store_sources_unchanged
from .tool_runtime import NativeToolProcess


def lut_output_functions(cell_type, init):
    match = re.fullmatch(r'LUT([1-6])', cell_type)
    if match:
        width = int(match[1]); outputs = {'O': width}
    elif cell_type == 'LUT6_2':
        width = 6; outputs = {'O5': 5, 'O6': 6}
    else:
        raise ValidationError('source Boolean function requires a supported LUT')
    if isinstance(init, str) and init and set(init) <= {'0', '1'}:
        init = int(init, 2)
    if isinstance(init, bool) or not isinstance(init, int) or not 0 <= init < 1 << (1 << width):
        raise ValidationError('source LUT function requires an exact bounded INIT')
    result = {}
    for output, size in outputs.items():
        terms = ['('+' & '.join(('' if index & (1 << bit) else '!')+'I'+str(bit)
                               for bit in range(size))+')'
                 for index in range(1 << size) if init & (1 << index)]
        result[output] = '1' if len(terms) == 1 << size else ' | '.join(terms) if terms else '0'
    return result


def build_source_lut_catalog(source, output, *, executable=None):
    binding = bind_store_source(source, 'emuir-compact-v2')
    native = resolve_native_executable('emuflow-native', executable)
    command = [native, 'phase2', 'build-lut-models', str(binding.path), str(output)]
    with NativeToolProcess(native, command, scratch_parent=output.parent, maximum_bytes=65536) as process:
        completed = process.finish()
    if completed.returncode:
        raise EmuFlowError('source LUT model catalog failed: '+completed.stdout)
    require_store_sources_unchanged((binding,))
    return binding


def add_lut_catalog_models(connection, model):
    # One row per distinct truth table, not per circuit instance. The original
    # indexed owner and native catalog retain all occurrence/parameter joins.
    for source_type, name, bits in connection.execute(
            'SELECT DISTINCT source_type,timing_type,init_bits FROM source_lut_models.variants'):
        base = model['cells'].get(source_type)
        if base is None or base.get('kind') != 'combinational':
            raise ValidationError('source LUT lacks a combinational timing model')
        specialized = deepcopy(base)
        specialized['output_functions'] = lut_output_functions(source_type, bits)
        if name in model['cells'] and model['cells'][name] != specialized:
            raise ValidationError('source LUT model identity collision')
        model['cells'][name] = specialized
