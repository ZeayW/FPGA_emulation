"""Bounded policy facts shared by the complete native partition stage."""
import fnmatch
import re
from functools import lru_cache
from pathlib import Path
import struct
from .errors import ValidationError
from .native_tools import native_file_generation
from .resources import RESOURCE_FIELDS
from .ir import VALID_CUT_CLASSES
from .combinational_cut import (_STATE_RESOURCES, _HARD_COMBINATIONAL_RESOURCES,
    _TRANSPORT_SAFE_SEQUENTIAL_INPUTS)


@lru_cache(maxsize=1)
def _pattern_range_policy():
    try:
        matched = fnmatch.fnmatchcase('a', '[z-aa]')
    except re.error:
        return 'strict-ranges-v1'
    if matched:
        return 'empty-ranges-v1'
    raise ValidationError('unsupported instance pattern range convention')


def write_candidate_policy(path, ir):
    source=Path(ir.path)
    generation=native_file_generation(source)
    with path.open('wb') as output:
        def put(code,*values):
            output.write(struct.pack('<'+code,*values))
        def text(value):
            encoded=value.encode('utf-8'); put('I',len(encoded)); output.write(encoded)
        def catalog(values):
            put('I',len(values))
            for value in values:
                text(value)
        put('QIQQQqqqqQQ',0x31514543464D5545,1,*generation,
            len(ir.value['instances']),len(ir.value['nets']))
        catalog(RESOURCE_FIELDS)
        mask=lambda fields:sum(1<<i for i,name in enumerate(RESOURCE_FIELDS) if name in fields)
        put('QQI',mask(_STATE_RESOURCES),mask(_HARD_COMBINATIONAL_RESOURCES),RESOURCE_FIELDS.index('lut'))
        text('$_DFF_'); catalog(['D'])
        put('I',len(_TRANSPORT_SAFE_SEQUENTIAL_INPUTS))
        for name, ports in sorted(_TRANSPORT_SAFE_SEQUENTIAL_INPUTS.items()):
            text(name); catalog(sorted(ports))
        catalog(sorted(VALID_CUT_CLASSES))
    return generation
